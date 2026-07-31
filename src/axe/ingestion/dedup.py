"""Deduplication service backed by ``dedup_log``."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import DedupLog


class DedupService:
    """Check and record content hashes to prevent duplicate ingestion."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def is_duplicate(self, content_hash: str, source_id: str | None = None) -> bool:
        """Return True if ``content_hash`` (optionally scoped by source_id) already seen."""
        stmt = select(DedupLog).where(DedupLog.content_hash == content_hash)
        if source_id is not None:
            stmt = stmt.where(DedupLog.source_id == source_id)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def mark_seen(
        self,
        content_hash: str,
        source_type: str,
        source_id: str | None = None,
    ) -> DedupLog:
        """Record a content hash as seen, or return the existing row.

        ``source_id`` is optional; for stricter per-source dedup pass the
        source-specific idempotency key (e.g. Gmail message id, Slack event id).
        """
        existing = await self._db.execute(
            select(DedupLog).where(DedupLog.content_hash == content_hash)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = DedupLog(
                content_hash=content_hash,
                source_id=source_id,
                source_type=source_type,
                first_seen_at=datetime.now(UTC),
            )
            self._db.add(row)
            await self._db.flush()
        return row
