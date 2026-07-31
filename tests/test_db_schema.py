"""Comprehensive database schema tests for AXE v2.1."""

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import (
    AuditLog,
    CorporateAction,
    DealRoom,
    FundEntity,
    InvestmentVehicle,
    LPRelationship,
    LPUpdate,
    MorningBrief,
    PMMemory,
    PMMemoryColdStart,
    PMMemoryCorrection,
    PMOAuthToken,
    PMQuietHours,
    PMUser,
    RetryQueue,
    SignalFeedback,
    SignalLog,
    SparringSession,
    ThesisPostMortem,
    ThesisTest,
    ThesisTestResult,
    ThesisVersion,
    TickerRegistry,
)

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


async def _ticker_registry(session: AsyncSession, pm_id: str, ticker: str) -> TickerRegistry:
    reg = TickerRegistry(id=str(uuid.uuid4()), pm_id=pm_id, ticker=ticker)
    session.add(reg)
    await session.flush()
    return reg


@pytest.mark.asyncio
async def test_pragmas_enabled(engine):
    """WAL mode and foreign keys are enabled at the database level."""
    async with engine.connect() as conn:
        journal = (await conn.execute(text("PRAGMA journal_mode;"))).scalar()
        fk = (await conn.execute(text("PRAGMA foreign_keys;"))).scalar()
    assert journal == "wal"
    assert fk == 1


@pytest.mark.asyncio
async def test_create_pm_user_and_fund(db_session: AsyncSession):
    """A PM user can be created with a fund relationship."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    assert user.id
    assert user.fund_entity_id == fund.id
    assert user.active is True


@pytest.mark.asyncio
async def test_thesis_version_unique_per_pm_ticker_version(db_session: AsyncSession):
    """The (pm_id, ticker, version) tuple must be unique."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    await _ticker_registry(db_session, user.id, "AAPL")

    tv = ThesisVersion(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        version=1,
        fund_entity_id=fund.id,
    )
    db_session.add(tv)
    await db_session.flush()

    duplicate = ThesisVersion(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        version=1,
        fund_entity_id=fund.id,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_signal_log_content_hash_index(db_session: AsyncSession):
    """Signal log stores content hash and can be deduplicated."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="email",
        content_hash="sha256_hash",
        extracted_signal={"ticker": "TSLA", "direction": "bullish"},
        citation={"url": "http://example.com/note", "quote": "..."},
    )
    db_session.add(signal)
    await db_session.flush()
    assert signal.id
    assert signal.extraction_confidence is None


@pytest.mark.asyncio
async def test_audit_log_update_blocked(db_session: AsyncSession):
    """AuditLog rows cannot be updated via ORM events."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    entry = AuditLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        fund_entity_id=fund.id,
        action_type="thesis_create",
        object_type="thesis_version",
        object_id=str(uuid.uuid4()),
    )
    db_session.add(entry)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="append-only"):
        entry.action_type = "thesis_update"
        await db_session.flush()


@pytest.mark.asyncio
async def test_audit_log_delete_blocked(db_session: AsyncSession):
    """AuditLog rows cannot be deleted via ORM events."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    entry = AuditLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        fund_entity_id=fund.id,
        action_type="thesis_create",
        object_type="thesis_version",
        object_id=str(uuid.uuid4()),
    )
    db_session.add(entry)
    await db_session.commit()

    loaded = await db_session.get(AuditLog, entry.id)
    assert loaded is not None
    with pytest.raises(RuntimeError, match="append-only"):
        await db_session.delete(loaded)
        await db_session.flush()

    await db_session.rollback()


@pytest.mark.asyncio
async def test_pm_memory_version_unique(db_session: AsyncSession):
    """The (pm_id, version) tuple must be unique."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    mem = PMMemory(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        version=1,
        synthesis_trigger="test",
    )
    db_session.add(mem)
    await db_session.flush()

    duplicate = PMMemory(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        version=1,
        synthesis_trigger="test",
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_thesis_version_status_and_direction_defaults(db_session: AsyncSession):
    """Thesis defaults are populated correctly."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    tv = ThesisVersion(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="NVDA",
        version=1,
        fund_entity_id=fund.id,
    )
    db_session.add(tv)
    await db_session.flush()
    assert tv.direction == "long"
    assert tv.status == "active"
    assert tv.asset_class == "equity"


@pytest.mark.asyncio
async def test_corporate_action_storage(db_session: AsyncSession):
    """Corporate actions can be persisted with JSON details."""
    action = CorporateAction(
        id=str(uuid.uuid4()),
        ticker="AAPL",
        action_type="ticker_change",
        effective_date=date(2026, 6, 1),
        details={"old_ticker": "OLD", "new_ticker": "AAPL"},
    )
    db_session.add(action)
    await db_session.flush()
    assert action.id


@pytest.mark.asyncio
async def test_signal_feedback_foreign_key(db_session: AsyncSession):
    """Signal feedback references signal_log and users."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="polygon",
        content_hash="abc123",
    )
    db_session.add(signal)
    await db_session.flush()

    feedback = SignalFeedback(
        id=str(uuid.uuid4()),
        signal_id=signal.id,
        pm_id=user.id,
        reaction="not_relevant",
    )
    db_session.add(feedback)
    await db_session.flush()
    assert feedback.id


@pytest.mark.asyncio
async def test_quiet_hours_storage(db_session: AsyncSession):
    """Quiet hours can be stored per PM."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    qh = PMQuietHours(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        start_time="22:00",
        end_time="07:00",
        timezone="America/New_York",
        override_keywords=["breaking", "urgent"],
    )
    db_session.add(qh)
    await db_session.flush()
    assert qh.override_keywords == ["breaking", "urgent"]


@pytest.mark.asyncio
async def test_thesis_test_result_cascade(db_session: AsyncSession):
    """Thesis test results reference tests."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    tv = ThesisVersion(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="META",
        version=1,
        fund_entity_id=fund.id,
    )
    db_session.add(tv)
    await db_session.flush()

    test = ThesisTest(
        id=str(uuid.uuid4()),
        thesis_version_id=tv.id,
        test_statement="Revenue growth > 10%",
    )
    db_session.add(test)
    await db_session.flush()

    result = ThesisTestResult(
        id=str(uuid.uuid4()),
        test_id=test.id,
        result="pass",
    )
    db_session.add(result)
    await db_session.flush()
    assert result.evaluated_at <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_deal_room_and_thesis(db_session: AsyncSession):
    """Deal rooms, deal documents, and deal thesis versions can be linked."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    deal = DealRoom(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        fund_entity_id=fund.id,
        name="Buyout Target A",
    )
    db_session.add(deal)
    await db_session.flush()

    assert deal.stage == "screening"
    assert deal.asset_class == "private_equity"


@pytest.mark.asyncio
async def test_cross_pm_isolation(db_session: AsyncSession):
    """PM A cannot see PM B's ticker registry rows."""
    fund = await _fund_entity(db_session)
    user_a = await _pm_user(db_session, fund.id)
    user_b = await _pm_user(db_session, fund.id)
    await _ticker_registry(db_session, user_a.id, "AAPL")
    await _ticker_registry(db_session, user_b.id, "TSLA")

    a_tickers = await db_session.execute(
        text("SELECT ticker FROM ticker_registry WHERE pm_id = :pm_id"),
        {"pm_id": user_a.id},
    )
    a_rows = {r[0] for r in a_tickers.fetchall()}
    assert a_rows == {"AAPL"}


@pytest.mark.asyncio
async def test_oauth_token_unique_per_provider(db_session: AsyncSession):
    """OAuth tokens are unique per PM + provider."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    token = PMOAuthToken(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        provider="google",
        token_payload={"access_token": "encrypted_blob"},
    )
    db_session.add(token)
    await db_session.flush()

    duplicate = PMOAuthToken(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        provider="google",
        token_payload={"access_token": "encrypted_blob_2"},
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_morning_brief_unique_per_pm_date(db_session: AsyncSession):
    """Only one morning brief per PM per date."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    brief = MorningBrief(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        date=date(2026, 7, 29),
        sections=[{"ticker": "AAPL", "signal": "earnings"}],
    )
    db_session.add(brief)
    await db_session.flush()

    dup = MorningBrief(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        date=date(2026, 7, 29),
        sections=[{"ticker": "TSLA", "signal": "upgrade"}],
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_sparring_session_minimum_structure(db_session: AsyncSession):
    """Sparring sessions hold structured adversarial output."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    session = SparringSession(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="NVDA",
        input_thesis="Bullish on data center growth.",
        bear_case=[
            {"id": "b1", "challenge": "Capex cycle may peak in 2027"},
            {"id": "b2", "challenge": "China export risk"},
        ],
        break_conditions=["Data center revenue down QoQ", "Gross margin < 70%"],
    )
    db_session.add(session)
    await db_session.flush()
    assert len(session.bear_case) == 2


@pytest.mark.asyncio
async def test_memory_correction_storage(db_session: AsyncSession):
    """Memory corrections are versioned and retained."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    corr = PMMemoryCorrection(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        field="decision_style",
        old_value="growth",
        new_value="contrarian",
        corrected_at=datetime.now(timezone.utc),
    )
    db_session.add(corr)
    await db_session.flush()
    assert corr.created_at


@pytest.mark.asyncio
async def test_investment_vehicle_and_lp(db_session: AsyncSession):
    """Investment vehicles and LP relationships can be created."""
    fund = await _fund_entity(db_session)

    vehicle = InvestmentVehicle(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        name="Fund IV",
    )
    db_session.add(vehicle)
    await db_session.flush()

    lp = LPRelationship(
        id=str(uuid.uuid4()),
        vehicle_id=vehicle.id,
        lp_name="Pension Fund X",
        side_letter_flags={"mfn": True},
    )
    db_session.add(lp)
    await db_session.flush()

    update = LPUpdate(
        id=str(uuid.uuid4()),
        vehicle_id=vehicle.id,
        quarter="Q2 2026",
        sections=[{"header": "Performance", "body": "+5%"}],
    )
    db_session.add(update)
    await db_session.flush()
    assert lp.side_letter_flags["mfn"] is True


@pytest.mark.asyncio
async def test_post_mortem_storage(db_session: AsyncSession):
    """Post-mortem outcomes are stored against thesis versions."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    tv = ThesisVersion(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AMD",
        version=1,
        fund_entity_id=fund.id,
    )
    db_session.add(tv)
    await db_session.flush()

    pm = ThesisPostMortem(
        id=str(uuid.uuid4()),
        thesis_version_id=tv.id,
        outcome="wrong",
        broken_assumption_id="a1",
        notes="Gross margin compression",
    )
    db_session.add(pm)
    await db_session.flush()
    assert pm.outcome == "wrong"


@pytest.mark.asyncio
async def test_retry_queue_status_enum(db_session: AsyncSession):
    """Retry queue defaults to pending status."""
    task = RetryQueue(
        id=str(uuid.uuid4()),
        task_type="ingestion",
        payload={"url": "http://example.com"},
    )
    db_session.add(task)
    await db_session.flush()
    assert task.status == "pending"
