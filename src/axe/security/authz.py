"""Lightweight RBAC dependency factory for AXE.

Roles come from authoritative membership in the verified HTTP request context,
never from caller-supplied headers or token role claims.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from axe.exceptions import AuthError
from axe.security.context import RequestContext, get_request_context, verified_http_context

# Well-known authoritative AXE membership roles enforced by require_role.
ROLE_PM = "pm"
ROLE_ADMIN = "admin"
ROLE_COMPLIANCE = "compliance"
ROLE_COMPLIANCE_OFFICER = "compliance_officer"

# Roles allowed to view and manage compliance escalations.
COMPLIANCE_ROLES = (ROLE_COMPLIANCE, ROLE_COMPLIANCE_OFFICER, ROLE_ADMIN)


def require_role(*allowed: str) -> Callable[..., Any]:
    """Return a FastAPI dependency that requires one of ``allowed`` roles.

    Example:
        @router.post(..., dependencies=[Depends(require_role("pm", "admin"))])
    """

    async def _check_role(
        request: Request,
        ctx: RequestContext = Depends(get_request_context),
    ) -> RequestContext:
        if ctx is not verified_http_context(request):
            raise AuthError("Verified HTTP membership required")
        if ctx.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required one of roles {list(allowed)}, got '{ctx.role}'",
            )
        return ctx

    return _check_role


def require_compliance_role() -> Callable[..., Any]:
    """Require an admin, compliance, or compliance_officer role."""
    return require_role(*COMPLIANCE_ROLES)
