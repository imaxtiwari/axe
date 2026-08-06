"""Thesis repository with versioning, ticker registry sync, and audit logging."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar, cast

from sqlalchemy import desc, func, select

from axe.db.models import AuditLog, ThesisVersion, TickerRegistry
from axe.db.uow import UnitOfWork
from axe.security.audit import _state_dict
from axe.security.context import RequestContext
from axe.security.isolation import IsolationService

T = TypeVar("T")


class _ThesisLocks:
    """Process-wide asyncio locks keyed by (pm_id, ticker)."""

    _locks: dict[tuple[str, str], asyncio.Lock] = {}

    @classmethod
    def get(cls, pm_id: str, ticker: str) -> asyncio.Lock:
        key = (pm_id, ticker)
        if key not in cls._locks:
            cls._locks[key] = asyncio.Lock()
        return cls._locks[key]


class _ContextHelper:
    """Bind a RequestContext when none is active; no-op otherwise."""

    def __init__(self, pm_id: str, fund_entity_id: str | None) -> None:
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self._token: Any | None = None

    def __enter__(self) -> None:
        if RequestContext.current_or_none() is None:
            self._token = RequestContext.set_current(
                RequestContext(pm_id=self.pm_id, fund_id=self.fund_entity_id)
            )

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            RequestContext.reset_current(self._token)
            self._token = None


class ThesisRepo:
    """CRUD for investment theses with immutable versioning."""

    def __init__(self, uow: UnitOfWork, pm_id: str, fund_entity_id: str) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        # Ensure an isolation context is available. In production this is set by
        # middleware; in tests/background workers we bind one from the repo identity.
        self._context = _ContextHelper(pm_id, fund_entity_id)
        with self._context:
            pass

    async def _with_context(self, coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        with self._context:
            return await coro_factory()

    async def create_thesis(
        self,
        ticker: str,
        *,
        bull_case: str | None = None,
        bear_case: str | None = None,
        key_assumptions: list[dict[str, Any]] | None = None,
        catalysts: list[Any] | None = None,
        conviction: int | None = None,
        unresolved_risks: list[Any] | None = None,
        is_draft: bool = False,
        asset_class: str = "equity",
        direction: str = "long",
        status: str = "active",
        mnpi_flag: bool = False,
    ) -> ThesisVersion:
        """Create the first version of a thesis for a ticker."""
        async with _ThesisLocks.get(self.pm_id, ticker):
            return await self._create_thesis_locked(
                ticker=ticker,
                bull_case=bull_case,
                bear_case=bear_case,
                key_assumptions=key_assumptions,
                catalysts=catalysts,
                conviction=conviction,
                unresolved_risks=unresolved_risks,
                is_draft=is_draft,
                asset_class=asset_class,
                direction=direction,
                status=status,
                mnpi_flag=mnpi_flag,
            )

    async def _create_thesis_locked(
        self,
        ticker: str,
        *,
        bull_case: str | None,
        bear_case: str | None,
        key_assumptions: list[dict[str, Any]] | None,
        catalysts: list[Any] | None,
        conviction: int | None,
        unresolved_risks: list[Any] | None,
        is_draft: bool,
        asset_class: str,
        direction: str,
        status: str,
        mnpi_flag: bool,
    ) -> ThesisVersion:
        version = await self._next_version(ticker)
        thesis = ThesisVersion(
            pm_id=self.pm_id,
            ticker=ticker,
            version=version,
            is_draft=is_draft,
            asset_class=asset_class,
            direction=direction,
            status=status,
            bull_case=bull_case,
            bear_case=bear_case,
            key_assumptions=key_assumptions or [],
            catalysts=catalysts or [],
            conviction=conviction,
            unresolved_risks=unresolved_risks or [],
            fund_entity_id=self.fund_entity_id,
            mnpi_flag=mnpi_flag,
        )
        self.session.add(thesis)
        await self.session.flush()
        await self._upsert_ticker_registry(ticker, version, asset_class, direction)
        await self._audit("thesis_create", thesis)
        await self.uow.commit()
        return thesis

    async def update_thesis(self, ticker: str, **changes: Any) -> ThesisVersion:
        """Create a new thesis version with the supplied changes applied."""
        async with _ThesisLocks.get(self.pm_id, ticker):
            prior = await self._latest_locked(ticker)
            if prior is None:
                return await self._create_thesis_locked(ticker=ticker, **changes)

            version = await self._next_version(ticker)
            next_version = ThesisVersion(
                pm_id=self.pm_id,
                ticker=ticker,
                version=version,
                is_draft=changes.get("is_draft", prior.is_draft),
                asset_class=changes.get("asset_class", prior.asset_class),
                direction=changes.get("direction", prior.direction),
                status=changes.get("status", prior.status),
                bull_case=changes.get("bull_case", prior.bull_case),
                bear_case=changes.get("bear_case", prior.bear_case),
                key_assumptions=changes.get("key_assumptions", prior.key_assumptions),
                catalysts=changes.get("catalysts", prior.catalysts),
                conviction=changes.get("conviction", prior.conviction),
                unresolved_risks=changes.get("unresolved_risks", prior.unresolved_risks),
                fund_entity_id=self.fund_entity_id,
                mnpi_flag=changes.get("mnpi_flag", prior.mnpi_flag),
            )
            self.session.add(next_version)
            await self.session.flush()
            await self._upsert_ticker_registry(
                ticker,
                version,
                next_version.asset_class,
                next_version.direction,
            )
            await self._audit("thesis_update", next_version, prior)
            await self.uow.commit()
            return next_version

    async def get_latest_thesis(self, ticker: str) -> ThesisVersion | None:
        """Return the highest-version thesis for a ticker."""

        async def _build() -> ThesisVersion | None:
            return cast(
                ThesisVersion | None,
                await self.session.scalar(
                    IsolationService.select_for(ThesisVersion)
                    .where(ThesisVersion.ticker == ticker)
                    .order_by(desc(ThesisVersion.version))
                    .limit(1)
                ),
            )

        return await self._with_context(_build)

    async def get_version(self, ticker: str, version: int) -> ThesisVersion | None:
        """Return a specific thesis version."""

        async def _build() -> ThesisVersion | None:
            return cast(
                ThesisVersion | None,
                await self.session.scalar(
                    IsolationService.select_for(ThesisVersion).where(
                        ThesisVersion.ticker == ticker,
                        ThesisVersion.version == version,
                    )
                ),
            )

        return await self._with_context(_build)

    async def list_thesis_versions(self, ticker: str) -> list[ThesisVersion]:
        """Return all thesis versions for a ticker, oldest first."""

        async def _build() -> list[ThesisVersion]:
            result = await self.session.execute(
                IsolationService.select_for(ThesisVersion)
                .where(ThesisVersion.ticker == ticker)
                .order_by(ThesisVersion.version)
            )
            return list(result.scalars().all())

        return await self._with_context(_build)

    async def get_version_diff(
        self,
        ticker: str,
        version_a: int,
        version_b: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return changed fields between two thesis versions.

        If ``version_b`` is omitted, ``version_a - 1`` is used.
        """
        a = await self.get_version(ticker, version_a)
        if a is None:
            raise ValueError(f"Version {version_a} not found for {ticker}")
        target_b = version_b if version_b is not None else version_a - 1
        b = await self.get_version(ticker, target_b)

        changes: dict[str, dict[str, Any]] = {}
        if b is None and version_b is None:
            # First version: no prior version to diff against.
            return changes
        if b is None:
            raise ValueError(f"Version {target_b} not found for {ticker}")

        for column in ThesisVersion.__table__.columns:
            name = column.name
            value_a = getattr(a, name)
            value_b = getattr(b, name)
            if column.name in {"id", "created_at"}:
                continue
            if self._values_differ(value_a, value_b):
                changes[name] = {"old": value_b, "new": value_a}
        return changes

    @staticmethod
    def _values_differ(a: Any, b: Any) -> bool:
        if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
            return json.dumps(a, sort_keys=True, default=str) != json.dumps(
                b, sort_keys=True, default=str
            )
        return bool(a != b)

    async def _next_version(self, ticker: str) -> int:
        max_version = await self.session.scalar(
            IsolationService.scope(
                select(func.max(ThesisVersion.version)),
                ThesisVersion,
                self.pm_id,
            ).where(ThesisVersion.ticker == ticker)
        )
        return (max_version or 0) + 1

    async def _latest_locked(self, ticker: str) -> ThesisVersion | None:
        result = await self.session.execute(
            IsolationService.scope(select(ThesisVersion), ThesisVersion, self.pm_id)
            .where(ThesisVersion.ticker == ticker)
            .order_by(desc(ThesisVersion.version))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _upsert_ticker_registry(
        self,
        ticker: str,
        version: int,
        asset_class: str,
        direction: str,
    ) -> None:
        result = await self.session.execute(
            IsolationService.scope(select(TickerRegistry), TickerRegistry, self.pm_id).where(
                TickerRegistry.ticker == ticker,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.last_thesis_version = version
            existing.asset_class = asset_class
            existing.direction = direction
        else:
            registry = TickerRegistry(
                pm_id=self.pm_id,
                ticker=ticker,
                asset_class=asset_class,
                direction=direction,
                last_thesis_version=version,
            )
            self.session.add(registry)
        await self.session.flush()

    async def _audit(
        self,
        action_type: str,
        new_thesis: ThesisVersion,
        prior: ThesisVersion | None = None,
    ) -> None:
        entry = AuditLog(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            action_type=action_type,
            object_type="thesis_version",
            object_id=new_thesis.id,
            before_state=_state_dict(prior) if prior else {},
            after_state=_state_dict(new_thesis),
        )
        self.session.add(entry)
        await self.session.flush()


class DriftDetectionService:
    """Surface theses that are eligible for drift detection and alerting."""

    def __init__(self, uow: UnitOfWork, pm_id: str) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        # Production sets RequestContext via middleware; tests and background
        # workers may need an explicit bind. `_ContextHelper`` is a no-op when
        # a context is already active.
        self._context = _ContextHelper(pm_id, None)

    async def alertable_latest_theses(self) -> list[ThesisVersion]:
        """Return the latest published (non-draft) thesis per ticker."""
        with self._context:
            subq = (
                IsolationService.scope(
                    select(
                        ThesisVersion.ticker,
                        func.max(ThesisVersion.version).label("max_version"),
                    ),
                    ThesisVersion,
                    self.pm_id,
                )
                .where(ThesisVersion.is_draft.is_(False))
                .select_from(ThesisVersion)
                .group_by(ThesisVersion.ticker)
                .subquery()
            )
            result = await self.session.execute(
                IsolationService.select_for(ThesisVersion)
                .join(
                    subq,
                    (ThesisVersion.ticker == subq.c.ticker)
                    & (ThesisVersion.version == subq.c.max_version),
                )
                .where(ThesisVersion.is_draft.is_(False))
                .order_by(ThesisVersion.ticker)
            )
            return list(result.scalars().all())
