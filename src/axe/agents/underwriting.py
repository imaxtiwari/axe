"""Underwriting agent for deal checklists and scenario analysis."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from axe.agents.llm import LLMProvider, LLMResponse


class Scenario(BaseModel):
    """A single scenario produced by the underwriting agent."""

    scenario_name: str = Field(..., description="Name of the scenario")
    assumptions: dict[str, Any] = Field(default_factory=dict)
    output_metrics: dict[str, Any] = Field(default_factory=dict)
    probability_weight: float = Field(default=0.33, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ScenarioOutput(BaseModel):
    """Structured scenario analysis output."""

    scenarios: list[Scenario] = Field(default_factory=list)
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Overall confidence across scenarios",
    )


class ChecklistItem(BaseModel):
    """A lightweight checklist item template."""

    category: str
    question: str
    required: bool = True
    sort_order: int = 0


DEFAULT_TEMPLATES: dict[str, list[ChecklistItem]] = {
    "equity": [
        ChecklistItem(category="Business", question="Confirm revenue model and TAM", required=True, sort_order=1),
        ChecklistItem(category="Financials", question="Validate last three years of audited financials", required=True, sort_order=2),
        ChecklistItem(category="Management", question="Assess management track record and incentives", required=True, sort_order=3),
        ChecklistItem(category="Risk", question="Identify key downside and break conditions", required=True, sort_order=4),
        ChecklistItem(category="Valuation", question="Benchmark valuation vs comparable transactions", required=False, sort_order=5),
    ],
    "credit": [
        ChecklistItem(category="Credit", question="Review leverage, coverage ratios, and covenants", required=True, sort_order=1),
        ChecklistItem(category="Collateral", question="Confirm collateral package and security ranking", required=True, sort_order=2),
        ChecklistItem(category="Issuer", question="Evaluate issuer cash flow stability", required=True, sort_order=3),
        ChecklistItem(category="Market", question="Assess secondary market liquidity", required=False, sort_order=4),
    ],
    "lp_gp": [
        ChecklistItem(category="Fund", question="Review fund terms, fees, and waterfall", required=True, sort_order=1),
        ChecklistItem(category="Track Record", question="Validate GP track record across funds", required=True, sort_order=2),
        ChecklistItem(category="Alignment", question="Confirm GP commitment and key-person provisions", required=True, sort_order=3),
        ChecklistItem(category="Strategy", question="Assess strategy consistency with target portfolio", required=False, sort_order=4),
    ],
}


class UnderwritingAgent:
    """Agent that drives deal underwriting checklists and scenario analysis.

    The agent is intentionally provider-agnostic: when an LLM provider is
    supplied it requests structured JSON output; otherwise it falls back to
    deterministic templates so tests can run without a live model.
    """

    VALID_STATUSES = {"open", "checked", "waived", "na"}
    CLOSED_STATUSES = {"checked", "waived", "na"}

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider

    @classmethod
    def default_checklist(cls, vehicle_type: str) -> list[ChecklistItem]:
        """Return the default checklist template for a vehicle type."""
        normalized = vehicle_type.lower().replace("/", "_").replace("-", "_")
        if normalized not in DEFAULT_TEMPLATES:
            raise ValueError(f"Unknown vehicle_type: {vehicle_type}")
        return DEFAULT_TEMPLATES[normalized]

    @classmethod
    def validate_checklist_complete(cls, items: list[dict[str, Any]]) -> None:
        """Raise ValueError if any required checklist item is incomplete."""
        required_incomplete = [
            (i, it)
            for i, it in enumerate(items)
            if it.get("required") and it.get("status", "open") not in cls.CLOSED_STATUSES
        ]
        if required_incomplete:
            names = [it.get("question", f"item {i}") for i, it in required_incomplete]
            raise ValueError(f"Required checklist items incomplete: {names}")

    async def run_scenarios(
        self,
        thesis_text: str,
        vehicle_type: str,
        checklist: list[dict[str, Any]],
    ) -> ScenarioOutput:
        """Generate scenario analysis once the checklist is complete."""
        self.validate_checklist_complete(checklist)
        if self.provider is None:
            return self._fallback_scenarios(thesis_text, vehicle_type)
        return await self._llm_scenarios(thesis_text, vehicle_type)

    def _fallback_scenarios(self, thesis_text: str, vehicle_type: str) -> ScenarioOutput:
        """Deterministic scenario output used when no LLM is configured."""
        base_metrics = {"thesis_length_chars": len(thesis_text)}
        if vehicle_type == "credit":
            scenarios = [
                Scenario(
                    scenario_name="Base case",
                    assumptions={"default_rate": "in_line", "recovery": 0.45},
                    output_metrics={"yield": "sticky", **base_metrics},
                    probability_weight=0.5,
                    confidence=0.6,
                ),
                Scenario(
                    scenario_name="Downside",
                    assumptions={"default_rate": "elevated", "recovery": 0.25},
                    output_metrics={"yield": "widens", **base_metrics},
                    probability_weight=0.3,
                    confidence=0.4,
                ),
                Scenario(
                    scenario_name="Upside",
                    assumptions={"default_rate": "below_trend", "recovery": 0.65},
                    output_metrics={"yield": "tightens", **base_metrics},
                    probability_weight=0.2,
                    confidence=0.35,
                ),
            ]
        else:
            scenarios = [
                Scenario(
                    scenario_name="Base case",
                    assumptions={"revenue_growth": "in_line", "margin": "stable"},
                    output_metrics={"irr": 0.18, **base_metrics},
                    probability_weight=0.5,
                    confidence=0.6,
                ),
                Scenario(
                    scenario_name="Downside",
                    assumptions={"revenue_growth": "decelerates", "margin": "compresses"},
                    output_metrics={"irr": 0.06, **base_metrics},
                    probability_weight=0.3,
                    confidence=0.45,
                ),
                Scenario(
                    scenario_name="Upside",
                    assumptions={"revenue_growth": "accelerates", "margin": "expands"},
                    output_metrics={"irr": 0.30, **base_metrics},
                    probability_weight=0.2,
                    confidence=0.35,
                ),
            ]
        avg_confidence = round(
            sum(s.confidence * s.probability_weight for s in scenarios), 3
        ) or 0.5
        return ScenarioOutput(scenarios=scenarios, confidence=avg_confidence)

    async def _llm_scenarios(self, thesis_text: str, vehicle_type: str) -> ScenarioOutput:
        """Request structured scenario output from the configured LLM."""
        assert self.provider is not None  # guarded by caller
        schema_prompt = ScenarioOutput.model_json_schema()
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an underwriting assistant. Produce structured "
                    "scenario analysis for the provided deal thesis."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Vehicle type: {vehicle_type}\n\n"
                    f"Deal thesis:\n{thesis_text}\n\n"
                    f"Respond with this JSON schema:\n{schema_prompt}"
                ),
            },
        ]
        response: LLMResponse = await self.provider.complete(
            messages, temperature=0.2, response_schema=ScenarioOutput
        )
        parsed = response.parsed or {}
        if not parsed:
            return self._fallback_scenarios(thesis_text, vehicle_type)
        try:
            return ScenarioOutput.model_validate(parsed)
        except Exception:  # pragma: no cover - defensive fallback
            return self._fallback_scenarios(thesis_text, vehicle_type)
