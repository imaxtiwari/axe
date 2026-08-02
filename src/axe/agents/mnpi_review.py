"""MNPI likelihood and materiality scoring agent."""

from __future__ import annotations

import os
import re

from pydantic import BaseModel, Field

from axe.agents.llm import LLMProvider, LLMResponse, get_default_provider


class MNPIReviewResult(BaseModel):
    """Structured MNPI review output."""

    mnpi_score: float = Field(
        ..., ge=0.0, le=1.0, description="Likelihood that the text contains MNPI."
    )
    materiality_score: float = Field(
        ..., ge=0.0, le=1.0, description="Likelihood that the information is material."
    )
    flagged: bool = Field(..., description="True when either score exceeds the threshold.")
    reasoning: str = Field(..., description="Brief justification for the scores.")


class MNPIReviewAgent:
    """Score arbitrary text for potential MNPI and materiality.

    Uses an LLM provider when available; falls back to deterministic keyword
    heuristics in tests or when the provider returns no structured output.
    """

    SYSTEM_PROMPT = (
        "You are a compliance reviewer evaluating investment-related text for "
        "material non-public information (MNPI). Score the text for:\n"
        "1) mnpi_score — likelihood it contains non-public, confidential, or "
        "insider information.\n"
        "2) materiality_score — likelihood the information would affect an "
        "investment decision or move a security price.\n"
        "3) flagged — set true if mnpi_score >= 0.7 OR materiality_score >= 0.7.\n"
        "4) reasoning — one-sentence chain of thought.\n"
        "Return only JSON matching the schema."
    )

    DEFAULT_THRESHOLD = 0.7

    MNPI_KEYWORDS = (
        "non-public",
        "non public",
        "confidential",
        "insider",
        "inside information",
        "material information",
        "not yet announced",
        "before release",
        "pre-release",
        "earnings before",
        "guidance before",
        "not disclosed",
        "private conversation",
        "under nda",
        "board discussion",
        "m&a discussion",
        "acquisition talks",
        "takeover talks",
        "pending fda",
        "deal pipeline",
    )

    MATERIALITY_KEYWORDS = (
        "earnings",
        "guidance",
        "revenue",
        "eps",
        "merger",
        "acquisition",
        "takeover",
        "buyout",
        "fda approval",
        "clinical trial",
        "departing cfo",
        "departing ceo",
        "layoffs",
        "restructuring",
        "bankruptcy",
        "definitive agreement",
        "lost contract",
        "won contract",
        "massive order",
    )

    def __init__(
        self,
        provider: LLMProvider | None = None,
        threshold: float | None = None,
    ) -> None:
        self.provider = provider or get_default_provider()
        self.threshold = threshold or float(os.getenv("MNPI_THRESHOLD", self.DEFAULT_THRESHOLD))

    async def review(
        self,
        text: str,
        ticker: str | None = None,
    ) -> MNPIReviewResult:
        """Return MNPI scores and a flag for the supplied text."""
        response = await self._llm_review(text, ticker)
        if response is not None and response.parsed:
            try:
                result = MNPIReviewResult(**response.parsed)
                # Recompute flag so callers cannot bypass the configured threshold.
                result.flagged = (
                    result.mnpi_score >= self.threshold
                    or result.materiality_score >= self.threshold
                )
                return result
            except Exception:
                pass
        return self._heuristic_review(text)

    async def _llm_review(
        self,
        text: str,
        ticker: str | None,
    ) -> LLMResponse | None:
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Ticker: {ticker or 'UNKNOWN'}\n\nText:\n{text}\n\n"
                    "Return the JSON review object."
                ),
            },
        ]
        try:
            return await self.provider.complete(
                messages,
                temperature=0.0,
                response_schema=MNPIReviewResult,
            )
        except Exception:
            return None

    def _heuristic_review(self, text: str) -> MNPIReviewResult:
        """Deterministic fallback using keyword density."""
        lower = text.lower()

        mnpi_hits = sum(1 for kw in self.MNPI_KEYWORDS if kw in lower)
        materiality_hits = sum(1 for kw in self.MATERIALITY_KEYWORDS if kw in lower)

        # Normalize roughly to [0, 1] based on keyword density.
        words = max(1, len(re.findall(r"\b\w+\b", lower)))
        mnpi_score = min(1.0, (mnpi_hits * 5) / words + (0.25 if mnpi_hits else 0.0))
        materiality_score = min(
            1.0, (materiality_hits * 5) / words + (0.15 if materiality_hits else 0.0)
        )

        flagged = mnpi_score >= self.threshold or materiality_score >= self.threshold
        reasoning = (
            f"Keyword heuristic: MNPI hits={mnpi_hits}, materiality hits={materiality_hits}."
        )
        if flagged:
            reasoning += " At least one score exceeded the configured threshold."
        return MNPIReviewResult(
            mnpi_score=round(mnpi_score, 3),
            materiality_score=round(materiality_score, 3),
            flagged=flagged,
            reasoning=reasoning,
        )


__all__ = ["MNPIReviewAgent", "MNPIReviewResult"]
