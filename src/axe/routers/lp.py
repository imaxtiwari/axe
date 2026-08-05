"""API router for LP updates and communications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import LPUpdate
from axe.db.session import get_async_session
from axe.db.uow import UnitOfWork
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.lp_comms import LPCommsService

router = APIRouter(prefix="/api/v1/lp-updates", tags=["lp-updates"])


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class DraftLPUpdateRequest(BaseModel):
    """Create a draft LP update for a vehicle and quarter."""

    vehicle_id: str = Field(..., min_length=1, max_length=36)
    quarter: str = Field(..., min_length=6, max_length=16)


class ApproveAndSendRequest(BaseModel):
    """Approve and/or send an LP update."""

    approved_by: str = Field(..., min_length=1, max_length=36)


class LPUpdateSectionResponse(BaseModel):
    """A single LP update section."""

    heading: str
    body: str


class LPUpdateResponse(BaseModel):
    """LP update record returned by the API."""

    id: str
    vehicle_id: str
    quarter: str
    sections: list[LPUpdateSectionResponse]
    status: str
    approved_by: str | None
    content_md: str | None
    content_html: str | None
    created_at: Any

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_identity(ctx: RequestContext) -> tuple[str, str]:
    """Require pm_id and fund_id for LP update operations."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LP update endpoints require X-PM-ID",
        )
    if not ctx.fund_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LP update endpoints require X-Fund-ID",
        )
    return ctx.pm_id, ctx.fund_id


# ---------------------------------------------------------------------------
# LP update endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/draft",
    response_model=LPUpdateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def draft_lp_update(
    body: DraftLPUpdateRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> LPUpdate:
    """Generate a draft LP update for the requested vehicle and quarter."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = LPCommsService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            update = await service.draft_update(
                vehicle_id=body.vehicle_id,
                quarter=body.quarter,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
    return update


@router.post(
    "/{update_id}/send",
    response_model=LPUpdateResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def send_lp_update_endpoint(
    update_id: str,
    body: ApproveAndSendRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> LPUpdate:
    """Approve and send an LP update, archiving it for compliance."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = LPCommsService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            update = await service.send_update(
                update_id=update_id,
                approved_by=body.approved_by,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            # Compliance gate errors and unexpected issues surface as 422.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    return update


__all__ = ["router"]
