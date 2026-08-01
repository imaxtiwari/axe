"""Security, encryption, audit, and isolation tests for AXE v2.1."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

from axe.config import Settings
from axe.db.models import (
    AuditLog,
    CatalystEvent,
    CorporateAction,
    DeckTemplate,
    FundEntity,
    MNPIReviewQueue,
    PMMemory,
    PMOAuthToken,
    PMUser,
    RetryQueue,
    SignalLog,
    ThesisVersion,
    TickerRegistry,
)
from axe.exceptions import AXEError, IsolationError
from axe.security.audit import AuditService, audit_action
from axe.security.context import RequestContext
from axe.security.encryption import (
    EncryptedJSON,
    EncryptionError,
    decrypt_ciphertext,
    encrypt_plaintext,
    generate_fernet_key,
    get_fernet,
)
from axe.security.isolation import IsolationService


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

    result = await db_session.execute(select(AuditLog).where(AuditLog.object_id == thesis_id))
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
            await _fund_entity(session)
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

    result = await db_session.execute(select(AuditLog).where(AuditLog.object_id == thesis_id))
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

    result = await db_session.execute(select(AuditLog).where(AuditLog.object_id == sid))
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

    result = await db_session.execute(select(AuditLog).where(AuditLog.object_id == sid))
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.action_type == "signal_ingest"


@pytest.fixture
def app_client():
    """FastAPI TestClient fixture with request-context middleware installed."""
    import asyncio

    from fastapi.testclient import TestClient

    from axe.db.base import Base
    from axe.db.session import async_engine
    from axe.exceptions import AuditError, AuthError, IsolationError
    from axe.main import create_app

    app = create_app(settings=Settings(app_env="test"))

    # Create tables in the DB the global async engine uses.
    async def _create_tables() -> None:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_tables())

    @app.get("/__test/auth_error")
    async def _auth_error():
        raise AuthError("invalid credentials")

    @app.get("/__test/isolation_error")
    async def _isolation_error():
        raise IsolationError("cross-pm access attempt")

    @app.get("/__test/audit_error")
    async def _audit_error():
        raise AuditError("audit log commit failed")

    @app.get("/__test/unknown_error")
    async def _unknown_error():
        raise RuntimeError("something unexpected happened")

    return TestClient(app)


def test_request_context_middleware_injects_identity(app_client: TestClient):
    """Middleware installs RequestContext and echoes the request id."""
    response = app_client.get(
        "/healthz",
        headers={
            "X-PM-ID": "pm_007",
            "X-Fund-ID": "fund_007",
            "X-Request-ID": "trace_42",
        },
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "trace_42"


def test_onboarding_router_blocks_cross_pm(app_client: TestClient):
    """Onboarding endpoints reject a request targeting a different PM."""
    response = app_client.post(
        "/onboarding/start",
        json={"pm_id": "pm_target"},
        headers={"X-PM-ID": "pm_attacker"},
    )
    assert response.status_code == 403


def test_transcripts_router_blocks_cross_pm(app_client: TestClient):
    """Transcript ingestion rejects a payload for a different PM."""
    response = app_client.post(
        "/api/v1/transcripts",
        json={
            "pm_id": "pm_target",
            "ticker": "AAPL",
            "source_type": "polygon",
            "signal_text": "beat EPS",
        },
        headers={"X-PM-ID": "pm_attacker"},
    )
    assert response.status_code == 403


def test_axe_error_to_response_contains_envelope():
    """AXEError.to_response returns request_id, code, message."""
    import json

    err = AXEError("internal detail", request_id="req_123")
    response = err.to_response()
    assert response.status_code == 500
    body = json.loads(response.body.decode("utf-8") if isinstance(response.body, (bytes, bytearray)) else str(response.body))
    assert body == {
        "request_id": "req_123",
        "code": "axe.internal_error",
        "message": "An internal error occurred.",
    }


def test_auth_error_returns_401_and_safe_body(app_client: TestClient):
    """AuthError returns a deterministic JSON envelope without internal details."""
    response = app_client.get("/__test/auth_error")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "auth.failed"
    assert body["message"] == "Authentication failed."
    assert "request_id" in body
    assert "internal" not in body
    assert "traceback" not in str(body).lower()


def test_isolation_error_returns_403_and_is_audited(app_client: TestClient):
    """IsolationError returns 403, safe message, and writes an AuditLog."""
    response = app_client.get(
        "/__test/isolation_error", headers={"X-PM-ID": "pm_victim", "X-Request-ID": "req_iso_1"}
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "isolation.violation"
    assert "isolation" in body["message"].lower()
    assert "request_id" in body
    assert "cross-pm" not in body["message"].lower()
    assert "traceback" not in str(body).lower()


def test_audit_error_returns_500_and_safe_body(app_client: TestClient):
    """AuditError returns a deterministic compliance-flavoured envelope."""
    response = app_client.get("/__test/audit_error")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "audit.failed"
    assert body["message"] == "A compliance logging error occurred."
    assert "request_id" in body
    assert "traceback" not in str(body).lower()


def test_unknown_error_returns_500_generic_code(app_client: TestClient):
    """Unhandled exceptions map to a generic 500 without leaking stack traces."""
    response = app_client.get("/__test/unknown_error")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "axe.internal_error"
    assert body["message"] == "An internal error occurred."
    assert "request_id" in body
    assert "something unexpected" not in str(body)
    assert "RuntimeError" not in str(body)
    assert "traceback" not in str(body).lower()


@pytest.mark.asyncio
async def test_thesis_repo_isolates_pm_reads(db_session: AsyncSession):
    """PM A cannot read PM B's thesis through the repository."""
    from axe.db.uow import UnitOfWork
    from axe.services.thesis import ThesisRepo

    fund = await _fund_entity(db_session)
    pm_a = await _pm_user(db_session, fund.id)
    pm_b = await _pm_user(db_session, fund.id)

    # PM A creates a thesis.
    async with UnitOfWork(db_session) as uow_a:
        repo_a = ThesisRepo(uow_a, pm_a.id, fund.id)
        await repo_a.create_thesis("AAPL", bull_case="A only")

    # PM B's repo sees no thesis for AAPL.
    async with UnitOfWork(db_session) as uow_b:
        repo_b = ThesisRepo(uow_b, pm_b.id, fund.id)
        latest = await repo_b.get_latest_thesis("AAPL")
        assert latest is None
        versions = await repo_b.list_thesis_versions("AAPL")
        assert versions == []


@pytest.mark.asyncio
async def test_isolation_service_scope_for_context_requires_context():
    """select_for raises IsolationError outside a RequestContext."""
    import pytest

    # Ensure no stray context is active.
    token = RequestContext.current_or_none()
    assert token is None
    with pytest.raises(IsolationError, match="No active RequestContext"):
        IsolationService.select_for(ThesisVersion)


@pytest.mark.asyncio
async def test_isolation_service_scope_for_context_uses_current_context():
    """select_for automatically applies pm_id from RequestContext."""
    with RequestContext.bind(pm_id="pm_ctx", fund_id="fund_ctx"):
        stmt = IsolationService.select_for(ThesisVersion)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "pm_id" in compiled
        assert "pm_ctx" in compiled


@pytest.mark.asyncio
async def test_global_tables_are_unscoped():
    """Tables marked isolation_scope='global' return an unmodified select."""
    for model in (FundEntity, CatalystEvent, CorporateAction, DeckTemplate):
        stmt = IsolationService.select_for(model)
        # A global select should not raise even with no context.
        assert IsolationService.isolation_scope(model) == "global"
        assert "WHERE" not in str(stmt).upper()


@pytest.mark.asyncio
async def test_isolation_service_require_isolated_blocks_cross_pm():
    """require_isolated raises when a loaded row belongs to a different PM."""
    with RequestContext.bind(pm_id="pm_a"):
        foreign = ThesisVersion(
            id=str(uuid.uuid4()),
            pm_id="pm_b",
            ticker="AAPL",
            version=1,
            fund_entity_id=str(uuid.uuid4()),
        )
        with pytest.raises(IsolationError, match="Cross-PM isolation violation"):
            IsolationService.require_isolated(foreign)


def test_global_models_have_explicit_marker():
    """Global models declare isolation_scope = 'global'."""
    assert FundEntity.isolation_scope == "global"
    assert CatalystEvent.isolation_scope == "global"
    assert CorporateAction.isolation_scope == "global"
    assert DeckTemplate.isolation_scope == "global"

    # Scoped models default to 'pm'.
    assert ThesisVersion.isolation_scope == "pm"
    assert TickerRegistry.isolation_scope == "pm"
    assert PMUser.isolation_scope == "pm"


@pytest.mark.asyncio
async def test_mnpi_service_flags_and_blocks_high_risk_signal(db_session: AsyncSession):
    """High-MNPI signal flags the signal, blocks alerts, and creates a review."""
    from axe.agents.mnpi_review import MNPIReviewAgent
    from axe.services.mnpi import MNPIService

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    # Force a high MNPI score deterministically.
    agent = MNPIReviewAgent(threshold=0.0)
    service = MNPIService(db_session, agent=agent)

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        source_type="polygon",
        content_hash="abc",
        raw_content="confidential non-public earnings guidance",
    )
    db_session.add(signal)
    await db_session.flush()

    alerts = [{"signal_id": signal.id, "message": "alert"}]
    outcome = await service.review_signal(
        signal_id=signal.id,
        signal_text=signal.raw_content,
        ticker="AAPL",
        pm_id=user.id,
        alert_payloads=alerts,
    )
    await db_session.flush()

    assert outcome.blocked is True
    assert outcome.review is not None
    assert outcome.review.status == "pending"
    assert outcome.review.alert_payloads == alerts

    refreshed = await db_session.get(SignalLog, signal.id)
    assert refreshed is not None
    assert refreshed.mnpi_flag is True

    queue = await db_session.execute(
        select(MNPIReviewQueue).where(MNPIReviewQueue.signal_id == signal.id)
    )
    assert queue.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_mnpi_service_allows_low_risk_signal(db_session: AsyncSession):
    """Non-MNPI signal passes through without blocking."""
    from axe.agents.mnpi_review import MNPIReviewAgent
    from axe.services.mnpi import MNPIService

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    # High threshold ensures benign text is not flagged.
    agent = MNPIReviewAgent(threshold=0.99)
    service = MNPIService(db_session, agent=agent)

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        source_type="polygon",
        content_hash="abc",
        raw_content="The weather is sunny today.",
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = await service.review_signal(
        signal_id=signal.id,
        signal_text=signal.raw_content,
        ticker="AAPL",
        pm_id=user.id,
        alert_payloads=[{"message": "alert"}],
    )
    await db_session.flush()

    assert outcome.blocked is False

    queue = await db_session.execute(
        select(MNPIReviewQueue).where(MNPIReviewQueue.signal_id == signal.id)
    )
    assert queue.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_mnpi_service_approve_releases_alert_and_audits(db_session: AsyncSession):
    """Approving a review un-flags the signal and enqueues a send_alert task."""
    from axe.agents.mnpi_review import MNPIReviewAgent
    from axe.services.mnpi import MNPIService

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    user.slack_user_id = "U123"
    user.email = "pm@example.com"

    agent = MNPIReviewAgent(threshold=0.0)
    service = MNPIService(db_session, agent=agent)

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        source_type="polygon",
        content_hash="abc",
        raw_content="confidential board discussion",
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = await service.review_signal(
        signal_id=signal.id,
        signal_text=signal.raw_content,
        ticker="AAPL",
        pm_id=user.id,
        alert_payloads=[{"signal_id": signal.id, "message": "alert"}],
    )
    await db_session.flush()
    review_id = outcome.review.id if outcome.review else ""

    await service.decide(review_id=review_id, decision="approved", reviewer_id="reviewer_1")
    await db_session.flush()

    review = await db_session.get(MNPIReviewQueue, review_id)
    assert review is not None
    assert review.status == "approved"
    assert review.reviewer_id == "reviewer_1"

    refreshed_signal = await db_session.get(SignalLog, signal.id)
    assert refreshed_signal is not None
    assert refreshed_signal.mnpi_flag is False

    tasks = await db_session.execute(
        select(RetryQueue).where(
            RetryQueue.pm_id == user.id,
            RetryQueue.task_type == "send_alert",
        )
    )
    queued = tasks.scalars().all()
    assert len(queued) == 1
    assert queued[0].payload["slack_user_id"] == "U123"
    assert queued[0].payload["email"] == "pm@example.com"

    audit = await db_session.execute(
        select(AuditLog).where(
            AuditLog.object_id == review_id,
            AuditLog.action_type == "mnpi_review_approved",
        )
    )
    assert audit.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_mnpi_service_reject_keeps_signal_flagged(db_session: AsyncSession):
    """Rejecting a review leaves the signal flagged and writes an audit entry."""
    from axe.agents.mnpi_review import MNPIReviewAgent
    from axe.services.mnpi import MNPIService

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    agent = MNPIReviewAgent(threshold=0.0)
    service = MNPIService(db_session, agent=agent)

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        source_type="polygon",
        content_hash="abc",
        raw_content="confidential merger talks",
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = await service.review_signal(
        signal_id=signal.id,
        signal_text=signal.raw_content,
        ticker="AAPL",
        pm_id=user.id,
        alert_payloads=[{"message": "alert"}],
    )
    await db_session.flush()
    review_id = outcome.review.id if outcome.review else ""

    await service.decide(review_id=review_id, decision="rejected", reviewer_id="reviewer_2")
    await db_session.flush()

    review = await db_session.get(MNPIReviewQueue, review_id)
    assert review is not None
    assert review.status == "rejected"

    refreshed_signal = await db_session.get(SignalLog, signal.id)
    assert refreshed_signal is not None
    assert refreshed_signal.mnpi_flag is True

    tasks = await db_session.execute(
        select(RetryQueue).where(
            RetryQueue.pm_id == user.id,
            RetryQueue.task_type == "send_alert",
        )
    )
    assert tasks.scalars().all() == []

    audit = await db_session.execute(
        select(AuditLog).where(
            AuditLog.object_id == review_id,
            AuditLog.action_type == "mnpi_review_rejected",
        )
    )
    assert audit.scalar_one_or_none() is not None


def test_mnpi_review_endpoint_exists(app_client: TestClient):
    """The MNPI decision endpoint is registered and returns expected errors."""
    response = app_client.post(
        "/api/v1/mnpi/nonexistent/decision",
        json={"decision": "approved", "reviewer_id": "reviewer_x"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_process_transcript_handler_blocks_mnpi_signal(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_transcript_handler does not enqueue alerts for high-MNPI signals."""
    from axe.agents.drift_detect import EarningsAlertService
    from axe.ingestion import handlers
    from axe.ingestion.handlers import process_transcript_handler

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    class _FakeEarningsAlertService:
        ALERT_SLA_SECONDS = EarningsAlertService.ALERT_SLA_SECONDS

        def __init__(self, uow) -> None:
            self.session = uow.session

        async def process_signal(self, **kwargs):
            signal_id = str(uuid.uuid4())
            signal = SignalLog(
                id=signal_id,
                pm_id=kwargs.get("pm_id"),
                ticker=kwargs.get("ticker"),
                source_type=kwargs.get("source_type"),
                raw_content=kwargs.get("signal_text", ""),
                content_hash=kwargs.get("content_hash", ""),
            )
            self.session.add(signal)
            await self.session.flush()
            return [{"signal_id": signal_id, "ticker": "AAPL", "message": "alert"}]

    monkeypatch.setattr(handlers, "EarningsAlertService", _FakeEarningsAlertService)
    monkeypatch.setenv("MNPI_THRESHOLD", "0.0")

    payload = {
        "pm_id": user.id,
        "ticker": "AAPL",
        "source_type": "polygon",
        "signal_text": "confidential non-public board discussion",
        "content_hash": "mnpi_hash",
    }

    processed = await process_transcript_handler(db_session, payload)
    assert processed is True

    tasks = await db_session.execute(
        select(RetryQueue).where(
            RetryQueue.pm_id == user.id,
            RetryQueue.task_type == "send_alert",
        )
    )
    assert tasks.scalars().all() == []

    reviews = await db_session.execute(
        select(MNPIReviewQueue).where(MNPIReviewQueue.pm_id == user.id)
    )
    review = reviews.scalar_one_or_none()
    assert review is not None
    assert review.status == "pending"


@pytest.mark.asyncio
async def test_process_transcript_handler_allows_clean_signal(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_transcript_handler enqueues alerts for non-MNPI signals."""
    from axe.agents.drift_detect import EarningsAlertService
    from axe.ingestion import handlers
    from axe.ingestion.handlers import process_transcript_handler

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    class _FakeEarningsAlertService:
        ALERT_SLA_SECONDS = EarningsAlertService.ALERT_SLA_SECONDS

        def __init__(self, uow) -> None:
            self.session = uow.session

        async def process_signal(self, **kwargs):
            signal_id = str(uuid.uuid4())
            signal = SignalLog(
                id=signal_id,
                pm_id=kwargs.get("pm_id"),
                ticker=kwargs.get("ticker"),
                source_type=kwargs.get("source_type"),
                raw_content=kwargs.get("signal_text", ""),
                content_hash=kwargs.get("content_hash", ""),
            )
            self.session.add(signal)
            await self.session.flush()
            return [{"signal_id": signal_id, "ticker": "AAPL", "message": "alert"}]

    monkeypatch.setattr(handlers, "EarningsAlertService", _FakeEarningsAlertService)
    monkeypatch.setenv("MNPI_THRESHOLD", "0.99")

    payload = {
        "pm_id": user.id,
        "ticker": "AAPL",
        "source_type": "polygon",
        "signal_text": "The company announced a new product color.",
        "content_hash": "clean_hash",
    }

    processed = await process_transcript_handler(db_session, payload)
    assert processed is True

    tasks = await db_session.execute(
        select(RetryQueue).where(
            RetryQueue.pm_id == user.id,
            RetryQueue.task_type == "send_alert",
        )
    )
    assert len(tasks.scalars().all()) == 1

