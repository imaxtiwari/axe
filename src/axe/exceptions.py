"""AXE deterministic, PII-safe exception tree and JSON response envelopes."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Request, status
from fastapi.responses import JSONResponse

from axe.security.context import RequestContext

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class AXEError(Exception):
    """Base class for all deterministic, PII-safe AXE errors.

    Subclasses must provide stable public ``code``/``message`` values and may
    attach arbitrary internal ``details`` that are never returned to clients.
    """

    http_status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "axe.internal_error"
    public_message: str = "An internal error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.public_message)
        self.internal_message = message or self.public_message
        self.details = details or {}
        self._request_id = request_id

    @property
    def request_id(self) -> str:
        """Return an explicit request_id or try to read it from active context."""
        if self._request_id:
            return self._request_id
        ctx = RequestContext.current_or_none()
        if ctx is not None:
            return ctx.request_id
        return "unknown"

    def to_response(self, request: Request | None = None) -> JSONResponse:
        """Return a PII-safe JSON error envelope."""
        body: dict[str, Any] = {
            "request_id": self.request_id,
            "code": self.code,
            "message": self.public_message,
        }
        return JSONResponse(status_code=self.http_status_code, content=body)

    def log_internal(self, *, exc_info: Any = True) -> None:
        """Log the full internal error payload for operational forensics.

        Safe details may be attached by subclasses; never log the original
        stack trace without structured formatting.
        """
        logger.error(
            "AXEError handled",
            extra={
                "code": self.code,
                "request_id": self.request_id,
                "internal_message": self.internal_message,
                "details": self.details,
            },
            exc_info=exc_info,
        )


class AuthError(AXEError):
    """Raised for authentication/authorization failures."""

    http_status_code = status.HTTP_401_UNAUTHORIZED
    code = "auth.failed"
    public_message = "Authentication failed."


class IsolationError(AXEError):
    """Raised when a request violates cross-PM/fund isolation boundaries."""

    http_status_code = status.HTTP_403_FORBIDDEN
    code = "isolation.violation"
    public_message = (
        "Access denied because the requested resource is outside your isolation boundary."
    )

    def __init__(
        self,
        message: str | None = None,
        *,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id, details=details)
        # Ensure a stable category is present for audit logs.
        self.details.setdefault("category", "isolation")


class AuditError(AXEError):
    """Raised when an audit invariant cannot be satisfied."""

    http_status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "audit.failed"
    public_message = "A compliance logging error occurred."


def _capture_code(code: str) -> None:
    """Increment the axe.errors.total observability counter."""
    from axe.observability import get_metrics

    try:
        counter_factory = get_metrics()
        counter = counter_factory.counter(
            "axe_errors_total", "Total handled AXE exceptions by code", labels=("code",)
        )
        counter.labels(code=code).inc()
    except Exception:
        logger.exception("Failed to increment axe_errors_total counter")


def make_exception_handler() -> Any:
    """Return a FastAPI exception handler for ``AXEError`` and unknowns."""

    async def axe_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle AXEError subtypes and catch-all errors deterministically."""
        if isinstance(exc, AXEError):
            response = exc.to_response(request)
            exc.log_internal()
            _capture_code(exc.code)

            # Security-sensitive failures are automatically audit-logged.
            if isinstance(exc, IsolationError):
                await _audit_isolation_failure(request, exc)

            return response

        # Unknown / unhandled exceptions: never leak stack traces.
        request_id = _request_id_for(request)
        unknown = AXEError(
            "Unhandled exception",
            request_id=request_id,
            details={"original_type": type(exc).__name__},
        )
        unknown.log_internal(exc_info=True)
        _capture_code(unknown.code)
        return unknown.to_response(request)

    return axe_exception_handler


def _request_id_for(request: Request | None) -> str:
    """Best-effort request_id extraction from request state/context."""
    if request is not None:
        state_request_id = getattr(request.state, "request_id", None)
        if state_request_id:
            return str(state_request_id)
        header = request.headers.get("x-request-id")
        if header:
            return str(header)
    ctx = RequestContext.current_or_none()
    if ctx is not None:
        return ctx.request_id
    return "unknown"


async def _audit_isolation_failure(request: Request | None, exc: IsolationError) -> None:
    """Persist an AuditLog entry for an isolation failure.

    Uses the request context for identity; if active context is unavailable,
    falls back to headers on the request object.
    """
    from axe.db.session import AsyncSessionLocal
    from axe.security.audit import AuditService

    ctx = RequestContext.current_or_none()
    if ctx is not None:
        pm_id = ctx.pm_id
        fund_id = ctx.fund_id
        client_ip = ctx.client_ip
        session_id = ctx.request_id
    elif request is not None:
        pm_id = request.headers.get("x-pm-id")
        fund_id = request.headers.get("x-fund-id")
        client_ip = request.client.host if request.client else None
        session_id = request.headers.get("x-request-id", exc.request_id)
    else:
        pm_id = None
        fund_id = None
        client_ip = None
        session_id = exc.request_id

    audit_service = AuditService(AsyncSessionLocal())
    try:
        await audit_service.log(
            action_type="isolation_failure",
            object_type="request",
            object_id=session_id or exc.request_id,
            before_state={
                "pm_id": pm_id,
                "fund_id": fund_id,
            },
            after_state=None,
            pm_id=pm_id,
            fund_entity_id=fund_id,
            source_ip=client_ip,
            session_id=session_id,
            retention_class="compliance",
            non_blocking=False,
        )
    except Exception:
        logger.exception("Failed to write isolation failure audit log")


class _GlobalErrorMiddleware:
    """Outermost ASGI middleware that catches unhandled exceptions.

    ``BaseHTTPMiddleware`` can prevent a broadly registered ``Exception``
    handler from seeing errors raised inside routes. This middleware sits
    outside the middleware stack and returns the generic AXE 500 envelope for
    anything that bubbles up uncaught, ensuring no stack trace is leaked.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            # Build a synthetic request object for id extraction if possible.
            request: Request | None = None
            with contextlib.suppress(Exception):
                request = Request(scope, receive=receive)

            request_id = _request_id_for(request)
            unknown = AXEError(
                "Unhandled exception",
                request_id=request_id,
                details={"original_type": type(exc).__name__},
            )
            unknown.log_internal(exc_info=True)
            _capture_code(unknown.code)

            response = unknown.to_response(request)
            await response(scope, receive, send)


def register_exception_handlers(app: Any) -> None:
    """Install global AXE exception handlers on a FastAPI app.

    Registers handlers for the concrete AXE exception classes first so they
    are caught even when ``BaseHTTPMiddleware`` is installed, then adds a
    catch-all ``Exception`` handler as a safety net.
    """
    handler = make_exception_handler()
    for exc_cls in (AuthError, IsolationError, AuditError, AXEError, Exception):
        app.add_exception_handler(exc_cls, handler)


def install_global_error_middleware(app: Any) -> None:
    """Register the outermost ASGI fallback middleware.

    Must be called before any other middleware is added so it wraps the
    entire application stack.
    """
    app.add_middleware(_GlobalErrorMiddleware)
