"""FastAPI router for the AXE onboarding flow."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.session import get_async_session
from axe.services.onboarding import OnboardingService

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


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
) -> dict[str, Any]:
    """Start or resume onboarding for a PM."""
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
) -> dict[str, Any]:
    """Return the current onboarding state and next prompt."""
    result = await _service(session, pm_id).get_status()
    await session.commit()
    return result


@router.post("/answer")
async def submit_answer(
    body: AnswerRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> dict[str, Any]:
    """Submit one cold-start answer."""
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
) -> dict[str, Any]:
    """Capture initial tickers, or skip thesis capture to complete onboarding."""
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
