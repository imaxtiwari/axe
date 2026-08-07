"""Async retry worker loop with a pluggable task registry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from axe.agents.specialist_signal import (
    AgentContext,
    record_specialist_signals,
)
from axe.agents.specialist_signal import (
    default_registry as default_specialist_registry,
)
from axe.db.uow import UnitOfWork
from axe.ingestion.dedup import DedupService
from axe.ingestion.handlers import (
    process_transcript_handler,
    send_alert_handler,
)
from axe.ingestion.retry import RetryQueue
from axe.services.connector import ConnectorService
from axe.services.thesis import DriftDetectionService

logger = logging.getLogger(__name__)

TaskHandler = Callable[[AsyncSession, dict[str, Any]], Awaitable[bool]]


class TaskRegistry:
    """Pluggable registry of named task handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_type: str, handler: TaskHandler) -> TaskHandler:
        """Register ``handler`` for ``task_type`` and return it."""
        self._handlers[task_type] = handler
        return handler

    def get(self, task_type: str) -> TaskHandler | None:
        return self._handlers.get(task_type)


async def _no_op_handler(session: AsyncSession, payload: dict[str, Any]) -> bool:
    """Default no-op handler used to satisfy the task registry contract."""
    return True


def default_registry() -> TaskRegistry:
    """Return a registry containing the supported AXE task types."""
    registry = TaskRegistry()
    for name in (
        "ingest_email",
        "synthesize_memory",
    ):
        registry.register(name, _no_op_handler)
    registry.register("process_transcript", process_transcript_handler)
    registry.register("send_alert", send_alert_handler)
    registry.register("run_connector", run_connector_handler)
    registry.register("specialize_signal", specialize_signal_handler)
    return registry


class RetryWorker:
    """Background loop that polls the retry queue and executes tasks."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: TaskRegistry | None = None,
        poll_interval: float = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self.registry = registry or default_registry()
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def tick(self) -> bool:
        """Process one pending task, if available.

        Returns True if a task was processed (success, failure, or duplicate).
        Returns False when the queue is empty.
        """
        async with self._session_factory() as session:
            try:
                return await self._process_once(session)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise
            except Exception:
                await session.rollback()
                raise

    async def _process_once(self, session: AsyncSession) -> bool:
        """Single queue-processing pass using an open session."""
        queue = RetryQueue(session)
        task = await queue.dequeue()
        if task is None:
            return False

        handler = self.registry.get(task.task_type)
        if handler is None:
            # Unknown task type: mark failed so it can be reviewed / dead-lettered.
            await queue.mark_failed_with_backoff(task.id)
            await session.commit()
            return True

        # Idempotency: de-duplicate by payload hash if requested.
        idempotency_key = task.payload.get("_idempotency_key")
        content_hash = task.payload.get("_content_hash")
        dedup = DedupService(session)
        if content_hash and await dedup.is_duplicate(content_hash, source_id=idempotency_key):
            await queue.mark_success(task.id)
            await session.commit()
            return True

        try:
            success = await handler(session, task.payload)
        except Exception:
            await queue.mark_failed_with_backoff(task.id)
            await session.commit()
            return True

        if success:
            await queue.mark_success(task.id)
            if content_hash:
                await dedup.mark_seen(content_hash, task.task_type, source_id=idempotency_key)
        else:
            await queue.mark_failed_with_backoff(task.id)
        await session.commit()
        return True

    async def _run_loop(self) -> None:
        """Poll the queue until stopped."""
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                self._stop_event.set()
                raise
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)

    def start(self) -> None:
        """Start the background worker loop."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the background worker loop and wait for it to finish."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self._task = None


async def run_connector_handler(
    session: AsyncSession,
    payload: dict[str, Any],
) -> bool:
    """Run a connector by source_type for a PM and persist new raw ingests.

    Expected payload keys:
      - source_type (str)
      - pm_id (str)
      - limit (int, optional)
    """
    source_type = payload.get("source_type")
    pm_id = payload.get("pm_id")
    limit = payload.get("limit")

    if not source_type or not pm_id:
        logger.error("run_connector payload missing source_type or pm_id")
        return True

    async with UnitOfWork(session) as uow:
        service = ConnectorService(uow)
        result = await service.run(
            source_type=source_type,
            pm_id=pm_id,
            limit=limit,
        )
        logger.info("Connector run %s completed: %s", source_type, result)
        await uow.commit()
    return True


async def specialize_signal_handler(
    session: AsyncSession,
    payload: dict[str, Any],
) -> bool:
    """Dispatch a raw ingest to the appropriate specialist agent.

    Uses the specialist signal registry to convert a ``RawIngest`` row into
    one or more ``SpecialistSignal`` rows. The raw ingest status is updated to
    ``processed`` on success, or ``failed`` if no agent is registered.

    Expected payload keys:
      - raw_ingest_id (str)
      - source_type (str)
      - pm_id (str)
    """
    raw_ingest_id = payload.get("raw_ingest_id")
    source_type = payload.get("source_type")
    pm_id = payload.get("pm_id")

    if not raw_ingest_id or not source_type or not pm_id:
        logger.error("specialize_signal payload missing required fields")
        return True

    async with UnitOfWork(session) as uow:
        raw_ingest = await uow.raw_ingests.get_by_id(raw_ingest_id)
        if raw_ingest is None:
            logger.warning("RawIngest %s not found; skipping specialization", raw_ingest_id)
            return True

        registry = default_specialist_registry()
        agent = registry.build(source_type)
        if agent is None:
            logger.warning("No specialist registered for source_type %s", source_type)
            raw_ingest.status = "failed"
            await uow.commit()
            return True

        context = await _build_agent_context_for_pm(uow, pm_id)
        outputs = await agent.process(raw_ingest, context)
        if outputs:
            record_specialist_signals(uow, raw_ingest_id, pm_id, outputs)

        raw_ingest.status = "processed"
        await uow.commit()

    logger.info(
        "specialize_signal complete: raw_ingest_id=%s source_type=%s pm_id=%s signals=%s",
        raw_ingest_id,
        source_type,
        pm_id,
        len(outputs),
    )
    return True


async def _build_agent_context_for_pm(uow: UnitOfWork, pm_id: str) -> AgentContext:
    """Build an ``AgentContext`` for ``pm_id`` from current theses and persona."""
    from axe.security.context import RequestContext

    ctx = RequestContext.current_or_none()
    fund_id = ctx.fund_id if ctx is not None else None

    persona_row = await uow.pm_personas.get_current()
    persona = persona_row.decision_triggers if persona_row else None

    thesis_service = DriftDetectionService(uow, pm_id)
    theses = await thesis_service.alertable_latest_theses_with_signals()

    return AgentContext(
        pm_id=pm_id,
        fund_id=fund_id,
        persona=persona,
        active_tickers={t.ticker for t in theses},
        recent_theses=[
            {
                "ticker": t.ticker,
                "direction": t.direction,
                "key_assumptions": t.key_assumptions or [],
            }
            for t in theses
        ],
    )


__all__ = ["TaskHandler", "TaskRegistry", "RetryWorker", "default_registry"]
