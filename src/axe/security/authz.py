"""Lightweight RBAC dependency factory for AXE.

Roles are supplied by the request context (``X-Role`` header or JWT claim) and
are enforced at the router/endpoint level with FastAPI ``Depends``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException, status

from axe.security.context import RequestContext, get_request_context

# Well-known AXE roles. These are the strings supplied by the request context
# (``X-Role`` header) and enforced by ``require_role``.
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
        ctx: RequestContext = Depends(get_request_context),
    ) -> RequestContext:
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
