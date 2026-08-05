"""API router for deal rooms and deal documents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import DealDocument, DealRoom, DealThesisVersion, ICMemo, ICSignOff, UnderwritingChecklist, UnderwritingScenario
from axe.db.session import get_async_session
from axe.db.uow import UnitOfWork
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.deal import DealDocumentService, DealRoomService, DealUnderwritingService
from axe.services.ic_memo import ICMemoService

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


class ChecklistInitRequest(BaseModel):
    """Start an underwriting checklist from a vehicle-type template."""

    vehicle_type: str = Field(..., max_length=64)


class ChecklistItemResponse(BaseModel):
    """Single underwriting checklist item."""

    id: str
    deal_id: str
    category: str
    question: str
    required: bool
    sort_order: int
    status: str
    evidence_url: str | None
    answered_by: str | None
    updated_at: Any

    model_config = {"from_attributes": True}


class ChecklistItemUpdateRequest(BaseModel):
    """Update a checklist item status."""

    status: str = Field(..., max_length=32)
    evidence_url: str | None = Field(default=None, max_length=2048)
    answered_by: str | None = Field(default=None, max_length=36)


class ScenarioResponse(BaseModel):
    """A persisted underwriting scenario."""

    id: str
    deal_id: str
    scenario_name: str
    assumptions: dict[str, Any]
    output_metrics: dict[str, Any]
    probability_weight: float | None
    confidence: float | None
    created_at: Any

    model_config = {"from_attributes": True}


class ScenarioRunResponse(BaseModel):
    """Scenario analysis output plus persisted scenario IDs."""

    overall_confidence: float
    scenarios: list[ScenarioResponse]


class ScenarioRunRequest(BaseModel):
    """Run scenario analysis against a deal thesis."""

    thesis_text: str = Field(..., min_length=1)
    vehicle_type: str | None = Field(default=None, max_length=64)


class DealThesisCreateRequest(BaseModel):
    """Create a versioned deal thesis for a deal room."""

    stage: str = Field(..., max_length=64)
    bull_case: str | None = Field(default=None)
    bear_case: str | None = Field(default=None)
    key_assumptions: list[str] = Field(default_factory=list)
    risks: list[str] | None = Field(default=None)


class DealThesisResponse(BaseModel):
    """Versioned deal thesis record."""

    id: str
    deal_id: str
    pm_id: str
    version: int
    stage: str
    bull_case: str | None
    bear_case: str | None
    key_assumptions: list[Any]
    risks: list[Any]
    created_at: Any

    model_config = {"from_attributes": True}


class MemoResponse(BaseModel):
    """IC memo returned by the API."""

    id: str
    deal_id: str
    pm_id: str
    fund_entity_id: str
    version: int
    status: str
    content_json: dict[str, Any]
    content_md: str | None
    final_signoff_at: Any | None
    created_at: Any

    model_config = {"from_attributes": True}


class MemoSignOffRequest(BaseModel):
    """Request body for signing an IC memo."""

    signer_pm_id: str = Field(..., max_length=36)
    role: str = Field(default="pm", max_length=64)
    signature_note: str | None = Field(default=None)


class MemoSignOffResponse(BaseModel):
    """An IC memo sign-off record."""

    id: str
    memo_id: str
    pm_id: str
    fund_entity_id: str
    role: str
    signature_note: str | None
    created_at: Any

    model_config = {"from_attributes": True}


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


# ---------------------------------------------------------------------------
# Underwriting endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{deal_id}/underwriting/checklist",
    response_model=list[ChecklistItemResponse],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def initialize_underwriting_checklist(
    deal_id: str,
    body: ChecklistInitRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[UnderwritingChecklist]:
    """Initialize a default underwriting checklist for the deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealUnderwritingService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            items = await service.initialize_checklist(deal_id, body.vehicle_type)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
    return items


@router.get(
    "/{deal_id}/underwriting/checklist",
    response_model=list[ChecklistItemResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_underwriting_checklist(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[UnderwritingChecklist]:
    """List the underwriting checklist for a deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealUnderwritingService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        items = await service.list_checklist(deal_id)
    return items


@router.patch(
    "/{deal_id}/underwriting/checklist/{item_id}",
    response_model=ChecklistItemResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def update_underwriting_checklist_item(
    deal_id: str,
    item_id: str,
    body: ChecklistItemUpdateRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> UnderwritingChecklist:
    """Update the status of a single checklist item."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealUnderwritingService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            item = await service.update_checklist_item(
                deal_id=deal_id,
                checklist_item_id=item_id,
                status=body.status,
                evidence_url=body.evidence_url,
                answered_by=body.answered_by,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
    return item


@router.post(
    "/{deal_id}/underwriting/scenarios",
    response_model=ScenarioRunResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def run_underwriting_scenarios(
    deal_id: str,
    body: ScenarioRunRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ScenarioRunResponse:
    """Generate and persist scenario analysis for a deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealUnderwritingService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            output, persisted = await service.run_scenarios(
                deal_id=deal_id,
                thesis_text=body.thesis_text,
                vehicle_type=body.vehicle_type,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    return ScenarioRunResponse(
        overall_confidence=output.confidence,
        scenarios=[ScenarioResponse.model_validate(s) for s in persisted],
    )


@router.get(
    "/{deal_id}/underwriting/scenarios",
    response_model=list[ScenarioResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_underwriting_scenarios(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[UnderwritingScenario]:
    """List persisted underwriting scenarios for a deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = DealUnderwritingService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        scenarios = await service.list_scenarios(deal_id)
    return scenarios


# ---------------------------------------------------------------------------
# Deal thesis endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{deal_id}/thesis",
    response_model=DealThesisResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def create_deal_thesis(
    deal_id: str,
    body: DealThesisCreateRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> DealThesisVersion:
    """Create the next version of a deal thesis."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        deal = await uow.deals.get_by_id(deal_id)
        if deal is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Deal {deal_id} not found",
            )
        latest = await uow.deal_theses.get_latest_for_deal(deal_id)
        version = (latest.version + 1) if latest else 1
        thesis = DealThesisVersion(
            deal_id=deal_id,
            pm_id=pm_id,
            version=version,
            stage=body.stage,
            bull_case=body.bull_case,
            bear_case=body.bear_case,
            key_assumptions=body.key_assumptions,
            risks=body.risks or [],
        )
        session.add(thesis)
        await session.flush()
        await uow.commit()
    return thesis


@router.get(
    "/{deal_id}/thesis",
    response_model=list[DealThesisResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_deal_theses(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[DealThesisVersion]:
    """List deal thesis versions."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        deal = await uow.deals.get_by_id(deal_id)
        if deal is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Deal {deal_id} not found",
            )
        theses = await uow.deal_theses.list_for_deal(deal_id)
    return theses


# ---------------------------------------------------------------------------
# IC memo endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{deal_id}/ic-memos",
    response_model=MemoResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def generate_ic_memo(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ICMemo:
    """Generate or regenerate the IC memo for a deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = ICMemoService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            memo = await service.generate_memo(deal_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    return memo


@router.get(
    "/{deal_id}/ic-memos",
    response_model=list[MemoResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_ic_memos(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[ICMemo]:
    """List all versions of IC memos for a deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = ICMemoService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        memos = await service.list_memos_for_deal(deal_id)
    return memos


@router.get(
    "/{deal_id}/ic-memos/latest",
    response_model=MemoResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def get_latest_ic_memo(
    deal_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ICMemo:
    """Return the latest IC memo version for a deal."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        memo = await uow.ic_memos.get_latest_for_deal(deal_id)
    if memo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No IC memo found for deal {deal_id}",
        )
    return memo


@router.get(
    "/{deal_id}/ic-memos/{memo_id}",
    response_model=MemoResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def get_ic_memo(
    deal_id: str,
    memo_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ICMemo:
    """Fetch a specific IC memo."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        memo = await uow.ic_memos.get_by_id(memo_id)
    if memo is None or memo.deal_id != deal_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memo {memo_id} not found",
        )
    return memo


@router.post(
    "/{deal_id}/ic-memos/{memo_id}/signoffs",
    response_model=MemoResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def sign_ic_memo(
    deal_id: str,
    memo_id: str,
    body: MemoSignOffRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ICMemo:
    """Record a sign-off on an IC memo."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        memo = await uow.ic_memos.get_by_id(memo_id)
        if memo is None or memo.deal_id != deal_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memo {memo_id} not found",
            )
        signer = await uow.pm_users.get_by_id(body.signer_pm_id)
        if signer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Signer {body.signer_pm_id} not found",
            )
        service = ICMemoService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            memo = await service.sign_memo(
                memo_id,
                body.signer_pm_id,
                role=body.role,
                signature_note=body.signature_note,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    return memo


@router.get(
    "/{deal_id}/ic-memos/{memo_id}/signoffs",
    response_model=list[MemoSignOffResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_ic_memo_signoffs(
    deal_id: str,
    memo_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[ICSignOff]:
    """List sign-offs for an IC memo."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        memo = await uow.ic_memos.get_by_id(memo_id)
        if memo is None or memo.deal_id != deal_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memo {memo_id} not found",
            )
        signoffs = await uow.ic_signoffs.list_for_memo(memo_id)
    return signoffs


@router.patch(
    "/{deal_id}/ic-memos/{memo_id}",
    response_model=MemoResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def update_ic_memo(
    deal_id: str,
    memo_id: str,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ICMemo:
    """Update draft memo content; blocked after final sign-off."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        memo = await uow.ic_memos.get_by_id(memo_id)
        if memo is None or memo.deal_id != deal_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memo {memo_id} not found",
            )
        service = ICMemoService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            memo = await service.update_memo(memo_id, **body)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    return memo


__all__ = ["router"]
