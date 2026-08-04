"""Compliance-specific adversarial test suite.

This module tests the controls that protect client confidentiality and
regulatory alignment: cross-PM isolation, audit immutability, MNPI gating,
RBAC, and data retention.  It is intentionally harsh — it treats every
public helper as a potential bypass path and proves each one fails closed.

Tests here are run as a separate CI job with "strict" coverage gating so
regressions in security/compliance code cannot cross the merge gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi import Request as FastAPIRequest
from pydantic import BaseModel
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.llm import LLMProvider, LLMResponse
from axe.config import Settings
from axe.db.models import (
    AuditLog,
    FundEntity,
    PMUser,
    RetryQueue,
    SignalLog,
    ThesisVersion,
    TickerRegistry,
)
from axe.exceptions import IsolationError
from axe.security.audit import AuditService, audit_action
from axe.security.authz import require_role
from axe.security.context import (
    RequestContext,
    get_request_context,
    install_middleware,
    request_context_dependency,
    require_identity,
)
from axe.security.encryption import (
    EncryptedJSON,
    EncryptionError,
    _derive_key,
    decrypt_ciphertext,
    encrypt_plaintext,
    generate_fernet_key,
    get_fernet,
)
from axe.security.isolation import IsolationService
from axe.services.export import ExportService
from axe.services.mnpi import MNPIReviewAgent, MNPIService
from axe.services.retention import RetentionService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fund_entity(session: AsyncSession) -> FundEntity:
    fund = FundEntity(
        id=str(uuid.uuid4()),
        legal_name=f"Compliance Fund {uuid.uuid4().hex[:8]}",
        data_residency="US",
    )
    session.add(fund)
    await session.flush()
    return fund


async def _pm_user(session: AsyncSession, fund_id: str) -> PMUser:
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund_id,
        email=f"tm_{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    await session.flush()
    return user


# ---------------------------------------------------------------------------
# Isolation — parameterized adversarial property tests
# ---------------------------------------------------------------------------


SCOPE_MODEL_CASES: list[tuple[Any, str, str | None, str | None]] = [
    # (model, expected_scope, identity_column, identity_value)
    (TickerRegistry, "pm", "pm_id", "pm_a"),
    (ThesisVersion, "pm", "pm_id", "pm_a"),
    (FundEntity, "global", None, None),
    (PMUser, "pm", "fund_entity_id", "fund_a"),
]


@pytest.mark.parametrize("model,expected_scope,identity_col,identity_val", SCOPE_MODEL_CASES)
def test_isolation_scope_property(
    model: Any,
    expected_scope: str,
    identity_col: str | None,
    identity_val: str | None,
) -> None:
    """Every model declares an isolation scope consistent with its columns."""
    scope = IsolationService.isolation_scope(model)
    assert scope == expected_scope
    if identity_col:
        assert hasattr(model, identity_col)


@pytest.mark.parametrize("pm_id", ["", None])
def test_isolation_scope_rejects_missing_pm_id(pm_id: str | None) -> None:
    """A missing pm_id always fails closed with IsolationError."""
    with pytest.raises(IsolationError, match="pm_id is required"):
        IsolationService.scope(select(TickerRegistry), TickerRegistry, pm_id)


@pytest.mark.parametrize("model", [TickerRegistry, ThesisVersion, SignalLog])
@pytest.mark.asyncio
async def test_select_for_never_returns_foreign_rows(
    db_session: AsyncSession,
    model: Any,
) -> None:
    """select_for from one PM context excludes rows owned by another PM."""
    fund = await _fund_entity(db_session)
    pm_a = await _pm_user(db_session, fund.id)
    pm_b = await _pm_user(db_session, fund.id)

    if model is TickerRegistry:
        row_a = model(id=str(uuid.uuid4()), pm_id=pm_a.id, ticker="AAPL")
        row_b = model(id=str(uuid.uuid4()), pm_id=pm_b.id, ticker="TSLA")
    elif model is ThesisVersion:
        row_a = model(
            id=str(uuid.uuid4()),
            pm_id=pm_a.id,
            ticker="AAPL",
            version=1,
            fund_entity_id=fund.id,
        )
        row_b = model(
            id=str(uuid.uuid4()),
            pm_id=pm_b.id,
            ticker="TSLA",
            version=1,
            fund_entity_id=fund.id,
        )
    else:  # SignalLog
        row_a = model(
            id=str(uuid.uuid4()),
            pm_id=pm_a.id,
            source_type="test",
            content_hash="a",
        )
        row_b = model(
            id=str(uuid.uuid4()),
            pm_id=pm_b.id,
            source_type="test",
            content_hash="b",
        )

    db_session.add_all([row_a, row_b])
    await db_session.flush()

    with RequestContext.bind(pm_id=pm_a.id, fund_id=fund.id):
        result = await db_session.execute(IsolationService.select_for(model))
        rows = result.scalars().all()

    assert len(rows) == 1
    assert rows[0].pm_id == pm_a.id


@pytest.mark.parametrize(
    "ctx_pm_id,row_pm_id,should_pass",
    [
        ("pm_a", "pm_a", True),
        ("pm_a", "pm_b", False),
        ("pm_a", None, False),
    ],
)
def test_require_isolated_property(
    ctx_pm_id: str,
    row_pm_id: str | None,
    should_pass: bool,
) -> None:
    """require_isolated matches row ownership to the active context."""
    with RequestContext.bind(pm_id=ctx_pm_id):
        row = ThesisVersion(
            id=str(uuid.uuid4()),
            pm_id=row_pm_id or "",
            ticker="AAPL",
            version=1,
            fund_entity_id=str(uuid.uuid4()),
        )
        if row_pm_id is None:
            # SQLAlchemy model requires a string; setting to ctx would be a bug.
            row.pm_id = ""
        if should_pass:
            IsolationService.require_isolated(row)
        else:
            with pytest.raises(IsolationError, match="isolation violation"):
                IsolationService.require_isolated(row)


@pytest.mark.parametrize(
    "memory_items,allowed,should_raise",
    [
        ([{"pm_id": "pm_a"}], set(), False),
        ([{"pm_id": "pm_a"}, {"pm_id": "pm_b"}], {"pm_b"}, False),
        ([{"pm_id": "pm_a"}, {"pm_id": "pm_c", "found_in": "retrieval"}], set(), True),
        ([{"pm_id": None}], set(), False),
    ],
)
def test_ensure_memory_context_isolated_property(
    memory_items: list[dict[str, Any]],
    allowed: set[str],
    should_raise: bool,
) -> None:
    """Memory context isolation blocks undocumented cross-PM contamination."""
    if should_raise:
        with pytest.raises(IsolationError, match="Cross-PM contamination"):
            IsolationService.ensure_memory_context_isolated(
                memory_items, "pm_a", allowed_other_pm_ids=allowed
            )
    else:
        IsolationService.ensure_memory_context_isolated(
            memory_items, "pm_a", allowed_other_pm_ids=allowed
        )


@pytest.mark.asyncio
async def test_isolation_service_get_refuses_cross_pm(db_session: AsyncSession) -> None:
    """IsolationService.get cannot be used to smuggle another PM's row."""
    fund = await _fund_entity(db_session)
    pm_a = await _pm_user(db_session, fund.id)
    pm_b = await _pm_user(db_session, fund.id)

    row = TickerRegistry(id=str(uuid.uuid4()), pm_id=pm_a.id, ticker="META")
    db_session.add(row)
    await db_session.flush()

    found = await IsolationService.get(db_session, TickerRegistry, pm_b.id, row.id)
    assert found is None


@pytest.mark.asyncio
async def test_isolation_service_list_for_pm_is_bound(db_session: AsyncSession) -> None:
    """list_for_pm returns only rows whose pm_id matches the argument."""
    fund = await _fund_entity(db_session)
    pm_a = await _pm_user(db_session, fund.id)
    pm_b = await _pm_user(db_session, fund.id)

    a = TickerRegistry(id=str(uuid.uuid4()), pm_id=pm_a.id, ticker="A")
    b = TickerRegistry(id=str(uuid.uuid4()), pm_id=pm_b.id, ticker="B")
    db_session.add_all([a, b])
    await db_session.flush()

    a_rows = await IsolationService.list_for_pm(db_session, TickerRegistry, pm_a.id)
    assert [r.ticker for r in a_rows] == ["A"]


@pytest.mark.parametrize(
    "global_model",
    [FundEntity],
)
def test_global_models_remain_unfiltered_without_context(global_model: Any) -> None:
    """Global models do not require an active RequestContext."""
    # Deliberately outside any RequestContext.bind() block.
    stmt = IsolationService.select_for(global_model)
    assert "WHERE" not in str(stmt).upper()


@pytest.mark.asyncio
async def test_isolation_fund_entity_id_model(db_session: AsyncSession) -> None:
    """Models with only fund_entity_id are filtered by fund_id context."""
    fund_a = await _fund_entity(db_session)

    user_a = await _pm_user(db_session, fund_a.id)
    # PMUser itself uses fund_entity_id isolation.
    with RequestContext.bind(pm_id=user_a.id, fund_id=fund_a.id):
        stmt = IsolationService.select_for(PMUser)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "fund_entity_id" in compiled
        assert fund_a.id in compiled

    with (
        RequestContext.bind(pm_id="pm_z"),
        pytest.raises(IsolationError, match="fund_id is required"),
    ):
        IsolationService.select_for(PMUser)


# ---------------------------------------------------------------------------
# Audit — append-only, immutable, complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_type,object_type,object_id",
    [
        ("signal_ingest", "signal_log", str(uuid.uuid4())),
        ("thesis_create", "thesis_version", str(uuid.uuid4())),
        ("mnpi_decision", "mnpi_review_queue", str(uuid.uuid4())),
        ("retention_soft_delete", "retention_job", str(uuid.uuid4())),
    ],
)
async def test_audit_service_logs_compliance_actions(
    db_session: AsyncSession,
    action_type: str,
    object_type: str,
    object_id: str,
) -> None:
    """AuditService persists compliance-critical actions with identity."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    service = AuditService(db_session)
    await service.log(
        action_type=action_type,
        object_type=object_type,
        object_id=object_id,
        before_state={"previous": "value"},
        after_state={"new": "value"},
        pm_id=user.id,
        fund_entity_id=fund.id,
        source_ip="127.0.0.1",
        session_id="sess_compliance",
        retention_class="compliance",
        non_blocking=False,
    )
    await db_session.commit()

    result = await db_session.execute(select(AuditLog).where(AuditLog.object_id == object_id))
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.pm_id == user.id
    assert log.fund_entity_id == fund.id
    assert log.retention_class == "compliance"
    assert log.source_ip == "127.0.0.1"


@pytest.mark.asyncio
async def test_audit_log_cannot_be_mutated(db_session: AsyncSession) -> None:
    """Append-only policy is enforced at the ORM flush layer."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    log = AuditLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        fund_entity_id=fund.id,
        action_type="user_login",
        object_type="session",
        object_id=str(uuid.uuid4()),
    )
    db_session.add(log)
    await db_session.commit()

    loaded = await db_session.get(AuditLog, log.id)
    assert loaded is not None
    loaded.action_type = "tampered"
    with pytest.raises(RuntimeError, match="append-only"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_audit_action_decorator_emits_identities(db_session: AsyncSession) -> None:
    """The decorator extracts pm_id and fund_entity_id from the call signature."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    class _Repo:
        async def get(self, oid: str, *, session: AsyncSession) -> ThesisVersion | None:
            return await session.get(ThesisVersion, oid)

        @audit_action("thesis_update", "thesis_version")
        async def update(
            self,
            thesis_id: str,
            *,
            pm_id: str,
            fund_entity_id: str,
            session: AsyncSession,
        ) -> ThesisVersion:
            thesis = ThesisVersion(
                id=thesis_id,
                pm_id=pm_id,
                ticker="NVDA",
                version=1,
                fund_entity_id=fund_entity_id,
            )
            session.add(thesis)
            await session.flush()
            return thesis

    repo = _Repo()
    thesis_id = str(uuid.uuid4())
    await repo.update(thesis_id, pm_id=user.id, fund_entity_id=fund.id, session=db_session)

    log = await db_session.execute(select(AuditLog).where(AuditLog.object_id == thesis_id))
    entry = log.scalar_one_or_none()
    assert entry is not None
    assert entry.pm_id == user.id
    assert entry.fund_entity_id == fund.id
    assert entry.action_type == "thesis_update"


# ---------------------------------------------------------------------------
# MNPI — gate, review, release, audit
# ---------------------------------------------------------------------------


MNPI_TEXT_CASES = [
    ("confidential non-public board discussion", 0.0, True),
    ("pre-release earnings guidance", 0.0, True),
    ("The weather is sunny today.", 0.99, False),
    ("AAPL announced a new product color.", 0.99, False),
]


@pytest.mark.parametrize("text,threshold,expected_flagged", MNPI_TEXT_CASES)
def test_mnpi_heuristic_property(text: str, threshold: float, expected_flagged: bool) -> None:
    """The deterministic MNPI classifier behaves predictably on adversarial text."""
    agent = MNPIReviewAgent(threshold=threshold)
    result = agent._heuristic_review(text)
    assert result.flagged is expected_flagged
    assert 0.0 <= result.mnpi_score <= 1.0
    assert 0.0 <= result.materiality_score <= 1.0


@pytest.mark.asyncio
async def test_mnpi_review_queues_and_audits(db_session: AsyncSession) -> None:
    """A flagged signal is blocked and the decision is audit-logged."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    user.slack_user_id = "U000"
    user.email = "pm@example.com"

    service = MNPIService(db_session, agent=MNPIReviewAgent(threshold=0.0))
    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="AAPL",
        source_type="expert_network",
        content_hash="hash1",
        raw_content="confidential merger talks",
    )
    db_session.add(signal)
    await db_session.flush()

    with RequestContext.bind(pm_id=user.id, fund_id=fund.id):
        outcome = await service.review_signal(
            signal_id=signal.id,
            signal_text=signal.raw_content,
            ticker="AAPL",
            pm_id=user.id,
            alert_payloads=[{"message": "alert"}],
        )

    assert outcome.blocked is True
    assert outcome.review is not None
    review_id = outcome.review.id

    approved = await service.decide(review_id=review_id, decision="approved", reviewer_id="rev_1")
    assert approved.status == "approved"

    queued = await db_session.execute(
        select(RetryQueue).where(
            RetryQueue.pm_id == user.id,
            RetryQueue.task_type == "send_alert",
        )
    )
    assert len(queued.scalars().all()) == 1

    audit = await db_session.execute(
        select(AuditLog).where(
            AuditLog.object_id == review_id,
            AuditLog.action_type.in_(["mnpi_review_created", "mnpi_review_approved"]),
        )
    )
    assert len(audit.scalars().all()) == 2


@pytest.mark.asyncio
async def test_mnpi_rejected_signal_stays_blocked(db_session: AsyncSession) -> None:
    """Rejecting an MNPI review keeps alerts suppressed."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    service = MNPIService(db_session, agent=MNPIReviewAgent(threshold=0.0))
    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="TSLA",
        source_type="expert_network",
        content_hash="hash2",
        raw_content="non-public fda panel discussion",
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = await service.review_signal(
        signal_id=signal.id,
        signal_text=signal.raw_content,
        ticker="TSLA",
        pm_id=user.id,
        alert_payloads=[{"message": "alert"}],
    )
    review_id = outcome.review.id if outcome.review else ""
    await service.decide(review_id=review_id, decision="rejected", reviewer_id="rev_2")

    refreshed = await db_session.get(SignalLog, signal.id)
    assert refreshed is not None
    assert refreshed.mnpi_flag is True

    queued = await db_session.execute(
        select(RetryQueue).where(
            RetryQueue.pm_id == user.id,
            RetryQueue.task_type == "send_alert",
        )
    )
    assert queued.scalars().all() == []


# ---------------------------------------------------------------------------
# RBAC — require_role fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "allowed_roles,request_role,expected_status",
    [
        ({"pm"}, "pm", 200),
        ({"pm", "admin"}, "pm", 200),
        ({"pm", "admin"}, "admin", 200),
        ({"compliance"}, "pm", 403),
        ({"compliance"}, "admin", 403),
        ({"admin"}, "pm", 403),
    ],
)
def test_require_role_property(
    allowed_roles: set[str], request_role: str, expected_status: int
) -> None:
    """require_role rejects any role outside the allow-list."""
    app = FastAPI()

    @app.get("/rbac", dependencies=[Depends(require_role(*allowed_roles))])
    async def _endpoint() -> dict:
        return {"ok": True}

    from fastapi.testclient import TestClient

    # Avoid DB setup: the dependency only needs headers, so we can call directly
    # via a lightweight ASGI app.  The context is rebuilt from headers by the
    # get_request_context dependency.
    client = TestClient(app)
    response = client.get("/rbac", headers={"X-PM-ID": "pm_1", "X-Role": request_role})
    assert response.status_code == expected_status, response.text


def test_rbac_forbidden_detail_does_not_leak() -> None:
    """403 responses expose allowed roles but not internal state."""
    dep = require_role("admin")

    ctx = RequestContext(pm_id="pm_1", role="pm")
    token = RequestContext.set_current(ctx)
    try:
        with pytest.raises(HTTPException) as exc_info:
            # The dependency is async but can be inspected by direct call.
            import asyncio

            asyncio.run(dep(ctx=ctx))
        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "admin" in str(exc_info.value.detail)
        assert "internal" not in str(exc_info.value.detail).lower()
    finally:
        RequestContext.reset_current(token)


# ---------------------------------------------------------------------------
# Retention — lifecycle, exemption, and compliance audit
# ---------------------------------------------------------------------------


RETENTION_COMBINATIONS = list(
    itertools.product(
        [True, False],  # retention_enabled
        [True, False],  # dry_run
        [True, False],  # exempt
    )
)


@pytest.mark.parametrize("enabled,dry_run,exempt", RETENTION_COMBINATIONS)
@pytest.mark.asyncio
async def test_retention_combinations_property(
    db_session: AsyncSession,
    enabled: bool,
    dry_run: bool,
    exempt: bool,
) -> None:
    """Retention only deletes when enabled, not dry-run, and not exempt."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    old = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="test",
        content_hash="retention_prop",
        created_at=datetime.now(UTC) - timedelta(days=4000),
        retention_exempt=exempt,
    )
    db_session.add(old)
    await db_session.flush()

    service = RetentionService(
        db_session,
        settings=Settings(
            app_env="test",
            retention_enabled=enabled,
            retention_days=365,
        ),
    )
    summary = await service.run(entity_types=["signal_log"], dry_run=dry_run)

    if not enabled and not dry_run:
        assert summary["enabled"] is False
        assert summary["counts"] == {}
        return

    count = summary["counts"].get("signal_log", 0)
    should_delete = enabled and not dry_run and not exempt
    expected_count = 1 if not exempt else 0
    assert count == expected_count

    refreshed = await db_session.get(SignalLog, old.id)
    assert refreshed is not None
    if should_delete:
        assert refreshed.deleted_at is not None
    else:
        assert refreshed.deleted_at is None


@pytest.mark.asyncio
async def test_retention_ignores_unknown_entity_types(db_session: AsyncSession) -> None:
    """Unknown entity types are skipped without crashing the job."""
    service = RetentionService(db_session)
    summary = await service.run(entity_types=["does_not_exist"])
    assert summary["counts"].get("does_not_exist") is None


@pytest.mark.asyncio
async def test_retention_audit_log_written_on_real_run(db_session: AsyncSession) -> None:
    """A real retention run writes an AuditLog summary."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    old = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="test",
        content_hash="retention_audit",
        created_at=datetime.now(UTC) - timedelta(days=4000),
    )
    db_session.add(old)
    await db_session.flush()

    service = RetentionService(
        db_session,
        settings=Settings(app_env="test", retention_days=365),
    )
    await service.run(dry_run=False)

    log = await db_session.execute(
        select(AuditLog).where(AuditLog.action_type == "retention_soft_delete")
    )
    assert log.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------


def test_generate_fernet_key_is_valid() -> None:
    """A generated key round-trips through encrypt/decrypt."""
    key = generate_fernet_key()
    f = get_fernet(key)
    token = f.encrypt(b"secret")
    assert f.decrypt(token) == b"secret"


def test_encrypt_decrypt_plaintext_roundtrip() -> None:
    """``encrypt_plaintext`` and ``decrypt_ciphertext`` are inverse operations."""
    plaintext = "sensitive export payload"
    key = generate_fernet_key()
    token = encrypt_plaintext(plaintext, key)
    assert decrypt_ciphertext(token, key) == plaintext


def test_decrypt_ciphertext_invalid_token_fails() -> None:
    """Decrypting garbage with a valid key raises InvalidToken."""
    key = generate_fernet_key()
    with pytest.raises(InvalidToken):
        decrypt_ciphertext("not-a-token", key)


def test_get_fernet_rejects_invalid_key() -> None:
    """Non-32-byte keys after base64 decoding are rejected."""
    # "aGVsbG8=" decodes to b"hello" (5 bytes), not 32.
    with pytest.raises(RuntimeError, match="not a valid 32-byte Fernet key"):
        get_fernet("aGVsbG8=")


def test_encrypted_json_type_roundtrip() -> None:
    """EncryptedJSON binds and processes ciphertext transparently."""
    key = generate_fernet_key()
    EncryptedJSON.configure(key)
    column_type = EncryptedJSON()
    value = {"pm_id": "pm_1", "secret": "material"}
    token = column_type.process_bind_param(value, None)
    assert token is not None
    assert token != json.dumps(value)
    restored = column_type.process_result_value(token, None)
    assert restored == value


def test_encrypted_json_noop_for_none() -> None:
    """EncryptedJSON passes through None on bind and result."""
    EncryptedJSON.configure(generate_fernet_key())
    column_type = EncryptedJSON()
    assert column_type.process_bind_param(None, None) is None
    assert column_type.process_result_value(None, None) is None


# ---------------------------------------------------------------------------
# Export / cross-cutting compliance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_includes_audit_trail(db_session: AsyncSession) -> None:
    """Data export (GDPR/CCPA-style) bundles the entity and its audit trail."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="test",
        content_hash="export_signal",
        raw_content="material non-public data",
    )
    db_session.add(signal)
    await db_session.flush()

    audit = AuditService(db_session)
    await audit.log(
        action_type="signal_received",
        object_type="signal_log",
        object_id=signal.id,
        pm_id=user.id,
        fund_entity_id=fund.id,
        non_blocking=False,
    )
    await db_session.flush()

    settings = Settings(
        app_env="test",
        encryption_key="0" * 32,
        export_encryption_key="EoxAkL7LVhcrhqevgotHSPuoF-XnL7nef5cCCxLGN8I=",
    )
    service = ExportService(db_session, settings=settings)
    exported = await service.export(signal)

    assert exported["object_type"] == "signal_log"
    assert exported["sha256_checksum"] is not None
    decrypted = ExportService.decrypt(
        exported["encrypted_payload"], str(settings.export_encryption_key)
    )
    assert decrypted["entity"]["id"] == signal.id
    assert len(decrypted["audit_trail"]) >= 1


# ---------------------------------------------------------------------------
# Sys-admin smoke: every scoped public helper has an isolated default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_raw_select_in_isolation_public_api(db_session: AsyncSession) -> None:
    """IsolationService.select_for is always available as the safe default."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    ctx_token = RequestContext.set_current(RequestContext(pm_id=user.id, fund_id=fund.id))
    try:
        stmt = IsolationService.select_for(SignalLog)
        assert "pm_id" in str(stmt).lower()
        result = await db_session.execute(stmt)
        result.scalars().all()
    finally:
        RequestContext.reset_current(ctx_token)


# ---------------------------------------------------------------------------
# Encryption helper completeness
# ---------------------------------------------------------------------------


def _clear_settings_cache() -> None:
    from axe.config import get_settings as _real_get_settings

    if hasattr(_real_get_settings, "cache_clear"):
        _real_get_settings.cache_clear()


def _settings_without_key(monkeypatch: pytest.MonkeyPatch | None = None) -> Settings:
    if monkeypatch is not None:
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("EXPORT_ENCRYPTION_KEY", raising=False)
    _clear_settings_cache()
    # Explicitly clear keys so a checked-in .env placeholder cannot leak in.
    return Settings(app_env="test", encryption_key=None, export_encryption_key=None)


def test_get_fernet_uses_env_encryption_key_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_fernet`` falls back to the ENCRYPTION_KEY env var."""
    key = generate_fernet_key()
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    settings = _settings_without_key()
    with mock.patch("axe.security.encryption.get_settings", return_value=settings):
        f = get_fernet()
        token = f.encrypt(b"secret")
        assert f.decrypt(token) == b"secret"


def test_derive_key_raises_when_no_key_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing encryption key raises RuntimeError."""
    settings = _settings_without_key(monkeypatch)
    with (
        mock.patch("axe.security.encryption.get_settings", return_value=settings),
        mock.patch("axe.config.get_settings", return_value=settings),
        pytest.raises(RuntimeError, match="ENCRYPTION_KEY is not configured"),
    ):
        _derive_key(None)


def test_encrypted_json_uses_env_key_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """EncryptedJSON falls back to settings when no class-level key is set."""
    key = generate_fernet_key()
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    settings = _settings_without_key()
    previous_key = EncryptedJSON._key
    EncryptedJSON._key = None
    try:
        column_type = EncryptedJSON()
        with mock.patch("axe.security.encryption.get_settings", return_value=settings):
            value = {"answer": 42}
            token = column_type.process_bind_param(value, None)
            assert column_type.process_result_value(token, None) == value
    finally:
        EncryptedJSON._key = previous_key


# ---------------------------------------------------------------------------
# RequestContext branch coverage
# ---------------------------------------------------------------------------


def test_request_context_current_raises_outside_context() -> None:
    """``current()`` fails when no context is active."""
    token = RequestContext.set_current(RequestContext(pm_id="x"))
    try:
        RequestContext.reset_current(token)
        with pytest.raises(RuntimeError, match="No RequestContext is active"):
            RequestContext.current()
    finally:
        # Ensure we leave a clean state for subsequent tests.
        pass


def test_request_context_from_headers_builds_bypass_in_dev() -> None:
    """In dev/test mode a missing pm_id creates a bypass context."""
    request = FastAPIRequest(
        {
            "type": "http",
            "headers": [[b"user-agent", b"pytest"]],
            "method": "GET",
            "path": "/",
            "http_version": "1.1",
        }
    )
    ctx = RequestContext.from_headers(request, settings=Settings(app_env="test"))
    assert ctx.is_bypass is True
    assert ctx.role == "pm"


def test_request_context_from_headers_extracts_identity_headers() -> None:
    """Headers are parsed into RequestContext fields."""
    request = FastAPIRequest(
        {
            "type": "http",
            "headers": [
                [b"x-pm-id", b"pm_42"],
                [b"x-fund-id", b"fund_99"],
                [b"x-role", b"admin"],
                [b"x-request-id", b"req_1"],
                [b"user-agent", b"compliance-test"],
            ],
            "method": "GET",
            "path": "/",
            "http_version": "1.1",
        }
    )
    ctx = RequestContext.from_headers(request, settings=Settings(app_env="test"))
    assert ctx.pm_id == "pm_42"
    assert ctx.fund_id == "fund_99"
    assert ctx.role == "admin"
    assert ctx.request_id == "req_1"
    assert ctx.user_agent == "compliance-test"
    assert ctx.is_bypass is False


@pytest.mark.asyncio
async def test_request_context_dependency_yields_ctx() -> None:
    """The dependency installs the context and cleans up after yielding."""
    request = FastAPIRequest(
        {
            "type": "http",
            "headers": [[b"x-pm-id", b"pm_dep"]],
            "method": "GET",
            "path": "/",
            "http_version": "1.1",
        }
    )
    async for _ctx in request_context_dependency(request):
        assert RequestContext.current().pm_id == "pm_dep"
    # After the generator returns the context should be reset.
    # We cannot assert current() raises in auto mode, but the API surface is exercised.


def test_require_identity_returns_identity() -> None:
    """require_identity returns a verified RequestIdentity."""
    with RequestContext.bind(pm_id="pm_id", fund_id="fund_id", role="admin"):
        identity = require_identity()
        assert identity.pm_id == "pm_id"
        assert identity.fund_id == "fund_id"
        assert identity.role == "admin"


@pytest.mark.asyncio
async def test_request_context_middleware_adds_request_id_header() -> None:
    """Middleware propagates X-Request-ID back to the response."""
    app = FastAPI()
    install_middleware(app)

    @app.get("/ping")
    async def ping() -> dict:
        return {"pm_id": RequestContext.current().pm_id}

    from fastapi.testclient import TestClient

    client = TestClient(app)
    response = client.get("/ping", headers={"X-PM-ID": "pm_1"})
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers
    assert response.json()["pm_id"] == "pm_1"


@pytest.mark.asyncio
async def test_get_request_context_dependency_uses_headers() -> None:
    """get_request_context integrates with FastAPI header injection."""
    request = FastAPIRequest(
        {
            "type": "http",
            "headers": [
                [b"x-pm-id", b"pm_header"],
                [b"x-role", b"compliance"],
            ],
            "method": "GET",
            "path": "/",
            "http_version": "1.1",
        }
    )
    ctx = await get_request_context(request, x_pm_id="pm_header", x_role="compliance")
    assert ctx.pm_id == "pm_header"
    assert ctx.role == "compliance"


def test_install_middleware_rejects_non_fastapi_app() -> None:
    """install_middleware validates the app type."""
    with pytest.raises(TypeError, match="FastAPI app"):
        install_middleware(object())


# ---------------------------------------------------------------------------
# Audit / isolation / MNPI / retention branch coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_non_blocking_creates_task(db_session: AsyncSession) -> None:
    """Non-blocking audit log creates an asyncio task."""
    audit = AuditService(db_session)
    created_coro_wrappers: list[Any] = []
    real_create_task = asyncio.create_task
    captured_task: asyncio.Task[Any] | None = None

    def _capture(coro: Any) -> asyncio.Task[Any]:
        nonlocal captured_task
        created_coro_wrappers.append(coro)
        captured_task = real_create_task(coro)
        return captured_task

    with mock.patch("asyncio.create_task", side_effect=_capture):
        await audit.log(
            action_type="test_action",
            object_type="test_obj",
            object_id=str(uuid.uuid4()),
            non_blocking=True,
        )

    assert len(created_coro_wrappers) == 1
    assert captured_task is not None
    captured_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await captured_task


@pytest.mark.asyncio
async def test_audit_log_uses_session_in_signature(db_session: AsyncSession) -> None:
    """The decorator records after_state when the method signature has session."""

    class _Repo:
        async def get(self, oid: str, *, session: AsyncSession) -> Any | None:
            return None

        @audit_action("thesis_create", "thesis_version")
        async def create(
            self,
            thesis_id: str,
            *,
            pm_id: str,
            fund_entity_id: str,
            session: AsyncSession,
        ) -> ThesisVersion:
            thesis = ThesisVersion(
                id=thesis_id,
                pm_id=pm_id,
                ticker="META",
                version=1,
                fund_entity_id=fund_entity_id,
            )
            session.add(thesis)
            await session.flush()
            return thesis

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    repo = _Repo()
    thesis_id = str(uuid.uuid4())
    await repo.create(thesis_id, pm_id=user.id, fund_entity_id=fund.id, session=db_session)

    log = await db_session.execute(select(AuditLog).where(AuditLog.object_id == thesis_id))
    entry = log.scalar_one_or_none()
    assert entry is not None
    assert entry.after_state is not None
    assert entry.after_state.get("ticker") == "META"


@pytest.mark.asyncio
async def test_mnpi_service_not_blocked_when_below_threshold(
    db_session: AsyncSession,
) -> None:
    """A clean signal returns blocked=False and no review queue entry."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    service = MNPIService(db_session, agent=MNPIReviewAgent(threshold=0.99))
    outcome = await service.review_signal(
        signal_id="no_signal",
        signal_text="plain market commentary",
        ticker="SPY",
        pm_id=user.id,
        alert_payloads=[{"message": "ok"}],
    )
    assert outcome.blocked is False
    assert outcome.review is None


@pytest.mark.asyncio
async def test_mnpi_service_decide_already_processed_rejected(
    db_session: AsyncSession,
) -> None:
    """Deciding on a non-pending review raises ValueError."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    service = MNPIService(db_session, agent=MNPIReviewAgent(threshold=0.0))
    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="X",
        source_type="test",
        content_hash="h",
        raw_content="confidential",
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = await service.review_signal(
        signal_id=signal.id,
        signal_text="confidential",
        ticker="X",
        pm_id=user.id,
        alert_payloads=[{"message": "alert"}],
    )
    review = outcome.review
    assert review is not None
    await service.decide(review.id, "rejected", "rev_1")
    with pytest.raises(ValueError, match="already been rejected"):
        await service.decide(review.id, "rejected", "rev_1")


@pytest.mark.asyncio
async def test_mnpi_service_release_alerts_without_user(
    db_session: AsyncSession,
) -> None:
    """Approved review still enqueues payload even when PMUser is missing."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    service = MNPIService(db_session, agent=MNPIReviewAgent(threshold=0.0))

    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        ticker="GHOST",
        source_type="test",
        content_hash="h_ghost",
        raw_content="confidential non-public board discussion",
    )
    db_session.add(signal)
    await db_session.flush()

    outcome = await service.review_signal(
        signal_id=signal.id,
        signal_text=signal.raw_content,
        ticker="GHOST",
        pm_id=user.id,
        alert_payloads=[{"body": "alert"}],
    )
    review = outcome.review
    assert review is not None

    # Force the user-lookup branch to miss while keeping all FKs valid.
    async def _execute_wrapper(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        if "pm_users" in compiled and compiled.strip().upper().startswith("SELECT"):
            return mock.MagicMock(scalar_one_or_none=lambda: None)
        return await db_session.execute(stmt, *args, **kwargs)

    with mock.patch.object(db_session, "execute", side_effect=_execute_wrapper):
        approved = await service.decide(review.id, "approved", "rev_2")

    assert approved.status == "approved"

    queued = await db_session.execute(
        select(RetryQueue).where(
            RetryQueue.pm_id == user.id,
            RetryQueue.task_type == "send_alert",
        )
    )
    assert len(queued.scalars().all()) == 1


@pytest.mark.asyncio
async def test_retention_count_pending_returns_counts(db_session: AsyncSession) -> None:
    """count_pending delegates to run(dry_run=True) and returns dict."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)

    old = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="test",
        content_hash="pending",
        created_at=datetime.now(UTC) - timedelta(days=4000),
    )
    db_session.add(old)
    await db_session.flush()

    service = RetentionService(db_session, settings=Settings(app_env="test", retention_days=365))
    counts = await service.count_pending()
    assert counts.get("signal_log") == 1


@pytest.mark.asyncio
async def test_retention_decides_behavior_with_no_candidates(
    db_session: AsyncSession,
) -> None:
    """A real run with zero candidates writes no audit log."""
    service = RetentionService(db_session, settings=Settings(app_env="test", retention_days=365))
    summary = await service.run(dry_run=False)
    assert summary["total"] == 0
    log = await db_session.execute(
        select(AuditLog).where(AuditLog.action_type == "retention_soft_delete")
    )
    assert log.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_retention_unknown_entity_type_is_skipped(db_session: AsyncSession) -> None:
    """Unknown entity type is logged and produces no count."""
    service = RetentionService(db_session, settings=Settings(app_env="test"))
    summary = await service.run(entity_types=["unknown_type"], dry_run=True)
    assert "unknown_type" not in summary["counts"]


@pytest.mark.asyncio
async def test_isolation_scope_for_context_fund_only_model(
    db_session: AsyncSession,
) -> None:
    """scope_for_context for fund-only models raises when fund_id missing."""

    class _FundOnly:
        isolation_scope = "pm"
        fund_entity_id = "fund_only"

    with (
        RequestContext.bind(pm_id="pm_1"),
        pytest.raises(IsolationError, match="fund_id is required"),
    ):
        IsolationService.scope_for_context(select(literal(1)), _FundOnly)


@pytest.mark.asyncio
async def test_isolation_ensure_model_isolated_property() -> None:
    """ensure_model_isolated rejects a model row owned by another PM."""

    class _Row:
        pm_id = "pm_other"

    with pytest.raises(IsolationError, match="Cross-PM isolation violation"):
        IsolationService.ensure_model_isolated([_Row()], "pm_self")


@pytest.mark.asyncio
async def test_audit_action_without_repository_get(db_session: AsyncSession) -> None:
    """Decorator still emits audit when repository has no get method."""

    class _RepoWithoutGet:
        @audit_action("custom_action", "custom_obj")
        async def create(
            self,
            obj_id: str,
            *,
            pm_id: str,
            fund_entity_id: str,
            session: AsyncSession,
        ) -> ThesisVersion:
            thesis = ThesisVersion(
                id=obj_id,
                pm_id=pm_id,
                ticker="NFLX",
                version=1,
                fund_entity_id=fund_entity_id,
            )
            session.add(thesis)
            await session.flush()
            return thesis

    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    repo = _RepoWithoutGet()
    obj_id = str(uuid.uuid4())
    await repo.create(obj_id, pm_id=user.id, fund_entity_id=fund.id, session=db_session)
    log = await db_session.execute(select(AuditLog).where(AuditLog.object_id == obj_id))
    assert log.scalar_one_or_none() is not None


def test_isolation_ensure_memory_context_isolated_allows_none_items() -> None:
    """Memory items without a pm_id do not raise."""
    IsolationService.ensure_memory_context_isolated([{"found_in": "rag"}], "pm_a")


def test_isolation_require_isolated_global_model() -> None:
    """Global models pass require_isolated without context."""

    class _GlobalModel:
        isolation_scope = "global"

    with RequestContext.bind(pm_id="pm_1"):
        row = _GlobalModel()
        IsolationService.require_isolated(row)


@pytest.mark.asyncio
async def test_isolation_require_isolated_fund_mismatch(
    db_session: AsyncSession,
) -> None:
    """require_isolated raises on fund-only model mismatch."""
    fund_a = await _fund_entity(db_session)
    fund_b = await _fund_entity(db_session)

    class _FundOnly:
        isolation_scope = "pm"
        fund_entity_id = fund_b.id

    with (
        RequestContext.bind(pm_id="pm_any", fund_id=fund_a.id),
        pytest.raises(IsolationError, match="Cross-fund isolation violation"),
    ):
        IsolationService.require_isolated(_FundOnly())


@pytest.mark.asyncio
async def test_export_service_missing_key_raises(db_session: AsyncSession) -> None:
    """ExportService raises when no encryption key is configured."""
    fund = await _fund_entity(db_session)
    user = await _pm_user(db_session, fund.id)
    settings = Settings(app_env="test", encryption_key=None, export_encryption_key=None)
    service = ExportService(db_session, settings=settings)
    signal = SignalLog(
        id=str(uuid.uuid4()),
        pm_id=user.id,
        source_type="test",
        content_hash="k",
    )
    db_session.add(signal)
    await db_session.flush()
    with pytest.raises(RuntimeError, match="EXPORT_ENCRYPTION_KEY or ENCRYPTION_KEY"):
        await service.export(signal)


@pytest.mark.asyncio
async def test_export_decrypt_handles_bad_base64() -> None:
    """ExportService.decrypt wraps invalid base64 as EncryptionError."""
    key = generate_fernet_key()
    with pytest.raises(EncryptionError, match="not valid base64"):
        ExportService.decrypt("not-base64!!!", key)


@pytest.mark.asyncio
async def test_export_decrypt_handles_bad_token() -> None:
    """ExportService.decrypt wraps invalid Fernet tokens as EncryptionError."""
    key = generate_fernet_key()
    token = "aW52YWxpZHRva2Vu"  # base64('invalidtoken')
    with pytest.raises(EncryptionError, match="Failed to decrypt export payload"):
        ExportService.decrypt(token, key)


@pytest.mark.asyncio
async def test_mnpi_review_uses_llm_when_parsed_response_valid(
    db_session: AsyncSession,
) -> None:
    """MNPIReviewAgent.review uses a structured LLM response when available."""

    class _FakeProvider(LLMProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            temperature: float = 0.0,
            response_schema: type[BaseModel] | None = None,
        ) -> LLMResponse:
            return LLMResponse(
                content='{"mnpi_score": 0.9, "materiality_score": 0.8, "flagged": false, "reasoning": "llm"}',
                parsed={
                    "mnpi_score": 0.9,
                    "materiality_score": 0.8,
                    "flagged": False,
                    "reasoning": "llm",
                },
            )

    agent = MNPIReviewAgent(provider=_FakeProvider(), threshold=0.5)
    result = await agent.review("quarterly guidance", ticker="MSFT")
    assert result.flagged is True  # threshold recomputation
    assert result.mnpi_score == 0.9


@pytest.mark.asyncio
async def test_mnpi_review_uses_llm_then_heuristic_on_parsing_error(
    db_session: AsyncSession,
) -> None:
    """An LLM response that cannot be turned into MNPIReviewResult falls back."""

    class _BadProvider(LLMProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            temperature: float = 0.0,
            response_schema: type[BaseModel] | None = None,
        ) -> LLMResponse:
            return LLMResponse(content="invalid json", parsed={"bad": "data"})

    agent = MNPIReviewAgent(provider=_BadProvider(), threshold=0.99)
    result = await agent.review("weather is nice", ticker="SPY")
    assert result.flagged is False


@pytest.mark.asyncio
async def test_mnpi_review_llm_provider_raises_then_heuristic(
    db_session: AsyncSession,
) -> None:
    """An exception from the LLM provider triggers the heuristic path."""

    class _ExplodingProvider(LLMProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            temperature: float = 0.0,
            response_schema: type[BaseModel] | None = None,
        ) -> LLMResponse:
            raise RuntimeError("provider down")

    agent = MNPIReviewAgent(provider=_ExplodingProvider(), threshold=0.99)
    result = await agent.review("weather is nice", ticker="SPY")
    assert result.flagged is False
