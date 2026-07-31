"""Async audit logging service and @audit_action decorator."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import AuditLog

F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])


class AuditService:
    """Append-only audit logging service.

    By default logs are written asynchronously in a fire-and-forget task so
    callers are never blocked. Awaiting the returned task is optional.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(
        self,
        action_type: str,
        object_type: str,
        object_id: str,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        *,
        pm_id: str | None = None,
        fund_entity_id: str | None = None,
        source_ip: str | None = None,
        session_id: str | None = None,
        retention_class: str = "standard",
        non_blocking: bool = False,
    ) -> None:
        """Write an audit log entry.

        If ``non_blocking`` is True, an ``asyncio.Task`` is created and returned.
        The caller must retain the returned reference in high-throughput paths to
        avoid "task destroyed" warnings.
        """
        entry = AuditLog(
            pm_id=pm_id,
            fund_entity_id=fund_entity_id,
            action_type=action_type,
            object_type=object_type,
            object_id=object_id,
            before_state=before_state or {},
            after_state=after_state or {},
            source_ip=source_ip,
            session_id=session_id,
            retention_class=retention_class,
        )

        async def _persist() -> None:
            async with AsyncSession(self.session.bind) as audit_session:  # type: ignore[arg-type]
                audit_session.add(entry)
                await audit_session.commit()

        if non_blocking:
            asyncio.create_task(_persist())
            return
        await _persist()


def audit_action(action_type: str, object_type: str) -> Callable[[F], F]:
    """Decorator that auto-logs before/after state for repository methods.

    The wrapped coroutine must accept ``pm_id`` (keyword) and should return an
    object that exposes an ``id`` attribute. The before-state is recorded by
    reading the repository's ``get`` method (if available) before mutation.
    """

    def decorator(func: F) -> F:
        sig = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            pm_id = bound.arguments.get("pm_id")
            fund_entity_id = bound.arguments.get("fund_entity_id")
            session = bound.arguments.get("session")

            before_state: dict[str, Any] | None = None
            repository = args[0] if args else None
            object_id = bound.arguments.get("object_id") or bound.arguments.get("id")

            if (
                repository is not None
                and hasattr(repository, "get")
                and object_id
                and session is not None
            ):
                try:
                    prior = await repository.get(object_id, session=session)
                    if prior is not None:
                        before_state = _state_dict(prior)
                except Exception:
                    before_state = None

            result = await func(*args, **kwargs)

            after_id = object_id
            if after_id is None and result is not None and hasattr(result, "id"):
                after_id = result.id

            audit_service = AuditService(session) if isinstance(session, AsyncSession) else None
            if audit_service is not None and after_id is not None:
                after_state = _state_dict(result) if result is not None else None
                await audit_service.log(
                    action_type=action_type,
                    object_type=object_type,
                    object_id=str(after_id),
                    before_state=before_state,
                    after_state=after_state,
                    pm_id=pm_id,
                    fund_entity_id=fund_entity_id,
                    non_blocking=False,
                )

            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _state_dict(obj: Any) -> dict[str, Any]:
    """Convert an object or dict to a JSON-serializable dict."""
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "__table__"):
        # SQLAlchemy model instance
        state: dict[str, Any] = {}
        for col in obj.__table__.columns:
            value = getattr(obj, col.name, None)
            try:
                json.dumps(value)
                state[col.name] = value
            except (TypeError, ValueError):
                state[col.name] = str(value) if value is not None else None
        return state
    return {"value": str(obj)}
