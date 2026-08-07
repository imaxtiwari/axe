"""API router for PM persona management and memory citations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.persona_models import PersonaStyleSnapshot
from axe.db.models import MemoryCitation, PMPeerMap
from axe.db.session import get_async_session
from axe.db.uow import UnitOfWork
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.persona import PersonaService

router = APIRouter(prefix="/api/v1/persona", tags=["persona"])


class PersonaRefreshRequest(BaseModel):
    """Optional overrides for a manual persona refresh."""

    lookback_days: int | None = Field(default=None, ge=1, le=365)
    include_dms: bool = Field(default=False)
    allowed_dm_participants: list[str] = Field(default_factory=list)


class PersonaResponse(BaseModel):
    """Current persona snapshot returned by the API."""

    persona_id: str | None
    pm_id: str | None
    writing_style_summary: str | None
    decision_triggers: dict[str, Any]
    trusted_sources: list[str]
    confidence_language: str | None
    peer_relationships: list[dict[str, Any]]

    model_config = ConfigDict(from_attributes=True)


class CitationResponse(BaseModel):
    """Memory citation returned by the API."""

    id: str
    pm_id: str
    source_type: str
    source_id: str
    snippet: str
    linked_ticker: str | None
    linked_deal_id: str | None
    sentiment: str | None
    extracted_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PeerMapResponse(BaseModel):
    """Peer map entry returned by the API."""

    id: str
    pm_id: str
    peer_email_or_slack_id: str
    peer_name: str | None
    relationship_type: str | None
    interaction_frequency: str | None
    topics: list[str]
    trust_level: str | None

    model_config = ConfigDict(from_attributes=True)


def _snapshot_to_response(snapshot: PersonaStyleSnapshot | None) -> PersonaResponse:
    if snapshot is None:
        return PersonaResponse(
            persona_id=None,
            pm_id=None,
            writing_style_summary=None,
            decision_triggers={},
            trusted_sources=[],
            confidence_language=None,
            peer_relationships=[],
        )
    return PersonaResponse(
        persona_id=snapshot.persona_id,
        pm_id=snapshot.pm_id,
        writing_style_summary=snapshot.writing_style_summary,
        decision_triggers=snapshot.decision_triggers,
        trusted_sources=snapshot.trusted_sources,
        confidence_language=snapshot.confidence_language,
        peer_relationships=[p.model_dump() for p in snapshot.peer_relationships],
    )


@router.post(
    "/refresh",
    response_model=PersonaResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def refresh_persona(
    body: PersonaRefreshRequest | None = None,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> PersonaResponse:
    """Mine historical communications and synthesize the current PM's persona."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Persona refresh requires X-PM-ID",
        )

    body = body or PersonaRefreshRequest()
    allowed = set(body.allowed_dm_participants) if body.allowed_dm_participants else None

    async with UnitOfWork(session) as uow:
        service = PersonaService(uow)
        snapshot = await service.refresh_persona(
            ctx.pm_id,
            lookback_days=body.lookback_days,
            include_dms=body.include_dms,
            allowed_dm_participants=allowed,
        )

    return _snapshot_to_response(snapshot)


@router.get(
    "/me",
    response_model=PersonaResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def get_current_persona(
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> PersonaResponse:
    """Return the most recently synthesized persona for the current PM."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Persona retrieval requires X-PM-ID",
        )

    async with UnitOfWork(session) as uow:
        service = PersonaService(uow)
        snapshot = await service.get_current_persona(ctx.pm_id)

    return _snapshot_to_response(snapshot)


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def delete_persona(
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> None:
    """Delete the current PM's persona and all mined citations/peer maps."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Persona deletion requires X-PM-ID",
        )

    async with UnitOfWork(session) as uow:
        service = PersonaService(uow)
        await service.delete_persona_and_mined_data(ctx.pm_id)


@router.get(
    "/citations",
    response_model=list[CitationResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_citations(
    limit: int | None = None,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[MemoryCitation]:
    """List mined memory citations for the current PM."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Citation listing requires X-PM-ID",
        )

    async with UnitOfWork(session) as uow:
        citations = await uow.memory_citations.list_for_pm(limit=limit)

    return citations


@router.get(
    "/peers",
    response_model=list[PeerMapResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_peers(
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[PMPeerMap]:
    """List mined peer relationships for the current PM."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Peer map listing requires X-PM-ID",
        )

    async with UnitOfWork(session) as uow:
        peers = await uow.pm_peer_maps.list_for_pm()

    return peers


__all__ = ["router"]
