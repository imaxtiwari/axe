"""Thesis extraction and versioning agent.

Uses a configurable LLM provider to parse unstructured input into a structured
``ExtractedThesis`` object, then reconciles it with the latest thesis version.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from axe.agents.llm import LLMProvider, get_default_provider
from axe.agents.persona_models import PersonaStyleSnapshot
from axe.db.models import ThesisVersion


class ExtractedThesis(BaseModel):
    """Structured extraction from raw unstructured input."""

    ticker: str | None = Field(default=None, description="Uppercase ticker or None if not supplied")
    direction: str | None = Field(default=None, description="long/short/neutral")
    conviction: int | None = Field(default=None, ge=1, le=5, description="1-5 conviction score")
    bull_case: str | None = None
    bear_case: str | None = None
    key_assumptions: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    catalyst_timeframe_days: int | None = None
    risks: list[str] = Field(default_factory=list)
    mnpi_flag: bool = False
    unsupported: bool = False
    not_investment_thesis: bool = False

    def is_empty(self) -> bool:
        return (
            self.ticker is None
            and self.direction is None
            and self.conviction is None
            and not self.bull_case
            and not self.bear_case
            and not self.key_assumptions
            and not self.catalysts
            and not self.risks
        )


class ThesisExtractAgent:
    """Agent that extracts a structured thesis from unstructured content."""

    SYSTEM_PROMPT = (
        "You are an investment-research extraction assistant for a long/short equity fund. "
        "Parse the user message into a structured thesis. Output JSON matching the schema. "
        "If the message is not an investment thesis, set not_investment_thesis=true. "
        "If the input is vague or unsupported, set unsupported=true."
    )

    def _build_system_prompt(self, persona: PersonaStyleSnapshot | None = None) -> str:
        """Return the system prompt, optionally infused with PM persona guidance."""
        prompt = self.SYSTEM_PROMPT
        if persona:
            snippet = persona.render_system_prompt_snippet()
            if snippet:
                prompt += (
                    "\n\nTailor language and emphasis to the PM's style. "
                    "Prioritize the PM's decision triggers and trusted sources when inferring "
                    "conviction and risks.\n" + snippet
                )
        return prompt

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_default_provider()

    async def extract(
        self,
        content: str,
        persona: PersonaStyleSnapshot | None = None,
    ) -> ExtractedThesis:
        """Extract a structured thesis from unstructured ``content``."""
        messages = [
            {"role": "system", "content": self._build_system_prompt(persona)},
            {"role": "user", "content": content},
        ]
        response = await self.provider.complete(
            messages,
            temperature=0.0,
            response_schema=ExtractedThesis,
        )
        payload = response.parsed or {}
        try:
            return ExtractedThesis(**payload)
        except Exception:  # pragma: no cover - defensive
            return ExtractedThesis(unsupported=True, not_investment_thesis=False)

    async def reconcile(
        self,
        extracted: ExtractedThesis,
        latest: ThesisVersion | None,
    ) -> ThesisVersion:
        """Reconcile an extraction with the latest thesis version, bumping version if needed."""
        import uuid

        base = latest or ThesisVersion(
            id=str(uuid.uuid4()),
            pm_id="",
            ticker=extracted.ticker or "",
            version=0,
        )

        new_version = base.version + 1
        thesis_direction = extracted.direction or base.direction
        if thesis_direction not in {"long", "short", "neutral"}:
            thesis_direction = "long"

        updated_bull = base.bull_case or ""
        if extracted.bull_case:
            updated_bull = (
                f"{updated_bull}\n\n--- Update v{new_version} ---\n{extracted.bull_case}".strip()
                if updated_bull and "--- Update" not in updated_bull
                else extracted.bull_case
            )

        updated_bear = base.bear_case or ""
        if extracted.bear_case:
            updated_bear = (
                f"{updated_bear}\n\n--- Update v{new_version} ---\n{extracted.bear_case}".strip()
                if updated_bear and "--- Update" not in updated_bear
                else extracted.bear_case
            )

        merged_assumptions = list(base.key_assumptions or [])
        for assumption in extracted.key_assumptions:
            if assumption.strip() not in merged_assumptions:
                merged_assumptions.append(assumption.strip())

        merged_catalysts = list(base.catalysts or [])
        for catalyst in extracted.catalysts:
            if catalyst.strip() not in merged_catalysts:
                merged_catalysts.append(catalyst.strip())

        merged_risks = list(base.unresolved_risks or [])
        for risk in extracted.risks:
            if risk.strip() not in merged_risks:
                merged_risks.append(risk.strip())

        return ThesisVersion(
            id=str(uuid.uuid4()),
            pm_id=base.pm_id,
            ticker=base.ticker,
            version=new_version,
            is_draft=False,
            asset_class=base.asset_class or "equity",
            direction=thesis_direction,
            status=base.status,
            bull_case=updated_bull or None,
            bear_case=updated_bear or None,
            key_assumptions=merged_assumptions,
            catalysts=merged_catalysts,
            conviction=extracted.conviction,
            unresolved_risks=merged_risks,
            fund_entity_id=base.fund_entity_id,
            mnpi_flag=extracted.mnpi_flag or base.mnpi_flag,
        )

    async def run(
        self,
        content: str,
        latest: ThesisVersion | None = None,
        persona: PersonaStyleSnapshot | None = None,
    ) -> ThesisVersion | None:
        """End-to-end extract + reconcile, returning None for non-thesis or empty input."""
        extracted = await self.extract(content, persona=persona)
        if extracted.not_investment_thesis or extracted.is_empty() or extracted.unsupported:
            return None
        return await self.reconcile(extracted, latest)

    async def from_natural_language(
        self,
        content: str,
        persona: PersonaStyleSnapshot | None = None,
    ) -> dict[str, Any]:
        """Legacy-compatible wrapper returning a dict for ThesisRepo.create_thesis."""
        extracted = await self.extract(content, persona=persona)
        return {
            "ticker": extracted.ticker,
            "bull_case": extracted.bull_case,
            "bear_case": extracted.bear_case,
            "key_assumptions": extracted.key_assumptions,
            "catalysts": extracted.catalysts,
            "unresolved_risks": extracted.risks,
            "conviction": extracted.conviction,
            "is_draft": False,
            "asset_class": "equity",
            "direction": extracted.direction or "long",
            "status": "active",
        }
