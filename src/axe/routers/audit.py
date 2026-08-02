"""Audit log export router (compliance only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import AuditLog
from axe.db.session import get_async_session
from axe.security.authz import require_role
from axe.services.retention import RetentionService

router = APIRouter(
    prefix="/api/v1/audit",
    tags=["audit"],
    dependencies=[Depends(require_role("compliance"))],
)


@router.get("/log")
async def export_audit_log(
    limit: int = 100,
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Export recent audit log entries for compliance review.

    This endpoint is intentionally read-only and restricted to compliance
    officers so they can inspect operational and security-relevant events.
    """
    result = await session.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    )
    rows = result.scalars().all()
    return {
        "entries": [
            {
                "id": row.id,
                "action_type": row.action_type,
                "object_type": row.object_type,
                "object_id": row.object_id,
                "pm_id": row.pm_id,
                "fund_entity_id": row.fund_entity_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "before_state": row.before_state,
                "after_state": row.after_state,
            }
            for row in rows
        ]
    }


@router.post("/retention/run")
async def trigger_retention_run(
    dry_run: bool = True,
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Trigger the data retention soft-delete job.

    Defaults to a dry run so callers can preview candidate counts. Set
    ``dry_run=false`` to actually apply soft-deletes and write an audit log
    entry for the run.
    """
    service = RetentionService(session)
    summary = await service.run(dry_run=dry_run)
    return summary
