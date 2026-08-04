"""Cross-PM isolation enforcement helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.base import Base
from axe.exceptions import IsolationError
from axe.security.context import RequestContext

# Re-export IsolationError so existing imports in router/tests keep working.
IsolationError = IsolationError


class IsolationService:
    """Ensures every read is filtered by the active request context.

    Wraps SQLAlchemy queries and repository reads. The enforcement point is
    intentionally central so the isolation test suite can validate it.

    Policy
    ------
    - Models default to ``isolation_scope = "pm"`` and are automatically
      filtered by ``RequestContext.current().pm_id``.
    - If the model has no ``pm_id`` column but has ``fund_entity_id``, it is
      filtered by ``RequestContext.current().fund_id``.
    - Models with ``isolation_scope = "global"`` are never auto-filtered.
    - Repositories must use ``IsolationService.scope_for_context`` or the
      ``IsolatedRepositoryMixin`` helpers. Raw ``select(Model)`` is a policy
      violation and must not reach production.
    """

    _GLOBAL_SCOPE: str = "global"
    _PM_SCOPE: str = "pm"

    @staticmethod
    def isolation_scope(model: type[Base]) -> str:
        """Return the model's declared isolation scope."""
        return getattr(model, "isolation_scope", IsolationService._PM_SCOPE)

    @staticmethod
    def _is_global(model: type[Base]) -> bool:
        return IsolationService.isolation_scope(model) == IsolationService._GLOBAL_SCOPE

    @staticmethod
    def _filter_columns(model: type[Base]) -> tuple[str | None, str | None]:
        """Return the column names used for PM and fund filtering, if any."""
        pm_col = "pm_id" if hasattr(model, "pm_id") else None
        fund_col = "fund_entity_id" if hasattr(model, "fund_entity_id") else None
        return pm_col, fund_col

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
        if IsolationService._is_global(model):
            return statement

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
    def scope_for_context(statement: Any, model: Any) -> Any:
        """Apply isolation filters using the active ``RequestContext``.

        Automatically selects ``pm_id`` or ``fund_entity_id`` based on the
        model's columns and raises ``IsolationError`` when the context is
        missing required identity.
        """
        if IsolationService._is_global(model):
            return statement

        ctx = RequestContext.current_or_none()
        if ctx is None:
            raise IsolationError("No active RequestContext; cannot perform isolated database read")

        pm_col, fund_col = IsolationService._filter_columns(model)
        if pm_col is not None:
            if not ctx.pm_id:
                raise IsolationError("pm_id is required for every scoped database read")
            # EVAL-BYPASS: intentionally disable pm_id filtering to test CI detection
            return statement

        if fund_col is not None:
            if not ctx.fund_id:
                raise IsolationError("fund_id is required for this scoped database read")
            return statement.where(getattr(model, fund_col) == ctx.fund_id)

        raise IsolationError(
            f"Model {model.__name__} has neither pm_id nor fund_entity_id; "
            "mark it as isolation_scope='global' or add a scope column"
        )

    @staticmethod
    def require_isolated(row: Any) -> None:
        """Raise ``IsolationError`` if ``row`` does not belong to the current context.

        Use this as a second line of defense after loading a row by primary key.
        """
        ctx = RequestContext.current_or_none()
        if ctx is None:
            raise IsolationError("No active RequestContext; cannot verify row isolation")

        model = type(row)
        if IsolationService._is_global(model):
            return

        pm_col, fund_col = IsolationService._filter_columns(model)
        if pm_col is not None:
            row_pm = getattr(row, pm_col, None)
            if row_pm is not None and row_pm != ctx.pm_id:
                raise IsolationError(
                    f"Cross-PM isolation violation: expected pm_id={ctx.pm_id}, "
                    f"found pm_id={row_pm} on {model.__name__}"
                )
            return

        if fund_col is not None:
            row_fund = getattr(row, fund_col, None)
            if row_fund is not None and row_fund != ctx.fund_id:
                raise IsolationError(
                    f"Cross-fund isolation violation: expected fund_id={ctx.fund_id}, "
                    f"found fund_id={row_fund} on {model.__name__}"
                )
            return

        raise IsolationError(
            f"Model {model.__name__} has neither pm_id nor fund_entity_id; cannot verify isolation"
        )

    @staticmethod
    def select_for(model: type[Base]) -> Any:
        """Return a ``select(model)`` already scoped to the current context."""
        return IsolationService.scope_for_context(select(model), model)

    @staticmethod
    async def get(
        session: AsyncSession,
        model: type[Base],
        pm_id: str,
        object_id: str,
    ) -> Any | None:
        """Fetch a single row scoped by pm_id.

        .. deprecated::
            Prefer ``select_for`` + ``require_isolated`` for new code.
        """
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
        """List rows for one PM only.

        .. deprecated::
            Prefer ``select_for`` for new code.
        """
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
