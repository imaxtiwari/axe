"""Tests for cross-PM isolation of persona, citations, and peer maps."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from axe.agents.llm import MockProvider
from axe.agents.memory_miner import MemoryMinerAgent, RawMessage, build_mock_fetchers
from axe.agents.persona import PersonaAgent
from axe.db.models import FundEntity, PMPersona, PMUser
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.persona import PersonaService


async def _setup_pm(session, email_suffix: str):
    fund = FundEntity(
        id=str(uuid.uuid4()),
        legal_name=f"Test Fund {uuid.uuid4().hex[:8]}",
        data_residency="US",
    )
    session.add(fund)
    await session.flush()
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        email=f"test_{email_suffix}@example.com",
    )
    session.add(user)
    await session.flush()
    return fund, user


def _make_fetchers_for(pm_id: str):
    msg = RawMessage(
        source_type="gmail",
        source_id=f"msg-{pm_id}",
        thread_id=f"thread-{pm_id}",
        timestamp=dt.datetime.now(dt.UTC),
        participants=[f"{pm_id}@example.com", "analyst@example.com"],
        body_text=f"Bullish on ticker-{pm_id}",
        is_dm=False,
    )
    return build_mock_fetchers(gmail_messages=[msg])


@pytest.mark.asyncio
async def test_pm_persona_isolated_between_pms(db_session) -> None:
    """Persona rows for one PM must not be visible to another PM."""
    fund_a, user_a = await _setup_pm(db_session, "a")
    fund_b, user_b = await _setup_pm(db_session, "b")

    for user, fund, ticker in [(user_a, fund_a, "AAPL"), (user_b, fund_b, "TSLA")]:
        fetchers = _make_fetchers_for(user.id)
        miner_llm = MockProvider(
            responses=[
                {
                    "parsed": {
                        "citations": [
                            {
                                "source_type": "gmail",
                                "source_id": f"msg-{user.id}",
                                "snippet": f"Bullish on {ticker}",
                                "linked_ticker": ticker,
                                "sentiment": "positive",
                                "confidence": 0.9,
                                "topics": [ticker],
                            }
                        ],
                        "peers": [
                            {
                                "peer_id": f"peer-{user.id}@example.com",
                                "relationship_type": "expert",
                                "interaction_frequency": "weekly",
                                "trust_level": "high",
                                "topics": [ticker],
                            }
                        ],
                    }
                }
            ]
        )
        persona_llm = MockProvider(
            responses=[
                {
                    "parsed": {
                        "writing_style_summary": f"Style for {user.id}",
                        "decision_triggers": {},
                        "trusted_sources": [],
                        "confidence_language": "Confident",
                    }
                }
            ]
        )

        ctx = RequestContext(pm_id=user.id, fund_id=fund.id, role="pm")
        token = RequestContext.set_current(ctx)
        try:
            async with UnitOfWork(db_session) as uow:
                miner = MemoryMinerAgent(llm=miner_llm, fetchers=fetchers)
                service = PersonaService(
                    uow,
                    miner=miner,
                    persona_agent=PersonaAgent(llm=persona_llm),
                )
                await service.refresh_persona(user.id)
        finally:
            RequestContext.reset_current(token)

    ctx_a = RequestContext(pm_id=user_a.id, fund_id=fund_a.id, role="pm")
    token_a = RequestContext.set_current(ctx_a)
    try:
        async with UnitOfWork(db_session) as uow:
            persona_a = await uow.pm_personas.get_current()
            citations_a = await uow.memory_citations.list_for_pm()
            peers_a = await uow.pm_peer_maps.list_for_pm()
    finally:
        RequestContext.reset_current(token_a)

    assert persona_a is not None
    assert persona_a.pm_id == user_a.id
    assert all(c.pm_id == user_a.id for c in citations_a)
    assert all(p.pm_id == user_a.id for p in peers_a)
    assert any(c.linked_ticker == "AAPL" for c in citations_a)
    assert not any(c.linked_ticker == "TSLA" for c in citations_a)

    ctx_b = RequestContext(pm_id=user_b.id, fund_id=fund_b.id, role="pm")
    token_b = RequestContext.set_current(ctx_b)
    try:
        async with UnitOfWork(db_session) as uow:
            persona_b = await uow.pm_personas.get_current()
            citations_b = await uow.memory_citations.list_for_pm()
            peers_b = await uow.pm_peer_maps.list_for_pm()
    finally:
        RequestContext.reset_current(token_b)

    assert persona_b is not None
    assert persona_b.pm_id == user_b.id
    assert all(c.pm_id == user_b.id for c in citations_b)
    assert all(p.pm_id == user_b.id for p in peers_b)
    assert any(c.linked_ticker == "TSLA" for c in citations_b)
    assert not any(c.linked_ticker == "AAPL" for c in citations_b)


@pytest.mark.asyncio
async def test_service_get_current_enforces_pm_id_match(db_session) -> None:
    """get_current_persona must return None when the current persona belongs to a different PM."""
    fund, user = await _setup_pm(db_session, "owner")

    ctx = RequestContext(pm_id=user.id, fund_id=fund.id, role="pm")
    token = RequestContext.set_current(ctx)
    try:
        async with UnitOfWork(db_session) as uow:
            model = PMPersona(
                id=str(uuid.uuid4()),
                pm_id=user.id,
                writing_style_summary="Owner style",
                decision_triggers={},
                peer_relationships_json={"peers": []},
                trusted_sources=[],
                confidence_language="Confident",
            )
            uow.session.add(model)
            await uow.commit()
    finally:
        RequestContext.reset_current(token)

    ctx_other = RequestContext(pm_id="other-pm", fund_id=fund.id, role="pm")
    token_other = RequestContext.set_current(ctx_other)
    try:
        async with UnitOfWork(db_session) as uow:
            service = PersonaService(uow)
            snapshot = await service.get_current_persona("other-pm")
    finally:
        RequestContext.reset_current(token_other)

    assert snapshot is None
