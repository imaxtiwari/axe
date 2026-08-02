"""Audit log export router (compliance only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import AuditLog
from axe.db.session import get_async_session
from axe.security.authz import require_role

router = APIRouter(
    prefix="/api/v1/audit",
    tags=["audit"],
    dependencies=[Depends(require_role("compliance", "admin"))],
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
