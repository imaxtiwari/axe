"""Tests for the compliance escalation API router.

Covers authorization, isolation, and endpoint behavior using the FastAPI test
client.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import ComplianceEscalation, FundEntity, PMUser
from axe.db.session import get_async_session
from axe.main import create_app
from axe.services.compliance_escalation import (
    ComplianceEscalationService,
    ComplianceEscalationTrigger,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Return an AsyncClient with the session dependency overridden."""
    app = create_app()

    async def _override_get_async_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_async_session] = _override_get_async_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    app.dependency_overrides.clear()


async def _fund(session: AsyncSession) -> FundEntity:
    fund = FundEntity(
        id=str(uuid.uuid4()),
        legal_name=f"Router Fund {uuid.uuid4().hex[:8]}",
        data_residency="US",
    )
    session.add(fund)
    await session.flush()
    return fund


async def _pm_user(
    session: AsyncSession,
    fund_id: str,
    *,
    role: str = "pm",
) -> PMUser:
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund_id,
        email=f"{role}_{uuid.uuid4().hex[:8]}@example.com",
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def _compliance_officer(session: AsyncSession, fund_id: str) -> PMUser:
    return await _pm_user(session, fund_id, role="compliance_officer")


async def _open_escalation(
    session: AsyncSession,
    fund_id: str,
    pm_id: str | None = None,
) -> ComplianceEscalation:
    service = ComplianceEscalationService(session)
    trigger = ComplianceEscalationTrigger(
        trigger_type="guardrail",
        severity="high",
        fund_entity_id=fund_id,
        pm_id=pm_id,
    )
    return await service.open(trigger, auto_assign=False)


class TestAuthorization:
    async def test_list_requires_compliance_role(
        self,
        db_session: AsyncSession,
        app_client: AsyncClient,
    ) -> None:
        fund = await _fund(db_session)
        await _open_escalation(db_session, fund.id)
        await db_session.commit()

        # PM role is rejected.
        response = await app_client.get(
            "/api/v1/compliance/escalations",
            headers={
                "X-PM-ID": "pm-1",
                "X-Fund-ID": fund.id,
                "X-Role": "pm",
            },
        )
        assert response.status_code == 403

        # Compliance officer role is allowed.
        response = await app_client.get(
            "/api/v1/compliance/escalations",
            headers={
                "X-PM-ID": "co-1",
                "X-Fund-ID": fund.id,
                "X-Role": "compliance_officer",
            },
        )
        assert response.status_code == 200
        assert len(response.json()["escalations"]) == 1

    async def test_assign_requires_compliance_role(
        self,
        db_session: AsyncSession,
        app_client: AsyncClient,
    ) -> None:
        fund = await _fund(db_session)
        officer = await _compliance_officer(db_session, fund.id)
        escalation = await _open_escalation(db_session, fund.id)
        await db_session.commit()

        response = await app_client.post(
            f"/api/v1/compliance/escalations/{escalation.id}/assign",
            json={"reviewer_id": officer.id},
            headers={
                "X-PM-ID": "pm-1",
                "X-Fund-ID": fund.id,
                "X-Role": "pm",
            },
        )
        assert response.status_code == 403

        response = await app_client.post(
            f"/api/v1/compliance/escalations/{escalation.id}/assign",
            json={"reviewer_id": officer.id},
            headers={
                "X-PM-ID": "co-1",
                "X-Fund-ID": fund.id,
                "X-Role": "compliance_officer",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "assigned"

    async def test_resolve_requires_compliance_role(
        self,
        db_session: AsyncSession,
        app_client: AsyncClient,
    ) -> None:
        fund = await _fund(db_session)
        escalation = await _open_escalation(db_session, fund.id)
        await db_session.commit()

        response = await app_client.post(
            f"/api/v1/compliance/escalations/{escalation.id}/resolve",
            json={"decision": "approved", "note": "ok"},
            headers={
                "X-PM-ID": "pm-1",
                "X-Fund-ID": fund.id,
                "X-Role": "pm",
            },
        )
        assert response.status_code == 403

        response = await app_client.post(
            f"/api/v1/compliance/escalations/{escalation.id}/resolve",
            json={"decision": "approved", "note": "ok"},
            headers={
                "X-PM-ID": "admin-1",
                "X-Fund-ID": fund.id,
                "X-Role": "admin",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "resolved"


class TestIsolation:
    async def test_list_isolated_by_fund(
        self,
        db_session: AsyncSession,
        app_client: AsyncClient,
    ) -> None:
        fund_a = await _fund(db_session)
        fund_b = await _fund(db_session)
        await _open_escalation(db_session, fund_a.id)
        await _open_escalation(db_session, fund_b.id)
        await db_session.commit()

        response = await app_client.get(
            "/api/v1/compliance/escalations",
            headers={
                "X-PM-ID": "co-1",
                "X-Fund-ID": fund_a.id,
                "X-Role": "compliance_officer",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["escalations"]) == 1
        assert data["escalations"][0]["fund_entity_id"] == fund_a.id


class TestValidation:
    async def test_assign_nonexistent_reviewer_returns_400(
        self,
        db_session: AsyncSession,
        app_client: AsyncClient,
    ) -> None:
        fund = await _fund(db_session)
        escalation = await _open_escalation(db_session, fund.id)
        await db_session.commit()

        response = await app_client.post(
            f"/api/v1/compliance/escalations/{escalation.id}/assign",
            json={"reviewer_id": "does-not-exist"},
            headers={
                "X-PM-ID": "co-1",
                "X-Fund-ID": fund.id,
                "X-Role": "compliance_officer",
            },
        )
        assert response.status_code == 400

    async def test_resolve_already_resolved_returns_400(
        self,
        db_session: AsyncSession,
        app_client: AsyncClient,
    ) -> None:
        fund = await _fund(db_session)
        escalation = await _open_escalation(db_session, fund.id)
        service = ComplianceEscalationService(db_session)
        await service.resolve(escalation.id, "approved")
        await db_session.commit()

        response = await app_client.post(
            f"/api/v1/compliance/escalations/{escalation.id}/resolve",
            json={"decision": "rejected"},
            headers={
                "X-PM-ID": "co-1",
                "X-Fund-ID": fund.id,
                "X-Role": "compliance_officer",
            },
        )
        assert response.status_code == 400
