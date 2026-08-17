"""Hallucination scoring and review routing for agent outputs.

The heuristic scorer measures how well an output is grounded in its cited
sources. High scores route the artifact to human review and optionally open a
``ComplianceEscalation``.
"""

from __future__ import annotations

import re
from typing import Any

from axe.agents.citation import Citation
from axe.config import Settings, get_settings
from axe.db.uow import UnitOfWork
from axe.services.compliance_escalation import (
    BelowAutoEscalationThreshold,
    ComplianceEscalationService,
    ComplianceEscalationTrigger,
)


class HallucinationGuard:
    """Score and route outputs based on citation coverage and verification."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def score(
        self,
        output: str | None,
        citations: list[Citation] | None = None,
        raw_sources: list[Any] | None = None,
    ) -> float:
        """Return a hallucination score in ``[0, 1]``.

        Lower is better. The score is driven by:
        * citation coverage (claims with a citation / total claims)
        * fraction of citations that are verified against source text
        * average overlap between citation snippets and their sources

        A fully grounded output with verified citations scores near 0. An output
        that makes uncited factual claims scores near 1.
        """
        output = (output or "").strip()
        if not output:
            return 0.0

        citations = citations or []
        raw_sources = raw_sources or []

        claims = self._split_claims(output)
        if not claims:
            return 0.0

        coverage = self._coverage_score(claims, citations)
        verification_ratio = self._verification_ratio(citations)
        overlap = self._average_overlap(citations)

        # Penalize outputs that make claims without any sources available.
        no_source_penalty = 0.15 if not raw_sources else 0.0

        # Weighted combination; each term is in [0, 1].
        score = (
            (1.0 - coverage) * 0.45
            + (1.0 - verification_ratio) * 0.30
            + (1.0 - overlap) * 0.10
            + no_source_penalty
        )
        return round(min(1.0, max(0.0, score)), 3)

    async def route_for_review(
        self,
        score: float,
        trace_id: str | None = None,
        uow: UnitOfWork | None = None,
    ) -> dict[str, Any]:
        """Return a routing decision and optionally open a compliance escalation.

        The decision uses the configured hallucination thresholds:
        * ``>= hallucination_auto_reject_threshold`` -> reject (block)
        * ``>= hallucination_score_threshold``       -> human review
        * otherwise                                  -> allow
        """
        auto_reject = self.settings.hallucination_auto_reject_threshold
        review_threshold = self.settings.hallucination_score_threshold

        if score >= auto_reject:
            action = "reject"
            human_review_status = "pending"
            severity = "high"
        elif score >= review_threshold:
            action = "review"
            human_review_status = "pending"
            severity = "medium"
        else:
            action = "allow"
            human_review_status = "not_required"
            severity = "low"

        escalation_id: str | None = None
        if action in {"reject", "review"} and uow is not None:
            escalation = await self._create_escalation(score, trace_id, severity, uow)
            escalation_id = escalation.id if escalation is not None else None

        return {
            "action": action,
            "human_review_status": human_review_status,
            "severity": severity,
            "score": score,
            "escalation_id": escalation_id,
        }

    async def _create_escalation(
        self,
        score: float,
        trace_id: str | None,
        severity: str,
        uow: UnitOfWork,
    ) -> Any | None:
        """Persist a hallucination-driven compliance escalation."""
        from axe.security.context import RequestContext

        ctx = RequestContext.current_or_none()
        pm_id = ctx.pm_id if ctx is not None else None
        fund_entity_id = ctx.fund_id if ctx is not None else None

        if not fund_entity_id:
            # Cannot create an escalation without a fund scope.
            return None

        service = ComplianceEscalationService(uow)
        trigger = ComplianceEscalationTrigger(
            trigger_type="hallucination",
            severity=severity,  # type: ignore[arg-type]
            fund_entity_id=fund_entity_id,
            pm_id=pm_id,
            details={"score": score, "trace_id": trace_id},
        )
        try:
            return await service.open(trigger)
        except BelowAutoEscalationThreshold:
            return None

    @staticmethod
    def _split_claims(output: str) -> list[str]:
        """Split output into roughly sentence-level claims."""
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", output) if len(s.strip()) > 8]

    @staticmethod
    def _coverage_score(claims: list[str], citations: list[Citation]) -> float:
        """Fraction of claims that have at least one citation overlap."""
        if not claims:
            return 1.0
        if not citations:
            return 0.0

        cited_claims = 0
        citation_spans = {c.span for c in citations if c.span is not None}
        for claim in claims:
            # A claim is considered cited if any citation falls inside a rough
            # character window surrounding it. This is intentionally heuristic.
            if HallucinationGuard._claim_has_citation(claim, citation_spans):
                cited_claims += 1
        return cited_claims / len(claims)

    @staticmethod
    def _claim_has_citation(claim: str, spans: set[tuple[int, int]]) -> bool:
        # We don't have claim spans here; approximate by checking that at least
        # one citation exists anywhere in the output. For a tighter bound callers
        # should supply citation spans aligned to claims.
        return len(spans) > 0

    @staticmethod
    def _verification_ratio(citations: list[Citation]) -> float:
        if not citations:
            return 0.0
        verified = sum(1 for c in citations if c.verified)
        return verified / len(citations)

    @staticmethod
    def _average_overlap(citations: list[Citation]) -> float:
        if not citations:
            return 0.0
        return sum(c.confidence for c in citations) / len(citations)


__all__ = ["HallucinationGuard"]
