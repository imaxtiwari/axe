"""Signal-vs-thesis drift detection agent."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.embedding import EmbeddingModel, cosine_similarity, get_default_embedding_model
from axe.agents.llm import LLMProvider, get_default_provider
from axe.db.models import (
    BrokenAssumption,
    SignalLog,
    ThesisTest,
    ThesisTestResult,
    ThesisVersion,
    utc_now,
)
from axe.services.thesis import ThesisRepo

Stance = Literal["CONFIRMS", "CONTRADICTS", "NEUTRAL", "UNCERTAIN"]
STANCES: set[Stance] = {"CONFIRMS", "CONTRADICTS", "NEUTRAL", "UNCERTAIN"}

# Backwards-compatible alias used by downstream modules/tests.
DriftStance = Stance


class SignalAssumptionPair(BaseModel):
    """Structured signal/assumption classification output."""

    stance: Stance = Field(
        ...,
        description="How the signal relates to the assumption: CONFIRMS, CONTRADICTS, NEUTRAL, UNCERTAIN.",
    )
    reasoning: str = Field(..., description="One-sentence chain-of-thought.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Model confidence in stance.")
    evidence_quote: str | None = Field(
        default=None,
        description="Relevant quote or excerpt from the signal supporting the stance.",
    )


class ThesisTestEvaluation(BaseModel):
    """Structured pass/fail evaluation of a thesis test against a signal."""

    result: Literal["pass", "fail", "inconclusive"] = Field(..., description="Test result.")
    reasoning: str = Field(..., description="One-sentence chain-of-thought.")
    evidence_quote: str | None = Field(default=None, description="Relevant quote or excerpt.")


class DriftDetectionAgent:
    """Classify signals against thesis assumptions with embedding pre-filter."""

    SYSTEM_PROMPT = (
        "You compare an incoming investment signal with a stated thesis assumption. "
        "Classify the relationship as one of: CONFIRMS, CONTRADICTS, NEUTRAL, UNCERTAIN. "
        "CONFIRMS means the evidence supports the assumption. "
        "CONTRADICTS means the evidence is inconsistent with the assumption and a PM should care. "
        "NEUTRAL means the evidence is unrelated or does not change the assumption. "
        "UNCERTAIN means the evidence is ambiguous, noisy, or insufficient to call. "
        "Output JSON matching the schema. Be concise."
    )

    DEFAULT_SIMILARITY_THRESHOLD = 0.72

    def __init__(
        self,
        provider: LLMProvider | None = None,
        embedding_model: EmbeddingModel | None = None,
        similarity_threshold: float | None = None,
    ) -> None:
        self.provider = provider or get_default_provider()
        self.embedding_model = embedding_model or get_default_embedding_model()
        self.similarity_threshold = similarity_threshold or self.DEFAULT_SIMILARITY_THRESHOLD

    async def classify(
        self,
        signal_text: str,
        assumption_text: str,
    ) -> SignalAssumptionPair:
        """Classify a signal against a single assumption, pre-filtered by embedding similarity."""
        relevance = await self._relevance(signal_text, assumption_text)
        if relevance < self.similarity_threshold:
            return SignalAssumptionPair(
                stance="UNCERTAIN",
                reasoning="Embedding cosine similarity is below calibrated threshold; skipping LLM.",
                confidence=0.0,
                evidence_quote=None,
            )

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Assumption: {assumption_text}\n\n"
                    f"Signal: {signal_text}\n\n"
                    "Classify the signal's relationship to the assumption."
                ),
            },
        ]
        response = await self.provider.complete(
            messages,
            temperature=0.0,
            response_schema=SignalAssumptionPair,
        )
        parsed = response.parsed or {}
        try:
            result = SignalAssumptionPair(**parsed)
        except Exception:  # pragma: no cover - defensive fallback
            result = SignalAssumptionPair(
                stance="UNCERTAIN",
                reasoning="Provider returned unparseable output; falling back to UNCERTAIN.",
                confidence=0.0,
                evidence_quote=None,
            )
        if result.stance not in STANCES:
            result.stance = "UNCERTAIN"
        if result.confidence < 0.0 or result.confidence > 1.0:
            result.confidence = 0.5
        return result

    async def _relevance(self, signal_text: str, assumption_text: str) -> float:
        signal_embedding = await self.embedding_model.embed(signal_text)
        assumption_embedding = await self.embedding_model.embed(assumption_text)
        return cosine_similarity(signal_embedding, assumption_embedding)

    async def classify_assumptions(
        self,
        signal_text: str,
        assumptions: list[dict[str, Any]],
    ) -> list[tuple[str | None, SignalAssumptionPair]]:
        """Classify ``signal_text`` against each assumption dict.

        Assumption dicts are expected to contain a string under the key
        ``statement`` or ``text``; their ``id`` field is preserved alongside
        results.
        """
        results: list[tuple[str | None, SignalAssumptionPair]] = []
        for assumption in assumptions:
            text = str(assumption.get("statement") or assumption.get("text") or "").strip()
            assumption_id = assumption.get("id")
            if not text:
                results.append(
                    (
                        assumption_id,
                        SignalAssumptionPair(
                            stance="NEUTRAL",
                            reasoning="Assumption text empty.",
                            confidence=0.0,
                            evidence_quote=None,
                        ),
                    )
                )
                continue
            pair = await self.classify(signal_text, text)
            results.append((assumption_id, pair))
        return results


class ThesisTestAgent:
    """Maintain and evaluate testable statements for thesis assumptions."""

    SYSTEM_PROMPT = (
        "You evaluate an incoming signal against a specific thesis test statement. "
        "Return 'pass' if the signal supports the test, 'fail' if it contradicts it, "
        "or 'inconclusive' if the signal is irrelevant or ambiguous. "
        "Output JSON matching the schema and include a brief reasoning."
    )

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_default_provider()

    async def generate_tests(
        self,
        assumption: dict[str, Any],
        *,
        count: int = 2,
    ) -> list[dict[str, str]]:
        """Generate test statement(s) for an assumption.

        In a production implementation this could call an LLM; here it provides a
        deterministic set of tests based on the assumption text.
        """
        text = str(assumption.get("statement") or assumption.get("text") or "").strip()
        if not text:
            return []
        tests = []
        tests.append(
            {
                "statement": f"Evidence supports: {text}",
                "pass_criteria": f"Signal provides direct, credible evidence supporting '{text}'.",
                "fail_criteria": f"Signal provides direct, credible evidence contradicting '{text}'.",
            }
        )
        if count >= 2:
            tests.append(
                {
                    "statement": f"Evidence contradicts: {text}",
                    "pass_criteria": f"Signal provides direct, credible evidence contradicting '{text}'.",
                    "fail_criteria": f"Signal provides direct, credible evidence supporting '{text}'.",
                }
            )
        return tests

    async def ensure_tests_for_thesis(
        self,
        session: AsyncSession,
        thesis: ThesisVersion,
    ) -> list[ThesisTest]:
        """Ensure each key assumption in ``thesis`` has associated test rows."""
        existing = await session.execute(
            select(ThesisTest).where(ThesisTest.thesis_version_id == thesis.id)
        )
        existing_by_assumption: dict[str, list[ThesisTest]] = {}
        for test in existing.scalars().all():
            aid = test.assumption_id or ""
            existing_by_assumption.setdefault(aid, []).append(test)

        created: list[ThesisTest] = []
        for idx, assumption in enumerate(thesis.key_assumptions or []):
            if not isinstance(assumption, dict):
                assumption = {"statement": str(assumption)}
            assumption_id = assumption.get("id") or str(idx)
            if existing_by_assumption.get(assumption_id):
                continue
            tests = await self.generate_tests(assumption, count=2)
            for test_data in tests:
                test = ThesisTest(
                    thesis_version_id=thesis.id,
                    assumption_id=assumption_id,
                    test_statement=test_data["statement"],
                    pass_criteria=test_data.get("pass_criteria"),
                    fail_criteria=test_data.get("fail_criteria"),
                    status="open",
                )
                session.add(test)
                created.append(test)
        await session.flush()
        return created

    async def evaluate_signal(
        self,
        signal_text: str,
        test: ThesisTest,
    ) -> ThesisTestEvaluation:
        """Evaluate ``signal_text`` against a single ``ThesisTest``."""
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Test statement: {test.test_statement}\n"
                    f"Pass criteria: {test.pass_criteria or 'n/a'}\n"
                    f"Fail criteria: {test.fail_criteria or 'n/a'}\n\n"
                    f"Signal: {signal_text}\n\n"
                    "Evaluate the test result."
                ),
            },
        ]
        response = await self.provider.complete(
            messages,
            temperature=0.0,
            response_schema=ThesisTestEvaluation,
        )
        parsed = response.parsed or {}
        try:
            return ThesisTestEvaluation(**parsed)
        except Exception:  # pragma: no cover - defensive fallback
            return ThesisTestEvaluation(
                result="inconclusive",
                reasoning="Provider returned unparseable output; falling back to inconclusive.",
                evidence_quote=None,
            )

    async def evaluate_signal_against_thesis(
        self,
        session: AsyncSession,
        thesis: ThesisVersion,
        signal_text: str,
    ) -> list[tuple[ThesisTest, ThesisTestResult]]:
        """Run all tests for ``thesis`` against ``signal_text`` and persist results."""
        await self.ensure_tests_for_thesis(session, thesis)
        result = await session.execute(
            select(ThesisTest).where(ThesisTest.thesis_version_id == thesis.id)
        )
        tests = list(result.scalars().all())
        outcomes: list[tuple[ThesisTest, ThesisTestResult]] = []
        for test in tests:
            evaluation = await self.evaluate_signal(signal_text, test)
            outcome = ThesisTestResult(
                test_id=test.id,
                result=evaluation.result,
                evidence=evaluation.evidence_quote,
            )
            session.add(outcome)
            outcomes.append((test, outcome))
        await session.flush()
        return outcomes


class EarningsAlertService:
    """Alert PMs when a Polygon earnings signal contradicts a thesis assumption."""

    ALERT_SLA_SECONDS = 30 * 60  # 30 minutes

    def __init__(
        self,
        session: AsyncSession,
        drift_agent: DriftDetectionAgent | None = None,
    ) -> None:
        self.session = session
        self.drift_agent = drift_agent or DriftDetectionAgent()

    async def process_signal(
        self,
        pm_id: str,
        ticker: str,
        source_type: str,
        source_url: str | None,
        signal_text: str,
        signal_id: str | None = None,
        raw_content: str | None = None,
        content_hash: str | None = None,
        arrived_at: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate ``signal_text`` and fire alerts for new contradictions.

        Returns a list of alert payloads that were created; callers are
        responsible for dispatching via Slack/email.
        """
        alerts: list[dict[str, Any]] = []
        if source_type != "polygon":
            return alerts

        repo = ThesisRepo(self.session, pm_id, "")
        latest = await repo.get_latest_thesis(ticker)
        if latest is None or latest.is_draft:
            return alerts

        assumptions = latest.key_assumptions or []
        if not assumptions:
            return alerts

        classifications = await self.drift_agent.classify_assumptions(signal_text, assumptions)
        broken_assumption_ids = await self._broken_assumption_ids(pm_id, ticker)

        for assumption_id, pair in classifications:
            if pair.stance != "CONTRADICTS":
                continue

            # Deduplicate already-broken assumptions.
            if assumption_id in broken_assumption_ids:
                continue

            signal = SignalLog(
                id=signal_id or str(uuid.uuid4()),
                pm_id=pm_id,
                ticker=ticker,
                source_type=source_type,
                content_hash=content_hash or "",
                raw_content=raw_content or signal_text,
                extracted_signal={"stance": pair.stance, "reasoning": pair.reasoning},
                citation={"url": source_url},
                relevance_score=await self.drift_agent._relevance(signal_text, self._assumption_text(assumptions, assumption_id)),
                thesis_assumption_id=assumption_id,
                stance=pair.stance,
                extraction_confidence=pair.confidence,
                alerted=False,
                created_at=arrived_at or utc_now(),
            )
            self.session.add(signal)
            await self.session.flush()

            alert = self._build_alert_payload(ticker, latest, assumption_id, pair, source_url, signal.id)
            alerts.append(alert)
            signal.alerted = True

            # Record the broken assumption so later signals do not re-alert.
            broken = BrokenAssumption(
                pm_id=pm_id,
                ticker=ticker,
                assumption_id=assumption_id or "",
                signal_id=signal.id,
            )
            self.session.add(broken)

        await self.session.flush()
        return alerts

    async def _broken_assumption_ids(
        self,
        pm_id: str,
        ticker: str,
    ) -> set[str | None]:
        """Return assumption IDs already marked as broken for this ticker."""
        result = await self.session.execute(
            select(SignalLog)
            .where(
                SignalLog.pm_id == pm_id,
                SignalLog.ticker == ticker,
                SignalLog.stance == "CONTRADICTS",
                SignalLog.alerted.is_(True),
            )
        )
        broken_ids = {row.thesis_assumption_id for row in result.scalars().all()}

        broken_rows = await self.session.execute(
            select(BrokenAssumption).where(
                BrokenAssumption.pm_id == pm_id,
                BrokenAssumption.ticker == ticker,
            )
        )
        broken_ids.update(
            row.assumption_id or None for row in broken_rows.scalars().all()
        )
        return broken_ids

    @staticmethod
    def _assumption_text(
        assumptions: list[dict[str, Any]],
        assumption_id: str | None,
    ) -> str:
        for assumption in assumptions:
            if not isinstance(assumption, dict):
                continue
            if assumption.get("id") == assumption_id:
                return str(assumption.get("statement") or assumption.get("text") or "")
        return ""

    @staticmethod
    def _build_alert_payload(
        ticker: str,
        thesis: ThesisVersion,
        assumption_id: str | None,
        pair: SignalAssumptionPair,
        source_url: str | None,
        signal_id: str,
    ) -> dict[str, Any]:
        assumption_text = EarningsAlertService._assumption_text(
            thesis.key_assumptions or [], assumption_id
        )
        link = f" [source link: {source_url}]" if source_url else ""
        body = (
            f"[{ticker}] THESIS ALERT — {assumption_text} may be breaking. "
            f"Evidence: {pair.evidence_quote or pair.reasoning}{link}"
        )
        return {
            "ticker": ticker,
            "pm_id": thesis.pm_id,
            "assumption_id": assumption_id,
            "assumption": assumption_text,
            "signal_id": signal_id,
            "source_url": source_url,
            "stance": pair.stance,
            "message": body,
            "slack_enabled": True,
            "email_enabled": True,
        }


__all__ = [
    "DriftDetectionAgent",
    "EarningsAlertService",
    "SignalAssumptionPair",
    "ThesisTestAgent",
    "ThesisTestEvaluation",
]
