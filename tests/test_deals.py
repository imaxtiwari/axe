"""Tests for the deal room and deal document API."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.config import Settings
from axe.db.models import (
    AuditLog,
    DealDocument,
    DealThesisVersion,
    FundEntity,
    ICMemo,
    ICSignOff,
    PMUser,
    UnderwritingChecklist,
    UnderwritingScenario,
)
from axe.db.session import get_async_session
from axe.ingestion.hashing import content_hash
from axe.main import create_app
from axe.services.ic_memo import get_default_provider


def _mock_provider():
    """Return a deterministic mock provider for IC memo tests."""
    from axe.agents.llm import MockProvider

    return MockProvider()


@pytest.fixture
async def client(
    db_session: AsyncSession,
) -> AsyncGenerator[TestClient, None]:
    """Return a TestClient whose DB dependency uses the test transaction."""

    async def _override_get_async_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    settings = Settings(app_env="test", database_url="sqlite+aiosqlite:///:memory:")
    app = create_app(settings=settings)
    app.dependency_overrides[get_async_session] = _override_get_async_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _fund_entity(session: AsyncSession) -> FundEntity:
    fund = FundEntity(
        id=str(uuid.uuid4()),
        legal_name=f"Test Fund {uuid.uuid4().hex[:8]}",
        data_residency="US",
    )
    session.add(fund)
    await session.flush()
    return fund


async def _pm_user(session: AsyncSession, fund_id: str) -> PMUser:
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund_id,
        email=f"test_{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    await session.flush()
    return user


def _headers(pm_id: str, fund_id: str, role: str = "pm") -> dict[str, str]:
    return {"X-PM-ID": pm_id, "X-Fund-ID": fund_id, "X-Role": role}


# ---------------------------------------------------------------------------
# Deal room CRUD and isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_deal(client: TestClient, db_session: AsyncSession) -> None:
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    response = client.post(
        "/api/v1/deals",
        json={"name": "Acme Buyout"},
        headers=_headers(user.id, fund.id),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Acme Buyout"
    assert body["pm_id"] == user.id
    assert body["fund_entity_id"] == fund.id
    assert body["stage"] == "screening"
    deal_id = body["id"]

    get_resp = client.get(
        f"/api/v1/deals/{deal_id}",
        headers=_headers(user.id, fund.id),
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == deal_id


@pytest.mark.asyncio
async def test_list_deals_only_same_fund(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    fund_a = await _fund_entity(db_session)
    user_a = await _pm_user(db_session, fund_a.id)
    fund_b = await _fund_entity(db_session)
    user_b = await _pm_user(db_session, fund_b.id)
    await db_session.commit()

    for name in ("Deal A1", "Deal A2"):
        client.post(
            "/api/v1/deals",
            json={"name": name},
            headers=_headers(user_a.id, fund_a.id),
        )
    client.post(
        "/api/v1/deals",
        json={"name": "Deal B1"},
        headers=_headers(user_b.id, fund_b.id),
    )

    list_a = client.get("/api/v1/deals", headers=_headers(user_a.id, fund_a.id))
    assert list_a.status_code == 200
    names_a = {d["name"] for d in list_a.json()}
    assert names_a == {"Deal A1", "Deal A2"}

    list_b = client.get("/api/v1/deals", headers=_headers(user_b.id, fund_b.id))
    assert list_b.status_code == 200
    names_b = {d["name"] for d in list_b.json()}
    assert names_b == {"Deal B1"}


@pytest.mark.asyncio
async def test_pm_cannot_see_other_fund_deal(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    fund_a = await _fund_entity(db_session)
    user_a = await _pm_user(db_session, fund_a.id)
    fund_b = await _fund_entity(db_session)
    user_b = await _pm_user(db_session, fund_b.id)
    await db_session.commit()

    create_resp = client.post(
        "/api/v1/deals",
        json={"name": "Secret Deal"},
        headers=_headers(user_a.id, fund_a.id),
    )
    deal_id = create_resp.json()["id"]

    cross_get = client.get(
        f"/api/v1/deals/{deal_id}",
        headers=_headers(user_b.id, fund_b.id),
    )
    assert cross_get.status_code == 404

    # Changing only fund_id (with attacker pm_id) should also fail because
    # isolation filters by the active RequestContext, not by URL parameters.
    cross_list = client.get("/api/v1/deals", headers=_headers(user_b.id, fund_a.id))
    assert cross_list.status_code == 200
    assert not any(d["id"] == deal_id for d in cross_list.json())


@pytest.mark.asyncio
async def test_update_and_delete_deal(client: TestClient, db_session: AsyncSession) -> None:
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    create_resp = client.post(
        "/api/v1/deals",
        json={"name": "Old Name"},
        headers=_headers(user.id, fund.id),
    )
    deal_id = create_resp.json()["id"]

    patch_resp = client.patch(
        f"/api/v1/deals/{deal_id}",
        json={"name": "New Name", "stage": "dd"},
        headers=_headers(user.id, fund.id),
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["name"] == "New Name"
    assert patched["stage"] == "dd"

    delete_resp = client.delete(
        f"/api/v1/deals/{deal_id}",
        headers=_headers(user.id, fund.id),
    )
    assert delete_resp.status_code == 204

    get_resp = client.get(
        f"/api/v1/deals/{deal_id}",
        headers=_headers(user.id, fund.id),
    )
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Document upload, metadata, idempotency, and audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_document_records_metadata_and_audit(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    deal_resp = client.post(
        "/api/v1/deals",
        json={"name": "CIM Deal"},
        headers=_headers(user.id, fund.id),
    )
    deal_id = deal_resp.json()["id"]

    file_bytes = b"%PDF-1.4 fake pdf content"
    payload: dict[str, Any] = {
        "source_type": "cim",
        "file_content_b64": base64.b64encode(file_bytes).decode("ascii"),
        "file_path": "/uploads/fake.pdf",
        "content_url": "https://example.com/fake.pdf",
        "mime_type": "application/pdf",
        "extracted_entities": {"issuer": "Acme"},
    }

    upload_resp = client.post(
        f"/api/v1/deals/{deal_id}/documents",
        json=payload,
        headers=_headers(user.id, fund.id),
    )
    assert upload_resp.status_code == 201, upload_resp.text
    body = upload_resp.json()
    assert body["is_new"] is True
    doc = body["document"]
    assert doc["deal_id"] == deal_id
    assert doc["source_type"] == "cim"
    assert doc["file_path"] == "/uploads/fake.pdf"
    assert doc["content_url"] == "https://example.com/fake.pdf"
    assert doc["mime_type"] == "application/pdf"
    assert doc["file_size"] == len(file_bytes)
    expected_hash = content_hash(file_bytes.decode("utf-8", errors="replace"))
    assert doc["content_hash"] == expected_hash
    doc_id = doc["id"]

    audit_result = await db_session.execute(
        select(AuditLog)
        .where(
            AuditLog.object_type == "deal_document",
            AuditLog.object_id == doc_id,
        )
        .order_by(AuditLog.server_timestamp)
    )
    audit_entries = list(audit_result.scalars().all())
    assert len(audit_entries) == 1
    assert audit_entries[0].action_type == "deal_document_create"
    assert audit_entries[0].pm_id == user.id
    assert audit_entries[0].fund_entity_id == fund.id


@pytest.mark.asyncio
async def test_duplicate_document_upload_is_idempotent(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    deal_resp = client.post(
        "/api/v1/deals",
        json={"name": "Dedup Deal"},
        headers=_headers(user.id, fund.id),
    )
    deal_id = deal_resp.json()["id"]

    file_bytes = b"duplicate content"
    payload = {
        "source_type": "lpa",
        "file_content_b64": base64.b64encode(file_bytes).decode("ascii"),
        "mime_type": "application/pdf",
    }

    first = client.post(
        f"/api/v1/deals/{deal_id}/documents",
        json=payload,
        headers=_headers(user.id, fund.id),
    )
    assert first.status_code == 201
    first_doc = first.json()["document"]
    assert first.json()["is_new"] is True

    second = client.post(
        f"/api/v1/deals/{deal_id}/documents",
        json=payload,
        headers=_headers(user.id, fund.id),
    )
    assert second.status_code == 201
    second_doc = second.json()["document"]
    assert second.json()["is_new"] is False
    assert second_doc["id"] == first_doc["id"]

    # Only one DealDocument row should exist.
    doc_rows = await db_session.execute(select(DealDocument).where(DealDocument.deal_id == deal_id))
    assert len(list(doc_rows.scalars().all())) == 1


@pytest.mark.asyncio
async def test_list_and_get_documents(client: TestClient, db_session: AsyncSession) -> None:
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    deal_resp = client.post(
        "/api/v1/deals",
        json={"name": "Doc Deal"},
        headers=_headers(user.id, fund.id),
    )
    deal_id = deal_resp.json()["id"]

    payload = {
        "source_type": "nda",
        "file_content_b64": base64.b64encode(b"nda bytes").decode("ascii"),
        "mime_type": "application/pdf",
    }
    upload_resp = client.post(
        f"/api/v1/deals/{deal_id}/documents",
        json=payload,
        headers=_headers(user.id, fund.id),
    )
    doc_id = upload_resp.json()["document"]["id"]

    list_resp = client.get(
        f"/api/v1/deals/{deal_id}/documents",
        headers=_headers(user.id, fund.id),
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    get_resp = client.get(
        f"/api/v1/deals/{deal_id}/documents/{doc_id}",
        headers=_headers(user.id, fund.id),
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == doc_id


@pytest.mark.asyncio
async def test_deal_create_requires_identity_headers(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    """Without X-PM-ID and X-Fund-ID the endpoint must fail."""
    response = client.post(
        "/api/v1/deals",
        json={"name": "No Identity"},
        headers={"X-Role": "pm"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_document_upload_for_missing_deal_is_404(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    payload = {
        "source_type": "cim",
        "file_content_b64": base64.b64encode(b"orphan").decode("ascii"),
    }
    response = client.post(
        f"/api/v1/deals/{uuid.uuid4()}/documents",
        json=payload,
        headers=_headers(user.id, fund.id),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_other_fund_pm_cannot_list_documents(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    fund_a = await _fund_entity(db_session)
    user_a = await _pm_user(db_session, fund_a.id)
    fund_b = await _fund_entity(db_session)
    user_b = await _pm_user(db_session, fund_b.id)
    await db_session.commit()

    deal_resp = client.post(
        "/api/v1/deals",
        json={"name": "Protected Deal"},
        headers=_headers(user_a.id, fund_a.id),
    )
    deal_id = deal_resp.json()["id"]

    client.post(
        f"/api/v1/deals/{deal_id}/documents",
        json={
            "source_type": "cim",
            "file_content_b64": base64.b64encode(b"secret").decode("ascii"),
        },
        headers=_headers(user_a.id, fund_a.id),
    )

    list_resp = client.get(
        f"/api/v1/deals/{deal_id}/documents",
        headers=_headers(user_b.id, fund_b.id),
    )
    # Fund B cannot see fund A's documents; the scoped list returns empty.
    assert list_resp.status_code == 200
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_admin_can_see_deals(client: TestClient, db_session: AsyncSession) -> None:
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    create_resp = client.post(
        "/api/v1/deals",
        json={"name": "Admin View Deal"},
        headers=_headers(user.id, fund.id, role="admin"),
    )
    assert create_resp.status_code == 201


# ---------------------------------------------------------------------------
# Underwriting checklist + scenario loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_underwriting_checklist_and_scenario_loop(
    client: TestClient,
    db_session: AsyncSession,
) -> None:
    """Create a deal, generate checklist, finalize, run scenarios, audit state transitions."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await db_session.commit()

    # 1. Create deal
    create_resp = client.post(
        "/api/v1/deals",
        json={"name": "Underwrite Me", "asset_class": "private_equity"},
        headers=_headers(user.id, fund.id),
    )
    assert create_resp.status_code == 201, create_resp.text
    deal_id = create_resp.json()["id"]

    # 2. Initialize checklist from equity template
    init_resp = client.post(
        f"/api/v1/deals/{deal_id}/underwriting/checklist",
        json={"vehicle_type": "equity"},
        headers=_headers(user.id, fund.id),
    )
    assert init_resp.status_code == 201, init_resp.text
    checklist = init_resp.json()
    assert len(checklist) == 5
    assert {item["category"] for item in checklist} == {
        "Business",
        "Financials",
        "Management",
        "Risk",
        "Valuation",
    }
    required_items = [item for item in checklist if item["required"]]
    assert len(required_items) == 4

    # 3. Scenarios cannot run before required items are checked
    blocked_resp = client.post(
        f"/api/v1/deals/{deal_id}/underwriting/scenarios",
        json={"thesis_text": "We believe Acme will grow revenue 20% annually."},
        headers=_headers(user.id, fund.id),
    )
    assert blocked_resp.status_code == 422

    # 4. Check all required items
    for item in required_items:
        patch_resp = client.patch(
            f"/api/v1/deals/{deal_id}/underwriting/checklist/{item['id']}",
            json={"status": "checked", "answered_by": user.id},
            headers=_headers(user.id, fund.id),
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["status"] == "checked"

    # 5. Run scenarios
    run_resp = client.post(
        f"/api/v1/deals/{deal_id}/underwriting/scenarios",
        json={"thesis_text": "We believe Acme will grow revenue 20% annually."},
        headers=_headers(user.id, fund.id),
    )
    assert run_resp.status_code == 201, run_resp.text
    run_body = run_resp.json()
    assert "overall_confidence" in run_body
    assert len(run_body["scenarios"]) == 3
    scenario_names = {s["scenario_name"] for s in run_body["scenarios"]}
    assert "Base case" in scenario_names

    # 6. List persisted scenarios
    list_resp = client.get(
        f"/api/v1/deals/{deal_id}/underwriting/scenarios",
        headers=_headers(user.id, fund.id),
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 3

    scenario_ids = [s["id"] for s in run_body["scenarios"]]

    # 7. Verify audit log state transitions
    audit_result = await db_session.execute(
        select(AuditLog)
        .where(
            AuditLog.object_id.in_(
                [deal_id] + [item["id"] for item in required_items] + scenario_ids
            ),
        )
        .order_by(AuditLog.server_timestamp)
    )
    audit_types = [entry.action_type for entry in audit_result.scalars().all()]
    assert "deal_create" in audit_types
    assert "underwriting_checklist_initialized" in audit_types
    assert "underwriting_checklist_updated" in audit_types
    assert "underwriting_scenarios_generated" in audit_types

    # 8. Verify DB rows exist for non-required template too
    db_checklist = await db_session.execute(
        select(UnderwritingChecklist).where(UnderwritingChecklist.deal_id == deal_id)
    )
    assert len(list(db_checklist.scalars().all())) == 5
    db_scenarios = await db_session.execute(
        select(UnderwritingScenario).where(UnderwritingScenario.deal_id == deal_id)
    )
    assert len(list(db_scenarios.scalars().all())) == 3


# ---------------------------------------------------------------------------
# IC memo generation, sign-off, and immutability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ic_memo_generation_sign_off_and_immutability(
    client: TestClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generate memo, sign by two users, attempt edit, assert immutable and audit trail."""
    monkeypatch.setattr("axe.services.ic_memo.get_default_provider", _mock_provider)
    fund = await _fund_entity(db_session)
    user_a = await _pm_user(db_session, fund.id)
    user_b = await _pm_user(db_session, fund.id)
    await db_session.commit()

    # 1. Create deal
    create_resp = client.post(
        "/api/v1/deals",
        json={"name": "IC Memo Deal", "asset_class": "private_equity"},
        headers=_headers(user_a.id, fund.id),
    )
    assert create_resp.status_code == 201, create_resp.text
    deal_id = create_resp.json()["id"]

    # 2. Initialize checklist and complete required items
    init_resp = client.post(
        f"/api/v1/deals/{deal_id}/underwriting/checklist",
        json={"vehicle_type": "equity"},
        headers=_headers(user_a.id, fund.id),
    )
    assert init_resp.status_code == 201, init_resp.text
    checklist = init_resp.json()
    required_items = [item for item in checklist if item["required"]]
    for item in required_items:
        patch_resp = client.patch(
            f"/api/v1/deals/{deal_id}/underwriting/checklist/{item['id']}",
            json={"status": "checked", "answered_by": user_a.id},
            headers=_headers(user_a.id, fund.id),
        )
        assert patch_resp.status_code == 200, patch_resp.text

    # 3. Generate scenarios
    run_resp = client.post(
        f"/api/v1/deals/{deal_id}/underwriting/scenarios",
        json={"thesis_text": "Acme will compound revenue at 20% annually."},
        headers=_headers(user_a.id, fund.id),
    )
    assert run_resp.status_code == 201, run_resp.text

    # 4. Create a deal thesis
    thesis_resp = client.post(
        f"/api/v1/deals/{deal_id}/thesis",
        json={
            "stage": "ic_review",
            "bull_case": " strong revenue growth and margin expansion",
            "bear_case": "Macro slowdown compresses multiples.",
            "key_assumptions": ["Revenue growth >15%", "Stable margins"],
            "risks": ["Competition", "Regulation"],
        },
        headers=_headers(user_a.id, fund.id),
    )
    assert thesis_resp.status_code == 201, thesis_resp.text

    # 5. Generate IC memo
    memo_resp = client.post(
        f"/api/v1/deals/{deal_id}/ic-memos",
        headers=_headers(user_a.id, fund.id),
    )
    assert memo_resp.status_code == 201, memo_resp.text
    memo = memo_resp.json()
    memo_id = memo["id"]
    assert memo["deal_id"] == deal_id
    assert memo["status"] == "draft"
    assert isinstance(memo["content_json"], dict)
    assert "recommendation" in memo["content_json"]
    assert isinstance(memo["content_md"], str)
    assert "# IC Memo" in memo["content_md"]

    # 6. Sign by two different users
    for signer in (user_a, user_b):
        sign_resp = client.post(
            f"/api/v1/deals/{deal_id}/ic-memos/{memo_id}/signoffs",
            json={"signer_pm_id": signer.id},
            headers=_headers(user_a.id, fund.id),
        )
        assert sign_resp.status_code == 200, sign_resp.text
        signed = sign_resp.json()
        assert signed["status"] == ("final_signed" if signer is user_b else "draft")

    # 7. Verify final signed state
    get_resp = client.get(
        f"/api/v1/deals/{deal_id}/ic-memos/{memo_id}",
        headers=_headers(user_a.id, fund.id),
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "final_signed"

    # 8. Attempted edit after final sign-off is blocked
    edit_resp = client.patch(
        f"/api/v1/deals/{deal_id}/ic-memos/{memo_id}",
        json={"content_md": "tampered"},
        headers=_headers(user_a.id, fund.id),
    )
    assert edit_resp.status_code == 422, edit_resp.text

    # 9. Verify audit log contains create, sign, and attempted-edit events
    audit_result = await db_session.execute(
        select(AuditLog)
        .where(AuditLog.object_type == "ic_memo", AuditLog.object_id == memo_id)
        .order_by(AuditLog.server_timestamp)
    )
    audit_types = [entry.action_type for entry in audit_result.scalars().all()]
    assert "ic_memo_created" in audit_types
    assert audit_types.count("ic_memo_signed") == 2
    assert "ic_memo_attempted_edit_after_final_signoff" in audit_types

    # 10. Verify DB rows
    db_memos = await db_session.execute(
        select(ICMemo).where(ICMemo.deal_id == deal_id)
    )
    assert len(list(db_memos.scalars().all())) == 1
    db_signoffs = await db_session.execute(
        select(ICSignOff).where(ICSignOff.memo_id == memo_id)
    )
    signoffs = list(db_signoffs.scalars().all())
    assert len(signoffs) == 2
    assert {so.pm_id for so in signoffs} == {user_a.id, user_b.id}

    db_thesis = await db_session.execute(
        select(DealThesisVersion).where(DealThesisVersion.deal_id == deal_id)
    )
    assert len(list(db_thesis.scalars().all())) == 1
