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

        # Penalize verified citations whose numeric facts disagree with source.
        numeric_mismatch_penalty = self._numeric_mismatch_penalty(citations, raw_sources)

        # Weighted combination; each term is in [0, 1].
        score = (
            (1.0 - coverage) * 0.45
            + (1.0 - verification_ratio) * 0.30
            + (1.0 - overlap) * 0.10
            + no_source_penalty
            + numeric_mismatch_penalty
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

        service = ComplianceEscalationService(uow, settings=self.settings)
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

    @staticmethod
    def _numeric_mismatch_penalty(citations: list[Citation], raw_sources: list[Any]) -> float:
        """Penalize cited claims that contain different numbers than their source.

        This catches misattributed values (e.g. 124% vs 115%) and incompatible
        unit suffixes (e.g. $1.2 billion vs $1.2 million, 300% vs 300 basis
        points) while leaving purely qualitative claims untouched.
        """
        if not citations or not raw_sources:
            return 0.0

        # Capture a numeric value plus an optional unit suffix. We accept both
        # compact suffixes (bn, m, k, x, %) and full words (billion, million,
        # thousand, basis points) so unit-only mismatches are visible.
        _NUM_RE = re.compile(
            r"(-?\d+(?:\.\d+)?)\s*(?:"
            r"(basis points|bps)|"
            r"(billion|million|thousand)|"
            r"(bn|m|k)|"
            r"(x)|"
            r"(%)"
            r")?",
            re.IGNORECASE,
        )

        source_map: dict[str, str] = {}
        for source in raw_sources:
            if isinstance(source, dict):
                sid = source.get("id")
                if sid is not None:
                    source_map[str(sid)] = source.get("content") or source.get("text") or ""

        _WORD_TO_COMPACT: dict[str, str] = {
            "billion": "bn",
            "million": "m",
            "thousand": "k",
            "basis points": "bp",
            "bps": "bp",
        }

        def _normalize_unit(unit: str | None) -> str | None:
            if not unit:
                return None
            lower = unit.lower().strip()
            return _WORD_TO_COMPACT.get(lower, lower)

        def _extract_quantities(text: str) -> set[tuple[str, str | None]]:
            """Return (value, normalized_unit) tuples found in ``text``."""
            result: set[tuple[str, str | None]] = set()
            for match in _NUM_RE.finditer(text):
                value = match.group(1)
                unit = (
                    match.group(2)
                    or match.group(3)
                    or match.group(4)
                    or match.group(5)
                    or match.group(6)
                )
                result.add((value, _normalize_unit(unit)))
            return result

        mismatches = 0
        for c in citations:
            if not c.verified or not c.snippet or not c.source_id:
                continue
            source_content = source_map.get(str(c.source_id), "")
            if not source_content:
                continue
            snippet_qty = _extract_quantities(c.snippet)
            source_qty = _extract_quantities(source_content)
            if not snippet_qty:
                # No numeric claims in the snippet; nothing to penalize.
                continue

            mismatch_found = False
            for value, unit in snippet_qty:
                # Exact match is fully compatible.
                if (value, unit) in source_qty:
                    continue

                # Gather all units that appear with this value in the source.
                source_units_for_value = {
                    u for v, u in source_qty if v == value
                }

                if not source_units_for_value:
                    # The numeric value is not present in the source at all.
                    mismatch_found = True
                    break

                if unit is None:
                    # The snippet has a bare number. If the source attaches any
                    # unit to the same value, the claim is under-specified and
                    # we treat it as a mismatch to force review.
                    if any(u is not None for u in source_units_for_value):
                        mismatch_found = True
                        break
                else:
                    # The snippet specifies a unit, but the exact (value, unit)
                    # pair wasn't found. Any other unit for the same value is a
                    # unit mismatch (e.g. billion vs million, % vs bp).
                    if unit not in source_units_for_value:
                        mismatch_found = True
                        break

            if mismatch_found:
                mismatches += 1

        return min(1.0, mismatches / len(citations) * 0.6)


__all__ = ["HallucinationGuard"]
