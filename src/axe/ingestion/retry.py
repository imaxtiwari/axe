"""Async retry queue service backed by ``retry_queue``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import RetryQueue as RetryQueueModel

_BACKOFF_MINUTES = [1, 5, 15, 60, 240]


def _next_run_time(attempts_made: int, now: datetime | None = None) -> datetime:
    """Compute the next retry run time from a fixed backoff schedule.

    ``attempts_made`` is the number of failed attempts already recorded.
    The schedule is: 1m, 5m, 15m, 1h, 4h. After the max index, clamp to the
    last value (the task will be dead-lettered before that anyway).
    """
    if now is None:
        now = datetime.now(UTC)
    offset = _BACKOFF_MINUTES[min(attempts_made - 1, len(_BACKOFF_MINUTES) - 1)]
    return now + timedelta(minutes=offset)


class RetryQueue:
    """Enqueue, inspect, and manage retry state for idempotent async tasks."""

    def __init__(self, db: AsyncSession, max_attempts: int = 5) -> None:
        self._db = db
        self._max_attempts = max_attempts

    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        pm_id: str | None = None,
    ) -> RetryQueueModel:
        """Insert a new pending task into the retry queue."""
        task = RetryQueueModel(
            id=str(uuid4()),
            task_type=task_type,
            payload=payload,
            pm_id=pm_id,
            attempts=0,
            status="pending",
            broker_attempted=False,
        )
        self._db.add(task)
        await self._db.flush()
        return task

    async def dequeue(self, now: datetime | None = None) -> RetryQueueModel | None:
        """Pick the oldest pending task whose retry window has elapsed.

        Pending, never-attempted tasks are always eligible. Failed tasks are
        eligible once ``next_run_at`` (computed from ``last_attempted_at``) is
        in the past. The task is returned but not mutated; callers should use
        ``mark_success`` or ``mark_failed_with_backoff``.
        """
        if now is None:
            now = datetime.now(UTC)
        # For failed tasks, we compare last_attempted_at + backoff to now.
        # Since we store last_attempted_at bare (no tz), treat it as UTC.
        stmt = (
            select(RetryQueueModel)
            .where(RetryQueueModel.status.in_(["pending", "failed"]))
            .where(RetryQueueModel.attempts < self._max_attempts)
            .order_by(RetryQueueModel.created_at.asc())
        )
        result = await self._db.execute(stmt)
        for task in result.scalars().all():
            if task.attempts == 0 or task.last_attempted_at is None:
                return task
            # Attempts already made == index into schedule for next backoff.
            next_at = _next_run_time(task.attempts, task.last_attempted_at)
            if next_at <= now:
                return task
        return None

    async def mark_success(self, task_id: str) -> RetryQueueModel | None:
        """Mark a task as succeeded."""
        result = await self._db.execute(
            select(RetryQueueModel).where(RetryQueueModel.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.status = "succeeded"
        task.last_attempted_at = datetime.now(UTC)
        await self._db.flush()
        return task

    async def mark_failed_with_backoff(self, task_id: str) -> RetryQueueModel | None:
        """Increment attempts, update last_attempted_at, and dead-letter if needed."""
        result = await self._db.execute(
            select(RetryQueueModel).where(RetryQueueModel.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.attempts += 1
        task.last_attempted_at = datetime.now(UTC)
        task.status = "failed"
        if task.attempts >= self._max_attempts:
            task.status = "dead_letter"
            task.dead_letter_at = datetime.now(UTC)
        await self._db.flush()
        return task

    async def dead_letter_after(self, task_id: str, attempts: int = 5) -> RetryQueueModel | None:
        """Force a task to dead-letter status regardless of current attempts."""
        result = await self._db.execute(
            select(RetryQueueModel).where(RetryQueueModel.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return None
        task.attempts = attempts
        task.status = "dead_letter"
        task.dead_letter_at = datetime.now(UTC)
        await self._db.flush()
        return task


__all__ = ["RetryQueue", "_next_run_time"]
