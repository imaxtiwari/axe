"""Retention policy service for compliant data lifecycle management.

The service performs soft-deletes on records past their configured retention
period.  Hard deletes are never issued; the ``deleted_at`` timestamp both
preserves the row for audit and hides it from normal product queries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from axe.config import Settings, get_settings
from axe.db.models import (
    CommunicationArchive,
    MeetingSummary,
    MorningBrief,
    SignalLog,
    SparringSession,
    ThesisPostMortem,
    ThesisTestResult,
    ThesisVersion,
    utc_now,
)
from axe.security.audit import AuditService

logger = logging.getLogger(__name__)

# Mapping from configured entity type names to model classes.
_RETENTION_MODELS: dict[str, type] = {
    "signal_log": SignalLog,
    "meeting_summary": MeetingSummary,
    "morning_brief": MorningBrief,
    "sparring_session": SparringSession,
    "thesis_version": ThesisVersion,
    "thesis_test_result": ThesisTestResult,
    "thesis_post_mortem": ThesisPostMortem,
    "communication_archive": CommunicationArchive,
}


class RetentionService:
    """Soft-delete records that have exceeded the configured retention period."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()

    @property
    def _cutoff(self) -> datetime:
        return utc_now() - timedelta(days=self.settings.retention_days)

    async def run(
        self,
        *,
        entity_types: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run the retention policy job and return a summary of actions taken.

        Args:
            entity_types: Optional subset of entity types to process.  Defaults
                to ``settings.retention_entity_types``.
            dry_run: If True, count candidates but do not modify rows.

        Returns:
            A dict with total soft-deleted counts per entity type, the cutoff
            timestamp used, and a flag indicating whether changes were applied.
        """
        if not self.settings.retention_enabled and not dry_run:
            return {
                "enabled": False,
                "dry_run": dry_run,
                "cutoff": self._cutoff.isoformat(),
                "counts": {},
            }

        types = entity_types or self.settings.retention_entity_types
        counts: dict[str, int] = {}
        total = 0

        for name in types:
            model = _RETENTION_MODELS.get(name)
            if model is None:
                logger.warning("Unknown retention entity type: %s", name)
                continue

            stmt = select(model).where(
                and_(
                    model.created_at < self._cutoff,
                    model.retention_exempt.is_(False),
                    model.deleted_at.is_(None),
                )
            )
            result = await self.session.execute(stmt)
            rows = result.scalars().all()
            row_ids = [row.id for row in rows]
            count = len(row_ids)
            counts[name] = count
            total += count

            if count == 0 or dry_run:
                continue

            await self.session.execute(
                update(model).where(model.id.in_(row_ids)).values(deleted_at=utc_now())
            )

        summary = {
            "enabled": self.settings.retention_enabled,
            "dry_run": dry_run,
            "cutoff": self._cutoff.isoformat(),
            "counts": counts,
            "total": total,
        }

        if not dry_run and total > 0:
            audit_service = AuditService(self.session)
            await audit_service.log(
                action_type="retention_soft_delete",
                object_type="retention_job",
                object_id=str(utc_now().timestamp()),
                after_state=summary,
                non_blocking=False,
            )
            # Ensure the audit row travels with the caller's session.
            await self.session.flush()

        return summary

    async def count_pending(self) -> dict[str, int]:
        """Return candidate counts per entity type without deleting anything."""
        result = await self.run(dry_run=True)
        counts = result.get("counts")
        if counts is None:
            return {}
        # ``run`` returns dict[str, int] for counts even though the top-level
        # return type is dict[str, Any]; cast for type checkers.
        return {str(k): int(v) for k, v in counts.items()}
