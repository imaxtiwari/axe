"""Investment communication agents for IC memos and LP updates."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.llm import LLMProvider, get_default_provider
from axe.agents.lp_update import (  # noqa: F401
    ComplianceGateError,
    LPUpdateAgent,
    send_lp_update,
)
from axe.db.models import DeckOutput, DeckTemplate, utc_now


__all__ = [
    "ComplianceGateError",
    "DeckBuilderAgent",
    "LPUpdateAgent",
    "send_lp_update",
]


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
            sections.append(
                {
                    "slide_number": slide_number,
                    "title": title_hint,
                    "bullets": bullets if isinstance(bullets, list) else [bullets],
                }
            )
            slide_number += 1

        if not sections:
            sections.append(
                {
                    "slide_number": 1,
                    "title": "Executive Summary",
                    "bullets": ["Key point to be filled by PM"],
                }
            )

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
