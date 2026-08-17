"""Compliance escalation service: open, assign, resolve, and list escalations.

Centralizes severity rules, reviewer assignment, audit logging, and isolation
for escalations created by the MNPI queue, guardrail failures, and hallucination
review routing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.config import Settings, get_settings
from axe.db.models import ComplianceEscalation, PMUser
from axe.db.uow import UnitOfWork
from axe.security.audit import AuditService
from axe.security.context import RequestContext

Severity = Literal["low", "medium", "high", "critical"]
Status = Literal["open", "assigned", "resolved", "dismissed"]
Decision = Literal["approved", "rejected", "dismissed"]

_VALID_SEVERITIES: set[str] = {"low", "medium", "high", "critical"}
_OPEN_STATUSES: set[str] = {"open", "assigned"}


class ComplianceEscalationTrigger:
    """Payload used to open a new escalation."""

    def __init__(
        self,
        *,
        trigger_type: str,
        severity: Severity,
        fund_entity_id: str,
        pm_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.trigger_type = trigger_type
        self.severity = severity
        self.fund_entity_id = fund_entity_id
        self.pm_id = pm_id
        self.details = details or {}


class ComplianceEscalationService:
    """Service for managing compliance escalations and reviewer workflows."""

    def __init__(
        self,
        session_or_uow: AsyncSession | UnitOfWork,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if isinstance(session_or_uow, UnitOfWork):
            self.uow = session_or_uow
            self.session = session_or_uow.session
        else:
            self.uow = None
            self.session = session_or_uow
        self._audit = AuditService(self.session)

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------
    async def open(
        self,
        trigger: ComplianceEscalationTrigger,
        *,
        auto_assign: bool = True,
    ) -> ComplianceEscalation:
        """Open a compliance escalation from a trigger.

        If ``auto_assign`` is True (default), the escalation is immediately
        assigned to the next available compliance officer in the same fund via
        round-robin. Opening is audit-logged with before/after state.
        """
        if trigger.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity '{trigger.severity}'; must be one of {_VALID_SEVERITIES}"
            )

        min_auto_severity = self.settings.compliance_auto_escalation_severity
        if not self._severity_meets(trigger.severity, min_auto_severity):
            # Below auto-escalation threshold: record a low-severity audit note
            # but do not create a queue item.
            await self._audit.log(
                action_type="compliance_escalation_suppressed",
                object_type="compliance_escalation",
                object_id="",
                before_state={},
                after_state={
                    "trigger_type": trigger.trigger_type,
                    "severity": trigger.severity,
                    "fund_entity_id": trigger.fund_entity_id,
                    "pm_id": trigger.pm_id,
                    "reason": f"severity below auto-escalation threshold {min_auto_severity}",
                },
                pm_id=trigger.pm_id,
                fund_entity_id=trigger.fund_entity_id,
                retention_class="compliance",
                non_blocking=False,
            )
            raise BelowAutoEscalationThreshold(
                f"Severity '{trigger.severity}' is below configured threshold "
                f"'{min_auto_severity}'"
            )

        reviewer_id: str | None = None
        status: Status = "open"
        if auto_assign:
            reviewer_id = await self._next_reviewer(trigger.fund_entity_id)
            if reviewer_id is not None:
                status = "assigned"

        details = dict(trigger.details)
        details["trigger_type"] = trigger.trigger_type

        escalation = ComplianceEscalation(
            pm_id=trigger.pm_id,
            fund_entity_id=trigger.fund_entity_id,
            trigger_type=trigger.trigger_type,
            severity=trigger.severity,
            status=status,
            reviewer_id=reviewer_id,
            details=details,
        )
        self.session.add(escalation)
        await self.session.flush()

        await self._audit.log(
            action_type="compliance_escalation_opened",
            object_type="compliance_escalation",
            object_id=escalation.id,
            before_state={},
            after_state=self._state(escalation),
            pm_id=trigger.pm_id,
            fund_entity_id=trigger.fund_entity_id,
            retention_class="compliance",
            non_blocking=False,
        )
        return escalation

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------
    async def assign_reviewer(
        self,
        escalation_id: str,
        reviewer_id: str,
    ) -> ComplianceEscalation:
        """Assign an escalation to a reviewer.

        Verifies the reviewer belongs to the same fund and has the configured
        reviewer role. Assignment is audit-logged with before/after state.
        """
        escalation = await self._get_escalation(escalation_id)
        before_state = self._state(escalation)

        reviewer = await self._get_reviewer(reviewer_id)
        if reviewer is None:
            raise ValueError(f"Reviewer {reviewer_id} not found")
        if reviewer.fund_entity_id != escalation.fund_entity_id:
            raise ValueError("Reviewer does not belong to the escalation fund")
        if reviewer.role != self.settings.compliance_reviewer_role:
            raise ValueError(
                f"Reviewer role '{reviewer.role}' is not "
                f"'{self.settings.compliance_reviewer_role}'"
            )

        escalation.reviewer_id = reviewer_id
        if escalation.status == "open":
            escalation.status = "assigned"
        await self.session.flush()

        await self._audit.log(
            action_type="compliance_escalation_assigned",
            object_type="compliance_escalation",
            object_id=escalation.id,
            before_state=before_state,
            after_state=self._state(escalation),
            pm_id=escalation.pm_id,
            fund_entity_id=escalation.fund_entity_id,
            retention_class="compliance",
            non_blocking=False,
        )
        return escalation

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    async def resolve(
        self,
        escalation_id: str,
        decision: Decision,
        note: str | None = None,
    ) -> ComplianceEscalation:
        """Resolve an escalation with a decision and optional note.

        Decisions map to statuses as: approved/rejected -> resolved,
        dismissed -> dismissed. Resolution closes the escalation and is
        audit-logged with before/after state.
        """
        escalation = await self._get_escalation(escalation_id)
        if escalation.status in {"resolved", "dismissed"}:
            raise ValueError(f"Escalation {escalation_id} is already {escalation.status}")

        before_state = self._state(escalation)

        new_status: Status
        if decision == "dismissed":
            new_status = "dismissed"
        else:
            new_status = "resolved"

        escalation.status = new_status
        escalation.closed_at = datetime.now(UTC)
        if note is not None:
            escalation.details["resolution_note"] = note
        escalation.details["decision"] = decision

        await self.session.flush()

        await self._audit.log(
            action_type=f"compliance_escalation_{decision}",
            object_type="compliance_escalation",
            object_id=escalation.id,
            before_state=before_state,
            after_state=self._state(escalation),
            pm_id=escalation.pm_id,
            fund_entity_id=escalation.fund_entity_id,
            retention_class="compliance",
            non_blocking=False,
        )
        return escalation

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    async def list_open(
        self,
        *,
        pm_id: str | None = None,
        fund_id: str | None = None,
        role: str | None = None,
    ) -> list[ComplianceEscalation]:
        """List open escalations scoped by the caller.

        If ``fund_id`` is provided, results are limited to that fund. If
        ``pm_id`` is provided and ``role`` is ``pm``, results are limited to
        escalations for that PM within the fund. Compliance officers and admins
        see all open escalations in the fund.
        """
        stmt = select(ComplianceEscalation).where(
            ComplianceEscalation.status.in_(_OPEN_STATUSES)
        )

        effective_fund_id = fund_id
        if effective_fund_id is None:
            ctx = RequestContext.current_or_none()
            if ctx is not None:
                effective_fund_id = ctx.fund_id

        if effective_fund_id is not None:
            stmt = stmt.where(ComplianceEscalation.fund_entity_id == effective_fund_id)

        if pm_id is not None and role == "pm":
            stmt = stmt.where(ComplianceEscalation.pm_id == pm_id)
        elif role == "pm":
            ctx = RequestContext.current_or_none()
            if ctx is not None and ctx.pm_id is not None:
                stmt = stmt.where(ComplianceEscalation.pm_id == ctx.pm_id)

        stmt = stmt.order_by(
            ComplianceEscalation.severity.desc(),
            ComplianceEscalation.opened_at.asc(),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _get_escalation(self, escalation_id: str) -> ComplianceEscalation:
        escalation = await self.session.get(ComplianceEscalation, escalation_id)
        if escalation is None:
            raise ValueError(f"Escalation {escalation_id} not found")
        return escalation

    async def _get_reviewer(self, reviewer_id: str) -> PMUser | None:
        result = await self.session.execute(
            select(PMUser).where(PMUser.id == reviewer_id)
        )
        return result.scalar_one_or_none()

    async def _next_reviewer(self, fund_entity_id: str) -> str | None:
        """Return the next active compliance officer in the fund (round-robin).

        Round-robin is approximated by selecting the active compliance officer
        who has been assigned the fewest currently-open escalations, breaking
        ties by PM user creation order. This keeps assignment stateless.
        """
        role = self.settings.compliance_reviewer_role
        result = await self.session.execute(
            select(PMUser)
            .where(
                PMUser.fund_entity_id == fund_entity_id,
                PMUser.role == role,
                PMUser.active.is_(True),
            )
            .order_by(PMUser.created_at.asc())
        )
        candidates = list(result.scalars().all())
        if not candidates:
            return None

        # Fewest open assigned escalations among candidates.
        best: PMUser | None = None
        best_count: int | None = None
        for candidate in candidates:
            count_result = await self.session.execute(
                select(ComplianceEscalation)
                .where(
                    ComplianceEscalation.reviewer_id == candidate.id,
                    ComplianceEscalation.status.in_(_OPEN_STATUSES),
                )
            )
            count = len(count_result.scalars().all())
            if best is None or count < best_count:  # type: ignore[operator]
                best = candidate
                best_count = count

        return best.id if best is not None else None

    @staticmethod
    def _severity_meets(severity: str, minimum: str) -> bool:
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        return order.get(severity, 0) >= order.get(minimum, 0)

    @staticmethod
    def _state(escalation: ComplianceEscalation) -> dict[str, Any]:
        return {
            "id": escalation.id,
            "pm_id": escalation.pm_id,
            "fund_entity_id": escalation.fund_entity_id,
            "trigger_type": escalation.trigger_type,
            "severity": escalation.severity,
            "status": escalation.status,
            "reviewer_id": escalation.reviewer_id,
            "details": escalation.details,
            "opened_at": (
                escalation.opened_at.isoformat() if escalation.opened_at else None
            ),
            "closed_at": (
                escalation.closed_at.isoformat() if escalation.closed_at else None
            ),
        }


class BelowAutoEscalationThreshold(ValueError):
    """Raised when a trigger's severity is below the configured auto threshold."""


__all__ = [
    "ComplianceEscalationService",
    "ComplianceEscalationTrigger",
    "BelowAutoEscalationThreshold",
]
