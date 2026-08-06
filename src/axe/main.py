"""FastAPI application entry point for AXE."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from axe.config import Settings, get_settings
from axe.exceptions import install_global_error_middleware, register_exception_handlers
from axe.observability import (
    configure_logging,
    init_sentry,
    init_tracing,
    instrument_fastapi,
    render_metrics,
)
from axe.routers.audit import router as audit_router
from axe.routers.deals import router as deals_router
from axe.routers.lp import router as lp_router
from axe.routers.mnpi import router as mnpi_router
from axe.routers.onboarding import router as onboarding_router
from axe.routers.transcripts import router as transcripts_router
from axe.security.context import install_middleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan events."""
    Path("./data").mkdir(parents=True, exist_ok=True)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory for creating the AXE FastAPI application."""
    if settings is None:
        settings = get_settings()

    configure_logging(settings.log_level)
    init_sentry(settings.sentry_dsn, settings.app_env)
    init_tracing("axe")

    app = FastAPI(
        title="AXE",
        description="Wall Street AI Co-pilot and Investment Operating System",
        version="0.1.0",
        lifespan=lifespan,
    )
    install_global_error_middleware(app)
    install_middleware(app)
    instrument_fastapi(app)
    register_exception_handlers(app)

    @app.get("/healthz", tags=["health"])
    async def health_check() -> dict[str, Any]:
        """Liveness probe."""
        return {"status": "ok", "env": settings.app_env}

    @app.get("/ready", tags=["health"])
    async def readiness_check() -> JSONResponse:
        """Readiness probe; verifies required dirs are present and writable."""
        try:
            Path("./data").mkdir(parents=True, exist_ok=True)
            probe = Path("./data") / ".ready"
            probe.write_text("ok")
            return JSONResponse({"status": "ready"})
        except OSError as exc:
            return JSONResponse({"status": "not_ready", "detail": str(exc)}, status_code=503)

    @app.get("/metrics", tags=["health"])
    async def metrics() -> Response:
        """Prometheus-compatible metrics endpoint."""
        data, content_type = render_metrics()
        return Response(content=data, media_type=content_type)

    @app.get("/", tags=["root"])
    async def root() -> dict[str, Any]:
        return {"app": "AXE", "version": "0.1.0"}

    app.include_router(onboarding_router)
    app.include_router(transcripts_router)
    app.include_router(mnpi_router)
    app.include_router(audit_router)
    app.include_router(deals_router)
    app.include_router(lp_router)

    return app


app = create_app()
