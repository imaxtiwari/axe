"""Tests for drift detection consuming structured SpecialistSignal records."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.drift_detect import DriftDetectionAgent, EarningsAlertService
from axe.agents.embedding import ThresholdMockEmbedding
from axe.agents.llm import MockProvider
from axe.agents.model_trace import TraceableProvider
from axe.config import Settings
from axe.db.models import BrokenAssumption, FundEntity, PMUser, SpecialistSignal
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.thesis import ThesisRepo


async def _seed_pm(session: AsyncSession) -> tuple[str, str]:
    fund_id = str(uuid.uuid4())
    pm_id = str(uuid.uuid4())
    fund = FundEntity(id=fund_id, legal_name="Drift Specialist Test Fund")
    pm = PMUser(
        id=pm_id,
        fund_entity_id=fund_id,
        email=f"pm-{pm_id[:8]}@example.com",
    )
    session.add_all([fund, pm])
    await session.flush()
    return fund_id, pm_id


def _specialist_signal(
    pm_id: str,
    ticker: str,
    summary: str,
    stance: str = "CONTRADICTS",
    source_type: str = "research_edge",
    agent_name: str = "ResearchEdgeSpecialist",
) -> SpecialistSignal:
    return SpecialistSignal(
        id=str(uuid.uuid4()),
        pm_id=pm_id,
        ticker=ticker,
        source_type=source_type,
        specialist_agent=agent_name,
        signal_type="research_note",
        summary=summary,
        stance=stance,
        confidence=0.75,
        evidence_json={"url": f"https://example.com/{ticker}"},
    )


@pytest.mark.asyncio
async def test_process_specialist_signal_creates_alert_and_signal_log(
    db_session: AsyncSession,
) -> None:
    """A SpecialistSignal that contradicts a thesis assumption produces an alert."""
    fund_id, pm_id = await _seed_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, pm_id, fund_id)
        await repo.create_thesis(
            "AAPL",
            key_assumptions=[{"id": "a1", "statement": "iPhone revenue grows 5% YoY"}],
            is_draft=False,
        )

    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "stance": "CONTRADICTS",
                    "reasoning": "iPhone revenue declined 10%.",
                    "confidence": 0.9,
                    "evidence_quote": "iPhone revenue dropped 10% YoY.",
                }
            }
        ]
    )
    embed = ThresholdMockEmbedding(similarity=0.85)

    signal = _specialist_signal(
        pm_id=pm_id,
        ticker="AAPL",
        summary="iPhone revenue dropped 10% YoY.",
    )
    db_session.add(signal)
    await db_session.flush()

    async with UnitOfWork(db_session) as uow:
        service = EarningsAlertService(
            uow=uow,
            drift_agent=DriftDetectionAgent(provider=provider, embedding_model=embed),
        )
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            result = await service.process_specialist_signals(pm_id, [signal])
        await uow.commit()

    assert len(result["alerts"]) == 1
    assert len(result["signal_logs"]) == 1
    assert result["human_reviews"] == []

    alert = result["alerts"][0]
    assert alert["ticker"] == "AAPL"
    assert alert["stance"] == "CONTRADICTS"
    assert alert["assumption_id"] == "a1"
    assert "iPhone revenue grows 5% YoY" in alert["message"]

    signal_log = result["signal_logs"][0]
    assert signal_log.stance == "CONTRADICTS"
    assert signal_log.alerted is True
    assert signal_log.pm_id == pm_id
    assert signal_log.source_type == "research_edge"

    broken = await db_session.execute(
        select(BrokenAssumption).where(BrokenAssumption.pm_id == pm_id)
    )
    broken_rows = list(broken.scalars().all())
    assert len(broken_rows) == 1
    assert broken_rows[0].assumption_id == "a1"


@pytest.mark.asyncio
async def test_process_specialist_signal_human_review_on_high_hallucination_score(
    db_session: AsyncSession,
) -> None:
    """A high hallucination_score on the captured ModelTrace routes to human review."""
    fund_id, pm_id = await _seed_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, pm_id, fund_id)
        await repo.create_thesis(
            "TSLA",
            key_assumptions=[{"id": "a1", "statement": "Deliveries grow 20% YoY"}],
            is_draft=False,
        )

    # We need two responses because classify_assumptions may call the LLM once
    # per assumption. The second response is unused when the first trace already
    # forces human review, but keep the queue sized defensively.
    inner_provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "stance": "CONTRADICTS",
                    "reasoning": "Deliveries missed.",
                    "confidence": 0.9,
                    "evidence_quote": "Deliveries dropped.",
                }
            }
        ]
    )
    embed = ThresholdMockEmbedding(similarity=0.85)

    signal = _specialist_signal(
        pm_id=pm_id,
        ticker="TSLA",
        summary="Deliveries dropped sharply this quarter.",
    )
    db_session.add(signal)
    await db_session.flush()

    async with UnitOfWork(db_session) as uow:
        traceable = TraceableProvider(
            inner_provider,
            agent="SpecialistDrift.ReviewTest",
            uow=uow,
            settings=Settings(app_env="test"),
        )
        service = EarningsAlertService(
            uow=uow,
            drift_agent=DriftDetectionAgent(provider=traceable, embedding_model=embed),
        )
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            await service.process_specialist_signals(pm_id, [signal])
        await uow.commit()

        # Manually bump the hallucination score on the persisted trace so the
        # service routes the classification to human review instead of alerting.
        if traceable.last_trace is not None:
            traceable.last_trace.hallucination_score = 0.95
            traceable.last_trace.human_review_status = "required"
            await uow.commit()

    # Re-run processing with a fresh service tied to the same traceable provider so
    # ``last_trace`` already has a high hallucination score and forces escalation.
    async with UnitOfWork(db_session) as uow:
        service2 = EarningsAlertService(
            uow=uow,
            drift_agent=DriftDetectionAgent(provider=traceable, embedding_model=embed),
        )
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            result2 = await service2.process_specialist_signals(pm_id, [signal])
        await uow.commit()

    assert len(result2["human_reviews"]) == 1
    assert len(result2["alerts"]) == 0

    review = result2["human_reviews"][0]
    assert review["ticker"] == "TSLA"
    assert review["pm_id"] == pm_id
    assert review["assumption_id"] == "a1"
    assert "HUMAN REVIEW REQUESTED" in review["message"]
    assert review["source_type"] == "research_edge"


@pytest.mark.asyncio
async def test_process_specialist_signal_no_thesis_no_alert(
    db_session: AsyncSession,
) -> None:
    """Specialist signals for tickers without published theses are ignored."""
    fund_id, pm_id = await _seed_pm(db_session)

    signal = _specialist_signal(
        pm_id=pm_id,
        ticker="UNKNOWN",
        summary="Something happened.",
    )
    db_session.add(signal)
    await db_session.flush()

    async with UnitOfWork(db_session) as uow:
        service = EarningsAlertService(uow=uow)
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            result = await service.process_specialist_signals(pm_id, [signal])

    assert result["alerts"] == []
    assert result["human_reviews"] == []
    assert result["signal_logs"] == []


@pytest.mark.asyncio
async def test_process_specialist_signal_draft_thesis_no_alert(
    db_session: AsyncSession,
) -> None:
    """Draft theses are not eligible for specialist-signal drift alerts."""
    fund_id, pm_id = await _seed_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, pm_id, fund_id)
        await repo.create_thesis(
            "NVDA",
            key_assumptions=[{"id": "a1", "statement": "Data-center revenue doubles"}],
            is_draft=True,
        )

    signal = _specialist_signal(
        pm_id=pm_id,
        ticker="NVDA",
        summary="Data-center revenue was flat.",
    )
    db_session.add(signal)
    await db_session.flush()

    async with UnitOfWork(db_session) as uow:
        service = EarningsAlertService(uow=uow)
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            result = await service.process_specialist_signals(pm_id, [signal])

    assert result["alerts"] == []
    assert result["human_reviews"] == []
    assert result["signal_logs"] == []


@pytest.mark.asyncio
async def test_process_specialist_signal_confirms_no_alert(
    db_session: AsyncSession,
) -> None:
    """Confirming specialist signals do not produce alerts."""
    fund_id, pm_id = await _seed_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, pm_id, fund_id)
        await repo.create_thesis(
            "MSFT",
            key_assumptions=[{"id": "a1", "statement": "Cloud revenue grows 25% YoY"}],
            is_draft=False,
        )

    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "stance": "CONFIRMS",
                    "reasoning": "Azure grew 30%.",
                    "confidence": 0.85,
                    "evidence_quote": "Azure revenue up 30%.",
                }
            }
        ]
    )
    embed = ThresholdMockEmbedding(similarity=0.85)

    signal = _specialist_signal(
        pm_id=pm_id,
        ticker="MSFT",
        summary="Azure revenue up 30%.",
        stance="CONFIRMS",
    )
    db_session.add(signal)
    await db_session.flush()

    async with UnitOfWork(db_session) as uow:
        service = EarningsAlertService(
            uow=uow,
            drift_agent=DriftDetectionAgent(provider=provider, embedding_model=embed),
        )
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            result = await service.process_specialist_signals(pm_id, [signal])
        await uow.commit()

    assert result["alerts"] == []
    assert result["human_reviews"] == []
    assert len(result["signal_logs"]) == 1
    assert result["signal_logs"][0].stance == "CONFIRMS"


@pytest.mark.asyncio
async def test_process_specialist_signal_deduplicates_broken_assumptions(
    db_session: AsyncSession,
) -> None:
    """Re-alerting for an already-broken assumption is suppressed."""
    fund_id, pm_id = await _seed_pm(db_session)
    async with UnitOfWork(db_session) as uow:
        repo = ThesisRepo(uow, pm_id, fund_id)
        await repo.create_thesis(
            "META",
            key_assumptions=[{"id": "a1", "statement": "Ad revenue grows 15% YoY"}],
            is_draft=False,
        )

    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "stance": "CONTRADICTS",
                    "reasoning": "Ad revenue dropped.",
                    "confidence": 0.9,
                    "evidence_quote": "Ad revenue down 5%.",
                }
            }
        ]
    )
    embed = ThresholdMockEmbedding(similarity=0.85)

    signal = _specialist_signal(
        pm_id=pm_id,
        ticker="META",
        summary="Ad revenue down 5%.",
    )
    db_session.add(signal)
    await db_session.flush()

    async with UnitOfWork(db_session) as uow:
        service = EarningsAlertService(
            uow=uow,
            drift_agent=DriftDetectionAgent(provider=provider, embedding_model=embed),
        )
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            first = await service.process_specialist_signals(pm_id, [signal])
        await uow.commit()
    assert len(first["alerts"]) == 1

    async with UnitOfWork(db_session) as uow:
        service2 = EarningsAlertService(
            uow=uow,
            drift_agent=DriftDetectionAgent(provider=provider, embedding_model=embed),
        )
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            second = await service2.process_specialist_signals(pm_id, [signal])
        await uow.commit()
    assert second["alerts"] == []
