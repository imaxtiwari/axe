"""Tests for hallucination scoring and review routing."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from axe.agents.citation import Citation, CitationExtractor, CitationVerifier
from axe.agents.hallucination_guard import HallucinationGuard
from axe.config import Settings
from axe.db.models import FundEntity, PMUser
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from tests.hallucination_eval_dataset import EVAL_PAIRS


@pytest.fixture
def settings() -> Settings:
    return Settings(
        hallucination_score_threshold=0.3,
        hallucination_auto_reject_threshold=0.7,
        citation_coverage_threshold=0.8,
    )


@pytest.fixture
def guard(settings: Settings) -> HallucinationGuard:
    return HallucinationGuard(settings=settings)


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


class TestHallucinationGuardScore:
    def test_score_empty_output(self, guard: HallucinationGuard) -> None:
        assert guard.score("") == 0.0
        assert guard.score(None) == 0.0

    def test_score_zero_for_no_claims(self, guard: HallucinationGuard) -> None:
        assert guard.score("Hi.") == 0.0

    def test_score_fully_verified_is_low(self, guard: HallucinationGuard) -> None:
        output = "Apple's revenue was $123.9 billion. [1]"
        sources = [
            {
                "id": "1",
                "source_type": "earnings_release",
                "content": "Apple reported Q1 revenue of $123.9 billion.",
            }
        ]
        extractor = CitationExtractor()
        verifier = CitationVerifier()
        citations = verifier.verify(extractor.extract(output, sources), sources)

        score = guard.score(output, citations=citations, raw_sources=sources)
        assert score < 0.3

    def test_score_uncited_claim_is_high(self, guard: HallucinationGuard) -> None:
        output = "NVIDIA is acquiring Arm for $80 billion. The deal closes in 2025."
        score = guard.score(output, citations=[], raw_sources=[])
        assert score >= 0.55

    def test_score_unverified_citations(self, guard: HallucinationGuard) -> None:
        output = "Tesla will deliver 2.5 million vehicles. [1]"
        sources = [
            {
                "id": "1",
                "source_type": "transcript",
                "content": "Tesla has not provided 2025 delivery guidance.",
            }
        ]
        extractor = CitationExtractor()
        verifier = CitationVerifier()
        citations = verifier.verify(extractor.extract(output, sources), sources)

        score = guard.score(output, citations=citations, raw_sources=sources)
        assert score >= guard.settings.hallucination_score_threshold

    def test_eval_dataset_consistency(self, guard: HallucinationGuard) -> None:
        for pair in EVAL_PAIRS:
            extractor = CitationExtractor()
            verifier = CitationVerifier()
            citations = verifier.verify(
                extractor.extract(pair["output"], pair["sources"]), pair["sources"]
            )
            score = guard.score(pair["output"], citations=citations, raw_sources=pair["sources"])

            if pair["should_fail"]:
                assert score >= guard.settings.hallucination_score_threshold, (
                    f"{pair['name']} should fail but scored {score}"
                )
            else:
                assert score < guard.settings.hallucination_score_threshold, (
                    f"{pair['name']} should pass but scored {score}"
                )


class TestHallucinationGuardRouting:
    async def test_route_allow(self, guard: HallucinationGuard) -> None:
        result = await guard.route_for_review(0.1)
        assert result["action"] == "allow"
        assert result["human_review_status"] == "not_required"
        assert result["escalation_id"] is None

    async def test_route_review(self, guard: HallucinationGuard) -> None:
        result = await guard.route_for_review(0.4)
        assert result["action"] == "review"
        assert result["human_review_status"] == "pending"

    async def test_route_reject(self, guard: HallucinationGuard) -> None:
        result = await guard.route_for_review(0.8)
        assert result["action"] == "reject"
        assert result["human_review_status"] == "pending"
        assert result["severity"] == "high"

    async def test_route_review_creates_escalation(
        self,
        guard: HallucinationGuard,
        uow: UnitOfWork,
        fund_and_pm: tuple[FundEntity, PMUser],
    ) -> None:
        ctx = RequestContext(pm_id="pm-1", fund_id="fund-1", role="pm")
        token = RequestContext.set_current(ctx)
        try:
            result = await guard.route_for_review(0.4, trace_id="trace-1", uow=uow)
            assert result["action"] == "review"
            assert result["escalation_id"] is not None

            escalation = await uow.compliance_escalations.get_by_id(result["escalation_id"])
            assert escalation is not None
            assert escalation.trigger_type == "hallucination"
            assert escalation.severity == "medium"
        finally:
            RequestContext.reset_current(token)

    async def test_route_without_fund_does_not_escalate(
        self, guard: HallucinationGuard, uow: UnitOfWork
    ) -> None:
        ctx = RequestContext(pm_id="pm-1", fund_id="", role="pm")
        token = RequestContext.set_current(ctx)
        try:
            result = await guard.route_for_review(0.4, trace_id="trace-1", uow=uow)
            assert result["escalation_id"] is None
        finally:
            RequestContext.reset_current(token)


class TestHallucinationGuardHelpers:
    def test_coverage_score(self, guard: HallucinationGuard) -> None:
        claims = ["A", "B"]
        citations = [Citation(snippet="A", span=(0, 2))]
        assert guard._coverage_score(claims, citations) == 1.0
        assert guard._coverage_score([], citations) == 1.0
        assert guard._coverage_score(claims, []) == 0.0

    def test_verification_ratio(self, guard: HallucinationGuard) -> None:
        citations = [
            Citation(snippet="A", verified=True),
            Citation(snippet="B", verified=False),
        ]
        assert guard._verification_ratio(citations) == 0.5

    def test_average_overlap(self, guard: HallucinationGuard) -> None:
        citations = [
            Citation(snippet="A", confidence=1.0),
            Citation(snippet="B", confidence=0.5),
        ]
        assert guard._average_overlap(citations) == 0.75
