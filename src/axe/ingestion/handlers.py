"""Ingestion queue handlers mapping incoming signals to alerts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.drift_detect import EarningsAlertService
from axe.db.models import PMUser, RetryQueue, utc_now
from axe.db.uow import UnitOfWork
from axe.ingestion.dedup import DedupService
from axe.services.alert import AlertDelivery, dispatch_earnings_alert
from axe.services.connector import normalize_payload_to_raw_ingest
from axe.services.mnpi import MNPIService

logger = logging.getLogger(__name__)


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

    async with UnitOfWork(session) as uow:
        service = EarningsAlertService(uow)
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
        if not alerts:
            return True

        # MNPI screen before any alert is dispatched. The first alert carries
        # the signal_id because process_signal creates one SignalLog per alert.
        signal_id_for_review = alerts[0].get("signal_id") or signal_id or ""
        mnpi_service = MNPIService(session)
        outcome = await mnpi_service.review_signal(
            signal_id=signal_id_for_review,
            signal_text=signal_text,
            ticker=ticker,
            pm_id=pm_id,
            alert_payloads=alerts,
        )
        if outcome.blocked:
            # Alerts are held on the review row; do not enqueue dispatch yet.
            await uow.commit()
            return True

    deadline = arrived_at + timedelta(seconds=EarningsAlertService.ALERT_SLA_SECONDS)

    for alert in alerts:
        alert_payload = {
            "pm_id": pm_id,
            "slack_user_id": payload.get("slack_user_id"),
            "email": payload.get("email"),
            "ticker": alert["ticker"],
            "signal_id": alert["signal_id"],
            "assumption_id": alert.get("assumption_id"),
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


async def process_connector_payload_handler(
    session: AsyncSession,
    payload: dict[str, Any],
) -> bool:
    """Normalize a pushed payload, persist it as RawIngest, and enqueue specialist work.

    Expected payload keys:
      - pm_id (str)
      - source_type (str)
      - external_id (str, optional)
      - raw_payload (dict, optional)
      - extracted_signal (dict, optional)
      - ticker (str, optional)
    """
    pm_id = payload.get("pm_id")
    source_type = payload.get("source_type")

    if not pm_id or not source_type:
        logger.error("process_connector_payload missing pm_id or source_type")
        return True

    raw_payload = payload.get("raw_payload") or {}
    extracted_signal = payload.get("extracted_signal") or {}
    ticker = payload.get("ticker")
    external_id = payload.get("external_id")

    raw = await normalize_payload_to_raw_ingest(
        pm_id=pm_id,
        source_type=source_type,
        external_id=external_id,
        raw_payload=raw_payload,
        extracted_signal=extracted_signal,
        ticker=ticker,
    )

    dedup = DedupService(session)
    if await dedup.is_duplicate(raw.content_hash, source_id=raw.dedup_key):
        logger.info(
            "Duplicate connector payload dropped: source_type=%s pm_id=%s",
            source_type,
            pm_id,
        )
        return True

    async with UnitOfWork(session) as uow:
        existing = await uow.raw_ingests.get_by_content_hash(raw.content_hash)
        if existing is not None:
            logger.info(
                "Duplicate raw_ingest found by content_hash: source_type=%s pm_id=%s",
                source_type,
                pm_id,
            )
            return True

        session.add(raw)
        await session.flush()

        task = RetryQueue(
            pm_id=pm_id,
            task_type="specialize_signal",
            payload={
                "pm_id": pm_id,
                "raw_ingest_id": raw.id,
                "source_type": source_type,
                "_content_hash": raw.content_hash,
                "_idempotency_key": raw.dedup_key,
            },
        )
        session.add(task)
        await dedup.mark_seen(
            raw.content_hash,
            source_type=source_type,
            source_id=raw.dedup_key,
        )

    logger.info(
        "Connector payload persisted: raw_ingest_id=%s source_type=%s pm_id=%s",
        raw.id,
        source_type,
        pm_id,
    )
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
    "process_connector_payload_handler",
    "send_alert_handler",
    "_parse_iso",
]
