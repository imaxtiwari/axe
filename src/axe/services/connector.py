"""Connector service for running ingestion connectors and persisting raw ingest rows.

The service is intentionally thin: connectors produce ``IngestCandidate`` objects,
the service deduplicates using ``content_hash`` + ``dedup_key``, persists new rows
as ``RawIngest``, enqueues ``specialize_signal`` worker tasks, and records audit
entries for each connector run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from axe.config import get_settings
from axe.connectors.base import ConnectorError

if TYPE_CHECKING:
    from axe.connectors.base import BaseConnector
from axe.db.models import RawIngest, RetryQueue
from axe.db.uow import UnitOfWork
from axe.ingestion.dedup import DedupService
from axe.ingestion.hashing import content_hash
from axe.security.context import RequestContext

logger = logging.getLogger(__name__)


class ConnectorService:
    """Run ingestion connectors, normalize output, and enqueue specialist work."""

    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow
        self.settings = get_settings()

    async def run(
        self,
        source_type: str,
        pm_id: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Run a single connector for ``pm_id`` and persist new raw ingests.

        Returns a summary dict with ``source_type``, ``fetched``, ``new``,
        ``duplicates``, ``cursor``, and ``errors``.
        """
        if source_type not in self.settings.connectors_enabled:
            raise ConnectorError(
                f"Connector source_type '{source_type}' is not enabled",
                source_type=source_type,
                is_retryable=False,
            )

        config = await self.uow.connector_configs.get_by_source(source_type)
        if config is None:
            raise ConnectorError(
                f"No connector config found for {source_type}",
                source_type=source_type,
                is_retryable=False,
            )
        if not config.enabled:
            return {
                "source_type": source_type,
                "fetched": 0,
                "new": 0,
                "duplicates": 0,
                "cursor": None,
                "errors": ["connector disabled"],
            }

        connector = _build_connector(source_type, config.credentials_encrypted or {})
        dedup = DedupService(self.uow.session)
        fetched = 0
        new = 0
        duplicates = 0
        errors: list[str] = []
        cursor = config.last_cursor
        batch_limit = limit or self.settings.connector_batch_size

        try:
            result = await connector.fetch(cursor=cursor, limit=batch_limit)
        except ConnectorError as exc:
            logger.exception("Connector run failed for %s", source_type)
            errors.append(str(exc))
            return {
                "source_type": source_type,
                "fetched": 0,
                "new": 0,
                "duplicates": 0,
                "cursor": cursor,
                "errors": errors,
            }

        fetched = len(result.candidates)
        for candidate in result.candidates:
            candidate_hash = candidate.content_hash()
            dedup_key = candidate.build_dedup_key(source_type)

            is_duplicate = await dedup.is_duplicate(
                candidate_hash,
                source_id=dedup_key,
            )
            if is_duplicate:
                duplicates += 1
                continue

            existing = await self.uow.raw_ingests.get_by_content_hash(candidate_hash)
            if existing is not None:
                duplicates += 1
                continue

            raw = self.uow.raw_ingests.create_ingest(
                pm_id=pm_id,
                source_type=source_type,
                external_id=candidate.external_id,
                content_hash=candidate_hash,
                dedup_key=dedup_key,
                raw_payload_json=candidate.raw_payload_json,
                extracted_signal_json=candidate.extracted_signal_json,
                extracted_at=datetime.now(UTC),
                status="pending",
            )
            await self.uow.session.flush()

            task = RetryQueue(
                pm_id=pm_id,
                task_type="specialize_signal",
                payload={
                    "pm_id": pm_id,
                    "raw_ingest_id": raw.id,
                    "source_type": source_type,
                    "_content_hash": candidate_hash,
                    "_idempotency_key": dedup_key,
                },
            )
            self.uow.session.add(task)
            await dedup.mark_seen(
                candidate_hash,
                source_type=source_type,
                source_id=dedup_key,
            )
            new += 1

        # Update cursor only when the connector returned a new one.
        if result.cursor is not None:
            config.last_cursor = result.cursor
            cursor = result.cursor

        trace_id = RequestContext.current_or_none()
        trace_id_str = trace_id.request_id if trace_id is not None else None
        await self.uow.audit.log(
            action_type="connector_run",
            object_type="connector_config",
            object_id=config.id,
            pm_id=pm_id,
            after_state={
                "source_type": source_type,
                "fetched": fetched,
                "new": new,
                "duplicates": duplicates,
                "cursor": cursor,
                "errors": errors,
            },
            trace_id=trace_id_str,
        )

        return {
            "source_type": source_type,
            "fetched": fetched,
            "new": new,
            "duplicates": duplicates,
            "cursor": cursor,
            "errors": errors,
        }

    async def run_all(self, pm_id: str) -> list[dict[str, Any]]:
        """Run all enabled connectors that have a config for ``pm_id``."""
        configs = await self.uow.connector_configs.list_for_pm()
        results: list[dict[str, Any]] = []
        for config in configs:
            if not config.enabled:
                continue
            result = await self.run(config.source_type, pm_id)
            results.append(result)
        return results


async def normalize_payload_to_raw_ingest(
    pm_id: str,
    source_type: str,
    external_id: str | None,
    raw_payload: dict[str, Any],
    *,
    extracted_signal: dict[str, Any] | None = None,
    ticker: str | None = None,
) -> RawIngest:
    """Normalize an arbitrary payload into a transient ``RawIngest`` instance.

    The returned object is not persisted; callers must add it to a session.  It is
    used by the handler layer when a payload is pushed directly into the connector
    pipeline without going through a connector implementation.
    """
    text_parts: list[str] = [f"source_type: {source_type}"]
    if ticker:
        text_parts.append(f"ticker: {ticker}")
    text_parts.append(str(raw_payload))
    content = "\n".join(text_parts)

    return RawIngest(
        pm_id=pm_id,
        source_type=source_type,
        external_id=external_id,
        content_hash=content_hash(content),
        dedup_key=external_id,
        raw_payload_json=raw_payload,
        extracted_signal_json=extracted_signal or {},
        extracted_at=datetime.now(UTC),
        status="pending",
    )


def _build_connector(source_type: str, config: dict[str, Any]) -> BaseConnector:
    from axe.connectors.broker_feed import BrokerFeedConnector
    from axe.connectors.crm import CRMConnector
    from axe.connectors.expert_network import ExpertNetworkConnector
    from axe.connectors.pdf_deck import PDFDeckConnector
    from axe.connectors.research_edge import ResearchEdgeConnector

    mapping: dict[str, type[BaseConnector]] = {
        "broker_feed": BrokerFeedConnector,
        "pdf_deck": PDFDeckConnector,
        "crm": CRMConnector,
        "expert_network": ExpertNetworkConnector,
        "research_edge": ResearchEdgeConnector,
    }
    connector_cls = mapping.get(source_type)
    if connector_cls is None:
        raise ConnectorError(
            f"No connector registered for source_type '{source_type}'",
            source_type=source_type,
            is_retryable=False,
        )
    return connector_cls(config)


__all__ = ["ConnectorService", "normalize_payload_to_raw_ingest"]
