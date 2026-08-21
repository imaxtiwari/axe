"""Model trace capture wrapper for LLM providers.

TraceableProvider wraps any LLMProvider, generates a per-call trace_id,
records latency, prompt hashes, token usage, and persists a ModelTrace row
through the UnitOfWork for guardrails, audit, and hallucination review.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from pydantic import BaseModel

from axe.agents.citation import CitationExtractor, CitationVerifier
from axe.agents.guardrails import GuardrailRunner
from axe.agents.hallucination_guard import HallucinationGuard
from axe.agents.llm import LLMProvider, LLMResponse
from axe.config import Settings, get_settings
from axe.db.models import ModelTrace
from axe.db.uow import UnitOfWork


class TraceableProvider(LLMProvider):
    """Wraps an LLMProvider and captures a ModelTrace for every completion.

    The wrapper is transparent to callers: it delegates to the inner provider
    and augments the returned ``LLMResponse`` with ``trace_id``.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        agent: str,
        uow: UnitOfWork | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.provider = provider
        self.agent = agent
        self.uow = uow
        self.settings = settings or get_settings()
        self.last_trace: ModelTrace | None = None

    def _prompt_hash(self, messages: list[dict[str, str]]) -> str:
        """Return a deterministic SHA-256 hash of the serialized messages."""
        canonical = json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _default_trace_id(self) -> str:
        """Return a unique trace identifier."""
        return str(uuid.uuid4())

    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
        raw_sources: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Delegate to the wrapped provider and capture a model trace.

        The captured trace includes citations, a hallucination score, and
        guardrail results. High-risk outputs are automatically routed to
        compliance review.
        """
        trace_id = self._default_trace_id()
        prompt_hash = self._prompt_hash(messages)
        schema_name = response_schema.__name__ if response_schema is not None else None
        started = time.monotonic()

        response = await self.provider.complete(messages, temperature, response_schema)
        latency_ms = int((time.monotonic() - started) * 1000)

        response.trace_id = trace_id
        await self._persist_trace(
            trace_id=trace_id,
            prompt_hash=prompt_hash,
            model=response.model or "unknown",
            response_schema=schema_name,
            latency_ms=latency_ms,
            usage=response.usage or {},
            output_text=response.get_text(),
            raw_sources=raw_sources,
            metadata=metadata,
        )
        return response

    async def _persist_trace(
        self,
        *,
        trace_id: str,
        prompt_hash: str,
        model: str,
        response_schema: str | None,
        latency_ms: int,
        usage: dict[str, int],
        output_text: str,
        raw_sources: list[Any] | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Persist a ModelTrace row when tracing is enabled and UoW is available."""
        if not self.settings.model_trace_capture_enabled:
            return
        if self.uow is None:
            return

        from axe.security.context import RequestContext

        ctx = RequestContext.current_or_none()
        pm_id = ctx.pm_id if ctx is not None else None

        # Extract and verify citations, compute hallucination score, and run
        # the configured guardrail suite.
        extractor = CitationExtractor()
        verifier = CitationVerifier()
        citations = extractor.extract(output_text, raw_sources)
        citations = verifier.verify(citations, raw_sources)
        citations_json = [c.model_dump(mode="json") for c in citations]

        hallucination_guard = HallucinationGuard(self.settings)
        hallucination_score = hallucination_guard.score(output_text, citations, raw_sources)
        routing = await hallucination_guard.route_for_review(
            hallucination_score, trace_id=trace_id, uow=self.uow
        )

        guardrail_runner = GuardrailRunner(uow=self.uow, settings=self.settings)
        guardrail_result = await guardrail_runner.check(
            output_text, raw_sources=raw_sources, metadata=metadata
        )
        # Persist the final human-review status so callers (e.g. drift_detect)
        # can rely on the trace row reflecting guardrail and hallucination
        # routing decisions together.
        if self.last_trace is not None:
            if guardrail_result.suggested_action in {"review", "reject"}:
                self.last_trace.human_review_status = "pending"
        if guardrail_result.severity in {"high", "critical"}:
            await guardrail_runner.escalate(guardrail_result, trace_id=trace_id)

        # Combine hallucination routing with guardrail severity for the final
        # human review status.
        human_review_status = routing["human_review_status"]
        if guardrail_result.suggested_action in {"review", "reject"}:
            human_review_status = "pending"

        self.last_trace = self.uow.model_traces.create_trace(
            id=trace_id,
            pm_id=pm_id,
            agent=self.agent,
            prompt_hash=prompt_hash,
            model=model,
            response_schema=response_schema,
            latency_ms=latency_ms,
            token_usage=usage,
            citations_json=citations_json,
            hallucination_score=hallucination_score,
            human_review_status=human_review_status,
        )
