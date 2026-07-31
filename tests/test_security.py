"""Security, encryption, audit, and isolation tests for AXE v2.1."""

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import (
    AuditLog,
    FundEntity,
    PMMemory,
    PMOAuthToken,
    PMUser,
    SignalLog,
    ThesisVersion,
    TickerRegistry,
)
from axe.security.audit import AuditService, audit_action
from axe.security.encryption import (
    EncryptionError,
    EncryptedJSON,
    decrypt_ciphertext,
    encrypt_plaintext,
    generate_fernet_key,
    get_fernet,
)
from axe.security.isolation import IsolationError, IsolationService


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


@pytest.fixture(autouse=True)
def _reset_encryption_key(monkeypatch: pytest.MonkeyPatch):
    """Set a deterministic Fernet key for EncryptedJSON DB round-trip tests."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    EncryptedJSON.configure(key)


@pytest.mark.asyncio
async def test_encrypted_json_orl_type(db_session: AsyncSession):
    """EncryptedJSON persists ciphertext in DB and returns plaintext in Python."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    token = PMOAuthToken(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        provider="google",
        token_payload={
            "access_token": "super_secret_access",
            "refresh_token": "super_secret_refresh",
        },
    )
    db_session.add(token)
    await db_session.flush()
    await db_session.refresh(token)

    assert token.token_payload["access_token"] == "super_secret_access"

    # Raw DB query should show encrypted bytes, not plaintext tokens.
    result = await db_session.execute(
        text("SELECT token_payload FROM pm_oauth_tokens WHERE id = :id"),
        {"id": token.id},
    )
    raw = result.scalar_one()
    assert "super_secret_access" not in str(raw)
    assert "super_secret_refresh" not in str(raw)


@pytest.mark.asyncio
async def test_isolation_service_blocks_cross_pm_reads(db_session: AsyncSession):
    """IsolationService ensures queries filter by pm_id."""
    fund = await _fund_entity(db_session)
    user_a = await _pm_user(db_session, fund.id)
    user_b = await _pm_user(db_session, fund.id)

    reg_a = TickerRegistry(id=str(uuid.uuid4()), pm_id=user_a.id, ticker="AAPL")
    reg_b = TickerRegistry(id=str(uuid.uuid4()), pm_id=user_b.id, ticker="TSLA")
    db_session.add_all([reg_a, reg_b])
    await db_session.flush()

    a_rows = await IsolationService.list_for_pm(db_session, TickerRegistry, user_a.id)
    b_rows = await IsolationService.list_for_pm(db_session, TickerRegistry, user_b.id)

    assert [r.ticker for r in a_rows] == ["AAPL"]
    assert [r.ticker for r in b_rows] == ["TSLA"]


@pytest.mark.asyncio
async def test_isolation_service_detects_contamination(db_session: AsyncSession):
    """IsolationService raises when another pm_id is detected in results."""
    fund = await _fund_entity(db_session)
    user_a = await _pm_user(db_session, fund.id)
    user_b = await _pm_user(db_session, fund.id)

    reg_a = TickerRegistry(id=str(uuid.uuid4()), pm_id=user_a.id, ticker="AAPL")
    reg_b = TickerRegistry(id=str(uuid.uuid4()), pm_id=user_b.id, ticker="TSLA")
    db_session.add_all([reg_a, reg_b])
    await db_session.flush()

    # Simulate a contaminated context by intentionally querying unscoped data.
    result = await db_session.execute(select(TickerRegistry))
    rows = result.scalars().all()
    with pytest.raises(IsolationError, match="isolation violation"):
        IsolationService.ensure_model_isolated(rows, user_a.id)


@pytest.mark.asyncio
async def test_audit_service_logs_create(db_session: AsyncSession):
    """AuditService writes an append-only audit record."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    service = AuditService(db_session)
    thesis_id = str(uuid.uuid4())
    await service.log(
        action_type="thesis_create",
        object_type="thesis_version",
        object_id=thesis_id,
        before_state={},
        after_state={"ticker": "NVDA", "version": 1, "conviction": 4},
        pm_id=user.id,
        fund_entity_id=fund.id,
        session_id="sess_123",
        non_blocking=False,
    )
    await db_session.commit()

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.object_id == thesis_id)
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.action_type == "thesis_create"
    assert log.after_state["conviction"] == 4


class _FakeRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, thesis_id: str, *, session: AsyncSession) -> ThesisVersion | None:
        return await session.get(ThesisVersion, thesis_id)

    @audit_action("thesis_update", "thesis_version")
    async def update_thesis(
        self,
        thesis_id: str,
        new_conviction: int,
        *,
        pm_id: str,
        fund_entity_id: str,
        session: AsyncSession,
    ) -> ThesisVersion:
        thesis = await session.get(ThesisVersion, thesis_id)
        if thesis is None:
            fund = await _fund_entity(session)
            thesis = ThesisVersion(
                id=thesis_id,
                pm_id=pm_id,
                ticker="NVDA",
                version=1,
                fund_entity_id=fund_entity_id,
                conviction=new_conviction,
            )
            session.add(thesis)
            await session.flush()
        else:
            thesis.conviction = new_conviction
        return thesis


@pytest.mark.asyncio
async def test_audit_action_decorator(db_session: AsyncSession):
    """The @audit_action decorator wraps a function and emits an audit log."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    repo = _FakeRepo(db_session)
    thesis_id = str(uuid.uuid4())
    thesis = await repo.update_thesis(
        thesis_id,
        5,
        pm_id=user.id,
        fund_entity_id=fund.id,
        session=db_session,
    )

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.object_id == thesis_id)
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.pm_id == user.id
    assert log.fund_entity_id == fund.id
    assert log.after_state.get("conviction") == 5
    assert thesis.conviction == 5


@pytest.mark.asyncio
async def test_audit_action_decorator_skips_without_pm_id(db_session: AsyncSession):
    """Decorator is a no-op when audit identity metadata is missing."""

    class _NoIdentityRepo:
        @audit_action("signal_ingest", "signal_log")
        async def ingest_signal(self, signal_id: str) -> dict[str, str]:
            return {"id": signal_id, "ingested": "true"}  # type: ignore[return-value]

    repo = _NoIdentityRepo()
    sid = str(uuid.uuid4())
    await repo.ingest_signal(sid)

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.object_id == sid)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_audit_log_tamper_resistance(db_session: AsyncSession):
    """AuditLog rows cannot be mutated or deleted via the ORM."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    log = AuditLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        fund_entity_id=fund.id,
        action_type="thesis_create",
        object_type="thesis_version",
        object_id=str(uuid.uuid4()),
    )
    db_session.add(log)
    await db_session.commit()

    loaded = await db_session.get(AuditLog, log.id)
    assert loaded is not None
    with pytest.raises(RuntimeError, match="append-only"):
        loaded.action_type = "tampered"
        await db_session.flush()

    await db_session.rollback()


@pytest.mark.asyncio
async def test_signal_log_mnpi_flag(db_session: AsyncSession):
    """Signals can be flagged as MNPI and filtered appropriately."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="expert_network",
        content_hash="abc",
        mnpi_flag=True,
    )
    db_session.add(signal)
    await db_session.flush()

    assert signal.mnpi_flag is True


@pytest.mark.asyncio
async def test_memory_uncertainty_labels(db_session: AsyncSession):
    """PMMemory can carry uncertainty labels per profile field."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    memory = PMMemory(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        version=1,
        synthesis_trigger="test",
        profile={"decision_style": "contrarian"},
        uncertainty_labels={"decision_style": "strong_evidence"},
    )
    db_session.add(memory)
    await db_session.flush()

    assert memory.uncertainty_labels["decision_style"] == "strong_evidence"


def test_generate_fernet_key():
    """A generated key can initialize a Fernet instance."""
    key = generate_fernet_key()
    f = get_fernet(key)
    encrypted = f.encrypt(b"hello")
    assert f.decrypt(encrypted) == b"hello"


def test_encrypt_plaintext_helpers():
    """encrypt_plaintext and decrypt_ciphertext round-trip."""
    key = generate_fernet_key()
    plaintext = "sensitive payload"
    ciphertext = encrypt_plaintext(plaintext, key)
    assert decrypt_ciphertext(ciphertext, key) == plaintext


def test_encrypted_json_none_values():
    """EncryptedJSON accepts/processes None bind/result values."""
    col = EncryptedJSON()
    assert col.process_bind_param(None, None) is None
    assert col.process_result_value(None, None) is None


def test_encryption_key_missing(monkeypatch: pytest.MonkeyPatch):
    """Missing ENCRYPTION_KEY raises RuntimeError."""
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    from axe.config import get_settings

    monkeypatch.setattr(get_settings(), "encryption_key", "")
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        get_fernet()


def test_encryption_invalid_key(monkeypatch: pytest.MonkeyPatch):
    """An invalid key raises RuntimeError."""
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    from axe.config import get_settings

    monkeypatch.setattr(get_settings(), "encryption_key", "not_a_valid_key")
    with pytest.raises(RuntimeError, match="32-byte"):
        get_fernet()


def test_encryption_error_bad_ciphertext():
    """EncryptedJSON decrypt wrapping a tampered token raises EncryptionError."""
    col = EncryptedJSON()
    col.configure(generate_fernet_key())
    ciphertext = col.process_bind_param({"foo": "bar"}, None)
    assert isinstance(ciphertext, str)
    tampered = ciphertext[:-4] + "AAAA"
    with pytest.raises(EncryptionError, match="Failed to decrypt"):
        col.process_result_value(tampered, None)


@pytest.mark.asyncio
async def test_isolation_service_get(db_session: AsyncSession):
    """IsolationService.get fetches scoped rows."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    other = await _pm_user(db_session, fund.id)

    reg = TickerRegistry(id=str(uuid.uuid4()), pm_id=user.id, ticker="AAPL")
    db_session.add(reg)
    await db_session.flush()

    found = await IsolationService.get(db_session, TickerRegistry, user.id, reg.id)
    assert found is not None
    assert found.ticker == "AAPL"

    notfound = await IsolationService.get(db_session, TickerRegistry, other.id, reg.id)
    assert notfound is None


def test_isolation_scope_invalid():
    """IsolationService.scope rejects missing pm_id or unsupported models."""

    class FakeStmt:
        def where(self, *args, **kwargs):
            return self

    class NoPmId:
        pass

    fake = FakeStmt()
    with pytest.raises(IsolationError, match="pm_id is required"):
        IsolationService.scope(fake, TickerRegistry, "")

    with pytest.raises(IsolationError, match="does not support pm_id"):
        IsolationService.scope(fake, NoPmId, "pm_123")


def test_isolation_memory_context_allows_own_pm():
    """ensure_memory_context_isolated permits the target pm_id and allow-list."""
    IsolationService.ensure_memory_context_isolated(
        [{"pm_id": "pm_a"}, {"pm_id": "pm_b"}],
        "pm_a",
        allowed_other_pm_ids={"pm_b"},
    )


def test_isolation_memory_context_detects_contamination():
    """ensure_memory_context_isolated raises on unexpected foreign pm_id."""
    with pytest.raises(IsolationError, match="Cross-PM contamination"):
        IsolationService.ensure_memory_context_isolated(
            [{"pm_id": "pm_a"}, {"pm_id": "pm_c", "found_in": "context"}],
            "pm_a",
        )


@pytest.mark.asyncio
async def test_audit_action_works_for_sync_result(db_session: AsyncSession):
    """audit_action handles functions that take an explicit object_id."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    @audit_action("signal_ingest", "signal_log")
    async def ingest_signal(
        *,
        object_id: str,
        pm_id: str,
        fund_entity_id: str,
        session: AsyncSession,
    ) -> str:
        return object_id

    sid = str(uuid.uuid4())
    await ingest_signal(
        object_id=sid,
        pm_id=user.id,
        fund_entity_id=fund.id,
        session=db_session,
    )

    result = await db_session.execute(
        select(AuditLog).where(AuditLog.object_id == sid)
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.action_type == "signal_ingest"
