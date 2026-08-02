"""Unit of Work layer for AXE.

Provides a single async context manager that owns an async SQLAlchemy session,
manages the transaction boundary (commit on success, rollback on exception), and
exposes thin repositories that share that session.

All repository reads use ``IsolationService`` helpers so isolation filters are
injected automatically from the active ``RequestContext``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import desc
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import (
    AuditLog,
    DealRoom,
    PMUser,
    ThesisVersion,
)
from axe.db.session import AsyncSessionLocal
from axe.security.isolation import IsolationService


class _BaseRepo:
    """Base repository bound to a UnitOfWork session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session


class ThesisRepository(_BaseRepo):
    """Thin read helpers for thesis version data.

    The transactional write path for theses ``create_thesis`` / ``update_thesis``
    remains in ``ThesisService`` for now; this repository is intentionally minimal
    until the full service is migrated.
    """

    async def get_latest(self, ticker: str) -> ThesisVersion | None:
        """Return the latest published thesis for a ticker scoped to the current PM."""
        result = await self.session.execute(
            IsolationService.select_for(ThesisVersion)
            .where(ThesisVersion.ticker == ticker)
            .order_by(desc(ThesisVersion.version))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_version(self, ticker: str, version: int) -> ThesisVersion | None:
        """Return a specific thesis version scoped to the current PM."""
        result = await self.session.execute(
            IsolationService.select_for(ThesisVersion).where(
                ThesisVersion.ticker == ticker,
                ThesisVersion.version == version,
            )
        )
        return result.scalar_one_or_none()

    async def list_versions(self, ticker: str) -> list[ThesisVersion]:
        """Return all thesis versions for a ticker, oldest first, scoped to current PM."""
        result = await self.session.execute(
            IsolationService.select_for(ThesisVersion)
            .where(ThesisVersion.ticker == ticker)
            .order_by(ThesisVersion.version)
        )
        return list(result.scalars().all())


class PMUserRepository(_BaseRepo):
    """Read helpers for PM users.

    PMUser rows are scoped by fund_entity_id because the table has no pm_id
    column; the PM's own identity is the row id.
    """

    async def get_by_id(self, pm_id: str) -> PMUser | None:
        result = await self.session.execute(
            IsolationService.select_for(PMUser).where(PMUser.id == pm_id)
        )
        return result.scalar_one_or_none()


class AuditRepository(_BaseRepo):
    """Append-only audit log helpers."""

    async def log(
        self,
        action_type: str,
        object_type: str,
        object_id: str,
        *,
        pm_id: str | None = None,
        fund_entity_id: str | None = None,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            pm_id=pm_id,
            fund_entity_id=fund_entity_id,
            action_type=action_type,
            object_type=object_type,
            object_id=object_id,
            before_state=before_state or {},
            after_state=after_state or {},
        )
        self.session.add(entry)
        await self.session.flush()
        return entry


class DealRepository(_BaseRepo):
    """Read helpers for deal rooms."""

    async def get_by_id(self, deal_id: str) -> DealRoom | None:
        result = await self.session.execute(
            IsolationService.select_for(DealRoom).where(DealRoom.id == deal_id)
        )
        return result.scalar_one_or_none()


class UnitOfWork:
    """Async Unit of Work context manager.

    Usage:
        async with UnitOfWork() as uow:
            await uow.theses.get_latest(pm_id, ticker)
            await uow.commit()

    The UoW opens an ``AsyncSession`` on ``__aenter__`` and closes it on
    ``__aexit__``. If ``commit()`` is not explicitly called, the transaction is
    rolled back when the context exits (even without exception).

    Repositories:
        - ``uow.theses``    -> ``ThesisRepository``
        - ``uow.pm_users``  -> ``PMUserRepository``
        - ``uow.audit``     -> ``AuditRepository``
        - ``uow.deals``     -> ``DealRepository``
    """

    session: AsyncSession

    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session  # type: ignore[assignment]
        self._owns_session = session is None
        self._committed = False
        self.theses = ThesisRepository(self.session)
        self.pm_users = PMUserRepository(self.session)
        self.audit = AuditRepository(self.session)
        self.deals = DealRepository(self.session)

    async def __aenter__(self) -> UnitOfWork:
        if self._owns_session:
            self.session = AsyncSessionLocal()
            self.theses = ThesisRepository(self.session)
            self.pm_users = PMUserRepository(self.session)
            self.audit = AuditRepository(self.session)
            self.deals = DealRepository(self.session)
        if self.session is None:  # pragma: no cover - type guard
            raise RuntimeError("UnitOfWork has no active session")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.session is None:
            return
        try:
            if self._owns_session:
                if exc_type is not None or not self._committed:
                    await self.session.rollback()
            else:
                # Nested UoW sharing a fixture-managed session must not touch
                # transaction boundaries or close the session. Only reset our
                # committed marker so callers can observe the decision.
                self._committed = False
        finally:
            if self._owns_session:
                await self.session.close()

    async def commit(self) -> None:
        """Explicitly commit the current transaction."""
        if self.session is None:  # pragma: no cover - type guard
            raise RuntimeError("UnitOfWork has no active session")
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        """Explicitly roll back the current transaction."""
        if self.session is None:  # pragma: no cover - type guard
            raise RuntimeError("UnitOfWork has no active session")
        await self.session.rollback()
        self._committed = False


async def get_uow() -> AsyncGenerator[UnitOfWork, None]:
    """FastAPI dependency yielding a request-scoped UnitOfWork.

    Use this as ``Depends(get_uow)`` in FastAPI endpoints.
    """
    async with UnitOfWork() as uow:
        yield uow
