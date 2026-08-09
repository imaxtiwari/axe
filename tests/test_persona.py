"""Tests for persona synthesis and service persistence."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from axe.agents.llm import MockProvider
from axe.agents.memory_miner import (
    MemoryMinerAgent,
    MinedCitation,
    MinedPeer,
    RawMessage,
    build_mock_fetchers,
)
from axe.agents.persona import PersonaAgent
from axe.agents.persona_models import PersonaStyleSnapshot
from axe.db.models import FundEntity, MemoryCitation, PMPeerMap, PMPersona, PMUser
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.persona import PersonaService


async def _setup_pm(session):
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
        email=f"test_{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    await session.flush()
    return fund, user


@pytest.mark.asyncio
async def test_persona_agent_synthesizes_snapshot(db_session) -> None:
    """PersonaAgent turns citations and peers into a PersonaStyleSnapshot."""
    citations = [
        MinedCitation(
            source_type="gmail",
            source_id="msg-1",
            snippet="Bullish on AAPL services",
            linked_ticker="AAPL",
            sentiment="positive",
            confidence=0.9,
            topics=["AAPL"],
        )
    ]
    peers = [
        MinedPeer(
            peer_id="analyst@example.com",
            peer_name="Trusted Analyst",
            relationship_type="expert",
            interaction_frequency="weekly",
            trust_level="high",
            topics=["AAPL"],
        )
    ]
    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "writing_style_summary": "Concise and data-driven.",
                    "decision_triggers": {"earnings surprise": "Revisits position sizing"},
                    "trusted_sources": ["sell-side research", "channel checks"],
                    "confidence_language": "Uses 'high conviction' sparingly.",
                }
            }
        ]
    )
    agent = PersonaAgent(llm=provider)
    snapshot = await agent.synthesize("pm-1", citations, peers)

    assert snapshot.pm_id == "pm-1"
    assert snapshot.writing_style_summary == "Concise and data-driven."
    assert snapshot.decision_triggers == {"earnings surprise": "Revisits position sizing"}
    assert snapshot.trusted_sources == ["sell-side research", "channel checks"]
    assert snapshot.confidence_language == "Uses 'high conviction' sparingly."
    assert len(snapshot.peer_relationships) == 1
    assert snapshot.peer_relationships[0].peer_name == "Trusted Analyst"


@pytest.mark.asyncio
async def test_persona_agent_snapshot_from_model_roundtrip(db_session) -> None:
    """snapshot_from_model and model_from_snapshot are inverse-ish."""
    model = PMPersona(
        id=str(uuid.uuid4()),
        pm_id="pm-1",
        writing_style_summary="Test style",
        decision_triggers={"trigger": "description"},
        peer_relationships_json={
            "peers": [
                {
                    "peer_id": "peer@example.com",
                    "peer_name": "Peer",
                    "relationship_type": "colleague",
                    "topics": ["AAPL"],
                    "trust_level": "medium",
                }
            ]
        },
        trusted_sources=["source"],
        confidence_language="Confident",
    )
    snapshot = PersonaAgent.snapshot_from_model(model)
    assert snapshot.pm_id == "pm-1"
    assert snapshot.writing_style_summary == "Test style"
    assert snapshot.peer_relationships[0].peer_id == "peer@example.com"

    new_model = PersonaAgent.model_from_snapshot(snapshot, PMPersona)
    assert new_model.pm_id == "pm-1"
    assert new_model.decision_triggers == {"trigger": "description"}
    peers_json = new_model.peer_relationships_json.get("peers", [])
    assert len(peers_json) == 1
    assert peers_json[0]["peer_id"] == "peer@example.com"


@pytest.mark.asyncio
async def test_persona_service_refresh_persists_citations_and_peers(db_session) -> None:
    """PersonaService refresh persists persona, citations, and peer maps."""
    fund, user = await _setup_pm(db_session)

    msg = RawMessage(
        source_type="gmail",
        source_id="msg-1",
        thread_id="thread-1",
        timestamp=dt.datetime.now(dt.UTC),
        participants=["pm@example.com", "analyst@example.com"],
        body_text="Bullish on AAPL",
        is_dm=False,
    )
    fetchers = build_mock_fetchers(gmail_messages=[msg])
    miner_llm = MockProvider(
        responses=[
            {
                "parsed": {
                    "citations": [
                        {
                            "source_type": "gmail",
                            "source_id": "msg-1",
                            "snippet": "Bullish on AAPL",
                            "linked_ticker": "AAPL",
                            "sentiment": "positive",
                            "confidence": 0.9,
                            "topics": ["AAPL"],
                        }
                    ],
                    "peers": [
                        {
                            "peer_id": "analyst@example.com",
                            "peer_name": "Trusted Analyst",
                            "relationship_type": "expert",
                            "interaction_frequency": "weekly",
                            "trust_level": "high",
                            "topics": ["AAPL"],
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
                    "writing_style_summary": "Data-driven.",
                    "decision_triggers": {},
                    "trusted_sources": [],
                    "confidence_language": "Confident",
                }
            }
        ]
    )

    async with UnitOfWork(db_session) as uow:
        ctx = RequestContext(pm_id=user.id, fund_id=fund.id, role="pm")
        token = RequestContext.set_current(ctx)
        try:
            miner = MemoryMinerAgent(llm=miner_llm, fetchers=fetchers)
            service = PersonaService(
                uow,
                miner=miner,
                persona_agent=PersonaAgent(llm=persona_llm),
            )
            snapshot = await service.refresh_persona(user.id)
        finally:
            RequestContext.reset_current(token)

    assert snapshot.pm_id == user.id
    assert snapshot.writing_style_summary == "Data-driven."

    result = await db_session.execute(select(PMPersona).where(PMPersona.pm_id == user.id))
    personas = list(result.scalars().all())
    assert len(personas) == 1

    result = await db_session.execute(select(MemoryCitation).where(MemoryCitation.pm_id == user.id))
    citations = list(result.scalars().all())
    assert len(citations) == 1
    assert citations[0].linked_ticker == "AAPL"

    result = await db_session.execute(select(PMPeerMap).where(PMPeerMap.pm_id == user.id))
    peers = list(result.scalars().all())
    assert len(peers) == 1
    assert peers[0].peer_email_or_slack_id == "analyst@example.com"


@pytest.mark.asyncio
async def test_persona_service_get_current_returns_none_when_missing(db_session) -> None:
    """get_current_persona returns None if no persona exists."""
    async with UnitOfWork(db_session) as uow:
        ctx = RequestContext(pm_id="pm-none", fund_id="fund-1", role="pm")
        token = RequestContext.set_current(ctx)
        try:
            service = PersonaService(uow)
            snapshot = await service.get_current_persona("pm-none")
        finally:
            RequestContext.reset_current(token)
    assert snapshot is None


@pytest.mark.asyncio
async def test_delete_persona_and_mined_data(db_session) -> None:
    """Deleting a persona removes persona, citations, and peers for the PM."""
    fund, user = await _setup_pm(db_session)

    msg = RawMessage(
        source_type="gmail",
        source_id="msg-1",
        thread_id="thread-1",
        timestamp=dt.datetime.now(dt.UTC),
        participants=["pm@example.com", "analyst@example.com"],
        body_text="Bullish on AAPL",
        is_dm=False,
    )
    fetchers = build_mock_fetchers(gmail_messages=[msg])
    miner_llm = MockProvider(
        responses=[
            {
                "parsed": {
                    "citations": [
                        {
                            "source_type": "gmail",
                            "source_id": "msg-1",
                            "snippet": "Bullish on AAPL",
                            "linked_ticker": "AAPL",
                            "sentiment": "positive",
                            "confidence": 0.9,
                            "topics": ["AAPL"],
                        }
                    ],
                    "peers": [
                        {
                            "peer_id": "analyst@example.com",
                            "peer_name": "Trusted Analyst",
                            "relationship_type": "expert",
                            "interaction_frequency": "weekly",
                            "trust_level": "high",
                            "topics": ["AAPL"],
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
                    "writing_style_summary": "Data-driven.",
                    "decision_triggers": {},
                    "trusted_sources": [],
                    "confidence_language": "Confident",
                }
            }
        ]
    )

    async with UnitOfWork(db_session) as uow:
        ctx = RequestContext(pm_id=user.id, fund_id=fund.id, role="pm")
        token = RequestContext.set_current(ctx)
        try:
            miner = MemoryMinerAgent(llm=miner_llm, fetchers=fetchers)
            service = PersonaService(
                uow,
                miner=miner,
                persona_agent=PersonaAgent(llm=persona_llm),
            )
            await service.refresh_persona(user.id)
        finally:
            RequestContext.reset_current(token)

    async with UnitOfWork(db_session) as uow:
        ctx = RequestContext(pm_id=user.id, fund_id=fund.id, role="pm")
        token = RequestContext.set_current(ctx)
        try:
            deleted_count = await service.delete_persona_and_mined_data(user.id)
        finally:
            RequestContext.reset_current(token)

    assert deleted_count == 3  # persona + 1 citation + 1 peer

    result = await db_session.execute(select(PMPersona).where(PMPersona.pm_id == user.id))
    assert list(result.scalars().all()) == []
    result = await db_session.execute(select(MemoryCitation).where(MemoryCitation.pm_id == user.id))
    assert list(result.scalars().all()) == []
    result = await db_session.execute(select(PMPeerMap).where(PMPeerMap.pm_id == user.id))
    assert list(result.scalars().all()) == []


@pytest.mark.asyncio
async def test_render_system_prompt_snippet_omits_empty_fields() -> None:
    """render_system_prompt_snippet only includes populated fields."""
    snapshot = PersonaStyleSnapshot(
        persona_id="p1",
        pm_id="pm-1",
        writing_style_summary="Concise.",
    )
    snippet = snapshot.render_system_prompt_snippet()
    assert "Writing style: Concise." in snippet
    assert "Decision triggers" not in snippet
    assert "Trusted sources" not in snippet
