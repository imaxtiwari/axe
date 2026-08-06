"""Tests for the ConnectorService dedup/normalize/enqueue pipeline."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from axe.connectors import ConnectorError
from axe.db.models import (
    AuditLog,
    ConnectorConfig,
    FundEntity,
    PMUser,
    RetryQueue,
)
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext
from axe.services.connector import ConnectorService, normalize_payload_to_raw_ingest


async def _fund_and_pm(session, uow: UnitOfWork) -> tuple[FundEntity, PMUser]:
    fund = FundEntity(id=str(uuid.uuid4()), legal_name=f"Fund {uuid.uuid4().hex[:8]}")
    session.add(fund)
    await session.flush()
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    await session.flush()
    return fund, user


@pytest.mark.asyncio
async def test_connector_service_run_persists_raw_ingest_and_enqueues(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        payload = [
            {
                "id": "re1",
                "ticker": "GOOGL:US",
                "title": "Note",
                "body": "Body",
                "published_at": "2024-01-01",
            }
        ]
        uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="research_edge",
            credentials_encrypted={"payload": payload},
            enabled=True,
        )
        await uow.commit()

        service = ConnectorService(uow)
        result = await service.run("research_edge", user.id)

        assert result["source_type"] == "research_edge"
        assert result["fetched"] == 1
        assert result["new"] == 1
        assert result["duplicates"] == 0
        assert result["errors"] == []

        raw_rows = await uow.raw_ingests.list_for_pm()
        assert any(r.source_type == "research_edge" and r.external_id == "re1" for r in raw_rows)

        stmt = select(RetryQueue).where(RetryQueue.pm_id == user.id)
        queue_result = await db_session.execute(stmt)
        tasks = list(queue_result.scalars().all())
        assert any(t.task_type == "specialize_signal" for t in tasks)
        task = next(t for t in tasks if t.task_type == "specialize_signal")
        assert task.payload["pm_id"] == user.id
        assert task.payload["source_type"] == "research_edge"
        assert task.payload["_idempotency_key"] == "research_edge:re1"
        assert "raw_ingest_id" in task.payload
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_connector_service_dedup_by_content_hash(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        payload = [
            {
                "id": "re1",
                "ticker": "GOOGL:US",
                "title": "Note",
                "body": "Body",
                "published_at": "2024-01-01",
            }
        ]
        uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="research_edge",
            credentials_encrypted={"payload": payload},
            enabled=True,
        )
        await uow.commit()

        service = ConnectorService(uow)
        first = await service.run("research_edge", user.id)
        assert first["new"] == 1

        second = await service.run("research_edge", user.id)
        assert second["fetched"] == 1
        assert second["new"] == 0
        assert second["duplicates"] == 1
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_connector_service_disabled_config(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="research_edge",
            credentials_encrypted={"payload": []},
            enabled=False,
        )
        await uow.commit()

        service = ConnectorService(uow)
        result = await service.run("research_edge", user.id)
        assert result["fetched"] == 0
        assert result["new"] == 0
        assert result["errors"] == ["connector disabled"]
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_connector_service_missing_config(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        service = ConnectorService(uow)
        with pytest.raises(ConnectorError):
            await service.run("research_edge", user.id)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_connector_service_disabled_source_type(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        service = ConnectorService(uow)
        with pytest.raises(ConnectorError):
            await service.run("not_enabled", user.id)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_connector_service_run_all(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="research_edge",
            credentials_encrypted={
                "payload": [
                    {"id": "r1", "ticker": "AAPL", "title": "T", "body": "B", "published_at": "x"}
                ]
            },
            enabled=True,
        )
        uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="crm",
            credentials_encrypted={"payload": [{"id": "c1", "subject": "S"}]},
            enabled=True,
        )
        await uow.commit()

        service = ConnectorService(uow)
        results = await service.run_all(user.id)
        assert len(results) == 2
        assert all(r["new"] == 1 for r in results)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_connector_service_cursor_update(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        payload = [
            {
                "id": f"r{i}",
                "ticker": "AAPL",
                "title": f"T{i}",
                "body": f"B{i}",
                "published_at": "x",
            }
            for i in range(3)
        ]
        config = uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="research_edge",
            credentials_encrypted={"payload": payload},
            enabled=True,
        )
        await uow.commit()

        service = ConnectorService(uow)
        result = await service.run("research_edge", user.id, limit=2)
        assert result["new"] == 2
        assert result["cursor"] == "2"

        refreshed = await db_session.get(ConnectorConfig, config.id)
        assert refreshed is not None
        assert refreshed.last_cursor == "2"
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_connector_service_writes_audit_log(db_session) -> None:
    uow = UnitOfWork(db_session)
    fund, user = await _fund_and_pm(db_session, uow)
    token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        payload = [{"id": "r1", "ticker": "AAPL", "title": "T", "body": "B", "published_at": "x"}]
        config = uow.connector_configs.create_config(
            pm_id=user.id,
            source_type="research_edge",
            credentials_encrypted={"payload": payload},
            enabled=True,
        )
        await uow.commit()

        service = ConnectorService(uow)
        await service.run("research_edge", user.id)

        stmt = select(AuditLog).where(AuditLog.object_id == config.id)
        audit_result = await db_session.execute(stmt)
        audit_rows = list(audit_result.scalars().all())
        assert any(r.action_type == "connector_run" for r in audit_rows)
    finally:
        RequestContext.reset_current(token)


@pytest.mark.asyncio
async def test_normalize_payload_to_raw_ingest() -> None:
    raw = await normalize_payload_to_raw_ingest(
        pm_id="pm1",
        source_type="manual",
        external_id="ext1",
        raw_payload={"x": 1},
        extracted_signal={"ticker": "AAPL"},
        ticker="AAPL",
    )
    assert raw.pm_id == "pm1"
    assert raw.source_type == "manual"
    assert raw.external_id == "ext1"
    assert raw.status == "pending"
    assert raw.extracted_signal_json == {"ticker": "AAPL"}
    assert raw.content_hash is not None
    assert raw.dedup_key == "ext1"
