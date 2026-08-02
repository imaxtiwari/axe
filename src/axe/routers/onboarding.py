"""FastAPI router for the AXE onboarding flow."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.session import get_async_session
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.onboarding import OnboardingService

router = APIRouter(
    prefix="/onboarding",
    tags=["onboarding"],
    dependencies=[Depends(require_role("admin"))],
)


class StartOnboardingRequest(BaseModel):
    pm_id: str


class AnswerRequest(BaseModel):
    pm_id: str
    question_number: int = Field(..., ge=1, le=5)
    answer: str = Field(..., min_length=1)


class ThesisCaptureRequest(BaseModel):
    pm_id: str
    tickers: list[str] = Field(default_factory=list)
    skip: bool = False


class OnboardingStatusRequest(BaseModel):
    pm_id: str


def _service(session: AsyncSession, pm_id: str) -> OnboardingService:
    return OnboardingService(session, pm_id)


@router.post("/start")
async def start_onboarding(
    body: StartOnboardingRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    ctx: Annotated[RequestContext, Depends(get_request_context)],
) -> dict[str, Any]:
    """Start or resume onboarding for a PM."""
    _verify_self_or_bypass(ctx, body.pm_id)
    try:
        result = await _service(session, body.pm_id).start()
        await session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{pm_id}/status")
async def onboarding_status(
    pm_id: str,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    ctx: Annotated[RequestContext, Depends(get_request_context)],
) -> dict[str, Any]:
    """Return the current onboarding state and next prompt."""
    _verify_self_or_bypass(ctx, pm_id)
    result = await _service(session, pm_id).get_status()
    await session.commit()
    return result


@router.post("/answer")
async def submit_answer(
    body: AnswerRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    ctx: Annotated[RequestContext, Depends(get_request_context)],
) -> dict[str, Any]:
    """Submit one cold-start answer."""
    _verify_self_or_bypass(ctx, body.pm_id)
    try:
        result = await _service(session, body.pm_id).submit_answer(
            body.question_number, body.answer
        )
        await session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/thesis-capture")
async def thesis_capture(
    body: ThesisCaptureRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    ctx: Annotated[RequestContext, Depends(get_request_context)],
) -> dict[str, Any]:
    """Capture initial tickers, or skip thesis capture to complete onboarding."""
    _verify_self_or_bypass(ctx, body.pm_id)
    service = _service(session, body.pm_id)
    try:
        if body.skip:
            result = await service.skip_thesis_capture()
        else:
            result = await service.submit_thesis_capture(body.tickers)
        await session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _verify_self_or_bypass(ctx: RequestContext, target_pm_id: str) -> None:
    """Require the request context to match the target PM, unless in dev bypass.

    Avoids accidental cross-PM calls without breaking local onboarding dev/test
    flows when no identity header is sent.
    """
    if ctx.pm_id is None and ctx.is_bypass:
        return
    if ctx.pm_id != target_pm_id:
        raise HTTPException(status_code=403, detail="Cannot operate on another PM's onboarding")
