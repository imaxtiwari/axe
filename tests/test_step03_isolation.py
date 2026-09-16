"""Regression coverage for the Step03 isolation-policy remediation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from axe.config import Settings
from axe.db.models import (
    AuditLog,
    FundEntity,
    LPUpdate,
    PMUser,
    SignalLog,
    ThesisPostMortem,
    ThesisTest,
    ThesisTestResult,
    ThesisVersion,
)
from axe.db.uow import (
    DealThesisRepository,
    ICMemoRepository,
    ICSignOffRepository,
    UnderwritingChecklistRepository,
    UnderwritingScenarioRepository,
    UnitOfWork,
)
from axe.exceptions import IsolationError
from axe.security.context import RequestContext
from axe.services.brief_scheduler import schedule_persona_refresh_jobs
from axe.services.compliance_escalation import ComplianceEscalationService
from axe.services.export import ExportService
from axe.services.ic_memo import ICMemoService
from axe.services.lp_comms import LPCommsService
from axe.services.retention import RetentionService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository,method,column",
    [
        (DealThesisRepository, "get_latest_for_deal", "deal_thesis_versions.pm_id"),
        (DealThesisRepository, "list_for_deal", "deal_thesis_versions.pm_id"),
        (ICMemoRepository, "get_latest_for_deal", "ic_memos.pm_id"),
        (ICMemoRepository, "list_for_deal", "ic_memos.pm_id"),
        (ICSignOffRepository, "list_for_memo", "ic_signoffs.fund_entity_id"),
        (UnderwritingChecklistRepository, "list_for_deal", "deal_rooms.pm_id"),
        (UnderwritingScenarioRepository, "list_for_deal", "deal_rooms.pm_id"),
    ],
)
async def test_repository_queries_require_scope(repository: Any, method: str, column: str) -> None:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=MagicMock())
    repo = repository(session)
    with pytest.raises(IsolationError):
        await getattr(repo, method)("foreign-object")
    session.execute.assert_not_called()
    with RequestContext.bind(pm_id="caller-pm", fund_id="caller-fund"):
        await getattr(repo, method)("foreign-object")
    statement = session.execute.call_args.args[0]
    compiled = statement.compile()
    assert f"{column} =" in str(statement.whereclause)
    assert (
        "caller-fund" if "fund_entity_id" in column else "caller-pm"
    ) in compiled.params.values()


@pytest.mark.asyncio
async def test_escalation_listing_fails_closed_and_inherits_pm_role() -> None:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=MagicMock())
    service = ComplianceEscalationService(session)
    with pytest.raises(IsolationError, match="fund_id"):
        await service.list_open()
    with (
        RequestContext.bind(fund_id="fund-a", role="pm"),
        pytest.raises(IsolationError, match="pm_id"),
    ):
        await service.list_open()
    session.execute.assert_not_called()
    with RequestContext.bind(pm_id="pm-a", fund_id="fund-a", role="pm"):
        await service.list_open()
    statement = session.execute.call_args.args[0]
    assert "compliance_escalation.pm_id =" in str(statement.whereclause)
    assert "fund-a" in statement.compile().params.values()
    assert "pm-a" in statement.compile().params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ["signal_log", "thesis_test_result", "thesis_post_mortem"])
async def test_retention_request_scope_and_system_maintenance(
    db_session: AsyncSession, entity_type: str
) -> None:
    fund = FundEntity(legal_name="Retention regression", data_residency="US")
    db_session.add(fund)
    await db_session.flush()
    users = [PMUser(fund_entity_id=fund.id, email=f"retention-{n}@example.com") for n in range(2)]
    db_session.add_all(users)
    await db_session.flush()
    rows: list[Any] = [
        SignalLog(
            pm_id=user.id,
            source_type="test",
            content_hash=f"retention-scope-{n}",
            created_at=datetime.now(UTC) - timedelta(days=4000),
        )
        for n, user in enumerate(users)
    ]
    if entity_type != "signal_log":
        rows = []
        for user in users:
            thesis = ThesisVersion(pm_id=user.id, fund_entity_id=fund.id, ticker="TEST", version=1)
            db_session.add(thesis)
            await db_session.flush()
            if entity_type == "thesis_test_result":
                thesis_test = ThesisTest(thesis_version_id=thesis.id, test_statement="Synthetic")
                db_session.add(thesis_test)
                await db_session.flush()
                row: Any = ThesisTestResult(test_id=thesis_test.id, result="pass")
            else:
                row = ThesisPostMortem(thesis_version_id=thesis.id, outcome="closed")
            row.created_at = datetime.now(UTC) - timedelta(days=4000)
            rows.append(row)
    db_session.add_all(rows)
    await db_session.flush()
    service = RetentionService(db_session, Settings(retention_days=365, retention_enabled=True))
    with RequestContext.bind(pm_id=users[0].id, fund_id=fund.id):
        result = await service.run(entity_types=[entity_type], dry_run=True)
        assert result["counts"] == {entity_type: 1}
        assert all(row.deleted_at is None for row in rows)
        result = await service.run(entity_types=[entity_type])
    assert result["counts"] == {entity_type: 1}
    await db_session.refresh(rows[0])
    await db_session.refresh(rows[1])
    assert rows[0].deleted_at is not None
    assert rows[1].deleted_at is None
    result = await service.run(entity_types=[entity_type])
    assert result["counts"] == {entity_type: 1}
    await db_session.refresh(rows[1])
    assert rows[1].deleted_at is not None


@pytest.mark.asyncio
async def test_export_excludes_foreign_audit_entries(db_session: AsyncSession) -> None:
    fund = FundEntity(legal_name="Export regression", data_residency="US")
    db_session.add(fund)
    await db_session.flush()
    users = [PMUser(fund_entity_id=fund.id, email=f"export-{n}@example.com") for n in range(2)]
    db_session.add_all(users)
    await db_session.flush()
    signal = SignalLog(pm_id=users[0].id, source_type="test", content_hash="export-scope")
    db_session.add(signal)
    await db_session.flush()
    for user in users:
        db_session.add(
            AuditLog(
                pm_id=user.id,
                fund_entity_id=fund.id,
                object_type="signal_log",
                object_id=signal.id,
                action_type="synthetic_export_probe",
            )
        )
    await db_session.flush()
    service = ExportService(db_session)
    exported = await service.export(signal)
    archive = service.decrypt(exported["encrypted_payload"], service._export_key())
    assert [row["pm_id"] for row in archive["audit_trail"]] == [users[0].id]


@pytest.mark.asyncio
async def test_reviewer_load_query_is_fund_scoped() -> None:
    session = MagicMock(spec=AsyncSession)
    candidates = MagicMock()
    candidates.scalars.return_value.all.return_value = [PMUser(id="reviewer-a")]
    counts = MagicMock()
    counts.all.return_value = []
    session.execute = AsyncMock(side_effect=[candidates, counts])
    service = ComplianceEscalationService(session)
    assert await service._next_reviewer("fund-a") == "reviewer-a"
    for call in session.execute.call_args_list:
        statement = call.args[0]
        assert "fund_entity_id =" in str(statement.whereclause)
        assert "fund-a" in statement.compile().params.values()


@pytest.mark.asyncio
async def test_memo_thesis_query_is_pm_scoped() -> None:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=MagicMock())
    service = ICMemoService(UnitOfWork(session), "pm-a", "fund-a", provider=MagicMock())
    await service._latest_deal_thesis("foreign-deal")
    statement = session.execute.call_args.args[0]
    assert "deal_thesis_versions.pm_id =" in str(statement.whereclause)
    assert "pm-a" in statement.compile().params.values()
    assert "foreign-deal" in statement.compile().params.values()


@pytest.mark.asyncio
async def test_lp_queries_scope_through_vehicle() -> None:
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=MagicMock(fund_entity_id="foreign-fund"))
    session.execute = AsyncMock(return_value=MagicMock())
    session.flush = AsyncMock()
    service = LPCommsService(UnitOfWork(session), "pm-a", "fund-a", provider=MagicMock())
    with pytest.raises(ValueError, match="not found"):
        await service.list_updates_for_vehicle("vehicle-a")
    session.execute.assert_not_called()
    session.get.return_value.fund_entity_id = "fund-a"
    await service.list_updates_for_vehicle("vehicle-a")
    await service._archive(
        LPUpdate(id="update-a", vehicle_id="vehicle-a", quarter="2026Q1", content_md="Synthetic")
    )
    assert session.execute.call_count == 2
    for call in session.execute.call_args_list:
        statement = call.args[0]
        assert "JOIN investment_vehicles" in str(statement)
        assert "investment_vehicles.fund_entity_id =" in str(statement.whereclause)
        assert "fund-a" in statement.compile().params.values()
        assert "vehicle-a" in statement.compile().params.values()


@pytest.mark.asyncio
async def test_persona_scheduler_dispatches_active_tenant_identities(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = []
    for n in range(2):
        fund = FundEntity(legal_name=f"Scheduler fund {n}", data_residency="US")
        db_session.add(fund)
        await db_session.flush()
        user = PMUser(fund_entity_id=fund.id, email=f"scheduler-{n}@example.com", active=True)
        db_session.add(user)
        users.append(user)
    db_session.add(
        PMUser(fund_entity_id=users[0].fund_entity_id, email="inactive@example.com", active=False)
    )
    await db_session.commit()
    refresh = AsyncMock()
    monkeypatch.setattr("axe.services.brief_scheduler.refresh_persona_for_pm", refresh)
    scheduler = MagicMock()
    schedule_persona_refresh_jobs(scheduler, db_session_factory)
    await scheduler.add_job.call_args.args[0]()
    assert refresh.await_count == 2
    assert {(call.args[0], call.kwargs["fund_id"]) for call in refresh.await_args_list} == {
        (user.id, user.fund_entity_id) for user in users
    }
