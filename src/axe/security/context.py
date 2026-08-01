"""Request-scoped security context for AXE.

Every production request carries identity metadata (pm_id, fund_id, role, etc.)
so that audit, isolation, and authorization decisions can be made without
threading ad-hoc dictionaries through every call chain.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, Request

from axe.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Module-level contextvar cache for request identity. Stored outside the
# dataclass to avoid ClassVar/frozen dataclass conflicts.
_ctx_var: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "axe_request_context", default=None
)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identity and provenance metadata for a single request.

    The context is stored in a contextvar by middleware and can be retrieved
    anywhere within the request lifecycle via ``RequestContext.current()``.
    """

    pm_id: str | None = None
    fund_id: str | None = None
    role: str = "pm"
    client_ip: str | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_agent: str | None = None
    is_bypass: bool = False

    @classmethod
    def current(cls) -> RequestContext:
        """Return the active request context.

        Raises ``RuntimeError`` when called outside a request and no test context
        has been explicitly set.
        """
        ctx = _ctx_var.get()
        if ctx is None:
            raise RuntimeError(
                "No RequestContext is active. "
                "This code must run inside an HTTP request or a context managed by RequestContext."
            )
        return ctx

    @classmethod
    def current_or_none(cls) -> RequestContext | None:
        """Return the active context, or ``None`` outside a request."""
        return _ctx_var.get()

    @classmethod
    def set_current(cls, ctx: RequestContext) -> Any:
        """Set the active context for the current asyncio task.

        Returns the contextvars token so callers can restore the previous value.
        """
        return _ctx_var.set(ctx)

    @classmethod
    def reset_current(cls, token: Any) -> None:
        """Reset the contextvar using a token returned by ``set_current``."""
        _ctx_var.reset(token)

    @classmethod
    def from_headers(
        cls,
        request: Request | None,
        *,
        settings: Settings | None = None,
    ) -> RequestContext:
        """Build a context from HTTP headers or request state.

        Header names are intentionally simple for reverse-proxy/front-door
        compatibility:
          - X-PM-ID
          - X-Fund-ID
          - X-Role
          - X-Request-ID
        """
        settings = settings or get_settings()

        headers: dict[str, str] = {}
        if request is not None:
            headers = {k.lower(): v for k, v in request.headers.items()}

        pm_id = headers.get("x-pm-id")
        fund_id = headers.get("x-fund-id")
        role = headers.get("x-role") or "pm"
        user_agent = headers.get("user-agent")
        request_id = headers.get("x-request-id") or uuid.uuid4().hex

        client_ip = None
        if request is not None:
            client_ip = request.client.host if request.client else None

        is_bypass = False
        if pm_id is None and not settings.is_production:
            # Development/test bypass: allow the context to exist without identity
            # so local exploration and unit tests are not blocked. This path is
            # explicitly logged at WARNING and never used in production.
            is_bypass = True
            logger.warning(
                "RequestContext running in dev bypass mode: "
                "pm_id/fund_id are missing. Production would reject this request."
            )

        return cls(
            pm_id=pm_id,
            fund_id=fund_id,
            role=role,
            client_ip=client_ip,
            request_id=request_id,
            user_agent=user_agent,
            is_bypass=is_bypass,
        )

    def ensure_identity(self) -> RequestIdentity:
        """Return a non-optional identity view, raising if identity is missing."""
        if not self.pm_id:
            raise RuntimeError(
                "RequestContext has no pm_id; identity is required for this operation"
            )
        return RequestIdentity(pm_id=self.pm_id, fund_id=self.fund_id, role=self.role)

    @classmethod
    @contextlib.contextmanager
    def bind(
        cls,
        *,
        pm_id: str | None = None,
        fund_id: str | None = None,
        role: str = "pm",
        client_ip: str | None = None,
        request_id: str | None = None,
        user_agent: str | None = None,
    ) -> Any:
        """Temporarily bind a ``RequestContext`` for the current asyncio task.

        Intended for tests, background workers, and any non-request code path
        that still needs isolation or audit identity.
        """
        ctx = cls(
            pm_id=pm_id,
            fund_id=fund_id,
            role=role,
            client_ip=client_ip,
            request_id=request_id or uuid.uuid4().hex,
            user_agent=user_agent,
            is_bypass=False,
        )
        token = cls.set_current(ctx)
        try:
            yield ctx
        finally:
            cls.reset_current(token)


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Guaranteed identity subset of ``RequestContext``.

    Use this when the operation absolutely requires a known user.
    """

    pm_id: str
    fund_id: str | None
    role: str


async def get_request_context(
    request: Request,
    x_pm_id: str | None = Header(None, alias="X-PM-ID"),
    x_fund_id: str | None = Header(None, alias="X-Fund-ID"),
    x_role: str | None = Header(None, alias="X-Role"),
    x_request_id: str | None = Header(None, alias="X-Request-ID"),
) -> RequestContext:
    """FastAPI dependency that injects the current ``RequestContext``.

    Reads headers from the request. Explicit parameters are declared so OpenAPI
    documents them.
    """
    ctx = RequestContext.from_headers(request)
    # Rebuild from explicit header values when present to keep OpenAPI-informed
    # defaults aligned with the actual context.
    if x_pm_id:
        ctx = RequestContext(
            pm_id=x_pm_id,
            fund_id=x_fund_id or ctx.fund_id,
            role=x_role or ctx.role,
            client_ip=ctx.client_ip,
            request_id=x_request_id or ctx.request_id,
            user_agent=ctx.user_agent,
            is_bypass=False,
        )
    return ctx


def require_identity() -> RequestIdentity:
    """FastAPI dependency that returns a guaranteed ``RequestIdentity``."""
    return RequestContext.current().ensure_identity()


async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Any],
) -> Any:
    """ASGI middleware that installs ``RequestContext`` for each request.

    The context is bound to a contextvar so code can call
    ``RequestContext.current()`` without carrying the request object around.
    """
    ctx = RequestContext.from_headers(request)
    token = RequestContext.set_current(ctx)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = ctx.request_id
        return response
    finally:
        RequestContext.reset_current(token)


def install_middleware(app: Any) -> None:
    """Register request-context middleware on a FastAPI app."""
    from fastapi import FastAPI

    if not isinstance(app, FastAPI):
        raise TypeError("install_middleware expects a FastAPI app")
    app.middleware("http")(request_context_middleware)


async def request_context_dependency(
    request: Request,
) -> AsyncGenerator[RequestContext, None]:
    """Alternative FastAPI dependency that yields the installed context.

    This is the canonical dependency used by routers. It reuses the middleware
    context so there is only one source of truth per request.
    """
    ctx = RequestContext.from_headers(request)
    token = RequestContext.set_current(ctx)
    try:
        yield ctx
    finally:
        RequestContext.reset_current(token)
