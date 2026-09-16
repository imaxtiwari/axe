"""Request-scoped security context; HTTP identity comes only from verified membership."""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from axe.config import Settings, get_settings

_ctx_var: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "axe_request_context", default=None
)
# Internal bind() deliberately cannot establish this request-specific proof.
_http_context: contextvars.ContextVar[tuple[Scope, RequestContext] | None] = contextvars.ContextVar(
    "axe_verified_http_context", default=None
)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identity and provenance metadata for a single request or internal operation."""

    pm_id: str | None = None
    fund_id: str | None = None
    role: str = "pm"
    client_ip: str | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_agent: str | None = None

    @classmethod
    def current(cls) -> RequestContext:
        ctx = _ctx_var.get()
        if ctx is None:
            raise RuntimeError("No RequestContext is active.")
        return ctx

    @classmethod
    def current_or_none(cls) -> RequestContext | None:
        return _ctx_var.get()

    @classmethod
    def set_current(cls, ctx: RequestContext) -> Any:
        """Return a contextvars token so callers can restore the previous value."""
        return _ctx_var.set(ctx)

    @classmethod
    def reset_current(cls, token: Any) -> None:
        _ctx_var.reset(token)

    def ensure_identity(self) -> RequestIdentity:
        if not self.pm_id:
            raise RuntimeError("RequestContext has no pm_id; identity is required")
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
        """Bind internal/test context. This is never evidence of HTTP authentication."""
        ctx = cls(
            pm_id=pm_id,
            fund_id=fund_id,
            role=role,
            client_ip=client_ip,
            request_id=request_id or uuid.uuid4().hex,
            user_agent=user_agent,
        )
        token = cls.set_current(ctx)
        try:
            yield ctx
        finally:
            cls.reset_current(token)


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Guaranteed identity subset of RequestContext."""

    pm_id: str
    fund_id: str | None
    role: str


def verified_http_context(request: Request) -> RequestContext:
    from axe.exceptions import AuthError

    proof = _http_context.get()
    if proof is None or proof[0] is not request.scope or _ctx_var.get() is not proof[1]:
        raise AuthError("Verified HTTP membership required")
    return proof[1]


async def get_request_context(request: Request) -> RequestContext:
    """Consume middleware's one verified context; do not re-resolve identity."""
    return verified_http_context(request)


def require_identity(request: Request) -> RequestIdentity:
    return verified_http_context(request).ensure_identity()


async def request_context_dependency(request: Request) -> AsyncGenerator[RequestContext, None]:
    yield verified_http_context(request)


class RequestContextMiddleware:
    """Default-deny HTTP boundary, including future routers, docs and metrics."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        from axe.security.jwt import JWTVerifier

        self.app = app
        self.verifier = JWTVerifier(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        from axe.db.session import AsyncSessionLocal
        from axe.exceptions import AuthError
        from axe.security.identity import resolve_identity

        request = Request(scope, receive)
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        ctx_token = _ctx_var.set(None)
        proof_token = _http_context.set(None)
        try:
            exempt = scope["method"] in {"GET", "HEAD"} and scope["path"] in {
                "/", "/healthz", "/ready"
            }
            if not exempt:
                try:
                    authorization = request.headers.getlist("authorization")
                    if len(authorization) != 1:
                        raise AuthError("Bearer token required")
                    parts = authorization[0].split()
                    if len(parts) != 2 or parts[0].lower() != "bearer":
                        raise AuthError("Bearer token required")
                    verified = await self.verifier.verify(parts[1])
                    factory = getattr(request.app.state, "identity_session_factory", AsyncSessionLocal)
                    async with factory() as session:
                        user = await resolve_identity(session, verified.email)
                        ctx = RequestContext(
                            pm_id=user.id,
                            fund_id=user.fund_entity_id,
                            role=user.role,
                            client_ip=request.client.host if request.client else None,
                            request_id=request_id,
                            user_agent=request.headers.get("user-agent"),
                        )
                    _ctx_var.set(ctx)
                    _http_context.set((scope, ctx))
                except Exception:
                    # Never expose tokens, provider failures, or DB errors. No handler effects.
                    response = AuthError(request_id=request_id).to_response()
                    response.headers["WWW-Authenticate"] = "Bearer"
                    response.headers["X-Request-ID"] = request_id
                    await response(scope, receive, send)
                    return

            async def send_with_id(message: Any) -> None:
                if message["type"] == "http.response.start":
                    message["headers"] = [
                        *message.get("headers", []),
                        (b"x-request-id", request_id.encode("latin-1")),
                    ]
                await send(message)

            await self.app(scope, receive, send_with_id)
        finally:
            _http_context.reset(proof_token)
            _ctx_var.reset(ctx_token)


def install_middleware(app: Any, settings: Settings | None = None) -> None:
    from fastapi import FastAPI

    if not isinstance(app, FastAPI):
        raise TypeError("install_middleware expects a FastAPI app")
    app.add_middleware(RequestContextMiddleware, settings=settings or get_settings())
