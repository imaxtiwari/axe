"""API router for connector configuration and manual runs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from axe.config import get_settings
from axe.connectors import list_connector_types
from axe.connectors.base import ConnectorError
from axe.db.models import ConnectorConfig
from axe.db.session import get_async_session
from axe.db.uow import UnitOfWork
from axe.security.authz import require_role
from axe.security.context import RequestContext, get_request_context
from axe.services.connector import ConnectorService

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])


class ConnectorConfigRequest(BaseModel):
    """Create or update a connector configuration for the authenticated PM."""

    source_type: str = Field(..., max_length=64)
    pm_id: str = Field(..., max_length=36)
    credentials: dict[str, Any] = Field(default_factory=dict)
    schedule: str | None = Field(default=None, max_length=64)
    enabled: bool = Field(default=False)
    last_cursor: str | None = Field(default=None)


class ConnectorConfigResponse(BaseModel):
    """Connector configuration record returned by the API."""

    id: str
    pm_id: str
    source_type: str
    schedule: str | None
    enabled: bool
    last_cursor: str | None
    created_at: Any
    updated_at: Any

    model_config = ConfigDict(from_attributes=True)


class ConnectorRunRequest(BaseModel):
    """Trigger a connector run manually."""

    pm_id: str = Field(..., max_length=36)
    limit: int | None = Field(default=None, ge=1)


class ConnectorRunResponse(BaseModel):
    """Result summary for a single connector run."""

    source_type: str
    fetched: int
    new: int
    duplicates: int
    cursor: str | None
    errors: list[str]


def _require_pm_id(ctx: RequestContext) -> str:
    """Require a PM identity from the request context."""
    if not ctx.pm_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Connector endpoints require X-PM-ID",
        )
    return ctx.pm_id


def _verify_self_or_admin(ctx: RequestContext, target_pm_id: str) -> None:
    """Allow admins to act on any PM; otherwise restrict to self."""
    if ctx.role == "admin":
        return
    if ctx.pm_id is None or ctx.pm_id != target_pm_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot operate on another PM's connectors",
        )


@router.post(
    "/{source_type}",
    response_model=ConnectorConfigResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def upsert_connector_config(
    source_type: str,
    body: ConnectorConfigRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ConnectorConfig:
    """Create or update a connector configuration for the current PM."""
    _require_pm_id(ctx)
    _verify_self_or_admin(ctx, body.pm_id)

    if source_type != body.source_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source_type in path must match body",
        )

    settings = get_settings()
    if source_type not in settings.connectors_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Connector source_type '{source_type}' is not enabled",
        )

    async with UnitOfWork(session) as uow:
        config = await uow.connector_configs.get_by_source(source_type)
        if config is None:
            config = uow.connector_configs.create_config(
                pm_id=body.pm_id,
                source_type=source_type,
                credentials_encrypted=body.credentials,
                schedule=body.schedule,
                enabled=body.enabled,
                last_cursor=body.last_cursor,
            )
        else:
            if config.pm_id != body.pm_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Connector config belongs to a different PM",
                )
            config.credentials_encrypted = body.credentials
            config.schedule = body.schedule
            config.enabled = body.enabled
            if body.last_cursor is not None:
                config.last_cursor = body.last_cursor
        await uow.commit()
    return config


@router.post(
    "/{source_type}/run",
    response_model=ConnectorRunResponse,
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def run_connector(
    source_type: str,
    body: ConnectorRunRequest,
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """Trigger a connector run for the requested PM."""
    _require_pm_id(ctx)
    _verify_self_or_admin(ctx, body.pm_id)

    settings = get_settings()
    if source_type not in settings.connectors_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Connector source_type '{source_type}' is not enabled",
        )

    async with UnitOfWork(session) as uow:
        service = ConnectorService(uow)
        try:
            result = await service.run(
                source_type=source_type,
                pm_id=body.pm_id,
                limit=body.limit,
            )
        except ConnectorError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        await uow.commit()
    return result


@router.get(
    "",
    response_model=list[ConnectorConfigResponse],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_connector_configs(
    session: AsyncSession = Depends(get_async_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[ConnectorConfig]:
    """List connector configurations visible to the current PM."""
    _require_pm_id(ctx)
    async with UnitOfWork(session) as uow:
        configs = await uow.connector_configs.list_for_pm()
    return configs


@router.get(
    "/types",
    response_model=list[str],
    dependencies=[Depends(require_role("pm", "admin"))],
)
async def list_connector_types_endpoint() -> list[str]:
    """Return all registered connector source types."""
    return list_connector_types()


__all__ = ["router"]
