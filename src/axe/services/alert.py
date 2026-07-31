"""Alert delivery service for thesis drift and earnings alerts."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from axe.config import get_settings

logger = logging.getLogger(__name__)


class AlertDelivery:
    """Deliver alert payloads via Slack and/or email."""

    def __init__(
        self,
        slack_bot_token: str | None = None,
        slack_post_hook: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        resend_api_key: str | None = None,
        from_email: str | None = None,
        resend_post_hook: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        settings = get_settings()
        self.slack_bot_token = slack_bot_token or settings.slack_bot_token
        self.slack_post_hook = slack_post_hook
        self.resend_api_key = resend_api_key or settings.resend_api_key
        settings_from = getattr(settings, "axe_from_email", None)
        self.from_email = (
            from_email or settings_from or settings.axe_email_domain or "alerts@axe.fund"
        )
        self.resend_post_hook = resend_post_hook

    async def send_slack_dm(
        self,
        slack_user_id: str,
        message: str,
    ) -> dict[str, Any]:
        """Open a DM conversation with ``slack_user_id`` and post ``message``."""
        if self.slack_post_hook:
            return await self.slack_post_hook(
                method="chat.postMessage",
                json={"channel": slack_user_id, "text": message, "as_user": True},
            )
        if not self.slack_bot_token:
            raise RuntimeError("Slack bot token is not configured")

        headers = {
            "Authorization": f"Bearer {self.slack_bot_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            # Open conversation to get a DM channel ID.
            conv = await client.post(
                "https://slack.com/api/conversations.open",
                headers=headers,
                json={"users": slack_user_id},
            )
            conv_data = conv.json()
            channel_id = conv_data.get("channel", {}).get("id")
            if not channel_id:
                logger.error("Unable to open Slack DM: %s", conv_data)
                return {"ok": False, "error": conv_data.get("error", "no_channel")}

            response = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={
                    "channel": channel_id,
                    "text": message,
                    "as_user": True,
                },
            )
            return response.json()

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
    ) -> dict[str, Any]:
        """Send an email via Resend."""
        if self.resend_post_hook:
            return await self.resend_post_hook(
                json={
                    "from": self.from_email,
                    "to": to_email,
                    "subject": subject,
                    "text": body,
                },
            )
        if not self.resend_api_key:
            raise RuntimeError("Resend API key is not configured")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {self.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": self.from_email,
                    "to": to_email,
                    "subject": subject,
                    "text": body,
                },
            )
            return response.json()

    async def dispatch(
        self,
        payload: dict[str, Any],
        slack_user_id: str | None,
        email: str | None,
    ) -> dict[str, Any]:
        """Dispatch an alert payload to Slack and/or email per payload flags."""
        results: dict[str, Any] = {"slack": None, "email": None}
        message = payload.get("message", "")
        subject = message.split(". Evidence:")[0] if ". Evidence:" in message else message

        if payload.get("slack_enabled") and slack_user_id:
            results["slack"] = await self.send_slack_dm(slack_user_id, message)
        if payload.get("email_enabled") and email:
            results["email"] = await self.send_email(email, subject, message)
        return results


async def dispatch_earnings_alert(
    alert_payload: dict[str, Any],
    slack_user_id: str | None,
    email: str | None,
    deadline_utc: datetime | None = None,
    delivery: AlertDelivery | None = None,
) -> dict[str, Any]:
    """Dispatch an earnings alert and verify it was sent within SLA.

    The ``deadline_utc`` defaults to 30 minutes from now. If dispatch completes
    after the deadline, a ``sla_violation`` flag is set.
    """
    deadline = deadline_utc or (datetime.now(UTC) + timedelta(minutes=30))
    delivery = delivery or AlertDelivery()

    result = await delivery.dispatch(alert_payload, slack_user_id, email)
    result["dispatched_at_utc"] = datetime.now(UTC).isoformat()
    result["deadline_utc"] = deadline.isoformat()
    result["sla_violation"] = datetime.now(UTC) > deadline
    return result


__all__ = ["AlertDelivery", "dispatch_earnings_alert"]
