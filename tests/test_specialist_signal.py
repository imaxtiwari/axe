"""Tests for specialist signal agents and registry."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.specialist_signal import (
    AgentContext,
    BrokerSpecialist,
    CRMSpecialist,
    EarningsSpecialist,
    ExpertNetworkSpecialist,
    PDFDeckSpecialist,
    ResearchEdgeSpecialist,
    SpecialistSignalAgent,
    SpecialistSignalOutput,
    SpecialistSignalRegistry,
    build_agent_context,
    default_registry,
    record_specialist_signals,
)
from axe.db.models import (
    FundEntity,
    PMUser,
    RawIngest,
    SpecialistSignal,
    ThesisVersion,
)
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext


def _raw_ingest(
    source_type: str,
    raw_payload: dict | None = None,
    extracted_signal: dict | None = None,
) -> RawIngest:
    return RawIngest(
        id=str(uuid.uuid4()),
        pm_id="pm_1",
        source_type=source_type,
        content_hash=uuid.uuid4().hex,
        raw_payload_json=raw_payload or {},
        extracted_signal_json=extracted_signal or {},
    )


@pytest.mark.asyncio
async def test_default_registry_has_all_source_types() -> None:
    registry = default_registry()
    expected = {
        "polygon",
        "research_edge",
        "expert_network",
        "broker_feed",
        "pdf_deck",
        "crm",
    }
    assert registry.source_types == expected


@pytest.mark.asyncio
async def test_registry_build_returns_none_for_unknown_source() -> None:
    registry = default_registry()
    assert registry.build("unknown_source") is None
    assert registry.get("unknown_source") is None


@pytest.mark.asyncio
async def test_register_and_build_custom_agent() -> None:
    class CustomSpecialist(SpecialistSignalAgent):
        source_type = "custom_source"
        specialist_name = "CustomSpecialist"

        async def process(
            self,
            raw_ingest: RawIngest,
            context: AgentContext,
        ) -> list[SpecialistSignalOutput]:
            return []

    registry = SpecialistSignalRegistry()
    registry.register(CustomSpecialist)
    agent = registry.build("custom_source")
    assert isinstance(agent, CustomSpecialist)


@pytest.mark.asyncio
async def test_earnings_specialist_extracts_signal() -> None:
    agent = EarningsSpecialist()
    ingest = _raw_ingest(
        source_type="polygon",
        extracted_signal={
            "ticker": "AAPL:US",
            "summary": "Q2 revenue beat on strong iPhone demand",
        },
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert len(outputs) == 1
    out = outputs[0]
    assert out.ticker == "AAPL"
    assert out.source_type == "polygon"
    assert out.specialist_agent == "EarningsSpecialist"
    assert out.signal_type == "earnings_update"
    assert out.stance == "CONFIRMS"
    assert out.confidence == pytest.approx(0.75)
    assert "beat" in out.summary.lower()


@pytest.mark.asyncio
async def test_earnings_specialist_returns_empty_without_summary() -> None:
    agent = EarningsSpecialist()
    ingest = _raw_ingest(
        source_type="polygon",
        raw_payload={"title": ""},
        extracted_signal={"ticker": "AAPL"},
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert outputs == []


@pytest.mark.asyncio
async def test_earnings_specialist_contradiction_keywords() -> None:
    agent = EarningsSpecialist()
    ingest = _raw_ingest(
        source_type="polygon",
        extracted_signal={
            "ticker": "TSLA",
            "summary": "Q3 revenue miss and margin decline",
        },
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert outputs[0].stance == "CONTRADICTS"
    assert outputs[0].confidence == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_research_edge_specialist_classifies_sentiment() -> None:
    agent = ResearchEdgeSpecialist()
    ingest = _raw_ingest(
        source_type="research_edge",
        raw_payload={"body": "Downgrade to sell; valuation looks stretched."},
        extracted_signal={"ticker": "MSFT", "title": "Cautious on MSFT"},
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert len(outputs) == 1
    out = outputs[0]
    assert out.ticker == "MSFT"
    assert out.stance == "CONTRADICTS"
    assert out.signal_type == "research_note"
    assert out.evidence_json["title"] == "Cautious on MSFT"


@pytest.mark.asyncio
async def test_expert_network_specialist_extracts_transcript() -> None:
    agent = ExpertNetworkSpecialist()
    ingest = _raw_ingest(
        source_type="expert_network",
        raw_payload={
            "question": "How is demand?",
            "answer": "Demand is soft and declining in North America.",
        },
        extracted_signal={"ticker": "NFLX", "provider": "ExpertCall"},
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert len(outputs) == 1
    out = outputs[0]
    assert out.ticker == "NFLX"
    assert out.source_type == "expert_network"
    assert out.stance == "CONTRADICTS"
    assert out.evidence_json["provider"] == "ExpertCall"
    assert out.evidence_json["has_turn"] is True


@pytest.mark.asyncio
async def test_broker_specialist_infer_buy_sell() -> None:
    agent = BrokerSpecialist()
    ingest = _raw_ingest(
        source_type="broker_feed",
        raw_payload={"quantity": -500, "price": 150.0, "date": "2026-08-08"},
        extracted_signal={"ticker": "AMZN"},
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert len(outputs) == 1
    out = outputs[0]
    assert out.ticker == "AMZN"
    assert out.signal_type == "position_activity"
    assert "sell" in out.summary.lower()
    assert out.evidence_json["action"] == "sell"
    assert out.evidence_json["quantity"] == -500


@pytest.mark.asyncio
async def test_pdf_deck_specialist_classifies_text() -> None:
    agent = PDFDeckSpecialist()
    ingest = _raw_ingest(
        source_type="pdf_deck",
        raw_payload={"page": 4, "byte_size": 1024, "mime_type": "application/pdf"},
        extracted_signal={
            "ticker": "PLTR",
            "text": "Traction is accelerating and growth opportunity is large",
        },
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert len(outputs) == 1
    out = outputs[0]
    assert out.ticker == "PLTR"
    assert out.stance == "CONFIRMS"
    assert out.evidence_json["page"] == 4


@pytest.mark.asyncio
async def test_crm_specialist_uses_record_type() -> None:
    agent = CRMSpecialist()
    ingest = _raw_ingest(
        source_type="crm",
        raw_payload={
            "subject": "New deal expansion",
            "description": "Customer signed a renewal and expanded seats.",
            "record_type": "opportunity",
        },
        extracted_signal={"ticker": "CRM", "record_type": "opportunity"},
    )
    outputs = await agent.process(ingest, AgentContext(pm_id="pm_1"))
    assert len(outputs) == 1
    out = outputs[0]
    assert out.ticker == "CRM"
    assert out.signal_type == "crm_opportunity"
    assert out.stance == "CONFIRMS"


@pytest.mark.asyncio
async def test_output_stance_and_confidence_normalization() -> None:
    out = SpecialistSignalOutput(
        source_type="x",
        specialist_agent="TestAgent",
        signal_type="test",
        summary="test",
        stance="contradicts",
        confidence=1.5,
    )
    assert out.stance == "CONTRADICTS"
    assert out.confidence == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_build_agent_context_from_thesis_versions() -> None:
    thesis = ThesisVersion(
        id=str(uuid.uuid4()),
        pm_id="pm_1",
        ticker="GOOGL",
        version=1,
        direction="long",
        key_assumptions=[{"id": "a1", "statement": "Search revenue grows"}],
    )
    ctx = build_agent_context(
        pm_id="pm_1",
        fund_id="fund_1",
        persona={"decision_triggers": {"priorities": ["AI revenue"]}},
        active_tickers=["GOOGL"],
        recent_theses=[thesis],
    )
    assert ctx.pm_id == "pm_1"
    assert ctx.fund_id == "fund_1"
    assert "GOOGL" in ctx.active_tickers
    assert ctx.recent_theses[0]["ticker"] == "GOOGL"


@pytest.mark.asyncio
async def test_ticker_is_active_logic() -> None:
    ctx_empty = AgentContext(pm_id="pm_1")
    ctx_active = AgentContext(pm_id="pm_1", active_tickers={"AAPL"})
    assert ctx_empty.ticker_is_active("AAPL") is True
    assert ctx_active.ticker_is_active("AAPL") is True
    assert ctx_active.ticker_is_active("TSLA") is False
    assert ctx_active.ticker_is_active(None) is False


async def _seed_pm(session: AsyncSession) -> tuple[str, str]:
    fund_id = str(uuid.uuid4())
    pm_id = str(uuid.uuid4())
    fund = FundEntity(id=fund_id, legal_name="Specialist Test Fund")
    pm = PMUser(
        id=pm_id,
        fund_entity_id=fund_id,
        email=f"pm-{pm_id[:8]}@example.com",
    )
    session.add_all([fund, pm])
    await session.flush()
    return fund_id, pm_id


@pytest.mark.asyncio
async def test_record_specialist_signals_persists_rows(db_session: AsyncSession) -> None:
    fund_id, pm_id = await _seed_pm(db_session)
    raw_ingest_id = str(uuid.uuid4())
    raw = RawIngest(
        id=raw_ingest_id,
        pm_id=pm_id,
        source_type="polygon",
        content_hash=uuid.uuid4().hex,
    )
    db_session.add(raw)
    await db_session.flush()

    uow = UnitOfWork(db_session)
    with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
        outputs = [
            SpecialistSignalOutput(
                ticker="AAPL",
                source_type="polygon",
                specialist_agent="EarningsSpecialist",
                signal_type="earnings_update",
                summary="Revenue beat",
                stance="CONFIRMS",
                confidence=0.8,
            )
        ]
        created = record_specialist_signals(uow, raw_ingest_id, pm_id, outputs)
        await uow.commit()

    assert len(created) == 1
    assert created[0].ticker == "AAPL"

    result = await db_session.execute(
        select(SpecialistSignal).where(SpecialistSignal.pm_id == pm_id)
    )
    rows = list(result.scalars().all())
    assert len(rows) == 1
    assert rows[0].raw_ingest_id == raw_ingest_id


@pytest.mark.asyncio
async def test_record_specialist_signals_empty_list(db_session: AsyncSession) -> None:
    fund_id, pm_id = await _seed_pm(db_session)
    raw_ingest_id = str(uuid.uuid4())
    raw = RawIngest(
        id=raw_ingest_id,
        pm_id=pm_id,
        source_type="polygon",
        content_hash=uuid.uuid4().hex,
    )
    db_session.add(raw)
    await db_session.flush()

    uow = UnitOfWork(db_session)
    with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
        created = record_specialist_signals(uow, raw_ingest_id, pm_id, [])
        await uow.commit()

    assert created == []


@pytest.mark.asyncio
async def test_normalize_ticker_strips_equity_suffixes() -> None:
    agent = EarningsSpecialist()
    assert agent._normalize_ticker("AAPL:US") == "AAPL"
    assert agent._normalize_ticker("MSFT US Equity") == "MSFT"
    assert agent._normalize_ticker("TSLA-US") == "TSLA"
    assert agent._normalize_ticker("  googl.us ") == "GOOGL"
    assert agent._normalize_ticker(None) is None
    assert agent._normalize_ticker("   ") is None
