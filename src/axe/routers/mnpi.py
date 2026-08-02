"""API router for MNPI review decisions."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.session import get_async_session
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.mnpi import MNPIService

router = APIRouter(prefix="/api/v1/mnpi", tags=["mnpi"])


class ReviewDecision(BaseModel):
    """Reviewer decision on a queued MNPI item."""

    decision: Literal["approved", "rejected"]
    reviewer_id: str


class ReviewResponse(BaseModel):
    """Result of recording a reviewer decision."""

    review_id: str
    status: str
    signal_id: str | None


@router.post(
    "/{review_id}/decision",
    response_model=ReviewResponse,
    dependencies=[Depends(require_role("compliance", "admin"))],
)
async def decide_review(
    review_id: str,
    body: ReviewDecision,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """Approve or reject a pending MNPI review item.

    Approval un-flags the source signal and enqueues the held alert payloads
    for dispatch; rejection leaves the signal flagged. Both outcomes are
    audit-logged.
    """
    service = MNPIService(session)
    try:
        review = await service.decide(
            review_id=review_id,
            decision=body.decision,
            reviewer_id=body.reviewer_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    await session.commit()
    return {
        "review_id": review.id,
        "status": review.status,
        "signal_id": review.signal_id,
    }


__all__ = ["router"]
