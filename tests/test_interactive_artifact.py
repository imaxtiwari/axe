"""Tests for the interactive artifact agent, service, and router."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.interactive_artifact import InteractiveArtifactAgent
from axe.db.models import (
    DeckOutput,
    FundEntity,
    InvestmentVehicle,
    LPUpdate,
    MorningBrief,
    PMUser,
)
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.interactive import ActionExecutionError, InteractiveArtifactService


async def _setup_pm(session: AsyncSession) -> tuple[FundEntity, PMUser]:
    fund = FundEntity(id="fund-interactive", legal_name="Interactive Fund")
    session.add(fund)
    await session.flush()
    user = PMUser(
        id="pm-interactive",
        fund_entity_id=fund.id,
        email="interactive@example.com",
    )
    session.add(user)
    await session.flush()
    return fund, user


@pytest.mark.asyncio
async def test_generate_morning_brief_actions_and_prompt(db_session: AsyncSession):
    """Actions and a decision prompt are generated for a morning brief with Focus One."""
    _, user = await _setup_pm(db_session)
    brief = MorningBrief(
        id="brief-1",
        pm_id=user.id,
        date=dt.date(2026, 1, 1),
        sections=[],
        focus_one={"ticker": "AAPL", "reason": "Earnings beat expectation", "urgency_score": 0.9},
        catalyst_week=[],
    )
    db_session.add(brief)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        agent = InteractiveArtifactAgent(db_session)
        action_plan = await agent.generate_actions("morning_brief", brief.id, user.id)
        prompt_plan = await agent.generate_decision_prompt("morning_brief", brief.id, user.id)
    finally:
        RequestContext.reset_current(ctx_token)

    assert action_plan.artifact_id == brief.id
    action_types = {a.action_type for a in action_plan.actions}
    assert "focus_one_buy_more" in action_types
    assert "focus_one_trim" in action_types
    assert "schedule_call" in action_types

    assert len(prompt_plan.prompts) == 1
    prompt = prompt_plan.prompts[0]
    assert "AAPL" in prompt.prompt_text
    assert any("Buy more" in opt for opt in prompt.options)
    assert any("Trim" in opt for opt in prompt.options)


@pytest.mark.asyncio
async def test_service_creates_and_executes_action(db_session: AsyncSession):
    """InteractiveArtifactService persists actions and can execute them."""
    _, user = await _setup_pm(db_session)
    brief = MorningBrief(
        id="brief-2",
        pm_id=user.id,
        date=dt.date(2026, 1, 1),
        sections=[],
        focus_one={"ticker": "TSLA", "reason": "Short report", "urgency_score": 0.85},
        catalyst_week=[],
    )
    db_session.add(brief)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            actions = await service.create_actions("morning_brief", brief.id)
            assert len(actions) == 3

        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            buy_action = next(a for a in actions if a.action_type == "focus_one_buy_more")
            executed = await service.execute_action(
                buy_action.id,
                pm_id=user.id,
                payload={"quantity": 100},
            )
    finally:
        RequestContext.reset_current(ctx_token)

    assert executed.status == "executed"
    assert executed.payload.get("draft_only") is True
    assert executed.payload.get("quantity") == 100


@pytest.mark.asyncio
async def test_service_resolves_decision_prompt(db_session: AsyncSession):
    """A decision prompt can be created and resolved."""
    _, user = await _setup_pm(db_session)
    brief = MorningBrief(
        id="brief-3",
        pm_id=user.id,
        date=dt.date(2026, 1, 1),
        sections=[],
        focus_one={"ticker": "NVDA", "reason": "Guidance raised", "urgency_score": 0.8},
        catalyst_week=[],
    )
    db_session.add(brief)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            prompts = await service.create_prompts("morning_brief", brief.id)
            assert len(prompts) == 1

        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            resolved = await service.resolve_decision_prompt(
                prompts[0].id,
                response="Buy more NVDA",
            )
    finally:
        RequestContext.reset_current(ctx_token)

    assert resolved.response == "Buy more NVDA"
    assert resolved.resolved_at is not None


@pytest.mark.asyncio
async def test_service_isolation_rejects_cross_pm_action(db_session: AsyncSession):
    """Executing another PM's action raises an isolation error."""
    _, user = await _setup_pm(db_session)
    other_user = PMUser(
        id="pm-other",
        fund_entity_id=user.fund_entity_id,
        email="other@example.com",
    )
    db_session.add(other_user)
    await db_session.flush()

    brief = MorningBrief(
        id="brief-4",
        pm_id=other_user.id,
        date=dt.date(2026, 1, 1),
        sections=[],
        focus_one={"ticker": "META", "reason": "Regulatory news", "urgency_score": 0.7},
        catalyst_week=[],
    )
    db_session.add(brief)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=other_user.id))
    try:
        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=other_user.id)
            actions = await service.create_actions("morning_brief", brief.id)
            action_id = actions[0].id
    finally:
        RequestContext.reset_current(ctx_token)

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            with pytest.raises(ActionExecutionError):
                await service.execute_action(action_id, pm_id=user.id)
    finally:
        RequestContext.reset_current(ctx_token)


@pytest.mark.asyncio
async def test_lp_update_approval_prompt(db_session: AsyncSession):
    """An LP update draft generates an approval decision prompt."""
    _, user = await _setup_pm(db_session)
    vehicle = InvestmentVehicle(
        id="vehicle-1",
        fund_entity_id=user.fund_entity_id,
        name="Test Vehicle",
    )
    db_session.add(vehicle)
    await db_session.flush()

    lp_update = LPUpdate(
        id="lpu-1",
        vehicle_id=vehicle.id,
        quarter="2026-Q1",
        sections=[],
        status="draft",
    )
    db_session.add(lp_update)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        agent = InteractiveArtifactAgent(db_session)
        plan = await agent.generate_decision_prompt("lp_update", lp_update.id, user.id)
    finally:
        RequestContext.reset_current(ctx_token)

    assert len(plan.prompts) == 1
    assert "2026-Q1" in plan.prompts[0].prompt_text
    assert "Approve and send" in plan.prompts[0].options


@pytest.mark.asyncio
async def test_deck_annotation_actions(db_session: AsyncSession):
    """A deck output generates annotation actions."""
    _, user = await _setup_pm(db_session)
    deck = DeckOutput(
        id="deck-1",
        pm_id=user.id,
        type="deal_deck",
        source_ids=[],
        content={
            "title": "Deal Deck",
            "slides": [{"slide_number": 1, "title": "Executive Summary", "bullets": ["Bullet 1"]}],
        },
    )
    db_session.add(deck)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        agent = InteractiveArtifactAgent(db_session)
        plan = await agent.generate_actions("deck_output", deck.id, user.id)
    finally:
        RequestContext.reset_current(ctx_token)

    action_types = {a.action_type for a in plan.actions}
    assert "add_slide_note" in action_types
    assert "request_follow_up" in action_types
    assert "share_with_team" in action_types


@pytest.mark.asyncio
async def test_alert_deep_link_rendering():
    """AlertDelivery appends deep links when artifact metadata is present."""
    from axe.services.alert import AlertDelivery

    delivery = AlertDelivery(public_base_url="https://test.axe")
    payload = {
        "message": "Earnings surprise on AAPL.",
        "artifact_type": "morning_brief",
        "artifact_id": "brief-5",
        "prompt_id": "prompt-5",
    }
    message = delivery._append_deep_links(payload["message"], payload)
    assert "Decide now:" in message
    assert "https://test.axe/artifacts/morning_brief/brief-5/decision-prompts/prompt-5" in message


@pytest.mark.asyncio
async def test_router_list_actions_generates_on_demand(db_session: AsyncSession):
    """The interactive router lists actions, generating them lazily.

    This test uses the service directly rather than the ASGI client because
    the global ASGI client uses a separate async engine/session that does not
    share the test-scoped transaction.
    """
    _, user = await _setup_pm(db_session)
    brief = MorningBrief(
        id="brief-router",
        pm_id=user.id,
        date=dt.date(2026, 1, 1),
        sections=[],
        focus_one={"ticker": "GOOGL", "reason": "Search share shift", "urgency_score": 0.75},
        catalyst_week=[],
    )
    db_session.add(brief)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            actions = await service.create_actions("morning_brief", brief.id)
            assert len(actions) >= 2
    finally:
        RequestContext.reset_current(ctx_token)


@pytest.mark.asyncio
async def test_router_resolve_prompt(db_session: AsyncSession):
    """The interactive service resolves a decision prompt end-to-end."""
    _, user = await _setup_pm(db_session)
    brief = MorningBrief(
        id="brief-resolve",
        pm_id=user.id,
        date=dt.date(2026, 1, 1),
        sections=[],
        focus_one={"ticker": "MSFT", "reason": "AI margin expansion", "urgency_score": 0.8},
        catalyst_week=[],
    )
    db_session.add(brief)
    await db_session.commit()

    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id))
    try:
        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            prompts = await service.create_prompts("morning_brief", brief.id)
            prompt_id = prompts[0].id

        async with UnitOfWork(db_session) as uow:
            service = InteractiveArtifactService(uow, pm_id=user.id)
            resolved = await service.resolve_decision_prompt(
                prompt_id,
                response="Buy more MSFT",
            )
    finally:
        RequestContext.reset_current(ctx_token)

    assert resolved.response == "Buy more MSFT"
    assert resolved.resolved_at is not None
