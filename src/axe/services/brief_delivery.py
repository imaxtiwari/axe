"""Format and deliver morning briefs via Slack DM and/or email."""

from __future__ import annotations

from typing import Any

from axe.agents.morning_brief import MorningBriefOutput
from axe.services.alert import AlertDelivery


def format_brief(brief: MorningBriefOutput) -> str:
    """Render a concise text/markdown brief from a structured brief object."""
    lines: list[str] = ["📅 *Your Morning Brief*", ""]

    focus = brief.focus_one
    if focus:
        lines.append(f"🔥 *Focus One: {focus.ticker}*")
        lines.append(f"_{focus.reason}_")
        lines.append(f"Urgency score: {focus.urgency_score:.2f}")
        lines.append("")

    if brief.sections:
        lines.append("*Signals vs Thesis*")
        for section in brief.sections:
            emoji = {"CONFIRMS": "✅", "CONTRADICTS": "🚨", "NEUTRAL": "➖", "UNCERTAIN": "❓"}.get(
                section.stance, "•"
            )
            lines.append(
                f"{emoji} *{section.ticker}* — {section.headline} "
                f"(score {section.relevance_score:.2f}, {section.stance})"
            )
            lines.append(f"   Assumption: {section.assumption_text}")
            lines.append(f"   {section.body}")
            if section.source_ids:
                lines.append(f"   Sources: {', '.join(section.source_ids)}")
            lines.append("")

    if brief.catalyst_week:
        lines.append("*Catalysts This Week*")
        for catalyst in brief.catalyst_week:
            ticker = f" [{catalyst.ticker}]" if catalyst.ticker else ""
            lines.append(
                f"• {catalyst.date}{ticker} — {catalyst.event_type}: {catalyst.description}"
            )
        lines.append("")

    lines.append("Reply here to update thesis, ask follow-up, or dismiss a signal.")
    return "\n".join(lines)


async def deliver_brief(
    brief: MorningBriefOutput,
    slack_user_id: str | None,
    email: str | None,
    delivery: AlertDelivery | None = None,
) -> dict[str, Any]:
    """Deliver a formatted brief to Slack and/or email.

    Returns a dict with ``slack_ok`` and ``email_ok`` booleans.
    """
    delivery = delivery or AlertDelivery()
    message = format_brief(brief)

    results: dict[str, Any] = {}
    if slack_user_id:
        try:
            slack_result = await delivery.send_slack_dm(slack_user_id, message)
            results["slack_ok"] = bool(slack_result.get("ok"))
        except Exception:
            results["slack_ok"] = False
    else:
        results["slack_ok"] = False

    if email:
        try:
            email_result = await delivery.send_email(email, "Your Morning Brief", message)
            results["email_ok"] = bool(email_result.get("id")) or email_result.get("ok", True)
        except Exception:
            results["email_ok"] = False
    else:
        results["email_ok"] = False

    results["message"] = message
    return results


__all__ = ["format_brief", "deliver_brief"]
