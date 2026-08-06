"""Schema tests for Sprint 0 foundation tables and columns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from axe.db.models import (
    ArtifactAction,
    AuditLog,
    ComplianceEscalation,
    ConnectorConfig,
    DecisionPrompt,
    FundEntity,
    MemoryCitation,
    ModelTrace,
    MorningBrief,
    PMPeerMap,
    PMPersona,
    PMUser,
    PolicyRule,
    RawIngest,
    SignalLog,
    SpecialistSignal,
    ThesisVersion,
)


async def _fund_entity(session):
    fund = FundEntity(id=str(uuid.uuid4()), legal_name=f"Fund {uuid.uuid4().hex[:8]}")
    session.add(fund)
    await session.flush()
    return fund


async def _pm_user(session, fund_id: str):
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund_id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_sprint0_extension_columns_exist(db_session):
    """New extension columns are present on existing tables."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="email",
        content_hash="hash",
        source_id="src-1",
        specialist_signal_id=str(uuid.uuid4()),
        parent_signal_id=str(uuid.uuid4()),
        chain_id="chain-1",
    )
    db_session.add(signal)
    await db_session.flush()
    assert signal.source_id == "src-1"
    assert signal.chain_id == "chain-1"

    tv = ThesisVersion(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        version=1,
        fund_entity_id=fund.id,
        pm_persona_snapshot_id=str(uuid.uuid4()),
    )
    db_session.add(tv)
    await db_session.flush()
    assert tv.pm_persona_snapshot_id is not None

    brief = MorningBrief(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        date=datetime.now(UTC).date(),
        decision_prompts_json=[{"text": "buy?"}],
        actions_json=[{"type": "alert"}],
        citation_links_json=[{"url": "http://x"}],
    )
    db_session.add(brief)
    await db_session.flush()
    assert brief.actions_json == [{"type": "alert"}]

    audit = AuditLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        action_type="test",
        object_type="signal_log",
        trace_id=str(uuid.uuid4()),
    )
    db_session.add(audit)
    await db_session.flush()
    assert audit.trace_id is not None


@pytest.mark.asyncio
async def test_connector_config_round_trip(db_session):
    """Connector configs store encrypted credentials per PM/source."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    config = ConnectorConfig(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="gmail",
        credentials_encrypted={"token": "secret"},
        enabled=True,
    )
    db_session.add(config)
    await db_session.flush()

    loaded = await db_session.get(ConnectorConfig, config.id)
    assert loaded is not None
    assert loaded.credentials_encrypted == {"token": "secret"}
    assert loaded.enabled is True


@pytest.mark.asyncio
async def test_raw_ingest_round_trip(db_session):
    """Raw ingestion records carry content hash and extracted signal JSON."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    ingest = RawIngest(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="pdf_deck",
        content_hash="sha256:abc",
        dedup_key="deck/123",
        raw_payload_json={"url": "s3://bucket/deck.pdf"},
        extracted_signal_json={"ticker": "TSLA"},
        status="processed",
    )
    db_session.add(ingest)
    await db_session.flush()

    loaded = await db_session.get(RawIngest, ingest.id)
    assert loaded is not None
    assert loaded.raw_payload_json["url"].endswith(".pdf")


@pytest.mark.asyncio
async def test_pm_persona_uniqueness(db_session):
    """Only one persona row per PM."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    persona = PMPersona(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        writing_style_summary="concise",
        decision_triggers={"cut_losses": -0.1},
    )
    db_session.add(persona)
    await db_session.flush()

    duplicate = PMPersona(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        writing_style_summary="verbose",
    )
    db_session.add(duplicate)
    with pytest.raises(Exception):  # noqa: B017
        await db_session.commit()


@pytest.mark.asyncio
async def test_memory_citation_round_trip(db_session):
    """Memory citations link snippets to tickers/deals."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    citation = MemoryCitation(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="slack",
        source_id="thread-1",
        snippet="trimming position",
        linked_ticker="NVDA",
        sentiment="bearish",
    )
    db_session.add(citation)
    await db_session.flush()

    loaded = await db_session.get(MemoryCitation, citation.id)
    assert loaded is not None
    assert loaded.linked_ticker == "NVDA"


@pytest.mark.asyncio
async def test_pm_peer_map_uniqueness(db_session):
    """Peer map is unique per PM + peer identifier."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    peer = PMPeerMap(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        peer_email_or_slack_id="peer@example.com",
        relationship_type="colleague",
    )
    db_session.add(peer)
    await db_session.flush()

    duplicate = PMPeerMap(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        peer_email_or_slack_id="peer@example.com",
    )
    db_session.add(duplicate)
    with pytest.raises(Exception):  # noqa: B017
        await db_session.commit()


@pytest.mark.asyncio
async def test_specialist_signal_round_trip(db_session):
    """Specialist signals attach to raw ingestion and carry evidence."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    signal = SpecialistSignal(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        raw_ingest_id=str(uuid.uuid4()),
        source_type="transcript",
        specialist_agent="earnings_specialist",
        signal_type="guidance_cut",
        summary="Q3 guide down",
        confidence=0.85,
        evidence_json={"quote": "we see softness"},
        assumptions_touched=["a1"],
    )
    db_session.add(signal)
    await db_session.flush()

    loaded = await db_session.get(SpecialistSignal, signal.id)
    assert loaded is not None
    assert loaded.specialist_agent == "earnings_specialist"


@pytest.mark.asyncio
async def test_artifact_action_round_trip(db_session):
    """Artifact actions track generated actions and execution status."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    action = ArtifactAction(
        id=str(uuid.uuid4()),
        artifact_type="morning_brief",
        artifact_id=str(uuid.uuid4()),
        pm_id=user.id,
        action_type="update_thesis",
        payload={"ticker": "META"},
        status="pending",
    )
    db_session.add(action)
    await db_session.flush()

    loaded = await db_session.get(ArtifactAction, action.id)
    assert loaded is not None
    assert loaded.status == "pending"


@pytest.mark.asyncio
async def test_decision_prompt_round_trip(db_session):
    """Decision prompts capture options and resolution state."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    prompt = DecisionPrompt(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        artifact_id=str(uuid.uuid4()),
        prompt_text="Increase position?",
        options_json=[{"label": "Yes"}, {"label": "No"}],
    )
    db_session.add(prompt)
    await db_session.flush()

    loaded = await db_session.get(DecisionPrompt, prompt.id)
    assert loaded is not None
    assert loaded.resolved_at is None


@pytest.mark.asyncio
async def test_model_trace_round_trip(db_session):
    """Model trace stores prompt hash, latency, token usage, and review status."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    trace = ModelTrace(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        agent="drift_detect",
        prompt_hash="sha256:deadbeef",
        model="gpt-4o-mini",
        response_schema="DriftOutput",
        latency_ms=123,
        token_usage={"total_tokens": 42},
        citations_json=[{"id": "c1"}],
    )
    db_session.add(trace)
    await db_session.flush()

    loaded = await db_session.get(ModelTrace, trace.id)
    assert loaded is not None
    assert loaded.human_review_status == "not_required"
    assert loaded.response_schema == "DriftOutput"


@pytest.mark.asyncio
async def test_policy_rule_round_trip(db_session):
    """Policy rules are fund-scoped and ordered by priority."""
    fund = await _fund_entity(db_session)

    rule = PolicyRule(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        rule_type="max_position_size",
        scope="fund",
        conditions_json={"max_pct": 10},
        action="block",
        priority=5,
        enabled=True,
    )
    db_session.add(rule)
    await db_session.flush()

    loaded = await db_session.get(PolicyRule, rule.id)
    assert loaded is not None
    assert loaded.action == "block"


@pytest.mark.asyncio
async def test_compliance_escalation_round_trip(db_session):
    """Compliance escalations track severity, status, and reviewer."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    escalation = ComplianceEscalation(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        fund_entity_id=fund.id,
        trigger_type="hallucination_review",
        severity="high",
        status="open",
    )
    db_session.add(escalation)
    await db_session.flush()

    loaded = await db_session.get(ComplianceEscalation, escalation.id)
    assert loaded is not None
    assert loaded.status == "open"
