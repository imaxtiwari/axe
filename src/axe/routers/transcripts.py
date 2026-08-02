"""API router for transcript/signal arrival and immediate drift processing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import RetryQueue
from axe.db.session import get_async_session
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context

router = APIRouter(
    prefix="/api/v1/transcripts",
    tags=["transcripts"],
    dependencies=[Depends(require_role("pm", "admin"))],
)


class TranscriptArrival(BaseModel):
    """Inbound transcript or signal payload from Polygon/email/etc."""

    pm_id: str = Field(..., description="Target PM user id")
    ticker: str = Field(..., description="Ticker symbol the signal relates to")
    source_type: str = Field(
        ..., description="Source type; triggers earnings alerts when 'polygon'"
    )
    source_url: str | None = Field(default=None, description="Original source URL")
    signal_text: str = Field(..., description="Extracted signal text")
    raw_content: str | None = Field(default=None, description="Raw transcript content")
    content_hash: str | None = Field(default=None, description="Optional content hash")
    signal_id: str | None = Field(default=None, description="External signal id")
    arrived_at: datetime | None = Field(default=None, description="Transcript arrival time (UTC)")
    slack_user_id: str | None = Field(default=None)
    email: str | None = Field(default=None)
    sync: bool = Field(
        default=False,
        description="If true, run drift detection synchronously instead of enqueuing",
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def receive_transcript(
    payload: TranscriptArrival,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """Receive a transcript/signal and enqueue drift detection (or run sync)."""
    if ctx.role != "admin" and ctx.pm_id is not None and ctx.pm_id != payload.pm_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Transcript pm_id does not match authenticated identity",
        )

    payload_dict = payload.model_dump(exclude={"sync"})

    if payload.sync:
        from axe.ingestion.handlers import process_transcript_handler

        processed = await process_transcript_handler(session, payload_dict)
        if not processed:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Failed to process transcript",
            )
        await session.commit()
        return {"status": "processed", "pm_id": payload.pm_id, "ticker": payload.ticker}

    task = RetryQueue(
        pm_id=payload.pm_id,
        task_type="process_transcript",
        payload=payload_dict,
    )
    session.add(task)
    await session.commit()
    return {
        "status": "queued",
        "pm_id": payload.pm_id,
        "ticker": payload.ticker,
        "task_id": task.id,
    }
