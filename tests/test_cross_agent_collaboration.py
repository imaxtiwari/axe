"""Tests for the cross-agent collaboration bus and hooks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from axe.agents.agent_collaboration import (
    AgentCollaborationBus,
    AgentMessage,
)
from axe.db.models import (
    AgentMessage as AgentMessageRow,
)
from axe.db.models import (
    AuditLog,
    DecisionPrompt,
    FundEntity,
    InvestmentVehicle,
    PMUser,
    TickerRegistry,
)
from axe.db.uow import UnitOfWork
from axe.exceptions import IsolationError
from axe.security.context import RequestContext


async def _seed_fund_and_pm(uow: UnitOfWork) -> tuple[str, str]:
    fund = FundEntity(legal_name="Test Fund")
    uow.session.add(fund)
    await uow.session.flush()

    pm = PMUser(
        fund_entity_id=fund.id,
        email=f"pm-{uuid.uuid4().hex[:8]}@axe.fund",
        timezone="America/New_York",
    )
    uow.session.add(pm)
    await uow.session.flush()
    return fund.id, pm.id


@pytest.mark.asyncio
async def test_publish_same_pm_message_is_persisted_and_audited(db_session: Any) -> None:
    async with UnitOfWork(db_session) as uow:
        fund_id, pm_id = await _seed_fund_and_pm(uow)

        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            bus = AgentCollaborationBus(uow=uow)
            message = AgentMessage(
                sender_agent="drift_detect",
                sender_pm_id=pm_id,
                fund_entity_id=fund_id,
                intent="conflict_alert",
                payload={"summary": "test"},
            )
            published = await bus.publish(message)

            assert published.id is not None
            row = await uow.agent_messages.get_by_id(published.id)
            assert row is not None
            assert row.sender_pm_id == pm_id
            assert row.fund_entity_id == fund_id

            audit = await uow.session.execute(
                select(AuditLog).where(
                    AuditLog.object_id == published.id,
                    AuditLog.action_type == "agent_message_published",
                )
            )
            assert audit.scalar_one_or_none() is not None

        await uow.commit()


@pytest.mark.asyncio
async def test_cross_pm_message_requires_allow_list_or_fund_scope(db_session: Any) -> None:
    async with UnitOfWork(db_session) as uow:
        fund_id, pm_a = await _seed_fund_and_pm(uow)
        pm_b = PMUser(
            fund_entity_id=fund_id,
            email=f"pm-{uuid.uuid4().hex[:8]}@axe.fund",
        )
        uow.session.add(pm_b)
        await uow.session.flush()
        pm_b_id = pm_b.id

        with RequestContext.bind(pm_id=pm_a, fund_id=fund_id):
            bus = AgentCollaborationBus(uow=uow)

            with pytest.raises(IsolationError):
                await bus.publish(
                    AgentMessage(
                        sender_agent="a",
                        sender_pm_id=pm_a,
                        recipient_pm_id=pm_b_id,
                        fund_entity_id=fund_id,
                        intent="conflict_alert",
                    )
                )

            allowed = await bus.publish(
                AgentMessage(
                    sender_agent="a",
                    sender_pm_id=pm_a,
                    recipient_pm_id=pm_b_id,
                    fund_entity_id=fund_id,
                    intent="conflict_alert",
                    allowed_other_pm_ids={pm_b_id},
                )
            )
            assert allowed.id is not None

            fund_scoped = await bus.publish(
                AgentMessage(
                    sender_agent="a",
                    sender_pm_id=pm_a,
                    recipient_pm_id=pm_b_id,
                    fund_entity_id=fund_id,
                    intent="conflict_alert",
                    scope="fund",
                )
            )
            assert fund_scoped.id is not None

        await uow.commit()


@pytest.mark.asyncio
async def test_cross_fund_message_is_rejected(db_session: Any) -> None:
    async with UnitOfWork(db_session) as uow:
        fund_a, pm_a = await _seed_fund_and_pm(uow)
        fund_b, pm_b = await _seed_fund_and_pm(uow)

        with RequestContext.bind(pm_id=pm_a, fund_id=fund_a):
            bus = AgentCollaborationBus(uow=uow)
            with pytest.raises(IsolationError):
                await bus.publish(
                    AgentMessage(
                        sender_agent="a",
                        sender_pm_id=pm_a,
                        recipient_pm_id=pm_b,
                        fund_entity_id=fund_b,
                        intent="conflict_alert",
                        scope="fund",
                    )
                )


@pytest.mark.asyncio
async def test_requires_decision_creates_prompt(db_session: Any) -> None:
    async with UnitOfWork(db_session) as uow:
        fund_id, pm_id = await _seed_fund_and_pm(uow)

        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            bus = AgentCollaborationBus(uow=uow)
            message = AgentMessage(
                sender_agent="drift_detect",
                sender_pm_id=pm_id,
                fund_entity_id=fund_id,
                intent="conflict_alert",
                payload={"summary": "thesis breaking"},
                requires_decision=True,
            )
            prompt = await bus.route_to_pm(message)

            assert prompt is not None
            assert prompt.pm_id == pm_id
            assert prompt.artifact_id == message.id
            assert prompt.prompt_text is not None
            assert "thesis breaking" in prompt.prompt_text
            assert "Acknowledge" in prompt.options_json

            audit = await uow.session.execute(
                select(AuditLog).where(
                    AuditLog.object_id == message.id,
                    AuditLog.action_type == "agent_message_routed_to_pm",
                )
            )
            assert audit.scalar_one_or_none() is not None

        await uow.commit()


@pytest.mark.asyncio
async def test_recent_messages_do_not_leak_across_funds(db_session: Any) -> None:
    async with UnitOfWork(db_session) as uow:
        fund_a, pm_a = await _seed_fund_and_pm(uow)
        fund_b, pm_b = await _seed_fund_and_pm(uow)

        with RequestContext.bind(pm_id=pm_a, fund_id=fund_a):
            bus_a = AgentCollaborationBus(uow=uow)
            await bus_a.publish(
                AgentMessage(
                    sender_agent="a",
                    sender_pm_id=pm_a,
                    fund_entity_id=fund_a,
                    intent="conflict_alert",
                    payload={"summary": "secret"},
                )
            )

        with RequestContext.bind(pm_id=pm_b, fund_id=fund_b):
            bus_b = AgentCollaborationBus(uow=uow)
            messages = await bus_b.recent_messages_for_pm(pm_b, fund_b, limit=10)

        assert all(m.fund_entity_id == fund_b for m in messages)
        assert not any(m.payload.get("summary") == "secret" for m in messages)
        await uow.commit()


@pytest.mark.asyncio
async def test_worker_route_agent_message_handler(db_session_factory: Any) -> None:
    from axe.ingestion.worker import route_agent_message_handler

    async with db_session_factory() as session, UnitOfWork(session) as uow:
        fund_id, pm_id = await _seed_fund_and_pm(uow)
        await uow.commit()

    async with db_session_factory() as session:
        message = AgentMessage(
            id=str(uuid.uuid4()),
            sender_agent="drift_detect",
            sender_pm_id=pm_id,
            fund_entity_id=fund_id,
            intent="conflict_alert",
            payload={"summary": "worker test"},
            requires_decision=True,
        )
        success = await route_agent_message_handler(session, message.model_dump_for_worker())
        assert success is True

        result = await session.execute(
            select(DecisionPrompt).where(DecisionPrompt.artifact_id == message.id)
        )
        prompt = result.scalar_one_or_none()
        assert prompt is not None
        assert prompt.pm_id == pm_id


@pytest.mark.asyncio
async def test_lp_update_publishes_question_forward_message(db_session: Any) -> None:
    from axe.agents.lp_update import LPUpdateAgent

    async with UnitOfWork(db_session) as uow:
        fund_id, pm_id = await _seed_fund_and_pm(uow)
        investment_vehicle = InvestmentVehicle(
            id=str(uuid.uuid4()),
            fund_entity_id=fund_id,
            name="Test Vehicle",
        )
        uow.session.add(investment_vehicle)
        await uow.session.flush()

        agent = LPUpdateAgent(
            uow.session,
            pm_id=pm_id,
            fund_entity_id=fund_id,
        )
        update = await agent.draft_update(investment_vehicle.id, "2026-Q2")
        assert update is not None

        result = await uow.session.execute(
            select(AgentMessageRow).where(
                AgentMessageRow.intent == "question_forward",
                AgentMessageRow.sender_pm_id == pm_id,
            )
        )
        assert result.scalar_one_or_none() is not None
        await uow.commit()


@pytest.mark.asyncio
async def test_morning_brief_includes_cross_agent_sections(db_session: Any) -> None:
    from axe.agents.morning_brief import MorningBriefAgent

    async with UnitOfWork(db_session) as uow:
        fund_id, pm_id = await _seed_fund_and_pm(uow)
        ticker = TickerRegistry(
            pm_id=pm_id,
            ticker="AAPL",
            active=True,
        )
        uow.session.add(ticker)
        await uow.session.flush()

        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            bus = AgentCollaborationBus(uow=uow)
            await bus.publish(
                AgentMessage(
                    sender_agent="drift_detect",
                    sender_pm_id=pm_id,
                    fund_entity_id=fund_id,
                    intent="conflict_alert",
                    payload={"summary": "AAPL peer conflict", "primary_ticker": "AAPL"},
                )
            )
            await uow.commit()

    async with UnitOfWork(db_session) as uow:
        agent = MorningBriefAgent(uow.session)
        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            brief = await agent.generate(pm_id)

        cross_sections = [s for s in brief.sections if "cross_agent" in s.assumption_id]
        assert len(cross_sections) >= 1
        assert any(s.ticker == "AAPL" for s in cross_sections)


@pytest.mark.asyncio
async def test_retention_expires_after_30_days(db_session: Any) -> None:
    async with UnitOfWork(db_session) as uow:
        fund_id, pm_id = await _seed_fund_and_pm(uow)

        row = AgentMessageRow(
            sender_agent="a",
            sender_pm_id=pm_id,
            fund_entity_id=fund_id,
            intent="opportunity_share",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        uow.session.add(row)
        await uow.session.flush()

        with RequestContext.bind(pm_id=pm_id, fund_id=fund_id):
            bus = AgentCollaborationBus(uow=uow)
            messages = await bus.recent_messages_for_pm(pm_id, fund_id, limit=10)

        assert row.id not in {m.id for m in messages}
