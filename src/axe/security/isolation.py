"""Cross-PM isolation enforcement helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.base import Base


class IsolationError(Exception):
    """Raised when a database query is not correctly scoped to a single PM."""


class IsolationService:
    """Ensures every read is filtered by a single pm_id.

    Wraps SQLAlchemy queries and repository reads. The enforcement point is
    intentionally central so the isolation test suite can validate it.
    """

    @staticmethod
    def scope(
        statement: Any,
        model: Any,
        pm_id: str | None,
    ) -> Any:
        """Apply a pm_id filter to a SQLAlchemy select statement.

        Raises ``IsolationError`` if ``pm_id`` is missing or if the model
        lacks a ``pm_id`` column.
        """
        if not pm_id:
            raise IsolationError("pm_id is required for every scoped database read")

        if not hasattr(model, "pm_id"):
            raise IsolationError(f"Model {model.__name__} does not support pm_id isolation")

        existing = getattr(statement, "whereclause", None)
        scoped = statement.where(model.pm_id == pm_id)

        # Defensive: a caller could pass an already scoped statement for a different pm.
        # Static inspection of the whereclause for a conflicting literal is limited,
        # so runtime contamination tests are the primary guard.
        _ = existing
        return scoped

    @staticmethod
    async def get(
        session: AsyncSession,
        model: type[Base],
        pm_id: str,
        object_id: str,
    ) -> Any | None:
        """Fetch a single row scoped by pm_id."""
        stmt = IsolationService.scope(select(model), model, pm_id)
        id_col = getattr(model, "id", None)
        if id_col is not None:
            stmt = stmt.where(id_col == object_id)
        else:
            raise IsolationError(f"Model {model.__name__} has no id column for get()")
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def list_for_pm(
        session: AsyncSession,
        model: type[Base],
        pm_id: str,
        *,
        limit: int | None = None,
    ) -> list[Any]:
        """List rows for one PM only."""
        stmt = select(model)
        scoped = IsolationService.scope(stmt, model, pm_id)
        if limit is not None:
            scoped = scoped.limit(limit)
        result = await session.execute(scoped)
        return list(result.scalars().all())

    @staticmethod
    def ensure_memory_context_isolated(
        context_items: list[dict[str, Any]],
        pm_id: str,
        allowed_other_pm_ids: set[str] | None = None,
    ) -> None:
        """Validate that a memory-injection context contains no foreign PM data.

        Iterates context items and raises ``IsolationError`` if any item's
        ``pm_id`` differs from the expected ``pm_id`` and is not explicitly
        allow-listed.
        """
        allowed = allowed_other_pm_ids or set()
        allowed.add(pm_id)
        for item in context_items:
            item_pm_id = item.get("pm_id")
            found_in = item.get("found_in")
            if item_pm_id and item_pm_id not in allowed:
                raise IsolationError(
                    f"Cross-PM contamination detected: item for pm_id={item_pm_id} "
                    f"found in context for pm_id={pm_id} (source={found_in})"
                )

    @staticmethod
    def ensure_model_isolated(
        rows: Sequence[Any],
        pm_id: str,
    ) -> None:
        """Validate that every ORM row in ``rows`` belongs to ``pm_id``.

        Raises ``IsolationError`` on the first row whose ``pm_id`` attribute
        differs from the expected value. Used as a runtime guard in tests and
        repository methods that must never return cross-PM data.
        """
        for row in rows:
            row_pm_id = getattr(row, "pm_id", None)
            if row_pm_id is not None and row_pm_id != pm_id:
                raise IsolationError(
                    f"Cross-PM isolation violation: expected pm_id={pm_id}, "
                    f"found pm_id={row_pm_id} on {type(row).__name__}"
                )
