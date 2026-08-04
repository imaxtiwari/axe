"""API router for deal rooms and deal documents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import DealDocument, DealRoom
from axe.db.session import get_async_session
from axe.db.uow import UnitOfWork
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.deal import DealDocumentService, DealRoomService

router = APIRouter(prefix="/api/v1/deals", tags=["deals"])


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class DealCreateRequest(BaseModel):
    """Create a new deal room for the authenticated PM/fund."""

    name: str = Field(..., min_length=1, max_length=255)
    stage: str = Field(default="screening", max_length=64)
    asset_class: str = Field(default="private_equity", max_length=64)
    target_ticker_or_private_name: str | None = Field(default=None, max_length=255)
    cim_url: str | None = Field(default=None, max_length=2048)
    status: str = Field(default="active", max_length=32)


class DealUpdateRequest(BaseModel):
    """Update mutable deal room fields."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    stage: str | None = Field(default=None, max_length=64)
    asset_class: str | None = Field(default=None, max_length=64)
    target_ticker_or_private_name: str | None = Field(default=None, max_length=255)
    cim_url: str | None = Field(default=None, max_length=2048)
    status: str | None = Field(default=None, max_length=32)


class DealResponse(BaseModel):
    """Deal room record returned by the API."""

    id: str
    pm_id: str
    fund_entity_id: str
    name: str
    stage: str
    asset_class: str
    target_ticker_or_private_name: str | None
    cim_url: str | None
    status: str
    created_at: Any

    model_config = {"from_attributes": True}


class DocumentUploadRequest(BaseModel):
    """Upload a document for a deal as base64-encoded bytes."""

    source_type: str = Field(..., max_length=64)
    file_content_b64: str = Field(..., description="Base64-encoded file bytes")
    file_path: str | None = Field(default=None, max_length=2048)
    content_url: str | None = Field(default=None, max_length=2048)
    mime_type: str | None = Field(default=None, max_length=255)
    extracted_entities: dict[str, Any] | None = Field(default=None)


class DocumentResponse(BaseModel):
    """Deal document returned by the API."""

    id: str
    deal_id: str
    source_type: str
    file_path: str | None
    content_url: str | None
    content_hash: str | None
    file_size: int | None
    mime_type: str | None
    ingestion_status: str
    extracted_entities: dict[str, Any]
    created_at: Any

    model_config = {"from_attributes": True}


class DocumentUploadResponse(BaseModel):
    """Result of a document upload, including idempotency signal."""

    document: DocumentResponse
    is_new: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_identity(ctx: RequestContext) -> tuple[str, str]:
    """Require pm_id and fund_id for deal-room operations."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Deal room endpoints require X-PM-ID",
        )
    if not ctx.fund_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Deal room endpoints require X-Fund-ID",
        )
    return ctx.pm_id, ctx.fund_id


# ---------------------------------------------------------------------------
# Deal room endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=DealResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def create_deal(
    body: DealCreateRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> DealRoom:
    """Create a new deal room for the current PM/fund."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealRoomService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        deal = await service.create_deal(
            name=body.name,
            stage=body.stage,
            asset_class=body.asset_class,
            target_ticker_or_private_name=body.target_ticker_or_private_name,
            cim_url=body.cim_url,
            status=body.status,
        )
    return deal


@router.get(
    "",
    response_model=list[DealResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_deals(
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[DealRoom]:
    """List deal rooms visible to the current PM/fund."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealRoomService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        deals = await service.list_deals()
    return deals


@router.get(
    "/{deal_id}",
    response_model=DealResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def get_deal(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> DealRoom:
    """Fetch a single deal room if it belongs to the current fund."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealRoomService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        deal = await service.get_deal(deal_id)
    if deal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deal {deal_id} not found",
        )
    return deal


@router.patch(
    "/{deal_id}",
    response_model=DealResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def update_deal(
    deal_id: str,
    body: DealUpdateRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> DealRoom:
    """Update a deal room."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealRoomService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            deal = await service.update_deal(deal_id, **body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
    return deal


@router.delete(
    "/{deal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def delete_deal(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> None:
    """Delete a deal room and its documents."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealRoomService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            await service.delete_deal(deal_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc


# ---------------------------------------------------------------------------
# Deal document endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{deal_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def upload_document(
    deal_id: str,
    body: DocumentUploadRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """Upload a document for a deal; duplicate content hashes are idempotent."""
    import base64

    pm_id, fund_id = _ensure_identity(ctx)
    try:
        file_content = base64.b64decode(body.file_content_b64)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file_content_b64 is not valid base64",
        ) from exc

    async with UnitOfWork(session) as uow:
        service = DealDocumentService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            doc, is_new = await service.upload_document(
                deal_id=deal_id,
                source_type=body.source_type,
                file_content=file_content,
                file_path=body.file_path,
                content_url=body.content_url,
                mime_type=body.mime_type,
                extracted_entities=body.extracted_entities,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
    return {
        "document": DocumentResponse.model_validate(doc),
        "is_new": is_new,
    }


@router.get(
    "/{deal_id}/documents",
    response_model=list[DocumentResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_documents(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[DealDocument]:
    """List documents for a deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealDocumentService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        docs = await service.list_documents(deal_id)
    return docs


@router.get(
    "/{deal_id}/documents/{document_id}",
    response_model=DocumentResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def get_document(
    deal_id: str,
    document_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> DealDocument:
    """Fetch a single document for a deal if it is visible to the current fund."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealDocumentService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        doc = await service.get_document(deal_id, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found",
        )
    return doc


__all__ = ["router"]
