"""Tests for the multi-layer guardrail system."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from axe.agents.guardrails import GuardrailResult, GuardrailRunner
from axe.config import Settings
from axe.db.models import ComplianceEscalation, FundEntity, PMUser, PolicyRule
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.policy import PolicyEngine


@pytest.fixture
def settings() -> Settings:
    return Settings(
        guardrail_mnpi_check_enabled=True,
        guardrail_policy_check_enabled=True,
        guardrail_self_consistency_enabled=True,
    )


@pytest_asyncio.fixture
async def uow(db_session: Any) -> AsyncGenerator[UnitOfWork, None]:
    async with UnitOfWork(session=db_session) as uow:
        yield uow


@pytest_asyncio.fixture
async def fund_and_pm(uow: UnitOfWork) -> tuple[FundEntity, PMUser]:
    fund = FundEntity(legal_name="Test Fund", id="fund-1")
    pm = PMUser(
        id="pm-1",
        fund_entity_id=fund.id,
        email="pm@example.com",
        role="pm",
    )
    uow.session.add(fund)
    uow.session.add(pm)
    await uow.session.flush()
    return fund, pm


class TestGuardrailChecks:
    async def test_clean_output_passes(self, settings: Settings) -> None:
        runner = GuardrailRunner(settings=settings)
        result = await runner.check("The market was up 2% today.")

        assert result.passed is True
        assert result.severity == "low"
        assert result.suggested_action == "allow"

    async def test_mnpi_flagged(self, settings: Settings) -> None:
        runner = GuardrailRunner(settings=settings)
        result = await runner.check(
            "We received confidential, non-public information about the deal."
        )

        assert result.passed is False
        assert "mnpi_language_detected" in result.violated_rules
        assert result.severity in {"medium", "high"}

    async def test_privacy_ssn_critical(self, settings: Settings) -> None:
        runner = GuardrailRunner(settings=settings)
        result = await runner.check("The PM's SSN is 123-45-6789.")

        assert result.passed is False
        assert result.severity == "critical"
        assert result.suggested_action == "reject"

    async def test_securities_regulation_flagged(self, settings: Settings) -> None:
        runner = GuardrailRunner(settings=settings)
        result = await runner.check("We guarantee the stock will earn $5 next year.")

        assert result.passed is False
        assert "securities_language_detected" in result.violated_rules
        assert result.severity == "high"

    async def test_self_consistency_flagged(self, settings: Settings) -> None:
        runner = GuardrailRunner(settings=settings)
        result = await runner.check("The team is bullish and bearish on this name.")

        assert result.passed is False
        assert "self_consistency_issue" in result.violated_rules
        assert result.severity == "medium"

    async def test_aggregated_severity_takes_worst(
        self, settings: Settings
    ) -> None:
        runner = GuardrailRunner(settings=settings)
        output = (
            "We received confidential, non-public information. "
            "The PM's SSN is 123-45-6789."
        )
        result = await runner.check(output)

        assert result.severity == "critical"
        assert result.suggested_action == "reject"
        assert "pii_detected" in result.violated_rules

    async def test_disabled_checks_are_skipped(self, settings: Settings) -> None:
        settings.guardrail_mnpi_check_enabled = False
        settings.guardrail_policy_check_enabled = False
        settings.guardrail_self_consistency_enabled = False
        runner = GuardrailRunner(settings=settings)
        result = await runner.check(
            "We received confidential, non-public information."
        )

        assert result.passed is True
        assert "mnpi_language_detected" not in result.violated_rules

    async def test_policy_check_matches_rule(
        self, uow: UnitOfWork, settings: Settings
    ) -> None:
        ctx = RequestContext(pm_id="pm-1", fund_id="fund-1", role="pm")
        token = RequestContext.set_current(ctx)
        try:
            rule = PolicyRule(
                id="rule-1",
                fund_entity_id="fund-1",
                rule_type="prohibited_topic",
                scope="fund",
                action="block",
                conditions_json={
                    "contains_any": ["crypto"],
                    "severity": "high",
                },
                enabled=True,
            )
            uow.session.add(rule)
            await uow.session.flush()

            runner = GuardrailRunner(uow=uow, settings=settings)
            result = await runner.check(
                "We should buy crypto tokens.", metadata={"artifact_type": "memo"}
            )

            assert result.passed is False
            assert "prohibited_topic" in result.violated_rules
            assert result.severity == "high"
        finally:
            RequestContext.reset_current(token)


class TestGuardrailEscalation:
    async def test_escalate_creates_compliance_escalation(
        self,
        uow: UnitOfWork,
        settings: Settings,
        fund_and_pm: tuple[FundEntity, PMUser],
    ) -> None:
        fund, pm = fund_and_pm
        ctx = RequestContext(pm_id=pm.id, fund_id=fund.id, role="pm")
        token = RequestContext.set_current(ctx)
        try:
            runner = GuardrailRunner(uow=uow, settings=settings)
            result = await runner.check(
                "We received confidential, non-public information.",
                metadata={"artifact_type": "signal"},
            )

            escalation = await runner.escalate(result, trace_id="trace-1")
            await uow.session.flush()

            assert isinstance(escalation, ComplianceEscalation)
            assert escalation.trigger_type == "guardrail"
            assert escalation.severity == result.severity
            assert escalation.fund_entity_id == fund.id
        finally:
            RequestContext.reset_current(token)

    async def test_escalate_ignored_for_low_severity(
        self, uow: UnitOfWork, settings: Settings
    ) -> None:
        runner = GuardrailRunner(uow=uow, settings=settings)
        result = GuardrailResult(passed=True, severity="low")
        escalation = await runner.escalate(result)
        assert escalation is None

    async def test_escalate_requires_fund(
        self, uow: UnitOfWork, settings: Settings
    ) -> None:
        ctx = RequestContext(pm_id="pm-1", fund_id="", role="pm")
        token = RequestContext.set_current(ctx)
        try:
            runner = GuardrailRunner(uow=uow, settings=settings)
            result = GuardrailResult(
                passed=False,
                severity="high",
                violated_rules=["mnpi_language_detected"],
            )
            escalation = await runner.escalate(result)
            assert escalation is None
        finally:
            RequestContext.reset_current(token)


class TestGuardrailResult:
    def test_default_result(self) -> None:
        result = GuardrailResult(passed=True)
        assert result.severity == "low"
        assert result.suggested_action == "allow"
        assert result.violated_rules == []
