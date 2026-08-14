"""Full AXE v2.1 SQLAlchemy model definitions."""

import datetime as dt
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from axe.db.base import Base
from axe.security.encryption import EncryptedJSON


def utc_now() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(UTC)


async def seed_deck_templates(session: Any) -> list["DeckTemplate"]:
    """Insert default deck templates if they do not already exist.

    Uses asset_class + audience as the unique key so repeated calls are
    idempotent. Returns the created or existing template rows.
    """
    from sqlalchemy import select

    from axe.agents.deck import DEFAULT_DECK_TEMPLATES

    created: list[DeckTemplate] = []
    for vehicle_type, spec in DEFAULT_DECK_TEMPLATES.items():
        result = await session.execute(
            select(DeckTemplate).where(
                DeckTemplate.asset_class == vehicle_type,
                DeckTemplate.audience == "ic_committee",
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            created.append(existing)
            continue
        template = DeckTemplate(
            id=str(uuid.uuid4()),
            name=spec["name"],
            asset_class=vehicle_type,
            audience="ic_committee",
            structure=spec["structure"],
        )
        session.add(template)
        created.append(template)
    await session.flush()
    return created


class FundEntity(Base):
    """A legal fund or advisor entity for compliance isolation."""

    __tablename__ = "fund_entities"

    isolation_scope = "global"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(String(64))
    data_residency: Mapped[str | None] = mapped_column(String(64))
    retention_years: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    mnpi_policy: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    pm_users: Mapped[list["PMUser"]] = relationship("PMUser", back_populates="fund_entity")


class PMUser(Base):
    """A portfolio-manager user account."""

    __tablename__ = "pm_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    slack_user_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York", nullable=False)
    role: Mapped[str] = mapped_column(String(64), default="pm", nullable=False)
    compliance_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    onboarding_state: Mapped[str] = mapped_column(String(32), default="not_started", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_pm_users_pm_created", "id", "created_at"),)

    fund_entity: Mapped["FundEntity"] = relationship("FundEntity", back_populates="pm_users")


class TickerRegistry(Base):
    """Tickers claimed/tracked by a PM."""

    __tablename__ = "ticker_registry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    asset_class: Mapped[str] = mapped_column(String(64), default="equity", nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="long", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    position_size_bucket: Mapped[str | None] = mapped_column(String(32))
    claimed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    last_thesis_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("pm_id", "ticker", name="uq_ticker_registry_pm_ticker"),
        Index("ix_ticker_registry_pm_created", "pm_id", "claimed_at"),
    )


class ThesisVersion(Base):
    """Versioned investment thesis snapshot."""

    __tablename__ = "thesis_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    asset_class: Mapped[str] = mapped_column(String(64), default="equity", nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="long", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    bull_case: Mapped[str | None] = mapped_column(Text)
    bear_case: Mapped[str | None] = mapped_column(Text)
    key_assumptions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    catalysts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    conviction: Mapped[int | None] = mapped_column(Integer)
    unresolved_risks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    mnpi_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    pm_persona_snapshot_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        UniqueConstraint("pm_id", "ticker", "version", name="uq_thesis_versions_pm_ticker_version"),
        Index("ix_thesis_versions_pm_created", "pm_id", "created_at"),
    )


class SignalLog(Base):
    """Ingested signal with extraction, stance, and citation metadata."""

    __tablename__ = "signal_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    raw_content: Mapped[str | None] = mapped_column(Text)
    extracted_signal: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    citation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_id: Mapped[str | None] = mapped_column(String(255))
    specialist_signal_id: Mapped[str | None] = mapped_column(String(36))
    parent_signal_id: Mapped[str | None] = mapped_column(String(36))
    chain_id: Mapped[str | None] = mapped_column(String(36))
    relevance_score: Mapped[float | None] = mapped_column(Float)
    thesis_assumption_id: Mapped[str | None] = mapped_column(String(64))
    stance: Mapped[str | None] = mapped_column(String(32))
    mnpi_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    alerted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_signal_log_content_hash", "content_hash"),
        Index("ix_signal_log_pm_created", "pm_id", "created_at"),
    )


class BrokenAssumption(Base):
    """Record of assumptions that have already triggered an alert.

    Prevents duplicate alerts when additional confirming/contradicting
    signals arrive for an assumption that is already known to be breaking.
    """

    __tablename__ = "broken_assumptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    assumption_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signal_log.id"))
    alerted_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "pm_id",
            "ticker",
            "assumption_id",
            name="uq_broken_assumptions_pm_ticker_assumption",
        ),
        Index("ix_broken_assumptions_pm_alerted", "pm_id", "alerted_at"),
    )


class MNPIReviewQueue(Base):
    """Compliance review queue for signals flagged as potential MNPI."""

    __tablename__ = "mnpi_review_queue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signal_log.id"))
    ticker: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    mnpi_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    materiality_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    alert_payloads: Mapped[list[Any]] = mapped_column(JSON, default=list)
    guardrail_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    guardrail_escalation_id: Mapped[str | None] = mapped_column(String(36))
    reviewer_id: Mapped[str | None] = mapped_column(String(36))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_mnpi_review_queue_pm_created", "pm_id", "created_at"),)


class SparringSession(Base):
    """Adversarial review session output."""

    __tablename__ = "sparring_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(32))
    deal_id: Mapped[str | None] = mapped_column(ForeignKey("deal_rooms.id"))
    thesis_version_id: Mapped[str | None] = mapped_column(ForeignKey("thesis_versions.id"))
    input_thesis: Mapped[str | None] = mapped_column(Text)
    bear_case: Mapped[list[Any]] = mapped_column(JSON, default=list)
    contradicting_signals: Mapped[list[Any]] = mapped_column(JSON, default=list)
    break_conditions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    citation_list: Mapped[list[Any]] = mapped_column(JSON, default=list)
    pm_response: Mapped[str | None] = mapped_column(Text)
    accepted_challenges: Mapped[list[Any]] = mapped_column(JSON, default=list)
    output_format: Mapped[str] = mapped_column(String(32), default="structured", nullable=False)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_sparring_sessions_pm_created", "pm_id", "created_at"),)


class MorningBrief(Base):
    """Daily morning brief delivery record."""

    __tablename__ = "morning_briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    sections: Mapped[list[Any]] = mapped_column(JSON, default=list)
    focus_one: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    catalyst_week: Mapped[list[Any]] = mapped_column(JSON, default=list)
    delivered_slack: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivered_email: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    decision_prompts_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    actions_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    citation_links_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("pm_id", "date", name="uq_morning_briefs_pm_date"),
        Index("ix_morning_briefs_pm_created", "pm_id", "created_at"),
    )


class MeetingSummary(Base):
    """Transcribed meeting summary linked to a ticker and/or deal."""

    __tablename__ = "meeting_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(32))
    deal_id: Mapped[str | None] = mapped_column(ForeignKey("deal_rooms.id"))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    transcript: Mapped[str | None] = mapped_column(Text)
    guidance_changes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    commitments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    tone_signals: Mapped[list[Any]] = mapped_column(JSON, default=list)
    thesis_conflicts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    mnpi_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    speaker_roles: Mapped[list[Any]] = mapped_column(JSON, default=list)
    citation_timestamps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_meeting_summaries_pm_created", "pm_id", "created_at"),)


class PMMemory(Base):
    """Synthesized long-term memory object per PM."""

    __tablename__ = "pm_memory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ticker_memories: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    interaction_patterns: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fund_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    asset_class_memories: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    deal_memories: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    uncertainty_labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_synthesized_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    synthesis_trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("pm_id", "version", name="uq_pm_memory_pm_version"),
        Index("ix_pm_memory_pm_created", "pm_id", "created_at"),
    )


class PMMemoryColdStart(Base):
    """Onboarding cold-start interview answers."""

    __tablename__ = "pm_memory_cold_start"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False, unique=True)
    q1_hold_period: Mapped[str | None] = mapped_column(Text)
    q2_cutting_losers: Mapped[str | None] = mapped_column(Text)
    q3_edge: Mapped[str | None] = mapped_column(Text)
    q4_when_wrong: Mapped[str | None] = mapped_column(Text)
    q5_double_down: Mapped[str | None] = mapped_column(Text)
    synthesized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class PMMemoryCorrection(Base):
    """User-corrected memory profile fields."""

    __tablename__ = "pm_memory_corrections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    field: Mapped[str] = mapped_column(String(128), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    corrected_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_pm_memory_corrections_pm_created", "pm_id", "created_at"),)


class PMOAuthToken(Base):
    """Encrypted OAuth tokens for a PM."""

    __tablename__ = "pm_oauth_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    # EncryptedJSON stores a dict with transparent Python-level encryption.
    token_payload: Mapped[dict[str, Any]] = mapped_column(
        EncryptedJSON, nullable=False, default=dict
    )
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime)
    scopes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("pm_id", "provider", name="uq_pm_oauth_tokens_pm_provider"),
        Index("ix_pm_oauth_tokens_pm_created", "pm_id", "created_at"),
    )


class RetryQueue(Base):
    """Retry queue for failed async tasks."""

    __tablename__ = "retry_queue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str | None] = mapped_column(ForeignKey("pm_users.id"))
    task_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # pending|succeeded|failed|dead_letter
    broker_attempted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dead_letter_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_retry_queue_pm_created", "pm_id", "created_at"),)


class AuditLog(Base):
    """Append-only compliance audit log."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str | None] = mapped_column(ForeignKey("pm_users.id"))
    fund_entity_id: Mapped[str | None] = mapped_column(ForeignKey("fund_entities.id"))
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str | None] = mapped_column(String(36))
    before_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_ip: Mapped[str | None] = mapped_column(String(45))
    session_id: Mapped[str | None] = mapped_column(String(255))
    client_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    server_timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    retention_class: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_audit_log_pm_created", "pm_id", "created_at"),)


# AuditLog is append-only: block UPDATE and DELETE via ORM events.
@event.listens_for(AuditLog, "before_update")
def _block_audit_update(mapper, connection, target):  # type: ignore[no-untyped-def]
    raise RuntimeError("AuditLog rows are append-only and cannot be updated.")


@event.listens_for(AuditLog, "before_delete")
def _block_audit_delete(mapper, connection, target):  # type: ignore[no-untyped-def]
    raise RuntimeError("AuditLog rows are append-only and cannot be deleted.")


class DedupLog(Base):
    """Deduplication ledger keyed by content hash."""

    __tablename__ = "dedup_log"

    isolation_scope = "global"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class SignalFeedback(Base):
    """PM feedback on surfaced signals."""

    __tablename__ = "signal_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    signal_id: Mapped[str] = mapped_column(ForeignKey("signal_log.id"), nullable=False)
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    reaction: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_signal_feedback_pm_created", "pm_id", "created_at"),)


class PMQuietHours(Base):
    """Quiet-hours configuration per PM."""

    __tablename__ = "pm_quiet_hours"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False, unique=True)
    start_time: Mapped[str] = mapped_column(String(8), nullable=False)  # HH:MM
    end_time: Mapped[str] = mapped_column(String(8), nullable=False)  # HH:MM
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    override_keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class CatalystEvent(Base):
    """Market-moving catalysts for the weekly calendar (earnings, Fed, macro, etc.)."""

    __tablename__ = "catalyst_events"

    isolation_scope = "global"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # earnings, fed, macro, conference
    event_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    event_time: Mapped[str | None] = mapped_column(String(16))  # e.g. "07:00 UTC"
    description: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    relevance_tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_catalyst_events_ticker_date", "ticker", "event_date"),)


class BriefReply(Base):
    """Slack replies to a delivered morning brief and the action taken."""

    __tablename__ = "brief_replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    brief_id: Mapped[str] = mapped_column(
        ForeignKey("morning_briefs.id"), nullable=False, index=True
    )
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    slack_thread_ts: Mapped[str | None] = mapped_column(String(32))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(
        String(32)
    )  # update_thesis, ask_followup, dismiss_signal
    action_taken: Mapped[str | None] = mapped_column(Text)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_brief_replies_brief_created", "brief_id", "created_at"),)


class ThesisTest(Base):
    """Testable thesis assumption checks."""

    __tablename__ = "thesis_tests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    thesis_version_id: Mapped[str] = mapped_column(ForeignKey("thesis_versions.id"), nullable=False)
    assumption_id: Mapped[str | None] = mapped_column(String(64))
    test_statement: Mapped[str] = mapped_column(Text, nullable=False)
    pass_criteria: Mapped[str | None] = mapped_column(Text)
    fail_criteria: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime)
    evidence_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class ThesisTestResult(Base):
    """Results of thesis tests run against signals."""

    __tablename__ = "thesis_test_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    test_id: Mapped[str] = mapped_column(ForeignKey("thesis_tests.id"), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class ThesisPostMortem(Base):
    """Post-closure thesis outcome record."""

    __tablename__ = "thesis_post_mortems"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    thesis_version_id: Mapped[str] = mapped_column(ForeignKey("thesis_versions.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    broken_assumption_id: Mapped[str | None] = mapped_column(String(64))
    ignored_signal_id: Mapped[str | None] = mapped_column(String(36))
    notes: Mapped[str | None] = mapped_column(Text)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class CorporateAction(Base):
    """Corporate actions affecting tickers (splits, M&A, ticker changes, etc.)."""

    __tablename__ = "corporate_actions"

    isolation_scope = "global"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[dt.date | None] = mapped_column(Date)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class DealRoom(Base):
    """Private-market or special-situation deal workspace."""

    __tablename__ = "deal_rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), default="screening", nullable=False)
    asset_class: Mapped[str] = mapped_column(String(64), default="private_equity", nullable=False)
    target_ticker_or_private_name: Mapped[str | None] = mapped_column(String(255))
    cim_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_deal_rooms_pm_created", "pm_id", "created_at"),)

    thesis_versions: Mapped[list["DealThesisVersion"]] = relationship(
        "DealThesisVersion", back_populates="deal", lazy="selectin"
    )
    ic_memos: Mapped[list["ICMemo"]] = relationship("ICMemo", back_populates="deal")


class DealDocument(Base):
    """Documents attached to a deal room."""

    __tablename__ = "deal_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    deal_id: Mapped[str] = mapped_column(ForeignKey("deal_rooms.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(2048))
    content_url: Mapped[str | None] = mapped_column(String(2048))
    extracted_entities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ingestion_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class DealThesisVersion(Base):
    """Versioned deal thesis within a deal room."""

    __tablename__ = "deal_thesis_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    deal_id: Mapped[str] = mapped_column(ForeignKey("deal_rooms.id"), nullable=False)
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoints: Mapped[list[Any]] = mapped_column(JSON, default=list)
    bull_case: Mapped[str | None] = mapped_column(Text)
    bear_case: Mapped[str | None] = mapped_column(Text)
    key_assumptions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    risks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("deal_id", "version", name="uq_deal_thesis_versions_deal_version"),
        Index("ix_deal_thesis_versions_pm_created", "pm_id", "created_at"),
    )

    deal: Mapped["DealRoom"] = relationship("DealRoom", back_populates="thesis_versions")


class UnderwritingChecklist(Base):
    """Deal-specific underwriting checklist items."""

    __tablename__ = "underwriting_checklists"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    deal_id: Mapped[str] = mapped_column(ForeignKey("deal_rooms.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(2048))
    answered_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class UnderwritingScenario(Base):
    """Scenario analyses attached to a deal."""

    __tablename__ = "underwriting_scenarios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    deal_id: Mapped[str] = mapped_column(ForeignKey("deal_rooms.id"), nullable=False)
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    probability_weight: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class ICMemo(Base):
    """Investment Committee memo with versioned content and sign-off status."""

    __tablename__ = "ic_memos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    deal_id: Mapped[str] = mapped_column(ForeignKey("deal_rooms.id"), nullable=False, index=True)
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    # Structured JSON payload from the LLM (recommendation, rationale, risks, etc.)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Human-readable markdown rendering of the memo
    content_md: Mapped[str | None] = mapped_column(Text)
    final_signoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("deal_id", "version", name="uq_ic_memos_deal_version"),
        Index("ix_ic_memos_pm_created", "pm_id", "created_at"),
    )

    deal: Mapped["DealRoom"] = relationship("DealRoom", back_populates="ic_memos")
    signoffs: Mapped[list["ICSignOff"]] = relationship(
        "ICSignOff", back_populates="memo", lazy="selectin"
    )


class ICSignOff(Base):
    """A single sign-off on an IC memo by an authenticated PM user."""

    __tablename__ = "ic_signoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    memo_id: Mapped[str] = mapped_column(ForeignKey("ic_memos.id"), nullable=False, index=True)
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("memo_id", "pm_id", name="uq_ic_signoffs_memo_pm"),
        Index("ix_ic_signoffs_memo_created", "memo_id", "created_at"),
    )

    memo: Mapped["ICMemo"] = relationship("ICMemo", back_populates="signoffs")


class DeckTemplate(Base):
    """Reusable IC / LP update deck templates."""

    __tablename__ = "deck_templates"

    isolation_scope = "global"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(64), nullable=False)
    audience: Mapped[str] = mapped_column(String(64), nullable=False)
    structure: Mapped[list[Any]] = mapped_column(JSON, default=list)


class DeckOutput(Base):
    """Generated deck / memo outputs."""

    __tablename__ = "deck_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    export_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_deck_outputs_pm_created", "pm_id", "created_at"),)


class InvestmentVehicle(Base):
    """Fund-level investment vehicle / strategy."""

    __tablename__ = "investment_vehicles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_entity: Mapped[str | None] = mapped_column(String(255))
    strategy: Mapped[str | None] = mapped_column(String(255))
    vintage: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    reporting_frequency: Mapped[str] = mapped_column(
        String(32), default="quarterly", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class LPRelationship(Base):
    """Limited-partner relationships per vehicle."""

    __tablename__ = "lp_relationships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("investment_vehicles.id"), nullable=False)
    lp_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    side_letter_flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class LPUpdate(Base):
    """LP update communication records."""

    __tablename__ = "lp_updates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("investment_vehicles.id"), nullable=False)
    quarter: Mapped[str] = mapped_column(String(16), nullable=False)
    sections: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(36))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Rendered outputs for the LP letter
    content_md: Mapped[str | None] = mapped_column(Text)
    content_html: Mapped[str | None] = mapped_column(Text)
    feedback_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    read_receipts_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class CommunicationArchive(Base):
    """Archive of inbound/outbound investment-related communications."""

    __tablename__ = "communication_archive"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_content: Mapped[str | None] = mapped_column(Text)
    # Recipient list, LP update reference, vehicle/quarter etc.
    archive_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    retention_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_communication_archive_pm_created", "pm_id", "created_at"),)


class ConnectorConfig(Base):
    """Inbound source connector configuration per PM."""

    __tablename__ = "connector_config"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Encrypted credentials dict (OAuth token, API key, connection string, etc.)
    credentials_encrypted: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON, default=dict)
    schedule: Mapped[str | None] = mapped_column(String(64))  # cron or interval label
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_cursor: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("pm_id", "source_type", name="uq_connector_config_pm_source"),
        Index("ix_connector_config_pm_created", "pm_id", "created_at"),
    )


class RawIngest(Base):
    """Raw payload and extraction result from a connector run."""

    __tablename__ = "raw_ingest"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(255))
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    extracted_signal_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # pending|processed|failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_raw_ingest_content_hash", "content_hash"),
        Index("ix_raw_ingest_pm_created", "pm_id", "created_at"),
    )


class PMPersona(Base):
    """Synthesized PM writing style, decision triggers, and trusted sources."""

    __tablename__ = "pm_persona"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    writing_style_summary: Mapped[str | None] = mapped_column(Text)
    decision_triggers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    peer_relationships_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    trusted_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    confidence_language: Mapped[str | None] = mapped_column(Text)
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("pm_id", name="uq_pm_persona_pm"),
        Index("ix_pm_persona_pm_created", "pm_id", "created_at"),
    )


class MemoryCitation(Base):
    """Cited snippet mined from email/Slack history linked to a ticker or deal."""

    __tablename__ = "memory_citation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)  # gmail|slack|crm|...
    source_id: Mapped[str | None] = mapped_column(String(255))
    snippet: Mapped[str | None] = mapped_column(Text)
    linked_ticker: Mapped[str | None] = mapped_column(String(32))
    linked_deal_id: Mapped[str | None] = mapped_column(String(36))
    sentiment: Mapped[str | None] = mapped_column(String(32))
    extracted_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_memory_citation_pm_created", "pm_id", "created_at"),)


class PMPeerMap(Base):
    """Trusted peer relationships mined from communications history."""

    __tablename__ = "pm_peer_map"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    peer_email_or_slack_id: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_name: Mapped[str | None] = mapped_column(String(255))
    relationship_type: Mapped[str | None] = mapped_column(
        String(64)
    )  # colleague, expert, lp, management
    interaction_frequency: Mapped[str | None] = mapped_column(String(32))  # daily, weekly, monthly
    topics: Mapped[list[Any]] = mapped_column(JSON, default=list)
    trust_level: Mapped[str | None] = mapped_column(String(32))  # high, medium, low
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("pm_id", "peer_email_or_slack_id", name="uq_pm_peer_map_pm_peer"),
        Index("ix_pm_peer_map_pm_created", "pm_id", "created_at"),
    )


class SpecialistSignal(Base):
    """Structured signal produced by a specialist agent from raw ingestion."""

    __tablename__ = "specialist_signal"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    raw_ingest_id: Mapped[str | None] = mapped_column(String(36))
    ticker: Mapped[str | None] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    specialist_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    stance: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    assumptions_touched: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_specialist_signal_pm_created", "pm_id", "created_at"),
        Index("ix_specialist_signal_raw_ingest", "raw_ingest_id"),
    )


class ArtifactAction(Base):
    """Action generated from an artifact and optionally executed by the PM."""

    __tablename__ = "artifact_action"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )  # pending|executed|dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_artifact_action_artifact", "artifact_type", "artifact_id"),
        Index("ix_artifact_action_pm_created", "pm_id", "created_at"),
    )


class AgentMessage(Base):
    """Persisted cross-agent collaboration message.

    Messages are isolated by fund and persisted with a TTL so the audit trail
    can be replayed without becoming an unbounded log.
    """

    __tablename__ = "agent_messages"
    isolation_scope = "fund"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sender_agent: Mapped[str] = mapped_column(String(128), nullable=False)
    recipient_agent: Mapped[str | None] = mapped_column(String(128))
    sender_pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    recipient_pm_id: Mapped[str | None] = mapped_column(ForeignKey("pm_users.id"))
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scope: Mapped[str] = mapped_column(String(16), default="pm", nullable=False)
    allowed_other_pm_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    required_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    requires_decision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_agent_messages_pm_created", "sender_pm_id", "created_at"),
        Index("ix_agent_messages_fund", "fund_entity_id"),
        Index("ix_agent_messages_recipient", "recipient_pm_id", "created_at"),
    )


class DecisionPrompt(Base):
    """Decision prompt attached to an artifact awaiting PM response."""

    __tablename__ = "decision_prompt"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str] = mapped_column(ForeignKey("pm_users.id"), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(36))
    prompt_text: Mapped[str | None] = mapped_column(Text)
    options_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    response: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("ix_decision_prompt_pm_created", "pm_id", "created_at"),)


class ModelTrace(Base):
    """Trace record for every LLM completion for guardrails and audit."""

    __tablename__ = "model_trace"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str | None] = mapped_column(ForeignKey("pm_users.id"))
    agent: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    response_schema: Mapped[str | None] = mapped_column(String(128))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    citations_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    hallucination_score: Mapped[float | None] = mapped_column(Float)
    human_review_status: Mapped[str] = mapped_column(
        String(32), default="not_required", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_model_trace_pm_created", "pm_id", "created_at"),
        Index("ix_model_trace_prompt_hash", "prompt_hash"),
    )


class PolicyRule(Base):
    """Fund-scoped policy rule for guardrails and compliance automation."""

    __tablename__ = "policy_rule"

    isolation_scope = "global"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)  # pm|fund|global
    conditions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_policy_rule_fund", "fund_entity_id"),
        Index("ix_policy_rule_scope", "scope"),
    )


class ComplianceEscalation(Base):
    """Compliance escalation opened by guardrails, MNPI, or hallucination review."""

    __tablename__ = "compliance_escalation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pm_id: Mapped[str | None] = mapped_column(ForeignKey("pm_users.id"))
    fund_entity_id: Mapped[str] = mapped_column(ForeignKey("fund_entities.id"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)  # low|medium|high|critical
    status: Mapped[str] = mapped_column(
        String(32), default="open", nullable=False
    )  # open|assigned|resolved|dismissed
    reviewer_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_compliance_escalation_fund_status", "fund_entity_id", "status"),
        Index("ix_compliance_escalation_pm_created", "pm_id", "created_at"),
    )
