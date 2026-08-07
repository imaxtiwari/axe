"""LP quarterly update agent.

Gathers vehicle, LP relationship, deal, thesis, and ticker data for a fund and
quarter, then drafts a markdown LP letter (with optional PDF-ready HTML) and
persist an ``LPUpdate`` record.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.llm import LLMProvider, get_default_provider
from axe.agents.persona_models import PersonaStyleSnapshot
from axe.db.models import (
    DealRoom,
    DealThesisVersion,
    InvestmentVehicle,
    LPRelationship,
    LPUpdate,
    PMPersona,
    TickerRegistry,
    utc_now,
)


class LPUpdateSection(BaseModel):
    """A single named section in the LP letter."""

    heading: str
    body: str


class LPUpdateContent(BaseModel):
    """Structured LP update payload produced by the agent."""

    title: str
    quarter: str
    vehicle_name: str
    performance_summary: LPUpdateSection
    top_holdings: LPUpdateSection
    new_deals: LPUpdateSection
    portfolio_news: LPUpdateSection
    key_risks: LPUpdateSection
    outlook: LPUpdateSection
    appendices: LPUpdateSection
    sources: list[str]
    generated_at: datetime


class LPUpdateAgent:
    """Gather vehicle activity and draft versioned LP update letters."""

    REQUIRED_SECTIONS = [
        "Performance Summary",
        "Top Holdings",
        "New & Deep-Dive Deals",
        "Portfolio News",
        "Key Risks",
        "Outlook",
        "Appendices",
    ]

    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider | None = None,
        *,
        pm_id: str | None = None,
        fund_entity_id: str | None = None,
        persona: PersonaStyleSnapshot | None = None,
    ) -> None:
        self.session = session
        self.provider = provider or get_default_provider()
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self.persona = persona

    async def gather_vehicle_activity(self, vehicle_id: str) -> dict[str, Any]:
        """Collect baseline vehicle and LP relationship data for an update."""
        vehicle = await self.session.get(InvestmentVehicle, vehicle_id)
        if vehicle is None:
            raise ValueError(f"Vehicle {vehicle_id} not found")

        result = await self.session.execute(
            select(LPRelationship).where(LPRelationship.vehicle_id == vehicle_id)
        )
        lps = list(result.scalars().all())

        recipient_emails = [lp.contact_email for lp in lps if lp.contact_email]

        return {
            "vehicle_id": vehicle.id,
            "vehicle_name": vehicle.name,
            "legal_entity": vehicle.legal_entity,
            "strategy": vehicle.strategy,
            "vintage": vehicle.vintage,
            "currency": vehicle.currency,
            "fund_entity_id": vehicle.fund_entity_id,
            "as_of": utc_now().strftime("%Y-%m-%d"),
            "lp_count": len(lps),
            "lp_names": [lp.lp_name for lp in lps],
            "recipient_emails": recipient_emails,
        }

    async def gather_portfolio_and_deals(
        self,
        fund_entity_id: str,
        quarter: str,
    ) -> dict[str, Any]:
        """Collect deals, theses, and public holdings touched in the quarter.

        Quarter is expected as ``YYYY-QN``. The quarter window is used to
        filter activity by ``created_at``; holdings without explicit quarter
        tags are surfaced as current positions.
        """
        window_start, window_end = self._quarter_window(quarter)

        deals_result = await self.session.execute(
            select(DealRoom)
            .where(
                DealRoom.fund_entity_id == fund_entity_id,
                DealRoom.created_at >= window_start,
                DealRoom.created_at < window_end,
            )
            .order_by(DealRoom.created_at.desc())
        )
        new_deals = list(deals_result.scalars().all())

        all_deals_result = await self.session.execute(
            select(DealRoom)
            .where(DealRoom.fund_entity_id == fund_entity_id)
            .order_by(DealRoom.created_at.desc())
        )
        all_deals = list(all_deals_result.scalars().all())

        deal_summaries: list[dict[str, Any]] = []
        for deal in all_deals[:10]:
            latest_thesis = await self._latest_deal_thesis(deal.id)
            deal_summaries.append(
                {
                    "id": deal.id,
                    "name": deal.name,
                    "stage": deal.stage,
                    "asset_class": deal.asset_class,
                    "status": deal.status,
                    "target": deal.target_ticker_or_private_name,
                    "thesis_stage": latest_thesis.stage if latest_thesis else None,
                    "key_assumptions": (latest_thesis.key_assumptions if latest_thesis else []),
                    "risks": latest_thesis.risks if latest_thesis else [],
                }
            )

        tickers: list[TickerRegistry] = []
        if self.pm_id:
            tickers_result = await self.session.execute(
                select(TickerRegistry)
                .where(TickerRegistry.pm_id == self.pm_id)
                .order_by(TickerRegistry.claimed_at.desc())
            )
            tickers = list(tickers_result.scalars().all())

        return {
            "new_deals": new_deals,
            "new_deal_count": len(new_deals),
            "deal_summaries": deal_summaries,
            "ticker_count": len(tickers),
            "tickers": [
                {
                    "ticker": t.ticker,
                    "name": t.name,
                    "asset_class": t.asset_class,
                }
                for t in tickers[:15]
            ],
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        }

    @staticmethod
    def _quarter_window(quarter: str) -> tuple[datetime, datetime]:
        """Return naive datetimes bracketing the given ``YYYY-QN`` quarter."""
        try:
            year_str, q_str = quarter.rsplit("-", 1)
            year = int(year_str)
            q = int(q_str.replace("Q", ""))
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"Quarter must be in YYYY-QN format, got {quarter}") from exc

        if not 1 <= q <= 4:
            raise ValueError(f"Quarter must be 1-4, got {q}")

        start_month = 3 * (q - 1) + 1
        end_month = start_month + 3
        end_year = year + (1 if end_month > 12 else 0)
        if end_month > 12:
            end_month -= 12

        return (
            datetime(year, start_month, 1),
            datetime(end_year, end_month, 1),
        )

    async def _latest_deal_thesis(self, deal_id: str) -> DealThesisVersion | None:
        result = await self.session.execute(
            select(DealThesisVersion)
            .where(DealThesisVersion.deal_id == deal_id)
            .order_by(desc(DealThesisVersion.version))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def draft_update(
        self,
        vehicle_id: str,
        quarter: str,
        activity: dict[str, Any] | None = None,
    ) -> LPUpdate:
        """Create a rich draft LP update with all required sections and footer."""
        vehicle = await self.session.get(InvestmentVehicle, vehicle_id)
        if vehicle is None:
            raise ValueError(f"Vehicle {vehicle_id} not found")

        if activity is None:
            vehicle_activity = await self.gather_vehicle_activity(vehicle_id)
            portfolio_activity = await self.gather_portfolio_and_deals(
                vehicle.fund_entity_id, quarter
            )
            vehicle_activity.update(portfolio_activity)
            activity = vehicle_activity

        content = await self._build_content(vehicle.name, quarter, activity)

        version_date = utc_now().strftime("%Y-%m-%d")
        sources = activity.get("sources") or ["Internal records"]
        footer_body = (
            f"Version {version_date} | Sources: {', '.join(sources)} | Draft — internal only."
        )

        sections = [
            {"heading": "Performance Summary", "body": content.performance_summary.body},
            {"heading": "Top Holdings", "body": content.top_holdings.body},
            {"heading": "New & Deep-Dive Deals", "body": content.new_deals.body},
            {"heading": "Portfolio News", "body": content.portfolio_news.body},
            {"heading": "Key Risks", "body": content.key_risks.body},
            {"heading": "Outlook", "body": content.outlook.body},
            {"heading": "Appendices", "body": content.appendices.body},
            {"heading": "Footer", "body": footer_body},
        ]

        markdown = self._build_markdown(
            title=content.title,
            quarter=content.quarter,
            vehicle_name=content.vehicle_name,
            sections=sections[:-1],
            footer=footer_body,
            sources=sources,
        )
        html = self._build_html(
            title=content.title,
            quarter=content.quarter,
            vehicle_name=content.vehicle_name,
            sections=sections[:-1],
            footer=footer_body,
            sources=sources,
        )

        update = LPUpdate(
            id=str(uuid.uuid4()),
            vehicle_id=vehicle_id,
            quarter=quarter,
            sections=sections,
            status="draft",
        )
        self.session.add(update)
        await self.session.flush()
        update.content_md = markdown
        update.content_html = html
        return update

    async def _get_persona(self) -> PersonaStyleSnapshot | None:
        """Load the current PM persona snapshot if not already injected."""
        if self.persona is not None:
            return self.persona
        if not self.pm_id:
            return None
        from axe.agents.persona import PersonaAgent

        result = await self.session.execute(
            select(PMPersona).where(PMPersona.pm_id == self.pm_id).order_by(PMPersona.created_at.desc())
        )
        model = result.scalars().first()
        if model is None:
            return None
        return PersonaAgent.snapshot_from_model(model)

    async def _build_content(
        self,
        vehicle_name: str,
        quarter: str,
        activity: dict[str, Any],
    ) -> LPUpdateContent:
        """Assemble deterministic update content.

        Uses the LLM only when an Azure-style provider is configured and returns
        a non-empty parsed payload. Otherwise falls back to deterministic text
        so tests remain stable.
        """
        from axe.agents.llm import MockProvider

        as_of = activity.get("as_of", utc_now().strftime("%Y-%m-%d"))
        new_deal_count = activity.get("new_deal_count", 0)
        deal_summaries = activity.get("deal_summaries", [])
        new_deals = activity.get("new_deals", [])
        tickers = activity.get("tickers", [])
        lp_count = activity.get("lp_count", 0)
        sources = activity.get("sources") or ["Internal records"]

        persona = await self._get_persona()

        # Try LLM for richer content when a real provider is available.
        if not isinstance(self.provider, MockProvider):
            llm_content = await self._try_llm_content(vehicle_name, quarter, activity, persona)
            if llm_content is not None:
                return llm_content

        return self._fallback_content(
            vehicle_name=vehicle_name,
            quarter=quarter,
            as_of=as_of,
            new_deal_count=new_deal_count,
            new_deals=new_deals,
            deal_summaries=deal_summaries,
            tickers=tickers,
            lp_count=lp_count,
            sources=sources,
        )

    async def _try_llm_content(
        self,
        vehicle_name: str,
        quarter: str,
        activity: dict[str, Any],
        persona: PersonaStyleSnapshot | None = None,
    ) -> LPUpdateContent | None:
        """Return structured LLM content when available."""
        system_content = (
            "You are an investor relations author drafting a quarterly LP "
            "letter. Produce concise, professional sections."
        )
        if persona:
            system_content += (
                "\n\nAdapt the tone to match the PM's persona:\n"
                + persona.render_system_prompt_snippet()
            )
        messages = [
            {"role": "system", "content": system_content},
            {
                "role": "user",
                "content": self._build_prompt(vehicle_name, quarter, activity),
            },
        ]
        try:
            response = await self.provider.complete(
                messages, temperature=0.3, response_schema=LPUpdateContent
            )
        except Exception:  # pragma: no cover - defensive fallback
            return None
        parsed = response.parsed
        if parsed:
            try:
                return LPUpdateContent.model_validate(parsed)
            except Exception:  # pragma: no cover - defensive fallback
                pass
        return None

    def _build_prompt(
        self,
        vehicle_name: str,
        quarter: str,
        activity: dict[str, Any],
    ) -> str:
        return (
            f"Vehicle: {vehicle_name}\n"
            f"Quarter: {quarter}\n"
            f"As of: {activity.get('as_of', 'N/A')}\n"
            f"LP count: {activity.get('lp_count', 0)}\n"
            f"New deals this quarter: {activity.get('new_deal_count', 0)}\n"
            f"Deal summaries: {activity.get('deal_summaries', [])}\n"
            f"Top holdings / tickers: {activity.get('tickers', [])}\n\n"
            "Produce the LP update letter sections as specified JSON."
        )

    def _fallback_content(
        self,
        *,
        vehicle_name: str,
        quarter: str,
        as_of: str,
        new_deal_count: int,
        new_deals: list[Any],
        deal_summaries: list[dict[str, Any]],
        tickers: list[dict[str, Any]],
        lp_count: int,
        sources: list[str],
    ) -> LPUpdateContent:
        """Deterministic LP update content used when no LLM is configured."""
        perf_body = (
            f"Performance summary for {vehicle_name} for {quarter} (as of {as_of}). "
            "Portfolio-level metrics, NAV commentary, and benchmark context "
            "will be inserted here."
        )

        if tickers:
            top_holdings_body = "\n".join(
                f"- {t.get('ticker', 'N/A')}: {t.get('name') or 'Unnamed'} "
                f"({t.get('asset_class', 'unknown')})"
                for t in tickers[:10]
            )
        else:
            top_holdings_body = "No public holdings on file."

        new_deals_body: str
        if new_deals:
            new_deals_body = "\n".join(
                f"- {getattr(d, 'name', 'Unnamed deal')} ({getattr(d, 'stage', 'unknown stage')})"
                for d in new_deals[:10]
            )
        else:
            new_deals_body = f"No new deals originated in {quarter}."

        deep_dive = "\n".join(
            f"- {d.get('name', 'Unnamed')}: {d.get('thesis_stage') or 'thesis in progress'}"
            for d in deal_summaries[:5]
        )
        if deep_dive:
            new_deals_body += "\n\nDeep dives:\n" + deep_dive

        news_body = (
            f"Portfolio news for {vehicle_name} during {quarter}: "
            "follow-ons, realisations, management changes, and key milestones."
        )

        risks_body = (
            "\n".join(f"- {risk}" for d in deal_summaries[:5] for risk in (d.get("risks") or []))
            or "No material risks flagged in current theses."
        )

        outlook_body = (
            f"Forward-looking commentary for {vehicle_name}: "
            "market outlook, capital deployment plans, and strategic priorities."
        )

        appendices_body = "Supplementary tables, capital account statements, and disclosures."

        return LPUpdateContent(
            title=f"{vehicle_name} - {quarter} LP Update",
            quarter=quarter,
            vehicle_name=vehicle_name,
            performance_summary=LPUpdateSection(heading="Performance Summary", body=perf_body),
            top_holdings=LPUpdateSection(heading="Top Holdings", body=top_holdings_body),
            new_deals=LPUpdateSection(heading="New & Deep-Dive Deals", body=new_deals_body),
            portfolio_news=LPUpdateSection(heading="Portfolio News", body=news_body),
            key_risks=LPUpdateSection(heading="Key Risks", body=risks_body),
            outlook=LPUpdateSection(heading="Outlook", body=outlook_body),
            appendices=LPUpdateSection(heading="Appendices", body=appendices_body),
            sources=sources,
            generated_at=utc_now(),
        )

    @staticmethod
    def _build_markdown(
        *,
        title: str,
        quarter: str,
        vehicle_name: str,
        sections: list[dict[str, str]],
        footer: str,
        sources: list[str],
    ) -> str:
        lines = [
            f"# {title}",
            "",
            f"**Vehicle:** {vehicle_name}  ",
            f"**Quarter:** {quarter}  ",
            "",
        ]
        for section in sections:
            lines.append(f"## {section['heading']}")
            lines.append(section["body"])
            lines.append("")
        if sources:
            lines.append("**Sources:** " + ", ".join(sources))
            lines.append("")
        lines.append(f"_{footer}_")
        return "\n".join(lines).strip()

    @staticmethod
    def _build_html(
        *,
        title: str,
        quarter: str,
        vehicle_name: str,
        sections: list[dict[str, str]],
        footer: str,
        sources: list[str],
    ) -> str:
        body_parts = [
            f"<h1>{_esc(title)}</h1>",
            f"<p><strong>Vehicle:</strong> {_esc(vehicle_name)}<br>",
            f"<strong>Quarter:</strong> {_esc(quarter)}</p>",
        ]
        for section in sections:
            body_parts.append(f"<h2>{_esc(section['heading'])}</h2>")
            body_parts.append(f"<p>{_esc(section['body']).replace(chr(10), '<br>')}</p>")
        if sources:
            body_parts.append(
                "<p><strong>Sources:</strong> " + ", ".join(_esc(s) for s in sources) + "</p>"
            )
        body_parts.append(f"<p><em>{_esc(footer)}</em></p>")
        body = "\n".join(body_parts)
        return (
            "<!DOCTYPE html>\n<html>\n<head>\n"
            f"<title>{_esc(title)}</title>\n"
            "</head>\n<body>\n"
            f"{body}\n"
            "</body>\n</html>"
        )


class ComplianceGateError(RuntimeError):
    """Raised when an external communication is attempted without human approval."""


async def send_lp_update(
    update: LPUpdate,
    approved_by: str | None = None,
    sent_at: datetime | None = None,
) -> LPUpdate:
    """Compliance gate: external LP updates require human approval before sending.

    This function intentionally does not deliver email; it only updates the
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


def _esc(value: str) -> str:
    """Basic XML/HTML escape for PDF-ready HTML generation."""
    import html as _html

    return _html.escape(value)


__all__ = [
    "ComplianceGateError",
    "LPUpdateAgent",
    "LPUpdateContent",
    "LPUpdateSection",
    "send_lp_update",
]
