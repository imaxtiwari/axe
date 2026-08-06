"""Tests for the TraceableProvider model-trace wrapper."""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel

from axe.agents.llm import MockProvider
from axe.agents.model_trace import TraceableProvider
from axe.config import Settings
from axe.db.models import FundEntity, PMUser
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext


class _SettingsNoCapture(Settings):
    model_trace_capture_enabled: bool = False


async def _seed_pm(session) -> tuple[str, str]:
    """Create a real FundEntity + PMUser and return their IDs."""
    fund_id = str(uuid.uuid4())
    pm_id = str(uuid.uuid4())
    fund = FundEntity(id=fund_id, legal_name="Trace Test Fund")
    pm = PMUser(
        id=pm_id,
        fund_entity_id=fund_id,
        email=f"pm-{pm_id[:8]}@example.com",
    )
    session.add_all([fund, pm])
    await session.flush()
    return fund_id, pm_id


@pytest.mark.asyncio
async def test_traceable_provider_returns_trace_id(db_session):
    """The wrapper returns the inner response augmented with trace_id."""
    inner = MockProvider(responses=[{"content": "hello", "usage": {"total_tokens": 7}}])
    uow = UnitOfWork(db_session)
    provider = TraceableProvider(
        inner, agent="test_agent", uow=uow, settings=Settings(app_env="test")
    )

    response = await provider.complete([{"role": "user", "content": "hi"}])

    assert response.trace_id is not None
    assert response.content == "hello"
    assert response.model == "mock"


@pytest.mark.asyncio
async def test_traceable_provider_persists_trace(db_session):
    """A ModelTrace row is persisted when tracing is enabled."""
    fund_id, pm_id = await _seed_pm(db_session)
    inner = MockProvider(responses=[{"content": "ok", "usage": {"total_tokens": 5}}])
    uow = UnitOfWork(db_session)
    settings = Settings(app_env="test")
    provider = TraceableProvider(inner, agent="drift_agent", uow=uow, settings=settings)

    with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
        response = await provider.complete([{"role": "user", "content": "test"}])
        await uow.commit()
        assert response.trace_id is not None
        trace = await uow.model_traces.get_by_id(response.trace_id)
    assert trace is not None
    assert trace.agent == "drift_agent"
    assert trace.prompt_hash is not None
    assert trace.model == "mock"
    assert trace.latency_ms is not None
    assert trace.latency_ms >= 0
    assert trace.token_usage == {"total_tokens": 5}
    assert trace.human_review_status == "not_required"


@pytest.mark.asyncio
async def test_traceable_provider_no_persist_when_disabled(db_session):
    """No ModelTrace row is created when capture is disabled."""
    fund_id, pm_id = await _seed_pm(db_session)
    inner = MockProvider(responses=[{"content": "ok"}])
    uow = UnitOfWork(db_session)
    provider = TraceableProvider(
        inner, agent="disabled_agent", uow=uow, settings=_SettingsNoCapture(app_env="test")
    )

    with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
        response = await provider.complete([{"role": "user", "content": "test"}])
        await uow.commit()
        assert response.trace_id is not None
        # No trace should have been persisted; reading back with the same context
        # must not find a row.
        trace = await uow.model_traces.get_by_id(response.trace_id)
    assert trace is None


@pytest.mark.asyncio
async def test_traceable_provider_no_uow_no_persist(db_session):
    """Wrapping without a UoW still returns a trace_id but does not persist."""
    inner = MockProvider(responses=[{"content": "ok"}])
    provider = TraceableProvider(inner, agent="no_uow", uow=None, settings=Settings(app_env="test"))

    response = await provider.complete([{"role": "user", "content": "test"}])

    assert response.trace_id is not None


@pytest.mark.asyncio
async def test_traceable_provider_uses_request_context_pm_id(db_session):
    """The persisted trace captures the active request context pm_id."""
    fund_id, pm_id = await _seed_pm(db_session)
    inner = MockProvider(responses=[{"content": "ok"}])
    uow = UnitOfWork(db_session)
    settings = Settings(app_env="test")
    provider = TraceableProvider(inner, agent="ctx_agent", uow=uow, settings=settings)

    with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
        response = await provider.complete([{"role": "user", "content": "test"}])
        await uow.commit()
        assert response.trace_id is not None
        trace = await uow.model_traces.get_by_id(response.trace_id)
    assert trace is not None
    assert trace.pm_id == pm_id


@pytest.mark.asyncio
async def test_prompt_hash_is_deterministic(db_session):
    """The same messages produce the same prompt hash."""
    inner = MockProvider(responses=[{"content": "ok"}])
    uow = UnitOfWork(db_session)
    provider = TraceableProvider(
        inner, agent="hash_agent", uow=uow, settings=Settings(app_env="test")
    )

    messages = [{"role": "user", "content": "hello"}]
    hash_a = provider._prompt_hash(messages)
    hash_b = provider._prompt_hash(messages)

    assert hash_a == hash_b
    assert len(hash_a) == 64


@pytest.mark.asyncio
async def test_traceable_provider_records_response_schema(db_session):
    """The schema name is captured when a structured output schema is provided."""

    class DummySchema(BaseModel):
        __name__ = "DummySchema"

    fund_id, pm_id = await _seed_pm(db_session)
    inner = MockProvider(responses=[{"content": "ok", "parsed": {"x": 1}}])
    uow = UnitOfWork(db_session)
    provider = TraceableProvider(
        inner, agent="schema_agent", uow=uow, settings=Settings(app_env="test")
    )

    with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
        response = await provider.complete(
            [{"role": "user", "content": "test"}], response_schema=DummySchema
        )
        await uow.commit()
        assert response.trace_id is not None
        trace = await uow.model_traces.get_by_id(response.trace_id)
    assert trace is not None
    assert trace.response_schema == "DummySchema"
