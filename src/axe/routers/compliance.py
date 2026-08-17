"""API router for compliance escalations.

Provides endpoints for listing, assigning, and resolving compliance
escalations. All endpoints require a compliance or admin role and respect
fund-scoped isolation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.session import get_async_session
from axe.security.authz import COMPLIANCE_ROLES, require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.compliance_escalation import (
    ComplianceEscalationService,
    ComplianceEscalationTrigger,
)

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])


class AssignRequest(BaseModel):
    """Request body for assigning an escalation to a reviewer."""

    reviewer_id: str


class ResolveRequest(BaseModel):
    """Request body for resolving an escalation."""

    decision: str  # approved | rejected | dismissed
    note: str | None = None


class EscalationOut(BaseModel):
    """Minimal serialization of a compliance escalation."""

    id: str
    pm_id: str | None
    fund_entity_id: str
    trigger_type: str
    severity: str
    status: str
    reviewer_id: str | None
    details: dict[str, Any]
    opened_at: str | None
    closed_at: str | None

    class Config:
        from_attributes = True


class EscalationListResponse(BaseModel):
    """Response wrapper for listing escalations."""

    escalations: list[EscalationOut]


class EscalationActionResponse(BaseModel):
    """Response wrapper for assignment/resolution actions."""

    escalation_id: str
    status: str
    reviewer_id: str | None


def _serialize(escalation: Any) -> dict[str, Any]:
    return {
        "id": escalation.id,
        "pm_id": escalation.pm_id,
        "fund_entity_id": escalation.fund_entity_id,
        "trigger_type": escalation.trigger_type,
        "severity": escalation.severity,
        "status": escalation.status,
        "reviewer_id": escalation.reviewer_id,
        "details": escalation.details,
        "opened_at": (
            escalation.opened_at.isoformat() if escalation.opened_at else None
        ),
        "closed_at": (
            escalation.closed_at.isoformat() if escalation.closed_at else None
        ),
    }


@router.get(
    "/escalations",
    response_model=EscalationListResponse,
    dependencies=[Depends(require_role(*COMPLIANCE_ROLES))],
)
async def list_escalations(
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """List open compliance escalations for the current fund."""
    service = ComplianceEscalationService(session)
    escalations = await service.list_open(
        pm_id=ctx.pm_id,
        fund_id=ctx.fund_id,
        role=ctx.role,
    )
    return {"escalations": [_serialize(e) for e in escalations]}


@router.post(
    "/escalations/{escalation_id}/assign",
    response_model=EscalationActionResponse,
    dependencies=[Depends(require_role(*COMPLIANCE_ROLES))],
)
async def assign_escalation(
    escalation_id: str,
    body: AssignRequest,
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Assign a compliance escalation to a reviewer."""
    service = ComplianceEscalationService(session)
    try:
        escalation = await service.assign_reviewer(
            escalation_id=escalation_id,
            reviewer_id=body.reviewer_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await session.commit()
    return {
        "escalation_id": escalation.id,
        "status": escalation.status,
        "reviewer_id": escalation.reviewer_id,
    }


@router.post(
    "/escalations/{escalation_id}/resolve",
    response_model=EscalationActionResponse,
    dependencies=[Depends(require_role(*COMPLIANCE_ROLES))],
)
async def resolve_escalation(
    escalation_id: str,
    body: ResolveRequest,
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Resolve a compliance escalation with a decision and optional note."""
    service = ComplianceEscalationService(session)
    try:
        escalation = await service.resolve(
            escalation_id=escalation_id,
            decision=body.decision,  # type: ignore[arg-type]
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await session.commit()
    return {
        "escalation_id": escalation.id,
        "status": escalation.status,
        "reviewer_id": escalation.reviewer_id,
    }


@router.post(
    "/escalations/trigger",
    response_model=EscalationOut,
    dependencies=[Depends(require_role(*COMPLIANCE_ROLES))],
)
async def create_escalation_trigger(
    body: dict[str, Any],
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """Manually open a compliance escalation (admin/compliance only).

    This endpoint is primarily for tests and backfills; normal escalations are
    opened automatically by guardrails, MNPI review, and hallucination review.
    """
    service = ComplianceEscalationService(session)
    trigger = ComplianceEscalationTrigger(
        trigger_type=body.get("trigger_type", "manual"),
        severity=body.get("severity", "medium"),
        fund_entity_id=body.get("fund_entity_id", ctx.fund_id),
        pm_id=body.get("pm_id", ctx.pm_id),
        details=body.get("details", {}),
    )
    try:
        escalation = await service.open(trigger)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await session.commit()
    return _serialize(escalation)


__all__ = ["router"]
