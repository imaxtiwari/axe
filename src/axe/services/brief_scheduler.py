"""APScheduler-based morning brief dispatcher."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from axe.agents.morning_brief import MorningBriefAgent, MorningBriefOutput, is_nyse_trading_day
from axe.db.models import MorningBrief, PMUser
from axe.services.brief_delivery import deliver_brief

logger = logging.getLogger(__name__)


def create_scheduler():
    """Return an APScheduler AsyncIOScheduler instance."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    return AsyncIOScheduler(timezone="UTC")


async def generate_and_deliver_for_pm(
    session: AsyncSession,
    pm: PMUser,
    as_of: datetime | None = None,
) -> MorningBrief | None:
    """Generate and deliver a brief for a single PM; return the saved MorningBrief."""
    if not is_nyse_trading_day(as_of.date() if as_of else date.today()):
        logger.info("Skipping brief for %s — not a NYSE trading day", pm.id)
        return None

    agent = MorningBriefAgent(session)
    brief = await agent.generate(pm.id, as_of=as_of)

    delivery_result = await deliver_brief(
        brief,
        slack_user_id=pm.slack_user_id,
        email=pm.email,
    )

    async def _deliver(_brief: MorningBriefOutput) -> dict[str, Any]:
        return delivery_result

    return await agent.save_and_deliver(pm.id, brief, deliver_fn=_deliver)


async def deliver_briefs_to_all_active_pms(
    session_maker: async_sessionmaker[AsyncSession],
    as_of: datetime | None = None,
) -> list[str]:
    """Generate and deliver briefs for all active PMs on a trading day."""
    as_of = as_of or datetime.now(UTC)
    if not is_nyse_trading_day(as_of.date()):
        logger.info("Not a NYSE trading day; no briefs sent.")
        return []

    delivered_ids: list[str] = []
    async with session_maker() as session:
        result = await session.execute(
            select(PMUser).where(PMUser.active == True)  # noqa: E712
        )
        pms = result.scalars().all()
        for pm in pms:
            try:
                saved = await generate_and_deliver_for_pm(session, pm, as_of=as_of)
                if saved:
                    delivered_ids.append(saved.id)
            except Exception:
                logger.exception("Failed to generate brief for pm=%s", pm.id)
    return delivered_ids


def schedule_brief_jobs(
    scheduler,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Register the 07:00 UTC Mon-Fri morning-brief cron job."""
    from apscheduler.triggers.cron import CronTrigger

    async def _job() -> None:
        await deliver_briefs_to_all_active_pms(session_maker)

    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=7, minute=0, day_of_week="mon-fri", timezone="UTC"),
        id="morning_brief_0700_utc",
        replace_existing=True,
    )


__all__ = [
    "create_scheduler",
    "deliver_briefs_to_all_active_pms",
    "generate_and_deliver_for_pm",
    "schedule_brief_jobs",
]
