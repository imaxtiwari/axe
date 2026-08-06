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

from axe.agents.llm import LLMProvider, LLMResponse
from axe.config import Settings, get_settings
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
    ) -> LLMResponse:
        """Delegate to the wrapped provider and capture a model trace."""
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
    ) -> None:
        """Persist a ModelTrace row when tracing is enabled and UoW is available."""
        if not self.settings.model_trace_capture_enabled:
            return
        if self.uow is None:
            return

        from axe.security.context import RequestContext

        ctx = RequestContext.current_or_none()
        pm_id = ctx.pm_id if ctx is not None else None

        # Calculate a placeholder hallucination score from citation coverage if
        # the response includes it; real guardrails will overwrite later.
        citations: list[Any] = []
        hallucination_score: float | None = None

        self.uow.model_traces.create_trace(
            id=trace_id,
            pm_id=pm_id,
            agent=self.agent,
            prompt_hash=prompt_hash,
            model=model,
            response_schema=response_schema,
            latency_ms=latency_ms,
            token_usage=usage,
            citations_json=citations,
            hallucination_score=hallucination_score,
        )
