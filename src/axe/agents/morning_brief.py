"""MorningBriefAgent — generate personalized AM briefs scored against thesis + memory."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.embedding import EmbeddingModel, cosine_similarity, get_default_embedding_model
from axe.agents.llm import LLMProvider, get_default_provider
from axe.agents.persona_models import PersonaStyleSnapshot
from axe.db.models import (
    CatalystEvent,
    MorningBrief,
    PMMemory,
    PMPersona,
    PMUser,
    SignalLog,
    SpecialistSignal,
    ThesisVersion,
    TickerRegistry,
)

logger = logging.getLogger(__name__)


class BriefSection(BaseModel):
    """One section of the morning brief tied to a thesis assumption."""

    ticker: str
    assumption_id: str
    assumption_text: str
    headline: str
    body: str
    source_ids: list[str] = Field(default_factory=list)
    stance: str = "NEUTRAL"
    relevance_score: float = 0.0


class FocusOne(BaseModel):
    """Single ticker needing the most attention today."""

    ticker: str
    reason: str
    urgency_score: float = 0.0


class CatalystItem(BaseModel):
    """A catalyst event for the week."""

    date: str
    event_type: str
    ticker: str | None
    description: str
    source_url: str | None


class SpecialistSignalItem(BaseModel):
    """One curated specialist signal surfaced in the morning brief."""

    id: str
    ticker: str
    source_type: str
    specialist_agent: str
    signal_type: str
    summary: str
    stance: str
    confidence: float
    evidence_json: dict[str, Any] = Field(default_factory=dict)


class MorningBriefOutput(BaseModel):
    """Structured morning brief."""

    sections: list[BriefSection] = Field(default_factory=list)
    specialist_signals: list[SpecialistSignalItem] = Field(default_factory=list)
    focus_one: FocusOne | None = None
    catalyst_week: list[CatalystItem] = Field(default_factory=list)


class ScoreSignalRequest(BaseModel):
    """Schema for LLM signal relevance scoring."""

    relevance_score: float = Field(
        ..., ge=0.0, le=1.0, description="How relevant the signal is to this assumption"
    )
    is_generic_macro: bool = Field(
        ..., description="True if the signal is a generic macro headline with no thesis link"
    )
    stance: str = Field(..., pattern="^(CONFIRMS|CONTRADICTS|NEUTRAL|UNCERTAIN)$")
    assumption_id: str
    reason: str


class MorningBriefAgent:
    """Gather last-24h signals, score vs. thesis + memory, and generate a brief."""

    def __init__(
        self,
        session: AsyncSession,
        llm: LLMProvider | None = None,
        embedding: EmbeddingModel | None = None,
        similarity_threshold: float = 0.72,
    ) -> None:
        self.session = session
        self.llm = llm or get_default_provider()
        self.embedding = embedding or get_default_embedding_model()
        self.similarity_threshold = similarity_threshold

    async def generate(
        self,
        pm_id: str,
        as_of: datetime | None = None,
        persona: PersonaStyleSnapshot | None = None,
    ) -> MorningBriefOutput:
        """Generate a brief for ``pm_id`` scoped to the 24h window ending at ``as_of``."""
        as_of = as_of or datetime.now(UTC)
        window_start = as_of - timedelta(hours=24)

        await self._get_user(pm_id)
        tickers = await self._get_tickers(pm_id)
        theses = await self._get_theses(pm_id)
        memory = await self._get_memory(pm_id)
        persona = persona or await self._get_persona(pm_id)
        signals = await self._get_recent_signals(pm_id, window_start, as_of)
        specialist_signals = await self._get_recent_specialist_signals(pm_id, window_start, as_of)
        catalysts = await self._get_catalysts(as_of.date())

        scored = await self._score_signals(
            signals=signals,
            theses=theses,
            tickers=tickers,
            memory=memory,
            persona=persona,
        )

        sections = self._build_sections(scored, theses)
        focus_one = self._pick_focus_one(sections, tickers, theses, memory)
        curated = self._curate_specialist_signals(specialist_signals, tickers)
        brief = MorningBriefOutput(
            sections=sections,
            specialist_signals=curated,
            focus_one=focus_one,
            catalyst_week=catalysts,
        )
        return brief

    async def save_and_deliver(
        self,
        pm_id: str,
        brief: MorningBriefOutput,
        deliver_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> MorningBrief:
        """Persist the brief, optionally deliver via ``deliver_fn`` and record result."""
        brief_record = MorningBrief(
            pm_id=pm_id,
            date=datetime.now(UTC).date(),
            sections=[s.model_dump() for s in brief.sections],
            focus_one=brief.focus_one.model_dump() if brief.focus_one else {},
            catalyst_week=[c.model_dump() for c in brief.catalyst_week],
            delivered_slack=False,
            delivered_email=False,
        )
        self.session.add(brief_record)
        await self.session.flush()

        if deliver_fn is not None:
            delivery_result = await deliver_fn(brief)
            brief_record.delivered_slack = bool(delivery_result.get("slack_ok"))
            brief_record.delivered_email = bool(delivery_result.get("email_ok"))

        await self.session.commit()
        return brief_record

    async def _get_user(self, pm_id: str) -> PMUser | None:
        result = await self.session.execute(select(PMUser).where(PMUser.id == pm_id))
        return result.scalar_one_or_none()

    async def _get_tickers(self, pm_id: str) -> list[TickerRegistry]:
        result = await self.session.execute(
            select(TickerRegistry).where(
                TickerRegistry.pm_id == pm_id,
                TickerRegistry.active == True,  # noqa: E712
            )
        )
        return list(result.scalars().all())

    async def _get_theses(self, pm_id: str) -> list[ThesisVersion]:
        result = await self.session.execute(
            select(ThesisVersion).where(
                ThesisVersion.pm_id == pm_id,
                ThesisVersion.is_draft == False,  # noqa: E712
                ThesisVersion.status.in_(["active", "open"]),
            )
        )
        return list(result.scalars().all())

    async def _get_memory(self, pm_id: str) -> PMMemory | None:
        result = await self.session.execute(
            select(PMMemory).where(PMMemory.pm_id == pm_id).order_by(PMMemory.version.desc())
        )
        return result.scalars().first()

    async def _get_persona(self, pm_id: str) -> PersonaStyleSnapshot | None:
        """Load the current persona snapshot for the PM, if one exists."""
        from axe.agents.persona import PersonaAgent

        result = await self.session.execute(
            select(PMPersona).where(PMPersona.pm_id == pm_id).order_by(PMPersona.created_at.desc())
        )
        model = result.scalars().first()
        if model is None:
            return None
        return PersonaAgent.snapshot_from_model(model)

    async def _get_recent_signals(
        self,
        pm_id: str,
        start: datetime,
        end: datetime,
    ) -> list[SignalLog]:
        result = await self.session.execute(
            select(SignalLog).where(
                SignalLog.pm_id == pm_id,
                SignalLog.created_at >= start,
                SignalLog.created_at <= end,
            )
        )
        return list(result.scalars().all())

    async def _get_recent_specialist_signals(
        self,
        pm_id: str,
        start: datetime,
        end: datetime,
    ) -> list[SpecialistSignal]:
        result = await self.session.execute(
            select(SpecialistSignal).where(
                SpecialistSignal.pm_id == pm_id,
                SpecialistSignal.created_at >= start,
                SpecialistSignal.created_at <= end,
            )
        )
        return list(result.scalars().all())

    def _curate_specialist_signals(
        self,
        signals: list[SpecialistSignal],
        tickers: list[TickerRegistry],
    ) -> list[SpecialistSignalItem]:
        """Return top 5 specialist signals filtered to active tickers and sorted by confidence."""
        active = {t.ticker for t in tickers if t.active}
        curated: list[SpecialistSignalItem] = []
        for signal in signals:
            ticker = signal.ticker
            if not ticker or ticker not in active:
                continue
            curated.append(
                SpecialistSignalItem(
                    id=signal.id,
                    ticker=ticker,
                    source_type=signal.source_type,
                    specialist_agent=signal.specialist_agent,
                    signal_type=signal.signal_type,
                    summary=signal.summary or "",
                    stance=signal.stance or "NEUTRAL",
                    confidence=signal.confidence or 0.0,
                    evidence_json=signal.evidence_json or {},
                )
            )
        curated.sort(key=lambda s: s.confidence, reverse=True)
        return curated[:5]

    async def _get_catalysts(self, as_of: date) -> list[CatalystItem]:
        """Return upcoming catalysts for the rest of the current week."""
        week_end = as_of + timedelta(days=7)
        result = await self.session.execute(
            select(CatalystEvent)
            .where(
                CatalystEvent.event_date >= as_of,
                CatalystEvent.event_date <= week_end,
            )
            .order_by(CatalystEvent.event_date)
        )
        events = result.scalars().all()
        return [
            CatalystItem(
                date=str(e.event_date),
                event_type=e.event_type,
                ticker=e.ticker,
                description=e.description or "",
                source_url=e.source_url,
            )
            for e in events
        ]

    async def _score_signals(
        self,
        signals: list[SignalLog],
        theses: list[ThesisVersion],
        tickers: list[TickerRegistry],
        memory: PMMemory | None,
        persona: PersonaStyleSnapshot | None = None,
    ) -> list[tuple[SignalLog, ThesisVersion, dict[str, Any], float]]:
        """Return scored tuples: (signal, thesis, assumption, relevance_score)."""
        scored: list[tuple[SignalLog, ThesisVersion, dict[str, Any], float]] = []
        ticker_set = {t.ticker for t in tickers}

        for signal in signals:
            ticker = signal.ticker
            # Drop signals not on the book (unless they are explicitly linked to a thesis).
            if ticker not in ticker_set and not signal.thesis_assumption_id:
                logger.debug("Skipping unrelated signal %s ticker %s", signal.id, ticker)
                continue

            thesis = next((t for t in theses if t.ticker == ticker), None)
            if not thesis:
                continue

            assumptions = thesis.key_assumptions or []
            for assumption in assumptions:
                assumption_text = (
                    assumption.get("text", "") if isinstance(assumption, dict) else str(assumption)
                )
                assumption_id = assumption.get("id", "") if isinstance(assumption, dict) else ""
                if not assumption_text:
                    continue

                sim = await self._embedding_similarity(
                    signal.raw_content or signal.extracted_signal.get("summary", ""),
                    assumption_text,
                )
                if sim < self.similarity_threshold:
                    continue

                score_meta = await self._llm_score_signal(
                    signal=signal,
                    thesis=thesis,
                    assumption_text=assumption_text,
                    memory=memory,
                    persona=persona,
                )
                scored.append(
                    (
                        signal,
                        thesis,
                        {"id": assumption_id, "text": assumption_text},
                        float(score_meta["relevance_score"]),
                    )
                )

        scored.sort(key=lambda x: x[3], reverse=True)
        return scored

    async def _embedding_similarity(self, signal_text: str, assumption_text: str) -> float:
        try:
            a = await self.embedding.embed(signal_text)
            b = await self.embedding.embed(assumption_text)
            return cosine_similarity(a, b)
        except Exception:
            logger.exception("Embedding similarity failed; defaulting to 0")
            return 0.0

    async def _llm_score_signal(
        self,
        signal: SignalLog,
        thesis: ThesisVersion,
        assumption_text: str,
        memory: PMMemory | None,
        persona: PersonaStyleSnapshot | None = None,
    ) -> dict[str, Any]:
        memory_context = ""
        if memory:
            profile = memory.profile or {}
            ticker_mem = (memory.ticker_memories or {}).get(thesis.ticker, {})
            memory_context = (
                f"PM priorities: {profile.get('summary', '')}. "
                f"Ticker memory for {thesis.ticker}: {ticker_mem}."
            )

        persona_snippet = ""
        if persona:
            persona_snippet = persona.render_system_prompt_snippet()

        prompt = (
            "You score how much the following signal matters to a specific thesis assumption.\n\n"
            f"Ticker: {thesis.ticker}\n"
            f"Thesis direction: {thesis.direction}\n"
            f"Assumption: {assumption_text}\n\n"
            f"Signal source: {signal.source_type}\n"
            f"Signal content: {signal.raw_content or signal.extracted_signal.get('summary', '')}\n\n"
            f"Memory context: {memory_context}\n\n"
        )
        if persona_snippet:
            prompt += f"PM persona guidance: {persona_snippet}\n\n"
        prompt += (
            "Return a JSON object with: relevance_score (0.0-1.0), is_generic_macro (bool), "
            "stance (CONFIRMS|CONTRADICTS|NEUTRAL|UNCERTAIN), and reason."
        )

        try:
            response = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_schema=ScoreSignalRequest,
            )
            parsed = response.parsed or {}
            if not isinstance(parsed, dict):
                return self._default_score()
            return {
                "relevance_score": float(parsed.get("relevance_score", 0.5)),
                "is_generic_macro": bool(parsed.get("is_generic_macro", False)),
                "stance": parsed.get("stance", "NEUTRAL"),
                "reason": str(parsed.get("reason", "")),
            }
        except Exception:
            logger.exception("LLM scoring failed; returning default score")
            return self._default_score()

    @staticmethod
    def _default_score() -> dict[str, Any]:
        return {
            "relevance_score": 0.5,
            "is_generic_macro": False,
            "stance": "NEUTRAL",
            "reason": "Fallback default score",
        }

    def _build_sections(
        self,
        scored: list[tuple[SignalLog, ThesisVersion, dict[str, Any], float]],
        theses: list[ThesisVersion],
    ) -> list[BriefSection]:
        sections_by_assumption: dict[str, BriefSection] = {}

        for signal, thesis, assumption, score in scored:
            key = f"{thesis.ticker}:{assumption['id']}"
            existing = sections_by_assumption.get(key)
            if existing and existing.relevance_score >= score:
                existing.source_ids.append(signal.id)
                continue

            sections_by_assumption[key] = BriefSection(
                ticker=thesis.ticker,
                assumption_id=assumption["id"],
                assumption_text=assumption["text"],
                headline=signal.extracted_signal.get("headline", "New signal") or "New signal",
                body=signal.extracted_signal.get("summary", signal.raw_content or ""),
                source_ids=[signal.id],
                stance=signal.stance or "NEUTRAL",
                relevance_score=score,
            )

        # Merge any thesis without signals into a holding section so the brief maps every assumption.
        for thesis in theses:
            for assumption in thesis.key_assumptions or []:
                if not isinstance(assumption, dict):
                    continue
                assumption_id = assumption.get("id", "")
                key = f"{thesis.ticker}:{assumption_id}"
                if key not in sections_by_assumption:
                    sections_by_assumption[key] = BriefSection(
                        ticker=thesis.ticker,
                        assumption_id=assumption_id,
                        assumption_text=assumption.get("text", ""),
                        headline="No new signals overnight",
                        body="No signals in the last 24h matched this assumption.",
                        source_ids=[],
                        stance="NEUTRAL",
                        relevance_score=0.0,
                    )

        return sorted(
            sections_by_assumption.values(), key=lambda s: s.relevance_score, reverse=True
        )

    def _pick_focus_one(
        self,
        sections: list[BriefSection],
        tickers: list[TickerRegistry],
        theses: list[ThesisVersion],
        memory: PMMemory | None,
    ) -> FocusOne | None:
        if not sections:
            return None

        # Highest relevance contradiction wins.
        contradictions = [s for s in sections if s.stance == "CONTRADICTS"]
        if contradictions:
            top = contradictions[0]
            return FocusOne(
                ticker=top.ticker,
                reason=f"Contradicting signal on assumption: {top.assumption_text}",
                urgency_score=top.relevance_score,
            )

        # Otherwise pick highest-scoring confirm/uncertain.
        top = sections[0]
        if top.relevance_score <= 0.0 and tickers:
            # Fallback to largest position bucket if available.
            biggest = max(
                tickers,
                key=lambda t: {"small": 1, "medium": 2, "large": 3}.get(
                    t.position_size_bucket or "", 0
                ),
            )
            return FocusOne(
                ticker=biggest.ticker,
                reason="Largest position; no new signals overnight.",
                urgency_score=0.0,
            )

        reason = (
            f"Uncertain signal on {top.assumption_text}"
            if top.stance == "UNCERTAIN"
            else f"Confirms key assumption: {top.assumption_text}"
        )
        return FocusOne(
            ticker=top.ticker,
            reason=reason,
            urgency_score=top.relevance_score,
        )


def is_nyse_trading_day(d: date) -> bool:
    """Return True if ``d`` is a weekday and not a fixed-date NYSE holiday."""
    if d.weekday() >= 5:
        return False
    # Approx fixed-date NYSE closures.
    fixed_holidays = {(1, 1), (7, 4), (12, 25)}
    return (d.month, d.day) not in fixed_holidays
