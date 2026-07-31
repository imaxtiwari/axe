"""Tests for thesis versioning, ticker registry sync, and drift exclusion."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from axe.agents.thesis_extract import ThesisExtractAgent
from axe.db.models import AuditLog, FundEntity, PMUser, TickerRegistry
from axe.db.uow import UnitOfWork
from axe.services.thesis import DriftDetectionService, ThesisRepo


async def _fund_entity(session: AsyncSession) -> FundEntity:
    fund = FundEntity(
        id=str(uuid.uuid4()),
        legal_name=f"Test Fund {uuid.uuid4().hex[:8]}",
        data_residency="US",
    )
    session.add(fund)
    await session.flush()
    return fund


async def _pm_user(session: AsyncSession, fund_id: str) -> PMUser:
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund_id,
        email=f"test_{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    await session.flush()
    return user


async def _setup_pm(
    session: AsyncSession,
) -> tuple[FundEntity, PMUser]:
    fund = await _fund_entity(session)
    user = await _pm_user(session, fund.id)
    await session.commit()
    return fund, user


@pytest.mark.asyncio
async def test_create_thesis_versions(db_session: AsyncSession) -> None:
    """Creating and updating a thesis produces immutable versioned rows."""
    fund, user = await _setup_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, user.id, fund.id)

        v1 = await repo.create_thesis(
            "AAPL",
            bull_case="Services growth remains strong.",
            conviction=4,
        )
        assert v1.version == 1

        v2 = await repo.update_thesis(
            "AAPL",
            bull_case="Services growth accelerated; margins expanded.",
            conviction=5,
        )
        assert v2.version == 2

        latest = await repo.get_latest_thesis("AAPL")
        assert latest is not None
        assert latest.version == 2
        assert latest.bull_case == "Services growth accelerated; margins expanded."

        all_versions = await repo.list_thesis_versions("AAPL")
        assert [v.version for v in all_versions] == [1, 2]


@pytest.mark.asyncio
async def test_get_version_and_version_diff(db_session: AsyncSession) -> None:
    """Specific versions can be fetched and diffed accurately."""
    fund, user = await _setup_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, user.id, fund.id)

        await repo.create_thesis(
            "TSLA",
            bull_case="Bull case 1",
            bear_case="Demand softening.",
            direction="long",
        )
        await repo.update_thesis(
            "TSLA",
            bull_case="Bull case 2",
            direction="short",
        )

        v1 = await repo.get_version("TSLA", 1)
        assert v1 is not None
        assert v1.bull_case == "Bull case 1"
        assert v1.direction == "long"

        diff = await repo.get_version_diff("TSLA", 2)
        assert set(diff.keys()) == {"bull_case", "direction", "version"}
        assert diff["bull_case"] == {"old": "Bull case 1", "new": "Bull case 2"}
        assert diff["direction"] == {"old": "long", "new": "short"}
        assert diff["version"] == {"old": 1, "new": 2}


@pytest.mark.asyncio
async def test_ticker_registry_auto_updated(db_session: AsyncSession) -> None:
    """Creating/updating a thesis updates the PM's ticker registry."""
    fund, user = await _setup_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, user.id, fund.id)

        await repo.create_thesis("NVDA", direction="long", asset_class="equity")
        await repo.update_thesis("NVDA", direction="short")

    result = await db_session.execute(
        select(TickerRegistry).where(
            TickerRegistry.pm_id == user.id,
            TickerRegistry.ticker == "NVDA",
        )
    )
    registry = result.scalar_one()
    assert registry.last_thesis_version == 2
    assert registry.direction == "short"
    assert registry.asset_class == "equity"


@pytest.mark.asyncio
async def test_audit_log_for_create_and_update(db_session: AsyncSession) -> None:
    """Every thesis mutation is recorded in the audit log."""
    fund, user = await _setup_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, user.id, fund.id)

        created = await repo.create_thesis("MSFT", bull_case="Cloud growth.")
        updated = await repo.update_thesis("MSFT", bull_case="Cloud growth; AI tailwinds.")

    result = await db_session.execute(
        select(AuditLog)
        .where(
            AuditLog.pm_id == user.id,
            AuditLog.object_type == "thesis_version",
        )
        .order_by(AuditLog.server_timestamp)
    )
    entries = list(result.scalars().all())
    assert len(entries) == 2
    assert entries[0].action_type == "thesis_create"
    assert entries[0].object_id == created.id
    assert entries[1].action_type == "thesis_update"
    assert entries[1].object_id == updated.id
    assert entries[1].before_state["id"] == created.id
    assert entries[1].after_state["id"] == updated.id


@pytest.mark.asyncio
async def test_extract_agent_and_create(db_session: AsyncSession) -> None:
    """The extraction agent can feed a structured payload into the repository."""
    from axe.agents.llm import MockProvider

    fund, user = await _setup_pm(db_session)
    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "ticker": "AMZN",
                    "bull_case": "AWS re-acceleration",
                    "conviction": 4,
                    "direction": "long",
                }
            }
        ]
    )
    agent = ThesisExtractAgent(provider=provider)

    payload = await agent.from_natural_language(
        "TICKER: AMZN. Bull: AWS re-acceleration. Conviction: 4."
    )
    assert payload["ticker"] == "AMZN"
    assert "bull_case" in payload
    assert payload["is_draft"] is False
    assert payload["direction"] == "long"
    assert payload["conviction"] == 4

    # Filter the payload down to the fields accepted by create_thesis.
    create_kwargs: dict[str, Any] = {
        k: v
        for k, v in payload.items()
        if k
        in {
            "bull_case",
            "bear_case",
            "key_assumptions",
            "catalysts",
            "conviction",
            "unresolved_risks",
            "is_draft",
            "asset_class",
            "direction",
            "status",
        }
    }
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, user.id, fund.id)
        thesis = await repo.create_thesis(payload["ticker"], **create_kwargs)
    assert thesis.ticker == "AMZN"
    assert thesis.direction == "long"
    assert thesis.is_draft is False


@pytest.mark.asyncio
async def test_draft_excluded_from_alerts(db_session: AsyncSession) -> None:
    """The drift detection service only surfaces non-draft theses."""
    fund, user = await _setup_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, user.id, fund.id)

        await repo.create_thesis("DRAFT", bull_case="Draft idea.", is_draft=True)
        await repo.create_thesis("PUB", bull_case="Published idea.", is_draft=False)

        drift = DriftDetectionService(uow, user.id)
        alertable = await drift.alertable_latest_theses()
        tickers = {t.ticker for t in alertable}
        assert tickers == {"PUB"}


@pytest.mark.asyncio
async def test_concurrent_writes_serialized(engine) -> None:
    """Concurrent updates for the same ticker produce sequential version numbers."""
    session_maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_maker() as session:
        fund = await _fund_entity(session)
        user = await _pm_user(session, fund.id)
        await session.commit()

    async def worker(i: int) -> int:
        async with session_maker() as session:
            async with UnitOfWork(session) as uow:
                repo = ThesisRepo(uow, user.id, fund.id)
                if i == 0:
                    await repo.create_thesis("CONC", bull_case=f"init {i}")
                else:
                    await repo.update_thesis("CONC", bull_case=f"update {i}")
            return i

    await asyncio.gather(*[worker(i) for i in range(10)])

    async with session_maker() as session:
        async with UnitOfWork(session) as uow:
            repo = ThesisRepo(uow, user.id, fund.id)
            versions = await repo.list_thesis_versions("CONC")
            assert [v.version for v in versions] == list(range(1, 11))

            latest = await repo.get_latest_thesis("CONC")
            assert latest is not None
            assert latest.version == 10

        registry = await session.scalar(
            select(TickerRegistry).where(
                TickerRegistry.pm_id == user.id,
                TickerRegistry.ticker == "CONC",
            )
        )
        assert registry is not None
        assert registry.last_thesis_version == 10
