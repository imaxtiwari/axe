"""DeckBuilderAgent for auto-deck generation from a deal thesis."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.llm import LLMProvider, get_default_provider
from axe.db.models import DeckOutput, DeckTemplate

# Default deck template structures keyed by vehicle type.
# These are also seeded into the DB by `seed_deck_templates` in models.py.
DEFAULT_DECK_TEMPLATES: dict[str, dict[str, Any]] = {
    "equity": {
        "name": "Equity Deal Deck",
        "structure": [
            {"title": "Cover", "bullets": ["{title}", "{vehicle_type}"]},
            {
                "title": "Executive Summary",
                "bullets": ["{bull_case}", "Conviction: {conviction}"],
            },
            {
                "title": "Investment Thesis",
                "bullets": ["{bull_case}", "Key assumptions drive the bull case."],
            },
            {
                "title": "Bull / Bear Cases",
                "bullets": ["Bull: {bull_case}", "Bear: {bear_case}"],
            },
            {
                "title": "Key Assumptions",
                "bullets": ["{key_assumptions}"],
            },
            {
                "title": "Risks & Mitigants",
                "bullets": ["{risks}"],
            },
            {
                "title": "Valuation",
                "bullets": ["Target return framework", "Comparable metrics"],
                "chart_spec": {"type": "bar", "data": ["base", "upside", "downside"]},
            },
            {"title": "Next Steps", "bullets": ["Finalize IC memo", "Confirm diligence timeline"]},
        ],
    },
    "credit": {
        "name": "Credit Deal Deck",
        "structure": [
            {"title": "Cover", "bullets": ["{title}", "{vehicle_type}"]},
            {
                "title": "Executive Summary",
                "bullets": ["{bull_case}", "Conviction: {conviction}"],
            },
            {
                "title": "Credit Thesis",
                "bullets": ["{bull_case}", "Key assumptions drive the credit case."],
            },
            {
                "title": "Bull / Bear Cases",
                "bullets": ["Bull: {bull_case}", "Bear: {bear_case}"],
            },
            {
                "title": "Key Assumptions",
                "bullets": ["{key_assumptions}"],
            },
            {
                "title": "Risks & Covenants",
                "bullets": ["{risks}"],
            },
            {
                "title": "Return Profile",
                "bullets": ["Base yield", "Stress recovery"],
                "chart_spec": {"type": "waterfall", "data": ["yield", "recovery"]},
            },
            {"title": "Next Steps", "bullets": ["Finalize IC memo", "Confirm diligence timeline"]},
        ],
    },
    "lp_gp": {
        "name": "LP/GP Commitment Deck",
        "structure": [
            {"title": "Cover", "bullets": ["{title}", "{vehicle_type}"]},
            {
                "title": "Executive Summary",
                "bullets": ["{bull_case}", "Conviction: {conviction}"],
            },
            {
                "title": "Fund Strategy Fit",
                "bullets": ["{bull_case}", "Key assumptions drive the commitment."],
            },
            {
                "title": "Bull / Bear Cases",
                "bullets": ["Bull: {bull_case}", "Bear: {bear_case}"],
            },
            {
                "title": "Key Assumptions",
                "bullets": ["{key_assumptions}"],
            },
            {
                "title": "Risks & Terms",
                "bullets": ["{risks}"],
            },
            {
                "title": "Portfolio Fit",
                "bullets": ["Allocation impact", "Vintage diversification"],
                "chart_spec": {"type": "pie", "data": ["current", "pro_forma"]},
            },
            {"title": "Next Steps", "bullets": ["Finalize IC memo", "Confirm diligence timeline"]},
        ],
    },
}


def _normalize_vehicle_type(vehicle_type: str | None) -> str:
    """Map an asset class or raw vehicle type to a supported deck vehicle type."""
    mapping = {
        "equity": "equity",
        "public_equity": "equity",
        "private_equity": "equity",
        "pe": "equity",
        "vc": "equity",
        "credit": "credit",
        "private_credit": "credit",
        "lp": "lp_gp",
        "lp_gp": "lp_gp",
        "fund_commitment": "lp_gp",
    }
    normalized = (vehicle_type or "equity").lower().replace(" ", "_").replace("-", "_")
    return mapping.get(normalized, "equity")


def _build_markdown(content: dict[str, Any]) -> str:
    """Render a Markdown memo from structured deck content."""
    lines: list[str] = [f"# {content.get('title', 'Untitled Deck')}", ""]
    for slide in content.get("slides", []):
        title = slide.get("title") or "Untitled slide"
        lines.append(f"## {title}")
        for bullet in slide.get("bullets", []):
            lines.append(f"- {bullet}")
        lines.append("")
    return "\n".join(lines).strip()


class DeckBuilderAgent:
    """Select templates and generate deterministic deal decks."""

    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider | None = None,
    ) -> None:
        self.session = session
        self.provider = provider or get_default_provider()

    async def select_template(
        self,
        asset_class: str,
        audience: str,
    ) -> DeckTemplate | None:
        """Return the best matching deck template for the requested memo."""
        result = await self.session.execute(
            select(DeckTemplate)
            .where(
                DeckTemplate.asset_class == asset_class,
                DeckTemplate.audience == audience,
            )
            .order_by(DeckTemplate.name)
        )
        return result.scalars().first()

    async def select_template_by_vehicle(
        self,
        vehicle_type: str,
    ) -> DeckTemplate | None:
        """Select a template deterministically by normalized vehicle type."""
        normalized = _normalize_vehicle_type(vehicle_type)
        result = await self.session.execute(
            select(DeckTemplate)
            .where(
                DeckTemplate.asset_class == normalized,
                DeckTemplate.audience == "ic_committee",
            )
            .order_by(DeckTemplate.name)
        )
        template = result.scalars().first()
        if template is not None:
            return template
        # Ensure a default template row exists for the normalized type.
        return await self._ensure_default_template(normalized)

    async def _ensure_default_template(self, vehicle_type: str) -> DeckTemplate:
        """Return the DB default template, creating it if necessary."""
        result = await self.session.execute(
            select(DeckTemplate)
            .where(
                DeckTemplate.asset_class == vehicle_type,
                DeckTemplate.audience == "ic_committee",
            )
            .order_by(DeckTemplate.name)
        )
        existing = result.scalars().first()
        if existing is not None:
            return existing
        spec = DEFAULT_DECK_TEMPLATES[vehicle_type]
        template = DeckTemplate(
            id=str(uuid.uuid4()),
            name=spec["name"],
            asset_class=vehicle_type,
            audience="ic_committee",
            structure=spec["structure"],
        )
        self.session.add(template)
        await self.session.flush()
        return template

    def _map_thesis_to_slide(
        self,
        template: dict[str, Any],
        thesis: Any | None,
        title: str,
        vehicle_type: str,
    ) -> dict[str, Any]:
        """Replace placeholders in a template slide with thesis data."""
        bull_case = ""
        bear_case = ""
        key_assumptions: list[str] = []
        risks: list[str] = []
        conviction = ""
        if thesis is not None:
            bull_case = getattr(thesis, "bull_case", "") or ""
            bear_case = getattr(thesis, "bear_case", "") or ""
            key_assumptions = getattr(thesis, "key_assumptions", []) or []
            risks = getattr(thesis, "risks", []) or []

        # Render placeholders deterministically.
        ka_text = "; ".join(key_assumptions) if key_assumptions else "No assumptions recorded."
        risk_text = "; ".join(risks) if risks else "No risks recorded."
        placeholders = {
            "{title}": title,
            "{vehicle_type}": vehicle_type,
            "{bull_case}": bull_case or "No bull case recorded.",
            "{bear_case}": bear_case or "No bear case recorded.",
            "{key_assumptions}": ka_text,
            "{risks}": risk_text,
            "{conviction}": conviction or "TBD",
        }

        rendered_bullets: list[str] = []
        raw_bullets = template.get("bullets") or []
        if isinstance(raw_bullets, str):
            raw_bullets = [raw_bullets]
        for bullet in raw_bullets:
            text = str(bullet)
            for placeholder, value in placeholders.items():
                text = text.replace(placeholder, value)
            rendered_bullets.append(text)

        slide: dict[str, Any] = {
            "title": template.get("title") or "Untitled slide",
            "bullets": rendered_bullets,
        }
        chart_spec = template.get("chart_spec")
        if chart_spec is not None:
            slide["chart_spec"] = chart_spec
        return slide

    async def build_deck(
        self,
        pm_id: str,
        asset_class: str,
        audience: str,
        title: str,
        source_ids: list[str],
        source_data: dict[str, Any] | None = None,
    ) -> DeckOutput:
        """Generate and persist a versioned deck output (legacy asset_class/audience API)."""
        template = await self.select_template(asset_class, audience)
        vehicle_type = _normalize_vehicle_type(asset_class)
        if template is None:
            template = await self._ensure_default_template(vehicle_type)
        structure = template.structure or []
        output = self._persist_deck(
            pm_id=pm_id,
            title=title,
            template=template,
            structure=structure,
            source_ids=source_ids,
            source_data=source_data,
            thesis=None,
            vehicle_type=vehicle_type,
            output_type="ic_memo",
            content_key="sections",
        )
        await self.session.flush()
        return output

    async def build_deck_from_thesis(
        self,
        pm_id: str,
        thesis: Any | None,
        *,
        vehicle_type: str | None = None,
        title: str | None = None,
        source_data: dict[str, Any] | None = None,
    ) -> DeckOutput:
        """Generate and persist a deal deck mapped from a DealThesisVersion."""
        normalized = _normalize_vehicle_type(vehicle_type)
        template = await self.select_template_by_vehicle(normalized)
        if template is None:
            raise RuntimeError(f"No deck template available for vehicle_type {vehicle_type}")
        structure = template.structure or DEFAULT_DECK_TEMPLATES[normalized]["structure"]
        deal_name = getattr(thesis, "deal_id", None) if thesis is not None else None
        deck_title = title or (f"{deal_name} Deck" if deal_name else "Deal Deck")
        source_ids: list[str] = []
        if thesis is not None:
            source_ids.append(str(getattr(thesis, "id", "")))
        output = self._persist_deck(
            pm_id=pm_id,
            title=deck_title,
            template=template,
            structure=structure,
            source_ids=source_ids,
            source_data=source_data,
            thesis=thesis,
            vehicle_type=normalized,
            output_type="deal_deck",
            content_key="slides",
        )
        await self.session.flush()
        return output

    def _persist_deck(
        self,
        pm_id: str,
        title: str,
        template: DeckTemplate,
        structure: list[Any],
        source_ids: list[str],
        source_data: dict[str, Any] | None,
        thesis: Any | None,
        vehicle_type: str,
        *,
        output_type: str = "deal_deck",
        content_key: str = "slides",
    ) -> DeckOutput:
        """Build slide content and persist a DeckOutput row."""
        slides: list[dict[str, Any]] = []
        for index, item in enumerate(structure, start=1):
            if isinstance(item, dict):
                slide: dict[str, Any] = self._map_thesis_to_slide(item, thesis, title, vehicle_type)
            else:
                slide = {
                    "title": str(item),
                    "bullets": ["Key point to be filled by PM"],
                }
            slide["slide_number"] = index
            slides.append(slide)

        if not slides:
            slides.append(
                {
                    "slide_number": 1,
                    "title": "Executive Summary",
                    "bullets": ["Key point to be filled by PM"],
                }
            )

        footer = f"Sources: {', '.join(source_ids) if source_ids else 'Internal'} | Draft — internal only."
        source_thesis_id = source_ids[0] if source_ids else None

        content: dict[str, Any] = {
            "title": title,
            "template_id": template.id,
            "asset_class": template.asset_class,
            "audience": template.audience,
            "vehicle_type": vehicle_type,
            "source_thesis_version_id": source_thesis_id,
            "source_data": source_data or {},
            content_key: slides,
            "footer": footer,
        }
        markdown_content: dict[str, Any] = {**content, "slides": slides}
        content["markdown"] = _build_markdown(markdown_content)

        output = DeckOutput(
            id=str(uuid.uuid4()),
            pm_id=pm_id,
            type=output_type,
            source_ids=list(source_ids),
            content=content,
        )
        self.session.add(output)
        return output


__all__ = ["DEFAULT_DECK_TEMPLATES", "DeckBuilderAgent"]
