"""Tests for MorningBriefAgent specialist-signals curation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.morning_brief import MorningBriefAgent
from axe.db.models import FundEntity, PMUser, SpecialistSignal, ThesisVersion, TickerRegistry


def _make_pm_and_fund(session: AsyncSession) -> tuple[FundEntity, PMUser]:
    fund = FundEntity(id=str(uuid4()), legal_name="Signal Fund")
    pm = PMUser(
        id=str(uuid4()),
        email="pm@signal.example",
        active=True,
        fund_entity_id=fund.id,
    )
    session.add_all([fund, pm])
    return fund, pm


@pytest.fixture
def mock_brief_llm():
    llm = AsyncMock()
    llm.complete.return_value.parsed = {}
    return llm


@pytest.fixture
def mock_brief_embedding():
    emb = AsyncMock()
    emb.embed.return_value = [0.0] * 384
    return emb


@pytest.mark.asyncio
async def test_specialist_signals_curated_active_tickers_top_5(
    db_session: AsyncSession, mock_brief_llm, mock_brief_embedding
):
    """Only active-ticker signals are surfaced, sorted by confidence, capped at 5."""
    fund, pm = _make_pm_and_fund(db_session)
    await db_session.flush()

    db_session.add(TickerRegistry(pm_id=pm.id, ticker="AAPL", active=True, status="active"))
    db_session.add(TickerRegistry(pm_id=pm.id, ticker="TSLA", active=True, status="active"))
    db_session.add(TickerRegistry(pm_id=pm.id, ticker="GOOGL", active=False, status="inactive"))
    db_session.add(
        ThesisVersion(
            pm_id=pm.id,
            ticker="AAPL",
            version=1,
            fund_entity_id=fund.id,
            is_draft=False,
            status="active",
            key_assumptions=[{"id": "a1", "text": "Revenue grows YoY"}],
        )
    )

    as_of = datetime.now(UTC)
    window_start = as_of - timedelta(hours=24)

    # Six active AAPL signals; only the top five should survive the cap.
    for confidence in [0.9, 0.85, 0.8, 0.75, 0.7, 0.65]:
        db_session.add(
            SpecialistSignal(
                pm_id=pm.id,
                ticker="AAPL",
                source_type="earnings",
                specialist_agent="earnings",
                signal_type="guidance_change",
                confidence=confidence,
                created_at=window_start + timedelta(minutes=1),
            )
        )

    # Lower-confidence active TSLA signal should appear after AAPL top five.
    db_session.add(
        SpecialistSignal(
            pm_id=pm.id,
            ticker="TSLA",
            source_type="broker",
            specialist_agent="broker",
            signal_type="estimate_revision",
            confidence=0.5,
            created_at=window_start + timedelta(minutes=2),
        )
    )

    # Signal for a ticker the PM does not track should be filtered out.
    db_session.add(
        SpecialistSignal(
            pm_id=pm.id,
            ticker="NVDA",
            source_type="research_edge",
            specialist_agent="research_edge",
            signal_type="upgrade",
            confidence=0.99,
            created_at=window_start + timedelta(minutes=3),
        )
    )

    # Signal for an inactive ticker should be filtered out even if confidence is high.
    db_session.add(
        SpecialistSignal(
            pm_id=pm.id,
            ticker="GOOGL",
            source_type="pdf_deck",
            specialist_agent="pdf_deck",
            signal_type="valuation",
            confidence=0.97,
            created_at=window_start + timedelta(minutes=4),
        )
    )

    # Stale signal outside the 24h window should not appear.
    db_session.add(
        SpecialistSignal(
            pm_id=pm.id,
            ticker="AAPL",
            source_type="expert_network",
            specialist_agent="expert_network",
            signal_type="channel_check",
            confidence=1.0,
            created_at=window_start - timedelta(minutes=1),
        )
    )

    await db_session.commit()

    agent = MorningBriefAgent(db_session, llm=mock_brief_llm, embedding=mock_brief_embedding)
    brief = await agent.generate(pm.id, as_of=as_of)

    signals = brief.specialist_signals
    assert len(signals) == 5
    tickers = [s.ticker for s in signals]
    assert "NVDA" not in tickers
    assert "GOOGL" not in tickers
    assert "TSLA" not in tickers
    assert all(s.ticker == "AAPL" for s in signals)

    expected_confidences = [0.9, 0.85, 0.8, 0.75, 0.7]
    assert [s.confidence for s in signals] == pytest.approx(expected_confidences)
    assert signals[0].ticker == "AAPL"


@pytest.mark.asyncio
async def test_specialist_signals_empty_when_none_active(
    db_session: AsyncSession, mock_brief_llm, mock_brief_embedding
):
    """If no recent specialist signals match active tickers, the section is empty."""
    fund, pm = _make_pm_and_fund(db_session)
    await db_session.flush()

    db_session.add(TickerRegistry(pm_id=pm.id, ticker="AAPL", active=True, status="active"))
    db_session.add(
        ThesisVersion(
            pm_id=pm.id,
            ticker="AAPL",
            version=1,
            fund_entity_id=fund.id,
            is_draft=False,
            status="active",
        )
    )

    as_of = datetime.now(UTC)
    window_start = as_of - timedelta(hours=24)
    # Signal for inactive ticker only.
    db_session.add(
        SpecialistSignal(
            pm_id=pm.id,
            ticker="META",
            source_type="crm",
            specialist_agent="crm",
            signal_type="management_change",
            confidence=0.95,
            created_at=window_start + timedelta(minutes=1),
        )
    )
    await db_session.commit()

    agent = MorningBriefAgent(db_session, llm=mock_brief_llm, embedding=mock_brief_embedding)
    brief = await agent.generate(pm.id, as_of=as_of)

    assert brief.specialist_signals == []
