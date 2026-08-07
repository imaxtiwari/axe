"""Specialist signal agents convert raw ingestion outputs into structured signals.

Each ``SpecialistSignalAgent`` focuses on one inbound source type (earnings,
research, expert network, broker feed, PDF deck, CRM). The agent receives a
``RawIngest`` row plus an ``AgentContext`` and emits ``SpecialistSignalOutput``
records that downstream drift detection and morning brief generation can consume.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from axe.agents.llm import LLMProvider, MockProvider, get_default_provider
from axe.db.models import RawIngest, SpecialistSignal, ThesisVersion
from axe.db.uow import UnitOfWork


class SpecialistSignalOutput(BaseModel):
    """Normalized output produced by a specialist agent.

    This schema mirrors the ``SpecialistSignal`` SQLAlchemy model so that a
    repository row can be created directly from the output.
    """

    ticker: str | None = Field(default=None, description="Ticker symbol if known.")
    source_type: str = Field(..., description="Inbound source type.")
    specialist_agent: str = Field(..., description="Class name of the specialist.")
    signal_type: str = Field(..., description="Signal category, e.g. earnings_update.")
    summary: str = Field(..., description="Concise human-readable signal summary.")
    stance: str = Field(
        default="NEUTRAL",
        description="Signal stance relative to the bull case for the ticker.",
    )
    confidence: float = Field(default=0.5, description="Confidence in the signal interpretation.")
    evidence_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured evidence extracted from the raw payload.",
    )
    assumptions_touched: list[Any] = Field(
        default_factory=list,
        description="Assumption ids/texts the signal may bear on.",
    )

    @field_validator("stance")
    @classmethod
    def _normalize_stance(cls, value: str | None) -> str:
        normalized = (value or "NEUTRAL").upper()
        allowed = {"CONFIRMS", "CONTRADICTS", "NEUTRAL", "UNCERTAIN"}
        return normalized if normalized in allowed else "NEUTRAL"

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float | None) -> float:
        if value is None:
            return 0.5
        return max(0.0, min(1.0, float(value)))


@dataclass
class AgentContext:
    """Context supplied to a specialist when processing a raw ingest."""

    pm_id: str
    fund_id: str | None = None
    persona: dict[str, Any] | None = None
    active_tickers: set[str] = field(default_factory=set)
    recent_theses: list[dict[str, Any]] = field(default_factory=list)

    def ticker_is_active(self, ticker: str | None) -> bool:
        """Return True when ``ticker`` is on the PM's active list or no list exists."""
        if not ticker:
            return False
        return not self.active_tickers or ticker in self.active_tickers


class SpecialistSignalAgent(ABC):
    """Abstract base class for source-specific specialist signal agents."""

    source_type: ClassVar[str]
    specialist_name: ClassVar[str]
    default_signal_type: ClassVar[str] = "signal"

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_default_provider()

    @abstractmethod
    async def process(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
    ) -> list[SpecialistSignalOutput]: ...

    @classmethod
    def _build_context_prompt(cls, context: AgentContext) -> str:
        persona = context.persona or {}
        triggers = persona.get("decision_triggers", {})
        priorities = triggers.get("priorities", [])
        priority_text = ", ".join(str(p) for p in priorities) if priorities else "none"
        recent_tickers = [t.get("ticker") for t in context.recent_theses if t.get("ticker")]
        return (
            f"Active tickers: {sorted(context.active_tickers) or 'unknown'}. "
            f"PM priorities: {priority_text}. "
            f"Recent thesis tickers: {recent_tickers or 'none'}."
        )

    def _normalize_ticker(self, ticker: str | None) -> str | None:
        if not ticker:
            return None
        ticker = ticker.strip().upper()
        for suffix in [":US", " US", "-US", ".US", " EQUITY", " US Equity"]:
            if suffix in ticker:
                ticker = ticker.split(suffix)[0]
        return ticker or None

    def _default_output(
        self,
        raw_ingest: RawIngest,
        summary: str,
        signal_type: str | None = None,
        stance: str = "NEUTRAL",
        confidence: float = 0.5,
        evidence_json: dict[str, Any] | None = None,
        assumptions_touched: list[Any] | None = None,
    ) -> SpecialistSignalOutput:
        return SpecialistSignalOutput(
            ticker=self._normalize_ticker(raw_ingest.extracted_signal_json.get("ticker")),
            source_type=raw_ingest.source_type,
            specialist_agent=self.specialist_name,
            signal_type=signal_type or self.default_signal_type,
            summary=summary,
            stance=stance,
            confidence=confidence,
            evidence_json=evidence_json or {},
            assumptions_touched=assumptions_touched or [],
        )


class EarningsSpecialist(SpecialistSignalAgent):
    """Specialist for Polygon/earnings source_type signals."""

    source_type = "polygon"
    specialist_name = "EarningsSpecialist"
    default_signal_type = "earnings_update"

    async def process(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
    ) -> list[SpecialistSignalOutput]:
        payload = raw_ingest.raw_payload_json or {}
        extracted = raw_ingest.extracted_signal_json or {}
        ticker = self._normalize_ticker(extracted.get("ticker"))

        # Deterministic fallback: if the payload contains explicit revenue/eps
        # beats or misses, produce a structured signal without calling an LLM.
        headline = str(extracted.get("headline") or payload.get("title") or "").strip()
        summary = str(extracted.get("summary") or payload.get("summary") or headline or "").strip()
        evidence = {
            "source": "polygon",
            "headline": headline,
            "summary": summary,
            "ticker": ticker,
        }
        stance, confidence = self._classify_text(summary)

        if not summary:
            return []

        if not isinstance(self.provider, MockProvider):
            # Structured LLM path for richer extraction.
            llm_out = await self._extract_with_llm(raw_ingest, context, summary)
            if llm_out:
                return [llm_out]

        return [
            self._default_output(
                raw_ingest,
                summary=summary,
                stance=stance,
                confidence=confidence,
                evidence_json=evidence,
            )
        ]

    async def _extract_with_llm(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
        summary: str,
    ) -> SpecialistSignalOutput | None:
        schema = SpecialistSignalOutput
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an earnings signal specialist. Extract a structured "
                    "investment signal from the raw earnings payload. Set stance to "
                    "CONFIRMS/CONTRADICTS/NEUTRAL/UNCERTAIN relative to the bull case."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{self._build_context_prompt(context)}\n\n"
                    f"Payload: {raw_ingest.raw_payload_json}\n"
                    f"Extracted signal: {raw_ingest.extracted_signal_json}\n\n"
                    "Return JSON matching the schema."
                ),
            },
        ]
        try:
            response = await self.provider.complete(
                messages, temperature=0.0, response_schema=schema
            )
            parsed = response.parsed or {}
            if not isinstance(parsed, dict):
                return None
            return SpecialistSignalOutput(
                ticker=self._normalize_ticker(parsed.get("ticker"))
                or self._normalize_ticker(raw_ingest.extracted_signal_json.get("ticker")),
                source_type=self.source_type,
                specialist_agent=self.specialist_name,
                signal_type=parsed.get("signal_type") or self.default_signal_type,
                summary=parsed.get("summary") or summary,
                stance=parsed.get("stance", "NEUTRAL"),
                confidence=float(parsed.get("confidence", 0.5)),
                evidence_json=parsed.get("evidence_json") or {},
                assumptions_touched=parsed.get("assumptions_touched") or [],
            )
        except Exception:
            return None

    @staticmethod
    def _classify_text(text: str) -> tuple[str, float]:
        lowered = text.lower()
        contradicting = any(w in lowered for w in ("miss", "decline", "drop", "fall", "cut"))
        confirming = any(w in lowered for w in ("beat", "growth", "rise", "increase", "raise"))
        if contradicting and not confirming:
            return "CONTRADICTS", 0.75
        if confirming and not contradicting:
            return "CONFIRMS", 0.75
        return "NEUTRAL", 0.5


class ResearchEdgeSpecialist(SpecialistSignalAgent):
    """Specialist for research edge / Smartkarma research notes."""

    source_type = "research_edge"
    specialist_name = "ResearchEdgeSpecialist"
    default_signal_type = "research_note"

    async def process(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
    ) -> list[SpecialistSignalOutput]:
        extracted = raw_ingest.extracted_signal_json or {}
        ticker = self._normalize_ticker(extracted.get("ticker"))
        title = str(extracted.get("title") or "").strip()
        published_at = extracted.get("published_at")

        # Build a deterministic summary from title + payload body.
        body = self._extract_body(raw_ingest.raw_payload_json)
        summary = title or body[:200]
        if not summary:
            return []

        stance, confidence = self._classify_text(body or title)

        return [
            self._default_output(
                raw_ingest,
                summary=summary,
                signal_type=self.default_signal_type,
                stance=stance,
                confidence=confidence,
                evidence_json={
                    "title": title,
                    "published_at": published_at,
                    "ticker": ticker,
                    "source_label": "research_edge",
                },
            )
        ]

    @staticmethod
    def _extract_body(payload: dict[str, Any]) -> str:
        if isinstance(payload, dict):
            return str(payload.get("body") or payload.get("summary") or "").strip()
        return ""

    @staticmethod
    def _classify_text(text: str) -> tuple[str, float]:
        lowered = text.lower()
        bearish = any(w in lowered for w in ("sell", "downgrade", "bearish", "overvalued"))
        bullish = any(w in lowered for w in ("buy", "upgrade", "bullish", "undervalued"))
        if bearish and not bullish:
            return "CONTRADICTS", 0.65
        if bullish and not bearish:
            return "CONFIRMS", 0.65
        return "NEUTRAL", 0.5


class ExpertNetworkSpecialist(SpecialistSignalAgent):
    """Specialist for expert network transcripts / interview turns."""

    source_type = "expert_network"
    specialist_name = "ExpertNetworkSpecialist"
    default_signal_type = "expert_transcript"

    async def process(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
    ) -> list[SpecialistSignalOutput]:
        extracted = raw_ingest.extracted_signal_json or {}
        payload = raw_ingest.raw_payload_json or {}
        ticker = self._normalize_ticker(extracted.get("ticker"))

        raw_text = str(payload.get("raw_text") or "")
        question = str(payload.get("question") or "")
        answer = str(payload.get("answer") or "")
        text = raw_text or f"{question}\n{answer}".strip()

        if not text:
            return []

        summary = text[:300]
        stance, confidence = self._classify_text(text)

        return [
            self._default_output(
                raw_ingest,
                summary=summary,
                signal_type=self.default_signal_type,
                stance=stance,
                confidence=confidence,
                evidence_json={
                    "provider": extracted.get("provider") or payload.get("provider"),
                    "ticker": ticker,
                    "transcript_date": extracted.get("transcript_date"),
                    "has_turn": bool(question or answer),
                },
            )
        ]

    @staticmethod
    def _classify_text(text: str) -> tuple[str, float]:
        lowered = text.lower()
        bearish = any(w in lowered for w in ("weak", "soft", "declining", "negative", "concern"))
        bullish = any(
            w in lowered for w in ("strong", "robust", "growing", "positive", "confident")
        )
        if bearish and not bullish:
            return "CONTRADICTS", 0.6
        if bullish and not bearish:
            return "CONFIRMS", 0.6
        return "NEUTRAL", 0.5


class BrokerSpecialist(SpecialistSignalAgent):
    """Specialist for broker feed / statement rows."""

    source_type = "broker_feed"
    specialist_name = "BrokerSpecialist"
    default_signal_type = "position_activity"

    async def process(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
    ) -> list[SpecialistSignalOutput]:
        extracted = raw_ingest.extracted_signal_json or {}
        payload = raw_ingest.raw_payload_json or {}
        ticker = self._normalize_ticker(extracted.get("ticker"))
        quantity = extracted.get("quantity") or payload.get("quantity")
        price = extracted.get("price") or payload.get("price")
        date = extracted.get("date") or payload.get("date")

        action = self._infer_action(quantity)
        summary = f"Broker {action} for {ticker or 'unknown'}"
        if quantity is not None:
            summary += f" (qty {quantity})"
        if price is not None:
            summary += f" @ {price}"

        return [
            self._default_output(
                raw_ingest,
                summary=summary,
                signal_type=self.default_signal_type,
                stance="NEUTRAL",
                confidence=0.5,
                evidence_json={
                    "ticker": ticker,
                    "quantity": quantity,
                    "price": price,
                    "date": date,
                    "action": action,
                },
            )
        ]

    @staticmethod
    def _infer_action(quantity: Any) -> str:
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            return "activity"
        if qty > 0:
            return "buy"
        if qty < 0:
            return "sell"
        return "activity"


class PDFDeckSpecialist(SpecialistSignalAgent):
    """Specialist for PDF pitch decks / CIMs."""

    source_type = "pdf_deck"
    specialist_name = "PDFDeckSpecialist"
    default_signal_type = "deck_excerpt"

    async def process(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
    ) -> list[SpecialistSignalOutput]:
        extracted = raw_ingest.extracted_signal_json or {}
        payload = raw_ingest.raw_payload_json or {}
        text = str(extracted.get("text") or "").strip()
        page = payload.get("page")
        if not text:
            return []

        summary = text[:300]
        stance, confidence = self._classify_text(text)

        return [
            self._default_output(
                raw_ingest,
                summary=summary,
                signal_type=self.default_signal_type,
                stance=stance,
                confidence=confidence,
                evidence_json={
                    "page": page,
                    "byte_size": payload.get("byte_size"),
                    "mime_type": payload.get("mime_type"),
                },
            )
        ]

    @staticmethod
    def _classify_text(text: str) -> tuple[str, float]:
        lowered = text.lower()
        bearish = any(w in lowered for w in ("risk", "decline", "competition", "lawsuit"))
        bullish = any(w in lowered for w in ("growth", "opportunity", "traction", "momentum"))
        if bearish and not bullish:
            return "CONTRADICTS", 0.55
        if bullish and not bearish:
            return "CONFIRMS", 0.55
        return "NEUTRAL", 0.5


class CRMSpecialist(SpecialistSignalAgent):
    """Specialist for CRM records (activities, contacts, opportunities, notes)."""

    source_type = "crm"
    specialist_name = "CRMSpecialist"
    default_signal_type = "crm_activity"

    async def process(
        self,
        raw_ingest: RawIngest,
        context: AgentContext,
    ) -> list[SpecialistSignalOutput]:
        extracted = raw_ingest.extracted_signal_json or {}
        payload = raw_ingest.raw_payload_json or {}
        record_type = extracted.get("record_type") or payload.get("record_type") or "activity"
        ticker = self._normalize_ticker(extracted.get("ticker"))

        text_parts = [
            str(payload.get(field) or "")
            for field in ("subject", "description", "body")
            if payload.get(field)
        ]
        summary = " | ".join(text_parts) or f"CRM {record_type}"
        summary = summary[:300]

        stance, confidence = self._classify_text(summary)

        return [
            self._default_output(
                raw_ingest,
                summary=summary,
                signal_type=f"crm_{record_type}",
                stance=stance,
                confidence=confidence,
                evidence_json={
                    "record_type": record_type,
                    "ticker": ticker,
                    "subject": payload.get("subject"),
                },
            )
        ]

    @staticmethod
    def _classify_text(text: str) -> tuple[str, float]:
        lowered = text.lower()
        bearish = any(w in lowered for w in ("complaint", "churn", "delay", "cancel"))
        bullish = any(w in lowered for w in ("expansion", "renewal", "new deal", "win"))
        if bearish and not bullish:
            return "CONTRADICTS", 0.6
        if bullish and not bearish:
            return "CONFIRMS", 0.6
        return "NEUTRAL", 0.5


class SpecialistSignalRegistry:
    """Registry mapping ``source_type`` strings to specialist agent classes."""

    def __init__(self) -> None:
        self._agents: dict[str, type[SpecialistSignalAgent]] = {}

    def register(self, agent_cls: type[SpecialistSignalAgent]) -> type[SpecialistSignalAgent]:
        """Register ``agent_cls`` keyed by its ``source_type``."""
        self._agents[agent_cls.source_type] = agent_cls
        return agent_cls

    def get(
        self,
        source_type: str,
    ) -> type[SpecialistSignalAgent] | None:
        return self._agents.get(source_type)

    def build(
        self,
        source_type: str,
        provider: LLMProvider | None = None,
    ) -> SpecialistSignalAgent | None:
        """Instantiate a registered specialist for ``source_type``."""
        cls = self.get(source_type)
        if cls is None:
            return None
        return cls(provider=provider)

    @property
    def source_types(self) -> set[str]:
        return set(self._agents.keys())


def default_registry() -> SpecialistSignalRegistry:
    """Return the standard AXE specialist signal registry."""
    registry = SpecialistSignalRegistry()
    for cls in (
        EarningsSpecialist,
        ResearchEdgeSpecialist,
        ExpertNetworkSpecialist,
        BrokerSpecialist,
        PDFDeckSpecialist,
        CRMSpecialist,
    ):
        registry.register(cls)
    return registry


def build_agent_context(
    pm_id: str,
    *,
    fund_id: str | None = None,
    persona: dict[str, Any] | None = None,
    active_tickers: list[str] | set[str] | None = None,
    recent_theses: list[ThesisVersion] | None = None,
) -> AgentContext:
    """Convenience helper to build ``AgentContext`` from domain objects."""

    theses_out: list[dict[str, Any]] = []
    if recent_theses:
        for thesis in recent_theses:
            if isinstance(thesis, ThesisVersion):
                theses_out.append(
                    {
                        "ticker": thesis.ticker,
                        "direction": thesis.direction,
                        "key_assumptions": thesis.key_assumptions or [],
                    }
                )
            elif isinstance(thesis, dict):
                theses_out.append(thesis)

    return AgentContext(
        pm_id=pm_id,
        fund_id=fund_id,
        persona=persona,
        active_tickers=set(active_tickers) if active_tickers else set(),
        recent_theses=theses_out,
    )


def record_specialist_signals(
    uow: UnitOfWork,
    raw_ingest_id: str,
    pm_id: str,
    outputs: list[SpecialistSignalOutput],
) -> list[SpecialistSignal]:
    """Persist a list of specialist outputs as ``SpecialistSignal`` rows."""
    created: list[SpecialistSignal] = []
    for output in outputs:
        signal = uow.specialist_signals.create_signal(
            id=str(uuid.uuid4()),
            pm_id=pm_id,
            raw_ingest_id=raw_ingest_id,
            ticker=output.ticker,
            source_type=output.source_type,
            specialist_agent=output.specialist_agent,
            signal_type=output.signal_type,
            summary=output.summary,
            stance=output.stance,
            confidence=output.confidence,
            evidence_json=output.evidence_json,
            assumptions_touched=output.assumptions_touched,
        )
        created.append(signal)
    return created


__all__ = [
    "AgentContext",
    "BrokerSpecialist",
    "CRMSpecialist",
    "EarningsSpecialist",
    "ExpertNetworkSpecialist",
    "PDFDeckSpecialist",
    "ResearchEdgeSpecialist",
    "SpecialistSignalAgent",
    "SpecialistSignalOutput",
    "SpecialistSignalRegistry",
    "build_agent_context",
    "default_registry",
    "record_specialist_signals",
]
