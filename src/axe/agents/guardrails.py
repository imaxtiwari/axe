"""Multi-layer guardrail system for agent outputs.

``GuardrailRunner`` applies fund-scoped compliance, privacy, securities-regulation,
and consistency checks to LLM-generated artifacts. Violations are surfaced as
``GuardrailResult`` objects with a severity, violated rules, and a suggested action.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from axe.config import Settings, get_settings
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.compliance_escalation import (
    ComplianceEscalationService,
    ComplianceEscalationTrigger,
)
from axe.services.policy import PolicyEngine, PolicyEvent


@dataclass
class GuardrailResult:
    """Outcome of running the guardrail suite over an artifact."""

    passed: bool
    severity: str = "low"  # low|medium|high|critical
    violated_rules: list[str] = field(default_factory=list)
    suggested_action: str = "allow"  # allow|review|reject
    details: dict[str, Any] = field(default_factory=dict)


# Check signature used by GuardrailRunner.
Check = Callable[..., Awaitable[GuardrailResult]]


class GuardrailRunner:
    """Run configured guardrail checks and aggregate results.

    The runner is intentionally async so checks may perform repository lookups,
    call external policy engines, or run heavier heuristics in the future.
    """

    # Patterns used by lightweight built-in checks.
    _SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    _PHONE_RE = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
    _EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    _INSIDER_RE = re.compile(
        r"\b(insider|non.?public|confidential|behind closed doors|not yet announced)\b",
        re.IGNORECASE,
    )
    _FORWARD_LOOKING_RE = re.compile(
        r"\b(will earn|will report|guidance|expects revenue of \$|projected EPS)\b",
        re.IGNORECASE,
    )
    _PROMISE_RE = re.compile(
        r"\b(guarantee|guaranteed|promise|sure thing|risk.?free)\b", re.IGNORECASE
    )

    def __init__(
        self,
        uow: UnitOfWork | None = None,
        settings: Settings | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.uow = uow
        self.settings = settings or get_settings()
        self.policy_engine = policy_engine or PolicyEngine(settings=self.settings)

        # Register the default suite of checks. Checks can be enabled/disabled via
        # settings and are run in this order.
        self._checks: list[tuple[str, Check]] = [
            ("mnpi", self.mnpi_check),
            ("policy", self.policy_check),
            ("privacy", self.privacy_check),
            ("securities_regulation", self.securities_regulation_check),
            ("self_consistency", self.self_consistency_check),
        ]

    async def check(
        self,
        output: str | None,
        *,
        raw_sources: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """Run all configured checks and return an aggregated result.

        The most severe violation wins; suggested action is derived from severity.
        """
        output = (output or "").strip()
        metadata = metadata or {}

        enabled = self._enabled_checks()
        coros = [
            check(output, raw_sources=raw_sources, metadata=metadata)
            for name, check in self._checks
            if name in enabled
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        aggregated: list[GuardrailResult] = []
        for result in results:
            if isinstance(result, BaseException):
                aggregated.append(
                    GuardrailResult(
                        passed=False,
                        severity="high",
                        violated_rules=["guardrail_execution_error"],
                        suggested_action="review",
                        details={"error": str(result)},
                    )
                )
            else:
                aggregated.append(result)

        return self._aggregate(aggregated)

    def _enabled_checks(self) -> set[str]:
        """Return the set of checks enabled by configuration."""
        enabled: set[str] = set()
        if self.settings.guardrail_mnpi_check_enabled:
            enabled.add("mnpi")
        if self.settings.guardrail_policy_check_enabled:
            enabled.add("policy")
        # Privacy and securities checks are always enabled; they are lightweight
        # pattern heuristics and do not require external services.
        enabled.update({"privacy", "securities_regulation"})
        if self.settings.guardrail_self_consistency_enabled:
            enabled.add("self_consistency")
        return enabled

    def _aggregate(self, results: list[GuardrailResult]) -> GuardrailResult:
        """Merge individual results into a single result."""
        severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        max_result = max(results, key=lambda r: severity_order.get(r.severity, 0))

        violated: list[str] = []
        details: dict[str, Any] = {"checks": {}}
        for result in results:
            if not result.passed:
                violated.extend(result.violated_rules)
                details["checks"].update(result.details.get("checks", {}))

        if max_result.severity in {"high", "critical"}:
            action = "reject"
        elif max_result.severity == "medium":
            action = "review"
        else:
            action = "allow"

        return GuardrailResult(
            passed=max_result.severity in {"low"},
            severity=max_result.severity,
            violated_rules=violated,
            suggested_action=action,
            details=details,
        )

    async def mnpi_check(
        self,
        output: str,
        *,
        raw_sources: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """Flag language that resembles material non-public information."""
        metadata = metadata or {}
        hits = self._INSIDER_RE.findall(output)
        if not hits:
            return GuardrailResult(passed=True, details={"checks": {"mnpi": "clean"}})

        severity = "high" if len(hits) >= 2 else "medium"
        return GuardrailResult(
            passed=False,
            severity=severity,
            violated_rules=["mnpi_language_detected"],
            suggested_action="review" if severity == "medium" else "reject",
            details={"checks": {"mnpi": {"hits": list(set(hits))}}},
        )

    async def policy_check(
        self,
        output: str,
        *,
        raw_sources: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """Evaluate the artifact against fund-scoped policy rules."""
        metadata = metadata or {}
        ctx = RequestContext.current_or_none()
        event = PolicyEvent(
            pm_id=ctx.pm_id if ctx is not None else None,
            fund_entity_id=ctx.fund_id if ctx is not None else None,
            artifact_type=metadata.get("artifact_type", "unknown"),
            content=output,
            metadata=metadata or {},
        )
        actions = await self.policy_engine.evaluate(event, uow=self.uow)

        if not actions:
            return GuardrailResult(passed=True, details={"checks": {"policy": "clean"}})

        severities = [a.severity for a in actions]
        severity = self._max_severity(severities)
        return GuardrailResult(
            passed=False,
            severity=severity,
            violated_rules=[a.rule_type for a in actions],
            suggested_action="reject" if severity in {"high", "critical"} else "review",
            details={"checks": {"policy": {"actions": [a.model_dump() for a in actions]}}},
        )

    async def privacy_check(
        self,
        output: str,
        *,
        raw_sources: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """Detect likely PII leakage in the output."""
        metadata = metadata or {}
        findings: dict[str, int] = {}
        for name, pattern in (
            ("ssn", self._SSN_RE),
            ("phone", self._PHONE_RE),
            ("email", self._EMAIL_RE),
        ):
            matches = pattern.findall(output)
            if matches:
                findings[name] = len(matches)

        if not findings:
            return GuardrailResult(passed=True, details={"checks": {"privacy": "clean"}})

        severity = "critical" if findings.get("ssn", 0) > 0 else "high"
        return GuardrailResult(
            passed=False,
            severity=severity,
            violated_rules=["pii_detected"],
            suggested_action="reject",
            details={"checks": {"privacy": findings}},
        )

    async def securities_regulation_check(
        self,
        output: str,
        *,
        raw_sources: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """Flag forward-looking or promissory statements that may run afoul of securities regs."""
        metadata = metadata or {}
        forward_hits = self._FORWARD_LOOKING_RE.findall(output)
        promise_hits = self._PROMISE_RE.findall(output)

        if not forward_hits and not promise_hits:
            return GuardrailResult(
                passed=True, details={"checks": {"securities_regulation": "clean"}}
            )

        severity = "high" if promise_hits else "medium"
        return GuardrailResult(
            passed=False,
            severity=severity,
            violated_rules=["securities_language_detected"],
            suggested_action="review" if severity == "medium" else "reject",
            details={
                "checks": {
                    "securities_regulation": {
                        "forward_looking": list(set(forward_hits)),
                        "promissory": list(set(promise_hits)),
                    }
                }
            },
        )

    async def self_consistency_check(
        self,
        output: str,
        *,
        raw_sources: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GuardrailResult:
        """Detect simple contradictions in the output.

        Current implementation looks for negation flips of the same verb/adjective
        in a single output. This is intentionally lightweight and can be replaced
        with an LLM-as-judge check in the future.
        """
        contradictions: list[dict[str, Any]] = []
        lowered = output.lower()

        pairs = [
            ("is", "is not"),
            ("will", "will not"),
            ("does", "does not"),
            ("has", "has not"),
            ("increase", "decrease"),
            ("bullish", "bearish"),
            ("buy", "sell"),
            ("long", "short"),
        ]

        for a, b in pairs:
            if f" {a} " in lowered and f" {b} " in lowered:
                contradictions.append({"terms": [a, b]})

        if not contradictions:
            return GuardrailResult(passed=True, details={"checks": {"self_consistency": "clean"}})

        return GuardrailResult(
            passed=False,
            severity="medium",
            violated_rules=["self_consistency_issue"],
            suggested_action="review",
            details={"checks": {"self_consistency": contradictions}},
        )

    async def escalate(
        self,
        result: GuardrailResult,
        trace_id: str | None = None,
    ) -> Any | None:
        """Open a compliance escalation for high-severity guardrail failures."""
        if result.severity not in {"high", "critical"}:
            return None
        if self.uow is None:
            return None

        ctx = RequestContext.current_or_none()
        pm_id = ctx.pm_id if ctx is not None else None
        fund_entity_id = ctx.fund_id if ctx is not None else None
        if not fund_entity_id:
            return None

        service = ComplianceEscalationService(self.uow)
        trigger = ComplianceEscalationTrigger(
            trigger_type="guardrail",
            severity=result.severity,  # type: ignore[arg-type]
            fund_entity_id=fund_entity_id,
            pm_id=pm_id,
            details={"violated_rules": result.violated_rules, "trace_id": trace_id},
        )
        return await service.open(trigger)

    @staticmethod
    def _max_severity(severities: list[str]) -> str:
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if not severities:
            return "low"
        return max(severities, key=lambda s: order.get(s, 0))


__all__ = ["GuardrailRunner", "GuardrailResult"]
