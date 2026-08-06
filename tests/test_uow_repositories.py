"""Unit tests for Sprint 0 UnitOfWork repositories."""

from __future__ import annotations

import uuid

import pytest

from axe.db.models import (
    FundEntity,
    PMUser,
)
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext


async def _fund_and_pm(uow: UnitOfWork):
    fund = FundEntity(id=str(uuid.uuid4()), legal_name=f"Fund {uuid.uuid4().hex[:8]}")
    uow.session.add(fund)
    await uow.session.flush()
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
    )
    uow.session.add(user)
    await uow.session.flush()
    return fund, user


@pytest.mark.asyncio
async def test_connector_config_repository(db_session):
    """ConnectorConfigRepository creates and retrieves config rows."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="polygon",
            credentials_encrypted={"api_key": "secret"},
            enabled=True,
        )
        await uow.commit()

        loaded = await uow.connector_configs.get_by_id(created.id)
        assert loaded is not None
        assert loaded.source_type == "polygon"

        by_source = await uow.connector_configs.get_by_source("polygon")
        assert by_source is not None
        assert by_source.id == created.id
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_raw_ingest_repository(db_session):
    """RawIngestRepository creates and finds rows by content hash."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.raw_ingests.create_ingest(
            pm_id=user.id,
            source_type="pdf_deck",
            content_hash="sha256:abc",
            raw_payload_json={"path": "/deck.pdf"},
        )
        await uow.commit()

        loaded = await uow.raw_ingests.get_by_id(created.id)
        assert loaded is not None
        assert loaded.source_type == "pdf_deck"

        by_hash = await uow.raw_ingests.get_by_content_hash("sha256:abc")
        assert by_hash is not None
        assert by_hash.id == created.id

        rows = await uow.raw_ingests.list_for_pm(limit=10)
        assert any(r.id == created.id for r in rows)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_pm_persona_repository(db_session):
    """PMPersonaRepository returns current persona for the active PM."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.pm_personas.create_persona(
            pm_id=user.id,
            writing_style_summary="terse",
        )
        await uow.commit()

        loaded = await uow.pm_personas.get_by_id(created.id)
        assert loaded is not None
        assert loaded.writing_style_summary == "terse"

        current = await uow.pm_personas.get_current()
        assert current is not None
        assert current.id == created.id
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_memory_citation_repository(db_session):
    """MemoryCitationRepository supports ticker-scoped listing."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.memory_citations.create_citation(
            pm_id=user.id,
            source_type="gmail",
            snippet="earnings look soft",
            linked_ticker="AAPL",
        )
        await uow.commit()

        loaded = await uow.memory_citations.get_by_id(created.id)
        assert loaded is not None

        rows = await uow.memory_citations.list_by_ticker("AAPL")
        assert any(r.id == created.id for r in rows)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_pm_peer_map_repository(db_session):
    """PMPeerMapRepository finds peers by identifier."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.pm_peer_maps.create_peer(
            pm_id=user.id,
            peer_email_or_slack_id="peer@example.com",
            peer_name="Peer One",
        )
        await uow.commit()

        loaded = await uow.pm_peer_maps.get_by_id(created.id)
        assert loaded is not None

        by_peer = await uow.pm_peer_maps.get_by_peer_id("peer@example.com")
        assert by_peer is not None
        assert by_peer.id == created.id
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_specialist_signal_repository(db_session):
    """SpecialistSignalRepository lists by raw_ingest_id."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    raw_id = str(uuid.uuid4())
    try:
        created = uow.specialist_signals.create_signal(
            pm_id=user.id,
            raw_ingest_id=raw_id,
            source_type="transcript",
            specialist_agent="earnings",
            signal_type="guidance_cut",
        )
        await uow.commit()

        loaded = await uow.specialist_signals.get_by_id(created.id)
        assert loaded is not None

        rows = await uow.specialist_signals.list_by_raw_ingest(raw_id)
        assert any(r.id == created.id for r in rows)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_artifact_action_repository(db_session):
    """ArtifactActionRepository lists actions for an artifact."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    artifact_id = str(uuid.uuid4())
    try:
        created = uow.artifact_actions.create_action(
            artifact_type="morning_brief",
            artifact_id=artifact_id,
            pm_id=user.id,
            action_type="update_thesis",
        )
        await uow.commit()

        loaded = await uow.artifact_actions.get_by_id(created.id)
        assert loaded is not None

        rows = await uow.artifact_actions.list_for_artifact("morning_brief", artifact_id)
        assert any(r.id == created.id for r in rows)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_decision_prompt_repository(db_session):
    """DecisionPromptRepository lists unresolved prompts."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.decision_prompts.create_prompt(
            pm_id=user.id,
            prompt_text="Trim position?",
        )
        await uow.commit()

        loaded = await uow.decision_prompts.get_by_id(created.id)
        assert loaded is not None

        rows = await uow.decision_prompts.list_open()
        assert any(r.id == created.id for r in rows)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_model_trace_repository(db_session):
    """ModelTraceRepository supports get by id and prompt hash."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.model_traces.create_trace(
            id=str(uuid.uuid4()),
            pm_id=user.id,
            agent="test",
            prompt_hash="sha256:abc",
            model="mock",
        )
        await uow.commit()

        loaded = await uow.model_traces.get_by_id(created.id)
        assert loaded is not None
        assert loaded.prompt_hash == "sha256:abc"

        by_hash = await uow.model_traces.get_by_prompt_hash("sha256:abc")
        assert any(r.id == created.id for r in by_hash)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_policy_rule_repository(db_session):
    """PolicyRuleRepository is scoped by fund and supports enabled listing."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.policy_rules.create_rule(
            fund_entity_id=fund.id,
            rule_type="mnpi",
            scope="fund",
            action="escalate",
            enabled=True,
        )
        await uow.commit()

        loaded = await uow.policy_rules.get_by_id(created.id)
        assert loaded is not None

        rows = await uow.policy_rules.list_enabled()
        assert any(r.id == created.id for r in rows)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_compliance_escalation_repository(db_session):
    """ComplianceEscalationRepository lists open escalations for the fund."""
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        created = uow.compliance_escalations.create_escalation(
            pm_id=user.id,
            fund_entity_id=fund.id,
            trigger_type="policy_violation",
            severity="high",
        )
        await uow.commit()

        loaded = await uow.compliance_escalations.get_by_id(created.id)
        assert loaded is not None

        rows = await uow.compliance_escalations.list_open_for_fund()
        assert any(r.id == created.id for r in rows)
    finally:
        RequestContext.reset_current(token)
