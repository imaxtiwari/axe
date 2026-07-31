"""Investment communication agents for IC memos and LP updates."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.llm import LLMProvider, get_default_provider
from axe.db.models import DeckOutput, DeckTemplate, InvestmentVehicle, LPUpdate, utc_now


class ComplianceGateError(RuntimeError):
    """Raised when an external communication is attempted without human approval."""


class DeckBuilderAgent:
    """Select templates, gather source data, and generate deck/memo structures."""

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

    @staticmethod
    def _build_markdown(content: dict[str, Any]) -> str:
        """Render a Markdown memo from structured deck content."""
        lines: list[str] = [
            f"# {content.get('title', 'Untitled Memo')}",
            "",
        ]
        for section in content.get("sections", []):
            title = section.get("title") or "Untitled section"
            lines.append(f"## {title}")
            for bullet in section.get("bullets", []):
                lines.append(f"- {bullet}")
            lines.append("")
        for note in content.get("footnotes", []):
            lines.append(f"_{note}_")
        return "\n".join(lines).strip()

    async def build_deck(
        self,
        pm_id: str,
        asset_class: str,
        audience: str,
        title: str,
        source_ids: list[str],
        source_data: dict[str, Any] | None = None,
    ) -> DeckOutput:
        """Generate and persist a versioned IC memo / deck output."""
        template = await self.select_template(asset_class, audience)
        structure = template.structure if template else []

        sections: list[dict[str, Any]] = []
        slide_number = 1
        for item in structure:
            if isinstance(item, dict):
                title_hint = item.get("title") or item.get("section") or f"Slide {slide_number}"
                bullets = item.get("bullets") or ["Key point to be filled by PM"]
            else:
                title_hint = str(item) or f"Slide {slide_number}"
                bullets = ["Key point to be filled by PM"]
            sections.append({
                "slide_number": slide_number,
                "title": title_hint,
                "bullets": bullets if isinstance(bullets, list) else [bullets],
            })
            slide_number += 1

        if not sections:
            sections.append({
                "slide_number": 1,
                "title": "Executive Summary",
                "bullets": ["Key point to be filled by PM"],
            })

        version_date = utc_now().strftime("%Y-%m-%d")
        footer = f"Version {version_date} | Sources: {', '.join(source_ids) if source_ids else 'Internal'} | Draft — internal only."

        content: dict[str, Any] = {
            "title": title,
            "template_id": template.id if template else None,
            "asset_class": asset_class,
            "audience": audience,
            "source_data": source_data or {},
            "sections": sections,
            "footer": footer,
            "footnotes": [footer],
        }
        content["markdown"] = self._build_markdown(content)

        output = DeckOutput(
            id=str(uuid.uuid4()),
            pm_id=pm_id,
            type="ic_memo",
            source_ids=list(source_ids),
            content=content,
        )
        self.session.add(output)
        await self.session.flush()
        return output


class LPUpdateAgent:
    """Gather vehicle activity and draft versioned LP update letters."""

    REQUIRED_SECTIONS = [
        "Executive Summary",
        "Portfolio Update",
        "Performance Commentary",
        "Outlook",
        "Appendices",
    ]

    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider | None = None,
    ) -> None:
        self.session = session
        self.provider = provider or get_default_provider()

    async def gather_vehicle_activity(
        self,
        vehicle_id: str,
    ) -> dict[str, Any]:
        """Collect baseline vehicle and LP relationship data for an update."""
        vehicle = await self.session.get(InvestmentVehicle, vehicle_id)
        if vehicle is None:
            raise ValueError(f"Vehicle {vehicle_id} not found")
        return {
            "vehicle_id": vehicle.id,
            "vehicle_name": vehicle.name,
            "legal_entity": vehicle.legal_entity,
            "strategy": vehicle.strategy,
            "vintage": vehicle.vintage,
            "currency": vehicle.currency,
            "as_of": utc_now().strftime("%Y-%m-%d"),
        }

    @staticmethod
    def _section(heading: str, body: str) -> dict[str, str]:
        return {"heading": heading, "body": body}

    async def draft_update(
        self,
        vehicle_id: str,
        quarter: str,
        activity: dict[str, Any] | None = None,
    ) -> LPUpdate:
        """Create a draft LP update with all required sections and footer."""
        vehicle = await self.session.get(InvestmentVehicle, vehicle_id)
        if vehicle is None:
            raise ValueError(f"Vehicle {vehicle_id} not found")

        data = activity or await self.gather_vehicle_activity(vehicle_id)
        version_date = utc_now().strftime("%Y-%m-%d")
        sources = data.get("sources") or ["Internal records"]
        footer = (
            f"Version {version_date} | Sources: {', '.join(sources)} | "
            "Draft — internal only."
        )

        sections = [
            self._section(
                "Executive Summary",
                f"Quarterly update for {vehicle.name} for {quarter}. "
                "This section summarises the key messages for Limited Partners.",
            ),
            self._section(
                "Portfolio Update",
                f"Portfolio-level activity for {vehicle.name} as of {data.get('as_of', version_date)}. "
                "Includes new investments, follow-ons, realisations, and reserves.",
            ),
            self._section(
                "Performance Commentary",
                f"Performance commentary for {vehicle.name}, including NAV, IRR, multiples and "
                "benchmark context where available.",
            ),
            self._section(
                "Outlook",
                f"Forward-looking commentary and strategic priorities for {vehicle.name}.",
            ),
            self._section(
                "Appendices",
                "Supplementary tables, capital account statements, and disclosures.",
            ),
            self._section("Footer", footer),
        ]

        update = LPUpdate(
            id=str(uuid.uuid4()),
            vehicle_id=vehicle_id,
            quarter=quarter,
            sections=sections,
            status="draft",
        )
        self.session.add(update)
        await self.session.flush()
        return update


async def send_lp_update(
    update: LPUpdate,
    approved_by: str | None = None,
    sent_at: datetime | None = None,
) -> LPUpdate:
    """Compliance gate: external LP updates require human approval before they may be sent.

    This function intentionally does *not* deliver email; it only updates the
    record status after verifying that a human has approved the draft.
    """
    if update.status != "approved" or not approved_by:
        raise ComplianceGateError(
            "LP update must be approved and include approved_by before it can be sent."
        )
    update.approved_by = approved_by
    update.sent_at = sent_at or utc_now()
    update.status = "sent"
    return update


__all__ = [
    "ComplianceGateError",
    "DeckBuilderAgent",
    "LPUpdateAgent",
    "send_lp_update",
]
