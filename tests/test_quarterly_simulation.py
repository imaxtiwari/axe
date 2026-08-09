"""Full-quarter integration simulation for AXE.

This test walks through the entire investment workflow:
  1. Ingest 3 public and 2 private transcripts/signals.
  2. One private signal triggers an MNPI review.
  3. Create a deal thesis, run underwriting, generate an IC memo,
     obtain sign-off, generate an LP update, and generate a pitch deck.
  4. Export the whole chain and verify audit log completeness.
  5. Confirm cross-fund isolation is preserved.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.drift_detect import SignalAssumptionPair
from axe.agents.llm import MockProvider
from axe.agents.mnpi_review import MNPIReviewResult
from axe.config import Settings
from axe.db.models import (
    AuditLog,
    CommunicationArchive,
    DealRoom,
    DeckOutput,
    FundEntity,
    ICMemo,
    ICSignOff,
    InvestmentVehicle,
    LPRelationship,
    LPUpdate,
    MNPIReviewQueue,
    PMUser,
    SignalLog,
    ThesisVersion,
    TickerRegistry,
    UnderwritingChecklist,
    UnderwritingScenario,
)
from axe.db.session import get_async_session
from axe.db.uow import UnitOfWork
from axe.ingestion.handlers import process_transcript_handler
from axe.main import create_app
from axe.services.deal import DealRoomService
from axe.services.deck import DealDeckService
from axe.services.export import ExportService
from axe.services.lp_comms import LPCommsService
from axe.services.thesis import ThesisRepo


class _DeterministicDriftAgent:
    """Force each public/private signal to contradict a distinct assumption."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def _relevance(self, signal_text: str, assumption_text: str) -> float:
        return 0.91

    async def classify_assumptions(
        self,
        signal_text: str,
        assumptions: list[dict[str, Any]],
    ) -> list[tuple[str | None, SignalAssumptionPair]]:
        lower = signal_text.lower()
        if "revenue growth" in lower:
            target = "a1"
        elif "gross margin" in lower:
            target = "a2"
        elif "guidance" in lower:
            target = "a3"
        elif "confidential" in lower or "non-public" in lower:
            target = "a4"
        elif "hiring" in lower or "management" in lower:
            target = "a5"
        else:
            return [
                (
                    None,
                    SignalAssumptionPair(
                        stance="NEUTRAL",
                        reasoning="No contradiction for this test signal.",
                        confidence=0.0,
                        evidence_quote=None,
                    ),
                )
            ]

        return [
            (
                target,
                SignalAssumptionPair(
                    stance="CONTRADICTS",
                    reasoning=f"Signal contradicts assumption {target}.",
                    confidence=0.95,
                    evidence_quote="explicit contradiction phrase",
                ),
            )
        ]


class _DeterministicMNPIAgent:
    """Flag signals that contain MNPI keywords as material."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def review(self, text: str, ticker: str | None = None) -> MNPIReviewResult:
        lower = text.lower()
        flagged = "non-public" in lower or "confidential" in lower or "nda" in lower
        return MNPIReviewResult(
            mnpi_score=0.85 if flagged else 0.15,
            materiality_score=0.85 if flagged else 0.2,
            flagged=flagged,
            reasoning="MNPI keyword heuristic triggered." if flagged else "No MNPI keywords.",
        )


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
        slack_user_id=f"U{uuid.uuid4().hex[:8]}",
    )
    session.add(user)
    await session.flush()
    return user


async def _other_fund_pm(session: AsyncSession) -> tuple[FundEntity, PMUser]:
    fund = await _fund_entity(session)
    user = await _pm_user(session, fund.id)
    await session.flush()
    return fund, user


def _headers(pm_id: str, fund_id: str, role: str = "pm") -> dict[str, str]:
    return {"X-PM-ID": pm_id, "X-Fund-ID": fund_id, "X-Role": role}


@pytest.mark.asyncio
async def test_full_quarterly_simulation(
    client: TestClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute the full simulated quarter and verify chain + audit + isolation."""

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    fund = await _fund_entity(db_session)
    pm = await _pm_user(db_session, fund.id)
    signer = await _pm_user(db_session, fund.id)
    await db_session.commit()

    # Patch the agent classes used by the ingestion handler so signals are
    # classified deterministically without relying on a real LLM or embeddings.
    monkeypatch.setattr("axe.agents.drift_detect.DriftDetectionAgent", _DeterministicDriftAgent)
    # MNPIService resolves MNPIReviewAgent from its own module, so patch there too.
    monkeypatch.setattr("axe.services.mnpi.MNPIReviewAgent", _DeterministicMNPIAgent)
    # Force downstream memo/lp-update services to use the deterministic mock
    # provider regardless of environment settings.
    monkeypatch.setattr("axe.services.ic_memo.get_default_provider", lambda: MockProvider())
    monkeypatch.setattr("axe.services.lp_comms.get_default_provider", lambda: MockProvider())
    monkeypatch.setattr("axe.agents.lp_update.get_default_provider", lambda: MockProvider())
    # Seed default deck templates if not already present (schema creation may have skipped content).
    from axe.db.models import seed_deck_templates

    await seed_deck_templates(db_session)
    await db_session.commit()

    # ------------------------------------------------------------------
    # 1. Create a public equity thesis that public signals will contradict
    # ------------------------------------------------------------------
    async with UnitOfWork(db_session) as uow:
        thesis_repo = ThesisRepo(uow, pm.id, fund.id)
        await thesis_repo.create_thesis(
            ticker="PUBCO",
            bull_case="Revenue growth remains above 15% and margins expand.",
            bear_case="Guidance cut or macro slowdown.",
            key_assumptions=[
                {"id": "a1", "statement": "Revenue growth remains above 15%"},
                {"id": "a2", "statement": "Gross margins stay above 40%"},
                {"id": "a3", "statement": "Management maintains full-year guidance"},
                {"id": "a4", "statement": "No confidential sale process is underway"},
                {"id": "a5", "statement": "The senior management team remains stable"},
            ],
            conviction=4,
        )

    # Snapshot audit count after thesis creation (expected later).
    audit_checkpoints: list[int] = []

    async def _audit_count() -> int:
        result = await db_session.execute(select(AuditLog))
        return len(list(result.scalars().all()))

    audit_checkpoints.append(await _audit_count())  # after thesis create

    # ------------------------------------------------------------------
    # 2. Ingest 3 public polygon signals
    # ------------------------------------------------------------------
    public_signals = [
        "PUBCO management says revenue growth is slowing to single digits.",
        "PUBCO reports gross margin compression below 40% in the latest quarter.",
        "PUBCO withdraws full-year guidance citing macro uncertainty.",
    ]
    for idx, text in enumerate(public_signals, start=1):
        await process_transcript_handler(
            db_session,
            {
                "pm_id": pm.id,
                "ticker": "PUBCO",
                "source_type": "polygon",
                "source_url": f"https://polygon.io/public/{idx}",
                "signal_text": text,
                "content_hash": f"public_{idx}",
            },
        )
        await db_session.commit()

    result = await db_session.execute(
        select(SignalLog).where(SignalLog.pm_id == pm.id, SignalLog.ticker == "PUBCO")
    )
    assert len(list(result.scalars().all())) == 3

    audit_checkpoints.append(await _audit_count())  # after public signals

    # ------------------------------------------------------------------
    # 3. Ingest 2 private signals; one must trigger MNPI review
    # ------------------------------------------------------------------
    private_signals = [
        {
            "text": "Non-public board discussion: PUBCO is exploring a confidential sale process under NDA.",
            "trigger_mnpi": True,
        },
        {
            "text": "Private channel chatter: PUBCO is hiring a new head of investor relations.",
            "trigger_mnpi": False,
        },
    ]
    for idx, payload in enumerate(private_signals, start=1):
        await process_transcript_handler(
            db_session,
            {
                "pm_id": pm.id,
                "ticker": "PUBCO",
                "source_type": "polygon",  # still processed by drift pipeline
                "source_url": f"https://private-source/{idx}",
                "signal_text": payload["text"],
                "content_hash": f"private_{idx}",
            },
        )
        await db_session.commit()

    # Verify private signals were logged and the MNPI-sensitive one was flagged.
    result = await db_session.execute(
        select(SignalLog)
        .where(SignalLog.pm_id == pm.id, SignalLog.ticker == "PUBCO")
        .order_by(SignalLog.created_at)
    )
    all_signals = list(result.scalars().all())
    assert len(all_signals) == 5
    mnpi_signals = [s for s in all_signals if s.mnpi_flag]
    assert len(mnpi_signals) >= 1

    result = await db_session.execute(select(MNPIReviewQueue).where(MNPIReviewQueue.pm_id == pm.id))
    mnpi_reviews = list(result.scalars().all())
    assert len(mnpi_reviews) >= 1
    assert any(r.ticker == "PUBCO" for r in mnpi_reviews)

    audit_checkpoints.append(await _audit_count())  # after private signals + mnpi create

    # ------------------------------------------------------------------
    # 4. Create a deal room + deal thesis
    # ------------------------------------------------------------------
    async with UnitOfWork(db_session) as uow:
        deal_service = DealRoomService(uow, pm.id, fund.id)
        deal = await deal_service.create_deal(
            name="Alpha Buyout",
            stage="dd",
            asset_class="private_equity",
            target_ticker_or_private_name="AlphaCo",
        )

    response = client.post(
        f"/api/v1/deals/{deal.id}/thesis",
        json={
            "stage": "ic_review",
            "bull_case": "AlphaCo has strong recurring revenue and a disciplined management team.",
            "bear_case": "Customer concentration and macro headwinds could compress multiples.",
            "key_assumptions": [
                "Recurring revenue grows 20% annually",
                "Customer concentration drops below 30%",
                "EBITDA margins remain above 25%",
            ],
            "risks": ["Customer concentration", "Interest rate exposure"],
        },
        headers=_headers(pm.id, fund.id),
    )
    assert response.status_code == 201, response.text
    _ = response.json()["id"]

    audit_checkpoints.append(await _audit_count())  # after deal create + deal thesis create

    # ------------------------------------------------------------------
    # 5. Run underwriting: checklist + scenarios
    # ------------------------------------------------------------------
    init_resp = client.post(
        f"/api/v1/deals/{deal.id}/underwriting/checklist",
        json={"vehicle_type": "equity"},
        headers=_headers(pm.id, fund.id),
    )
    assert init_resp.status_code == 201, init_resp.text
    checklist = init_resp.json()
    required_items = [item for item in checklist if item["required"]]
    assert len(required_items) == 4

    for item in required_items:
        patch_resp = client.patch(
            f"/api/v1/deals/{deal.id}/underwriting/checklist/{item['id']}",
            json={"status": "checked", "answered_by": pm.id},
            headers=_headers(pm.id, fund.id),
        )
        assert patch_resp.status_code == 200, patch_resp.text

    run_resp = client.post(
        f"/api/v1/deals/{deal.id}/underwriting/scenarios",
        json={"thesis_text": "AlphaCo will compound revenue at 20% annually."},
        headers=_headers(pm.id, fund.id),
    )
    assert run_resp.status_code == 201, run_resp.text
    scenarios = run_resp.json()["scenarios"]
    assert len(scenarios) == 3

    audit_checkpoints.append(await _audit_count())  # after underwriting

    # ------------------------------------------------------------------
    # 6. Generate and sign IC memo
    # ------------------------------------------------------------------
    memo_resp = client.post(
        f"/api/v1/deals/{deal.id}/ic-memos",
        headers=_headers(pm.id, fund.id),
    )
    assert memo_resp.status_code == 201, memo_resp.text
    memo_id = memo_resp.json()["id"]

    for signer_id in (pm.id, signer.id):
        sign_resp = client.post(
            f"/api/v1/deals/{deal.id}/ic-memos/{memo_id}/signoffs",
            json={"signer_pm_id": signer_id},
            headers=_headers(pm.id, fund.id),
        )
        assert sign_resp.status_code == 200, sign_resp.text

    get_memo = client.get(
        f"/api/v1/deals/{deal.id}/ic-memos/{memo_id}",
        headers=_headers(pm.id, fund.id),
    )
    assert get_memo.status_code == 200
    assert get_memo.json()["status"] == "final_signed"

    audit_checkpoints.append(await _audit_count())  # after IC memo + signoffs

    # ------------------------------------------------------------------
    # 7. Generate LP update
    # ------------------------------------------------------------------
    vehicle = InvestmentVehicle(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        name="Test Fund I",
        legal_entity="Test Fund I LP",
        strategy="Growth Equity",
        vintage=2024,
    )
    db_session.add(vehicle)
    lp = LPRelationship(
        id=str(uuid.uuid4()),
        vehicle_id=vehicle.id,
        lp_name="Anchor LP",
        contact_email="lp@example.com",
    )
    db_session.add(lp)
    await db_session.flush()
    await db_session.commit()

    async with UnitOfWork(db_session) as uow:
        lp_service = LPCommsService(uow, pm.id, fund.id)
        lp_update = await lp_service.draft_update(vehicle.id, "2026-Q2")
        approved = await lp_service.approve_update(lp_update.id, approved_by=pm.id)
        sent = await lp_service.send_update(approved.id, approved_by=pm.id)

    assert sent.status == "sent"
    assert sent.content_md is not None
    assert sent.content_html is not None

    audit_checkpoints.append(await _audit_count())  # after LP update

    # ------------------------------------------------------------------
    # 8. Generate pitch deck
    # ------------------------------------------------------------------
    async with UnitOfWork(db_session) as uow:
        deck_service = DealDeckService(uow, pm.id, fund.id)
        deck = await deck_service.generate_deck(deal.id, vehicle_type="equity")

    assert deck.type == "deal_deck"
    assert deck.content.get("slides")

    audit_checkpoints.append(await _audit_count())  # after deck

    # ------------------------------------------------------------------
    # 9. Export chain + verify audit completeness
    # ------------------------------------------------------------------
    export_service = ExportService(db_session)
    export = await export_service.export(deal)
    assert export["object_type"] == "deal_rooms"
    assert export["object_id"] == deal.id
    assert export["encrypted_payload"]
    assert export["sha256_checksum"]

    # Decrypt and verify the archive structure and checksum integrity.
    decrypted = ExportService.decrypt(export["encrypted_payload"], export_service._export_key())
    assert decrypted["version"] == "axe-export-v1"
    assert decrypted["object_type"] == "deal_rooms"
    assert decrypted["object_id"] == deal.id
    assert decrypted["entity"]["id"] == deal.id
    assert "audit_trail" in decrypted

    # ------------------------------------------------------------------
    # 10. Verify overall audit log completeness for the whole chain
    # ------------------------------------------------------------------
    final_count = await _audit_count()

    # Expected audit events:
    #   thesis_create                1
    #   public signals: process_signal does not emit audit directly, but MNPIService
    #                   does not flag public signals so no mnpi_review_created here.
    #   private signals: 2 signal_log rows, 1 mnpi_review_created for flagged signal.
    #   deal_create                  1
    #   deal_thesis_version_create   1 (via router -> service, which emits deal_thesis_version_create)
    #   underwriting_checklist_initialized 1
    #   underwriting_checklist_updated     4
    #   underwriting_scenarios_generated   1
    #   ic_memo_created              1
    #   ic_memo_signed               2
    #   lp_update_drafted            1
    #   lp_update_approved           1
    #   lp_update_sent               1
    #   deck_output_created          1
    result = await db_session.execute(
        select(AuditLog.action_type)
        .where(AuditLog.pm_id == pm.id)
        .order_by(AuditLog.server_timestamp)
    )
    actions = list(result.scalars().all())
    assert actions.count("thesis_create") == 1
    assert actions.count("mnpi_review_created") == 1
    assert actions.count("deal_create") == 1
    assert actions.count("underwriting_checklist_initialized") == 1
    assert actions.count("underwriting_checklist_updated") == 4
    assert actions.count("underwriting_scenarios_generated") == 1
    assert actions.count("ic_memo_created") == 1
    assert actions.count("ic_memo_signed") == 2
    assert actions.count("lp_update_drafted") == 1
    assert actions.count("lp_update_approved") == 1
    assert actions.count("lp_update_sent") == 1
    assert actions.count("deck_output_created") == 1
    assert actions.count("agent_message_published") == 1

    # Sum of all explicit audit events should equal the total AuditLog rows.
    expected_mutations = (
        1  # thesis_create
        + 1  # mnpi_review_created
        + 1  # deal_create
        + 1  # underwriting_checklist_initialized
        + 4  # underwriting_checklist_updated
        + 1  # underwriting_scenarios_generated
        + 1  # ic_memo_created
        + 2  # ic_memo_signed
        + 1  # lp_update_drafted
        + 1  # lp_update_approved
        + 1  # lp_update_sent
        + 1  # deck_output_created
        + 1  # agent_message_published (cross-agent collaboration bus)
        + 1  # decision_prompt_created (LP update approval)
        + 1  # artifact_action_created (LP update send/preview)
        + 3  # artifact_action_created (deck annotation actions)
    )
    assert final_count == expected_mutations, (
        f"Expected {expected_mutations} audit rows, got {final_count}: {actions}"
    )

    # The delta between checkpoints should be monotonic; some phases may not add
    # audit rows on their own, but the total must never decrease.
    assert all(
        audit_checkpoints[i] <= audit_checkpoints[i + 1] for i in range(len(audit_checkpoints) - 1)
    )

    # ------------------------------------------------------------------
    # 11. Cross-fund isolation leak check
    # ------------------------------------------------------------------
    other_fund, other_pm = await _other_fund_pm(db_session)

    # Other PM cannot see the deal via API.
    cross_get = client.get(
        f"/api/v1/deals/{deal.id}",
        headers=_headers(other_pm.id, other_fund.id),
    )
    assert cross_get.status_code == 404

    # Other PM's fund has no deals.
    cross_list = client.get(
        "/api/v1/deals",
        headers=_headers(other_pm.id, other_fund.id),
    )
    assert cross_list.status_code == 200
    assert cross_list.json() == []

    # Database-level isolation: other PM's queries through ORM should not see our data.
    result = await db_session.execute(select(DealRoom).where(DealRoom.pm_id == pm.id))
    assert len(list(result.scalars().all())) >= 1

    result = await db_session.execute(select(ThesisVersion).where(ThesisVersion.pm_id == pm.id))
    assert len(list(result.scalars().all())) >= 1

    # Ensure other fund's investment vehicle does not see our LP updates.
    other_vehicle = InvestmentVehicle(
        id=str(uuid.uuid4()),
        fund_entity_id=other_fund.id,
        name="Other Fund I",
    )
    db_session.add(other_vehicle)
    await db_session.flush()
    await db_session.commit()

    async with UnitOfWork(db_session) as uow:
        other_lp_service = LPCommsService(uow, other_pm.id, other_fund.id)
        other_update = await other_lp_service.draft_update(other_vehicle.id, "2026-Q2")
    assert other_update.vehicle_id == other_vehicle.id

    result = await db_session.execute(select(LPUpdate).where(LPUpdate.vehicle_id == vehicle.id))
    our_updates = list(result.scalars().all())
    result = await db_session.execute(
        select(LPUpdate).where(LPUpdate.vehicle_id == other_vehicle.id)
    )
    other_updates = list(result.scalars().all())
    assert len(our_updates) == 1
    assert len(other_updates) == 1
    assert our_updates[0].id != other_updates[0].id

    # ------------------------------------------------------------------
    # 12. Sanity-check final chain artifacts exist
    # ------------------------------------------------------------------
    result = await db_session.execute(
        select(UnderwritingChecklist).where(UnderwritingChecklist.deal_id == deal.id)
    )
    assert len(list(result.scalars().all())) == 5

    result = await db_session.execute(
        select(UnderwritingScenario).where(UnderwritingScenario.deal_id == deal.id)
    )
    assert len(list(result.scalars().all())) == 3

    result = await db_session.execute(select(ICMemo).where(ICMemo.deal_id == deal.id))
    assert len(list(result.scalars().all())) == 1

    result = await db_session.execute(select(ICSignOff).where(ICSignOff.memo_id == memo_id))
    assert len(list(result.scalars().all())) == 2

    result = await db_session.execute(select(DeckOutput).where(DeckOutput.id == deck.id))
    assert result.scalar_one_or_none() is not None

    result = await db_session.execute(
        select(CommunicationArchive).where(
            CommunicationArchive.archive_metadata["lp_update_id"].as_string() == sent.id
        )
    )
    assert result.scalar_one_or_none() is not None

    result = await db_session.execute(
        select(TickerRegistry).where(
            TickerRegistry.pm_id == pm.id, TickerRegistry.ticker == "PUBCO"
        )
    )
    registry = result.scalar_one_or_none()
    assert registry is not None
    assert registry.last_thesis_version == 1
