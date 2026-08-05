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

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import (
    AuditLog,
    DealDocument,
    DealRoom,
    DealThesisVersion,
    DeckOutput,
    ICMemo,
    ICSignOff,
    PMUser,
    ThesisVersion,
    UnderwritingChecklist,
    UnderwritingScenario,
)
from axe.db.session import AsyncSessionLocal
from axe.exceptions import IsolationError
from axe.security.context import RequestContext
from axe.security.isolation import IsolationService


def _require_fund_id() -> str:
    """Return the active fund_id or raise IsolationError."""
    ctx = RequestContext.current_or_none()
    if ctx is None or not ctx.fund_id:
        raise IsolationError("fund_id is required for this scoped database read")
    return ctx.fund_id


class _BaseRepo:
    """Base repository bound to a UnitOfWork session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session


class DealThesisRepository(_BaseRepo):
    """Read helpers for deal thesis versions."""

    async def get_by_id(self, thesis_id: str) -> DealThesisVersion | None:
        result = await self.session.execute(
            select(DealThesisVersion).where(DealThesisVersion.id == thesis_id)
        )
        return result.scalar_one_or_none()

    async def get_latest_for_deal(self, deal_id: str) -> DealThesisVersion | None:
        result = await self.session.execute(
            select(DealThesisVersion)
            .where(DealThesisVersion.deal_id == deal_id)
            .order_by(desc(DealThesisVersion.version))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_deal(self, deal_id: str) -> list[DealThesisVersion]:
        result = await self.session.execute(
            select(DealThesisVersion)
            .where(DealThesisVersion.deal_id == deal_id)
            .order_by(desc(DealThesisVersion.version))
        )
        return list(result.scalars().all())


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


class ICMemoRepository(_BaseRepo):
    """CRUD helpers for IC memos."""

    async def get_by_id(self, memo_id: str) -> ICMemo | None:
        result = await self.session.execute(select(ICMemo).where(ICMemo.id == memo_id))
        return result.scalar_one_or_none()

    async def get_latest_for_deal(self, deal_id: str) -> ICMemo | None:
        result = await self.session.execute(
            select(ICMemo).where(ICMemo.deal_id == deal_id).order_by(desc(ICMemo.version)).limit(1)
        )
        return result.scalar_one_or_none()

    def create_memo(self, **kwargs: Any) -> ICMemo:
        memo = ICMemo(**kwargs)
        self.session.add(memo)
        return memo

    async def list_for_deal(self, deal_id: str) -> list[ICMemo]:
        result = await self.session.execute(
            select(ICMemo).where(ICMemo.deal_id == deal_id).order_by(desc(ICMemo.version))
        )
        return list(result.scalars().all())


class ICSignOffRepository(_BaseRepo):
    """CRUD helpers for IC memo sign-offs."""

    async def get_by_id(self, signoff_id: str) -> ICSignOff | None:
        result = await self.session.execute(select(ICSignOff).where(ICSignOff.id == signoff_id))
        return result.scalar_one_or_none()

    async def list_for_memo(self, memo_id: str) -> list[ICSignOff]:
        result = await self.session.execute(
            select(ICSignOff).where(ICSignOff.memo_id == memo_id).order_by(ICSignOff.created_at)
        )
        return list(result.scalars().all())

    def create_signoff(self, **kwargs: Any) -> ICSignOff:
        signoff = ICSignOff(**kwargs)
        self.session.add(signoff)
        return signoff


class DeckOutputRepository(_BaseRepo):
    """CRUD helpers for generated deck outputs."""

    async def get_by_id(self, deck_id: str) -> DeckOutput | None:
        result = await self.session.execute(select(DeckOutput).where(DeckOutput.id == deck_id))
        return result.scalar_one_or_none()

    def create_output(self, **kwargs: Any) -> DeckOutput:
        output = DeckOutput(**kwargs)
        self.session.add(output)
        return output


class DealRepository(_BaseRepo):
    """CRUD helpers for deal rooms."""

    async def get_by_id(self, deal_id: str) -> DealRoom | None:
        result = await self.session.execute(
            IsolationService.select_for(DealRoom).where(DealRoom.id == deal_id)
        )
        return result.scalar_one_or_none()

    async def list_deals(self) -> list[DealRoom]:
        result = await self.session.execute(
            IsolationService.select_for(DealRoom).order_by(DealRoom.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_deals_for_fund(self, fund_entity_id: str) -> list[DealRoom]:
        result = await self.session.execute(
            IsolationService.select_for(DealRoom)
            .where(DealRoom.fund_entity_id == fund_entity_id)
            .order_by(DealRoom.created_at.desc())
        )
        return list(result.scalars().all())

    def create_deal(self, **kwargs: Any) -> DealRoom:
        deal = DealRoom(**kwargs)
        self.session.add(deal)
        return deal

    async def update_deal(self, deal: DealRoom, **changes: Any) -> DealRoom:
        for key, value in changes.items():
            if hasattr(deal, key):
                setattr(deal, key, value)
        await self.session.flush()
        return deal


class DealDocumentRepository(_BaseRepo):
    """CRUD helpers for deal documents scoped through a deal room."""

    @staticmethod
    def _scoped_stmt():
        """Return a select(DealDocument) joined to DealRoom and scoped by fund."""
        from sqlalchemy import select

        return (
            select(DealDocument)
            .join(DealRoom, DealDocument.deal_id == DealRoom.id)
            .where(DealRoom.fund_entity_id == _require_fund_id())
        )

    async def get_by_id(self, document_id: str) -> DealDocument | None:
        stmt = self._scoped_stmt().where(DealDocument.id == document_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_deal(self, deal_id: str) -> list[DealDocument]:
        stmt = (
            self._scoped_stmt()
            .where(DealDocument.deal_id == deal_id)
            .order_by(DealDocument.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_content_hash(self, deal_id: str, content_hash: str) -> DealDocument | None:
        stmt = self._scoped_stmt().where(
            DealDocument.deal_id == deal_id,
            DealDocument.content_hash == content_hash,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    def create_document(self, **kwargs: Any) -> DealDocument:
        doc = DealDocument(**kwargs)
        self.session.add(doc)
        return doc


class UnderwritingChecklistRepository(_BaseRepo):
    """CRUD helpers for deal underwriting checklists."""

    async def get_by_id(self, checklist_id: str) -> UnderwritingChecklist | None:
        result = await self.session.execute(
            select(UnderwritingChecklist).where(UnderwritingChecklist.id == checklist_id)
        )
        return result.scalar_one_or_none()

    async def list_for_deal(
        self,
        deal_id: str,
        *,
        include_scoped: bool = True,
    ) -> list[UnderwritingChecklist]:
        stmt = (
            select(UnderwritingChecklist)
            .where(UnderwritingChecklist.deal_id == deal_id)
            .order_by(UnderwritingChecklist.sort_order, UnderwritingChecklist.updated_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    def create_item(self, **kwargs: Any) -> UnderwritingChecklist:
        item = UnderwritingChecklist(**kwargs)
        self.session.add(item)
        return item


class UnderwritingScenarioRepository(_BaseRepo):
    """CRUD helpers for deal underwriting scenarios."""

    async def list_for_deal(self, deal_id: str) -> list[UnderwritingScenario]:
        result = await self.session.execute(
            select(UnderwritingScenario)
            .where(UnderwritingScenario.deal_id == deal_id)
            .order_by(UnderwritingScenario.created_at.desc())
        )
        return list(result.scalars().all())

    def create_scenario(self, **kwargs: Any) -> UnderwritingScenario:
        scenario = UnderwritingScenario(**kwargs)
        self.session.add(scenario)
        return scenario


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
        - ``uow.theses``                 -> ``ThesisRepository``
        - ``uow.deal_theses``            -> ``DealThesisRepository``
        - ``uow.pm_users``               -> ``PMUserRepository``
        - ``uow.audit``                  -> ``AuditRepository``
        - ``uow.deals``                  -> ``DealRepository``
        - ``uow.deal_documents``         -> ``DealDocumentRepository``
        - ``uow.underwriting_checklists`` -> ``UnderwritingChecklistRepository``
        - ``uow.underwriting_scenarios``  -> ``UnderwritingScenarioRepository``
        - ``uow.ic_memos``                -> ``ICMemoRepository``
        - ``uow.ic_signoffs``             -> ``ICSignOffRepository``
        - ``uow.deck_outputs``            -> ``DeckOutputRepository``
    """

    session: AsyncSession

    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session  # type: ignore[assignment]
        self._owns_session = session is None
        self._committed = False
        self.theses = ThesisRepository(self.session)
        self.deal_theses = DealThesisRepository(self.session)
        self.pm_users = PMUserRepository(self.session)
        self.audit = AuditRepository(self.session)
        self.deals = DealRepository(self.session)
        self.deal_documents = DealDocumentRepository(self.session)
        self.underwriting_checklists = UnderwritingChecklistRepository(self.session)
        self.underwriting_scenarios = UnderwritingScenarioRepository(self.session)
        self.ic_memos = ICMemoRepository(self.session)
        self.ic_signoffs = ICSignOffRepository(self.session)
        self.deck_outputs = DeckOutputRepository(self.session)

    async def __aenter__(self) -> UnitOfWork:
        if self._owns_session:
            self.session = AsyncSessionLocal()
            self.theses = ThesisRepository(self.session)
            self.deal_theses = DealThesisRepository(self.session)
            self.pm_users = PMUserRepository(self.session)
            self.audit = AuditRepository(self.session)
            self.deals = DealRepository(self.session)
            self.deal_documents = DealDocumentRepository(self.session)
            self.underwriting_checklists = UnderwritingChecklistRepository(self.session)
            self.underwriting_scenarios = UnderwritingScenarioRepository(self.session)
            self.ic_memos = ICMemoRepository(self.session)
            self.ic_signoffs = ICSignOffRepository(self.session)
            self.deck_outputs = DeckOutputRepository(self.session)
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
