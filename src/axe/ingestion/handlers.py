"""Ingestion queue handlers mapping incoming signals to alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.drift_detect import EarningsAlertService
from axe.db.models import PMUser, RetryQueue, utc_now
from axe.services.alert import AlertDelivery, dispatch_earnings_alert


def _parse_iso(ts: Any | None) -> datetime:
    """Parse an ISO timestamp or return the current UTC time."""
    if isinstance(ts, datetime):
        return ts
    if ts:
        try:
            parsed = datetime.fromisoformat(str(ts))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            pass
    return utc_now()


async def process_transcript_handler(
    session: AsyncSession,
    payload: dict[str, Any],
) -> bool:
    """Evaluate a transcript/signal against the PM's thesis and enqueue alerts.

    Expected payload keys:
      - pm_id (str)
      - ticker (str)
      - source_type (str)
      - source_url (str, optional)
      - signal_text (str)
      - raw_content (str, optional)
      - content_hash (str, optional)
      - signal_id (str, optional)
      - arrived_at (ISO datetime string, optional)
    """
    pm_id = payload.get("pm_id")
    ticker = payload.get("ticker")
    source_type = payload.get("source_type")
    source_url = payload.get("source_url")
    signal_text = payload.get("signal_text", "")

    if not pm_id or not ticker or not source_type or not signal_text:
        # Missing required fields; do not retry.
        return True

    arrived_at = _parse_iso(payload.get("arrived_at"))
    content_hash = payload.get("content_hash") or ""
    signal_id = payload.get("signal_id")

    service = EarningsAlertService(session)
    alerts = await service.process_signal(
        pm_id=pm_id,
        ticker=ticker,
        source_type=source_type,
        source_url=source_url,
        signal_text=signal_text,
        signal_id=signal_id,
        raw_content=payload.get("raw_content"),
        content_hash=content_hash,
        arrived_at=arrived_at,
    )

    deadline = arrived_at + timedelta(seconds=EarningsAlertService.ALERT_SLA_SECONDS)

    for alert in alerts:
        alert_payload = {
            "pm_id": pm_id,
            "slack_user_id": payload.get("slack_user_id"),
            "email": payload.get("email"),
            "ticker": alert["ticker"],
            "signal_id": alert["signal_id"],
            "assumption_id": alert["assumption_id"],
            "message": alert["message"],
            "source_url": alert.get("source_url"),
            "deadline_utc": deadline.isoformat(),
            "arrived_at_utc": arrived_at.isoformat(),
        }
        task = RetryQueue(
            pm_id=pm_id,
            task_type="send_alert",
            payload=alert_payload,
        )
        session.add(task)

    await session.flush()
    return True


async def send_alert_handler(
    session: AsyncSession,
    payload: dict[str, Any],
    alert_delivery: AlertDelivery | None = None,
) -> bool:
    """Dispatch a single earnings alert to configured Slack/email channels.

    The handler returns ``True`` on successful dispatch and raises on failure so
    the worker retry logic can drive re-attempts.
    """
    delivery = alert_delivery or AlertDelivery()
    pm_id = payload.get("pm_id")
    slack_user_id = payload.get("slack_user_id")
    email = payload.get("email")

    if (not slack_user_id or not email) and pm_id:
        user = await session.execute(select(PMUser).where(PMUser.id == pm_id))
        row = user.scalar_one_or_none()
        if row:
            slack_user_id = slack_user_id or row.slack_user_id
            email = email or row.email

    deadline_utc = payload.get("deadline_utc")
    deadline = _parse_iso(deadline_utc)

    result = await dispatch_earnings_alert(
        payload,
        slack_user_id=slack_user_id,
        email=email,
        deadline_utc=deadline,
        delivery=delivery,
    )

    dispatched = result.get("dispatched") or result.get("sent", False)
    sla_violation = result.get("sla_violation", False)
    error = result.get("error")

    if not dispatched and not sla_violation:
        raise RuntimeError(error or "Alert dispatch failed")

    return True


__all__ = [
    "process_transcript_handler",
    "send_alert_handler",
    "_parse_iso",
]
