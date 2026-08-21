"""Tests for the compliance escalation service.

Covers open/assign/resolve lifecycle, reviewer assignment rules, audit logging,
and trigger wiring from guardrails, MNPI, and hallucination review.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.guardrails import GuardrailResult, GuardrailRunner
from axe.agents.hallucination_guard import HallucinationGuard
from axe.agents.mnpi_review import MNPIReviewResult
from axe.config import Settings
from axe.db.models import AuditLog, ComplianceEscalation, FundEntity, PMUser, SignalLog
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.compliance_escalation import (
    ComplianceEscalationService,
    ComplianceEscalationTrigger,
)
from axe.services.mnpi import MNPIReviewAgent, MNPIService

pytestmark = pytest.mark.asyncio


async def _fund(session: AsyncSession) -> FundEntity:
    fund = FundEntity(
        id=str(uuid.uuid4()),
        legal_name=f"Fund {uuid.uuid4().hex[:8]}",
        data_residency="US",
    )
    session.add(fund)
    await session.flush()
    return fund


async def _pm_user(
    session: AsyncSession,
    fund_id: str,
    *,
    role: str = "pm",
    active: bool = True,
) -> PMUser:
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund_id,
        email=f"{role}_{uuid.uuid4().hex[:8]}@example.com",
        role=role,
        active=active,
    )
    session.add(user)
    await session.flush()
    return user


async def _compliance_officer(session: AsyncSession, fund_id: str) -> PMUser:
    return await _pm_user(session, fund_id, role="compliance_officer")


class TestOpenLifecycle:
    async def test_open_creates_escalation(self, db_session: AsyncSession) -> None:
        fund = await _fund(db_session)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
            pm_id=None,
            details={"violated_rules": ["pii_detected"]},
        )
        escalation = await service.open(trigger)

        assert escalation.id is not None
        assert escalation.fund_entity_id == fund.id
        assert escalation.trigger_type == "guardrail"
        assert escalation.severity == "high"
        assert escalation.status in {"open", "assigned"}

    async def test_open_auto_assigns_reviewer(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        officer = await _compliance_officer(db_session, fund.id)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
        )
        escalation = await service.open(trigger)

        assert escalation.status == "assigned"
        assert escalation.reviewer_id == officer.id

    async def test_open_no_reviewer_when_no_officer(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
        )
        escalation = await service.open(trigger)

        assert escalation.status == "open"
        assert escalation.reviewer_id is None

    async def test_open_below_threshold_is_suppressed(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        settings = Settings(compliance_auto_escalation_severity="high")
        service = ComplianceEscalationService(db_session, settings=settings)

        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="medium",
            fund_entity_id=fund.id,
        )
        with pytest.raises(ValueError):
            await service.open(trigger)

        # Audit suppression record is written.
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.action_type == "compliance_escalation_suppressed"
            )
        )
        assert result.scalar_one_or_none() is not None

    async def test_open_invalid_severity_fails(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="unknown",  # type: ignore[arg-type]
            fund_entity_id=fund.id,
        )
        with pytest.raises(ValueError):
            await service.open(trigger)


class TestAssignReviewer:
    async def test_assign_reviewer(self, db_session: AsyncSession) -> None:
        fund = await _fund(db_session)
        officer = await _compliance_officer(db_session, fund.id)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="mnpi_review",
            severity="high",
            fund_entity_id=fund.id,
        )
        escalation = await service.open(trigger, auto_assign=False)
        assert escalation.status == "open"

        updated = await service.assign_reviewer(escalation.id, officer.id)
        assert updated.status == "assigned"
        assert updated.reviewer_id == officer.id

        # Audit log records the assignment with before/after state.
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.action_type == "compliance_escalation_assigned"
            )
        )
        entry = result.scalar_one()
        assert entry.before_state["status"] == "open"
        assert entry.after_state["status"] == "assigned"
        assert entry.after_state["reviewer_id"] == officer.id

    async def test_assign_reviewer_wrong_fund_fails(
        self, db_session: AsyncSession
    ) -> None:
        fund_a = await _fund(db_session)
        fund_b = await _fund(db_session)
        officer_b = await _compliance_officer(db_session, fund_b.id)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="mnpi_review",
            severity="high",
            fund_entity_id=fund_a.id,
        )
        escalation = await service.open(trigger, auto_assign=False)

        with pytest.raises(ValueError, match="does not belong"):
            await service.assign_reviewer(escalation.id, officer_b.id)

    async def test_assign_non_officer_fails(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        pm = await _pm_user(db_session, fund.id)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="mnpi_review",
            severity="high",
            fund_entity_id=fund.id,
        )
        escalation = await service.open(trigger, auto_assign=False)

        with pytest.raises(ValueError, match="compliance_officer"):
            await service.assign_reviewer(escalation.id, pm.id)


class TestResolve:
    async def test_resolve_approved(self, db_session: AsyncSession) -> None:
        fund = await _fund(db_session)
        officer = await _compliance_officer(db_session, fund.id)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="hallucination",
            severity="high",
            fund_entity_id=fund.id,
        )
        escalation = await service.open(trigger)
        escalation_id = escalation.id

        resolved = await service.resolve(
            escalation_id, "approved", note="looks fine"
        )
        assert resolved.status == "resolved"
        assert resolved.closed_at is not None
        assert resolved.details["decision"] == "approved"
        assert resolved.details["resolution_note"] == "looks fine"

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.action_type == "compliance_escalation_approved"
            )
        )
        entry = result.scalar_one()
        assert entry.before_state["status"] in {"open", "assigned"}
        assert entry.after_state["status"] == "resolved"

    async def test_resolve_dismissed(self, db_session: AsyncSession) -> None:
        fund = await _fund(db_session)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
        )
        escalation = await service.open(trigger)

        resolved = await service.resolve(escalation.id, "dismissed")
        assert resolved.status == "dismissed"

    async def test_resolve_already_resolved_fails(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        service = ComplianceEscalationService(db_session)

        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
        )
        escalation = await service.open(trigger)
        await service.resolve(escalation.id, "approved")

        with pytest.raises(ValueError, match="already"):
            await service.resolve(escalation.id, "rejected")


class TestListOpen:
    async def test_list_open_scoped_by_fund(
        self, db_session: AsyncSession
    ) -> None:
        fund_a = await _fund(db_session)
        fund_b = await _fund(db_session)
        service = ComplianceEscalationService(db_session)

        trigger_a = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund_a.id,
        )
        trigger_b = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund_b.id,
        )
        escalation_a = await service.open(trigger_a)
        await service.open(trigger_b)

        with RequestContext.bind(fund_id=fund_a.id, role="compliance_officer"):
            open_items = await service.list_open()

        assert len(open_items) == 1
        assert open_items[0].id == escalation_a.id

    async def test_list_open_pm_sees_own_only(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        pm_a = await _pm_user(db_session, fund.id)
        pm_b = await _pm_user(db_session, fund.id)
        service = ComplianceEscalationService(db_session)

        trigger_a = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
            pm_id=pm_a.id,
        )
        trigger_b = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
            pm_id=pm_b.id,
        )
        await service.open(trigger_a)
        await service.open(trigger_b)

        with RequestContext.bind(
            pm_id=pm_a.id, fund_id=fund.id, role="pm"
        ):
            open_items = await service.list_open(role="pm")

        assert len(open_items) == 1
        assert open_items[0].pm_id == pm_a.id


class TestReviewerAssignmentRoundRobin:
    async def test_round_robin_load_balances(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        officer_a = await _compliance_officer(db_session, fund.id)
        officer_b = await _compliance_officer(db_session, fund.id)
        service = ComplianceEscalationService(db_session)

        # Give officer_a an existing open escalation.
        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity="high",
            fund_entity_id=fund.id,
        )
        first = await service.open(trigger)
        assert first.reviewer_id == officer_a.id

        # Second escalation should go to officer_b because officer_a has one
        # open assignment.
        second = await service.open(trigger)
        assert second.reviewer_id == officer_b.id


class TestTriggerWiring:
    async def test_guardrail_escalation(self, db_session: AsyncSession) -> None:
        fund = await _fund(db_session)
        await _compliance_officer(db_session, fund.id)

        with RequestContext.bind(fund_id=fund.id, role="pm"):
            async with UnitOfWork(db_session) as uow:
                runner = GuardrailRunner(uow=uow)
                result = GuardrailResult(
                    passed=False,
                    severity="high",
                    violated_rules=["pii_detected"],
                    suggested_action="reject",
                )
                escalation = await runner.escalate(result)
                assert escalation is not None
                assert escalation.trigger_type == "guardrail"
                await uow.commit()

    async def test_hallucination_escalation(
        self, db_session: AsyncSession
    ) -> None:
        fund = await _fund(db_session)
        await _compliance_officer(db_session, fund.id)

        with RequestContext.bind(fund_id=fund.id, role="pm"):
            async with UnitOfWork(db_session) as uow:
                guard = HallucinationGuard(
                    settings=Settings(
                        hallucination_score_threshold=0.3,
                        hallucination_auto_reject_threshold=0.7,
                    )
                )
                routing = await guard.route_for_review(
                    score=0.75, trace_id="trace-1", uow=uow
                )
                assert routing["action"] == "reject"
                assert routing["escalation_id"] is not None
                await uow.commit()

        result = await db_session.execute(
            select(ComplianceEscalation).where(
                ComplianceEscalation.id == routing["escalation_id"]
            )
        )
        escalation = result.scalar_one()
        assert escalation.trigger_type == "hallucination"
        assert escalation.severity == "high"

    async def test_mnpi_escalation(self, db_session: AsyncSession) -> None:
        import hashlib

        fund = await _fund(db_session)
        pm = await _pm_user(db_session, fund.id)
        await _compliance_officer(db_session, fund.id)

        # Seed a signal log row.
        raw = "insider confidential"
        signal = SignalLog(
            id=str(uuid.uuid4()),
            pm_id=pm.id,
            ticker="AAPL",
            source_type="manual",
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
            raw_content=raw,
        )
        db_session.add(signal)
        await db_session.flush()

        agent = MNPIReviewAgent()
        # Force the agent to flag by mocking review.
        agent.review = mock.AsyncMock(  # type: ignore[method-assign]
            return_value=MNPIReviewResult(
                flagged=True,
                mnpi_score=0.8,
                materiality_score=0.6,
                reasoning="mock",
            )
        )

        with RequestContext.bind(pm_id=pm.id, fund_id=fund.id, role="pm"):
            service = MNPIService(db_session, agent=agent)
            outcome = await service.review_signal(
                signal_id=signal.id,
                signal_text="insider confidential",
                ticker="AAPL",
                pm_id=pm.id,
                alert_payloads=[],
            )

        assert outcome.blocked is True
        result = await db_session.execute(
            select(ComplianceEscalation).where(
                ComplianceEscalation.trigger_type == "mnpi_review"
            )
        )
        escalation = result.scalar_one()
        assert escalation.fund_entity_id == fund.id
        assert escalation.severity == "critical"
