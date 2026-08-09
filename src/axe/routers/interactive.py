"""API router for interactive artifact actions and decision prompts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import ArtifactAction, DecisionPrompt
from axe.db.session import get_async_session
from axe.db.uow import UnitOfWork
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.interactive import ActionExecutionError, InteractiveArtifactService

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class ArtifactActionResponse(BaseModel):
    """Artifact action returned by the API."""

    id: str
    artifact_type: str
    artifact_id: str
    pm_id: str
    action_type: str
    payload: dict[str, Any]
    status: str
    created_at: Any
    executed_at: Any | None

    model_config = {"from_attributes": True}


class DecisionPromptResponse(BaseModel):
    """Decision prompt returned by the API."""

    id: str
    pm_id: str
    artifact_id: str | None
    prompt_text: str | None
    options_json: list[Any]
    response: str | None
    deadline_at: Any | None
    resolved_at: Any | None
    created_at: Any

    model_config = {"from_attributes": True}


class ActionListResponse(BaseModel):
    """List of actions for an artifact."""

    artifact_type: str
    artifact_id: str
    actions: list[ArtifactActionResponse]


class PromptListResponse(BaseModel):
    """List of prompts for an artifact."""

    artifact_id: str
    prompts: list[DecisionPromptResponse]


class ExecuteActionRequest(BaseModel):
    """Payload for executing an artifact action."""

    payload: dict[str, Any] | None = Field(default=None)


class ResolvePromptRequest(BaseModel):
    """Payload for resolving a decision prompt."""

    response: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_identity(ctx: RequestContext) -> tuple[str, str | None]:
    """Require pm_id for interactive artifact endpoints."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Interactive artifact endpoints require X-PM-ID",
        )
    return ctx.pm_id, ctx.fund_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{artifact_type}/{artifact_id}/actions",
    response_model=ActionListResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_artifact_actions(
    artifact_type: str,
    artifact_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """List actions for an artifact, generating them if none exist."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = InteractiveArtifactService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        actions = await service.list_actions_for_artifact(artifact_type, artifact_id)
        if not actions:
            actions = await service.create_actions(artifact_type, artifact_id)
    return {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "actions": actions,
    }


@router.post(
    "/actions/{action_id}/execute",
    response_model=ArtifactActionResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def execute_artifact_action(
    action_id: str,
    body: ExecuteActionRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ArtifactAction:
    """Execute a pending artifact action."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = InteractiveArtifactService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            action = await service.execute_action(
                action_id,
                pm_id=pm_id,
                payload=body.payload or {},
            )
        except ActionExecutionError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
    return action


@router.get(
    "/{artifact_type}/{artifact_id}/decision-prompts",
    response_model=PromptListResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_artifact_prompts(
    artifact_type: str,
    artifact_id: str,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """List decision prompts for an artifact, generating them if none exist."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = InteractiveArtifactService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        prompts = await service.list_prompts_for_artifact(artifact_id)
        if not prompts:
            prompts = await service.create_prompts(artifact_type, artifact_id)
    return {
        "artifact_id": artifact_id,
        "prompts": prompts,
    }


@router.post(
    "/decision-prompts/{prompt_id}/resolve",
    response_model=DecisionPromptResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def resolve_artifact_prompt(
    prompt_id: str,
    body: ResolvePromptRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> DecisionPrompt:
    """Resolve a decision prompt with the PM's response."""
    pm_id, fund_id = _ensure_identity(ctx)
    async with UnitOfWork(session) as uow:
        service = InteractiveArtifactService(uow, pm_id=pm_id, fund_entity_id=fund_id)
        try:
            prompt = await service.resolve_decision_prompt(
                prompt_id,
                response=body.response,
            )
        except ActionExecutionError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
    return prompt
