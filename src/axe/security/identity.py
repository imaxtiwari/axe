"""Authoritative membership resolution from a verified email address."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import FundEntity, PMUser
from axe.exceptions import AuthError


async def resolve_identity(session: AsyncSession, email: str) -> PMUser:
    """Return the active ``PMUser`` for ``email``.

    The database is the authoritative source of pm_id, fund_entity_id, and role.
    Signed JWT claims never override these values.
    """
    result = await session.execute(
        select(PMUser)
        .join(FundEntity, FundEntity.id == PMUser.fund_entity_id)
        .where(PMUser.email == email, PMUser.active.is_(True))
        .execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    validate_membership(user)
    assert user is not None
    return user


def validate_membership(user: PMUser | None) -> None:
    """Reject malformed or disabled membership rather than inventing defaults."""
    if (
        user is None
        or user.active is not True
        or not user.id
        or not user.id.strip()
        or not user.fund_entity_id
        or not user.fund_entity_id.strip()
        or user.role not in {"pm", "admin", "compliance", "compliance_officer"}
    ):
        raise AuthError("No valid active AXE membership")


async def resolve_owner(session: AsyncSession, pm_id: str) -> PMUser:
    """Resolve a queued owner afresh, including existence of its fund."""
    result = await session.execute(
        select(PMUser)
        .join(FundEntity, FundEntity.id == PMUser.fund_entity_id)
        .where(PMUser.id == pm_id)
        .execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    validate_membership(user)
    assert user is not None
    return user
