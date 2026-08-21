"""Tests for MorningBriefAgent, BriefReplyAgent, delivery and scheduler."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.brief_reply import BriefReplyAgent
from axe.agents.embedding import cosine_similarity
from axe.agents.morning_brief import (
    BriefSection,
    CatalystItem,
    FocusOne,
    MorningBriefAgent,
    MorningBriefOutput,
    is_nyse_trading_day,
)
from axe.db.models import (
    FundEntity,
    MorningBrief,
    PMUser,
    SignalFeedback,
    SignalLog,
    ThesisVersion,
)
from axe.services.brief_delivery import deliver_brief, format_brief
from axe.services.brief_scheduler import (
    deliver_briefs_to_all_active_pms,
    generate_and_deliver_for_pm,
    schedule_brief_jobs,
)


def _make_brief(pm_id: str, brief_id: str = "brief_1") -> MorningBrief:
    return MorningBrief(
        id=brief_id,
        pm_id=pm_id,
        date=date.today(),
        sections=[],
        focus_one={},
        catalyst_week=[],
    )


@pytest.fixture
def mock_llm():
    """Return a mocked LLMProvider."""
    llm = AsyncMock()
    llm.embed.return_value = [[0.1] * 384]
    return llm


@pytest.fixture
def mock_delivery():
    deliv = AsyncMock()
    deliv.send_slack_dm.return_value = {"ok": True}
    deliv.send_email.return_value = {"id": "msg_123", "ok": True}
    return deliv


@pytest.mark.asyncio
async def test_focus_one_contradiction_wins(db_session: AsyncSession, mock_llm):
    fund = FundEntity(id=str(uuid4()), legal_name="Test Fund")
    pm = PMUser(id=str(uuid4()), email="pm@example.com", active=True, fund_entity_id=fund.id)
    db_session.add_all([fund, pm])
    await db_session.flush()
    thesis = ThesisVersion(
        id=str(uuid4()),
        pm_id=pm.id,
        ticker="META",
        version=1,
        fund_entity_id=fund.id,
        key_assumptions=[{"id": "a1", "text": "Ad revenue grows 20% YoY"}],
    )
    db_session.add(thesis)
    await db_session.commit()

    agent = MorningBriefAgent(db_session, llm=mock_llm)
    section = BriefSection(
        ticker="META",
        assumption_id="a1",
        assumption_text="Ad revenue grows 20% YoY",
        headline="Snap ad revenue declines 30%",
        body="Snap reported weak ad demand.",
        stance="CONTRADICTS",
        relevance_score=0.95,
    )

    focus = agent._pick_focus_one([section], [], [thesis], None)

    assert focus is not None
    assert focus.ticker == "META"
    assert "Contradicting signal" in focus.reason
    assert focus.urgency_score > 0.7


@pytest.mark.asyncio
async def test_build_sections_returns_holding_for_thesis_without_signals(
    db_session: AsyncSession, mock_llm
):
    fund = FundEntity(id=str(uuid4()), legal_name="Test Fund")
    pm = PMUser(id=str(uuid4()), email="pm@example.com", active=True, fund_entity_id=fund.id)
    db_session.add_all([fund, pm])
    await db_session.flush()
    thesis = ThesisVersion(
        id=str(uuid4()),
        pm_id=pm.id,
        ticker="META",
        version=1,
        fund_entity_id=fund.id,
        key_assumptions=[{"id": "a1", "text": "Ad revenue grows 20% YoY"}],
    )
    db_session.add(thesis)
    await db_session.commit()

    agent = MorningBriefAgent(db_session, llm=mock_llm)
    sections = agent._build_sections([], [thesis])
    assert any(s.ticker == "META" and not s.source_ids for s in sections)


@pytest.mark.asyncio
async def test_catalyst_calendar_this_week():
    today = date(2026, 7, 27)  # Monday
    event_tsla = CatalystItem(
        date=str(today + timedelta(days=1)),
        event_type="earnings",
        ticker="TSLA",
        description="TSLA earnings",
        source_url="https://polygon.io/events/1",
    )
    event_nvda = CatalystItem(
        date=str(today + timedelta(days=8)),
        event_type="earnings",
        ticker="NVDA",
        description="NVDA earnings",
        source_url="https://polygon.io/events/2",
    )
    events = {
        today + timedelta(days=1): [event_tsla],
        today + timedelta(days=8): [event_nvda],
    }
    # Include only events within the next seven days.
    week = [
        item
        for items in events.values()
        for item in items
        if date.fromisoformat(item.date) <= today + timedelta(days=7)
    ]
    assert len(week) == 1
    assert week[0].ticker == "TSLA"


@pytest.mark.asyncio
async def test_brief_not_generated_on_holiday(db_session: AsyncSession, mock_llm):
    fund = FundEntity(id=str(uuid4()), legal_name="Test Fund")
    pm = PMUser(
        id=str(uuid4()),
        email="pm@example.com",
        active=True,
        slack_user_id="U1",
        fund_entity_id=fund.id,
    )
    db_session.add_all([fund, pm])
    await db_session.commit()

    # Christmas Day 2026 falls on a Friday.
    holiday = datetime(2026, 12, 25, 7, 0, tzinfo=UTC)
    brief = await generate_and_deliver_for_pm(db_session, pm, as_of=holiday)
    assert brief is None


@pytest.mark.asyncio
async def test_brief_generated_and_saved(db_session: AsyncSession, mock_llm, mock_delivery):
    fund = FundEntity(id=str(uuid4()), legal_name="Test Fund")
    pm = PMUser(
        id=str(uuid4()),
        email="pm@example.com",
        active=True,
        slack_user_id="U1",
        fund_entity_id=fund.id,
    )
    db_session.add_all([fund, pm])
    await db_session.commit()

    agent = MorningBriefAgent(db_session, llm=mock_llm)
    with (
        patch.object(agent, "_build_sections", new=MagicMock(return_value=[])),
        patch.object(agent, "_pick_focus_one", new=MagicMock(return_value=None)),
    ):
        brief = await agent.generate(pm.id)

        async def _deliver(_brief: MorningBriefOutput) -> dict[str, Any]:
            return await mock_delivery()

        saved = await agent.save_and_deliver(pm.id, brief, deliver_fn=_deliver)

    assert saved is not None
    assert saved.pm_id == pm.id
    assert saved.sections == []


@pytest.mark.asyncio
async def test_format_brief_contains_focus_one():
    brief = MorningBriefOutput(
        focus_one=FocusOne(
            ticker="NVDA",
            reason="Strong confirms signal on AI demand.",
            urgency_score=0.92,
        ),
        sections=[],
        catalyst_week=[],
    )
    text = format_brief(brief)
    assert "NVDA" in text
    assert "0.92" in text
    assert "Strong confirms signal" in text


@pytest.mark.asyncio
async def test_deliver_brief_sends_slack_and_email(mock_delivery):
    brief = MorningBriefOutput(
        focus_one=None,
        sections=[],
        catalyst_week=[],
    )
    result = await deliver_brief(
        brief,
        slack_user_id="U123",
        email="pm@example.com",
        delivery=mock_delivery,
    )
    assert result["slack_ok"] is True
    assert result["email_ok"] is True


@pytest.mark.asyncio
async def test_scheduler_cron_job_registered():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.start(paused=True)
    session_maker = MagicMock()
    schedule_brief_jobs(scheduler, session_maker)
    jobs = scheduler.get_jobs()
    assert any(j.id == "morning_brief_0700_utc" for j in jobs)
    scheduler.shutdown()


@pytest.mark.asyncio
async def test_full_scheduler_dispatch(db_session: AsyncSession, mock_llm, mock_delivery):
    fund1 = FundEntity(id=str(uuid4()), legal_name="Fund 1")
    fund2 = FundEntity(id=str(uuid4()), legal_name="Fund 2")
    pm1 = PMUser(
        id=str(uuid4()),
        email="pm1@example.com",
        active=True,
        slack_user_id="U1",
        fund_entity_id=fund1.id,
    )
    pm2 = PMUser(
        id=str(uuid4()),
        email="pm2@example.com",
        active=False,
        slack_user_id="U2",
        fund_entity_id=fund2.id,
    )
    db_session.add_all([fund1, fund2, pm1, pm2])
    await db_session.commit()

    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    maker.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(MorningBriefAgent, "generate", new=AsyncMock(return_value=MagicMock())),
        patch.object(
            MorningBriefAgent,
            "save_and_deliver",
            new=AsyncMock(side_effect=lambda pm_id, brief, deliver_fn: MagicMock(id=str(uuid4()))),
        ),
        patch("axe.services.brief_scheduler.deliver_brief", new=mock_delivery),
    ):
        ids = await deliver_briefs_to_all_active_pms(
            maker, as_of=datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
        )

    assert len(ids) == 1


@pytest.mark.asyncio
async def test_reply_update_thesis_creates_version(db_session: AsyncSession, mock_llm):
    fund = FundEntity(id=str(uuid4()), legal_name="Test Fund")
    pm = PMUser(id=str(uuid4()), email="pm@example.com", active=True, fund_entity_id=fund.id)
    db_session.add_all([fund, pm])
    await db_session.flush()
    thesis = ThesisVersion(
        id=str(uuid4()),
        pm_id=pm.id,
        ticker="AAPL",
        version=1,
        fund_entity_id=fund.id,
        key_assumptions=[{"id": "a1", "text": "iPhone units grow 5%"}],
    )
    morning_brief = _make_brief(pm.id, "brief_1")
    db_session.add_all([thesis, morning_brief])
    await db_session.commit()

    mock_llm.complete.return_value = MagicMock(
        parsed={
            "intent": "update_thesis",
            "target_thesis_ticker": "AAPL",
            "target_assumption_id": "a1",
            "new_assumption_text": "iPhone units grow 8%",
            "target_signal_id": None,
            "follow_up_question": None,
            "dismiss_reason": None,
            "raw_explanation": "Updated assumption",
        }
    )

    agent = BriefReplyAgent(db_session, llm=mock_llm)
    reply = await agent.handle(pm.id, "brief_1", "Update AAPL thesis: iPhone units grow 8%")

    assert reply.intent == "update_thesis"
    result = await db_session.execute(select(ThesisVersion).where(ThesisVersion.pm_id == pm.id))
    versions = result.scalars().all()
    assert len(versions) == 2
    latest = max(versions, key=lambda v: v.version)
    assert latest.version == 2
    assert any(a.get("text") == "iPhone units grow 8%" for a in latest.key_assumptions or [])


@pytest.mark.asyncio
async def test_reply_dismiss_signal_creates_feedback(db_session: AsyncSession, mock_llm):
    fund = FundEntity(id=str(uuid4()), legal_name="Test Fund")
    pm = PMUser(id=str(uuid4()), email="pm@example.com", active=True, fund_entity_id=fund.id)
    db_session.add_all([fund, pm])
    await db_session.flush()
    signal = SignalLog(
        id="sig_123",
        pm_id=pm.id,
        ticker="AAPL",
        source_type="news",
        raw_content="noise",
        content_hash="abc123",
        idempotency_key="idemp_123",
    )
    morning_brief = _make_brief(pm.id, "brief_2")
    db_session.add_all([signal, morning_brief])
    await db_session.commit()

    mock_llm.complete.return_value = MagicMock(
        parsed={
            "intent": "dismiss_signal",
            "target_signal_id": "sig_123",
            "target_thesis_ticker": None,
            "target_assumption_id": None,
            "new_assumption_text": None,
            "follow_up_question": None,
            "dismiss_reason": "Old news",
            "raw_explanation": "Signal is stale",
        }
    )

    agent = BriefReplyAgent(db_session, llm=mock_llm)
    reply = await agent.handle(pm.id, "brief_2", "Dismiss sig_123 — old news")

    assert reply.intent == "dismiss_signal"
    result = await db_session.execute(
        select(SignalFeedback).where(SignalFeedback.signal_id == "sig_123")
    )
    fb = result.scalar_one_or_none()
    assert fb is not None
    assert fb.reaction == "dismissed"


@pytest.mark.asyncio
async def test_nyse_holiday_july_sixth():
    # 2026-12-25 is Christmas (Friday), 2026-12-28 is the following Monday.
    assert not is_nyse_trading_day(date(2026, 12, 25))
    assert is_nyse_trading_day(date(2026, 12, 28))


def test_format_brief_simple():
    brief = MorningBriefOutput(
        focus_one=FocusOne(ticker="AAPL", reason="CPU", urgency_score=0.8),
        sections=[],
        catalyst_week=[],
    )
    text = format_brief(brief)
    assert "AAPL" in text
    assert "CPU" in text


def test_cosine_similarity_range():
    # identical
    assert abs(cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-6
    # orthogonal
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-6
