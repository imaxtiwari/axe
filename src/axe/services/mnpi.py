"""MNPI review service: score signals, queue reviews, release approved alerts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.guardrails import GuardrailRunner
from axe.agents.mnpi_review import MNPIReviewAgent, MNPIReviewResult
from axe.db.models import MNPIReviewQueue, PMUser, RetryQueue, SignalLog
from axe.security.audit import AuditService
from axe.security.context import RequestContext

Decision = Literal["approved", "rejected"]


class MNPIReviewOutcome:
    """Result of reviewing a single signal for MNPI."""

    def __init__(
        self,
        blocked: bool,
        review: MNPIReviewQueue | None = None,
        result: MNPIReviewResult | None = None,
    ) -> None:
        self.blocked = blocked
        self.review = review
        self.result = result


class MNPIService:
    """Runtime MNPI detection and review workflow.

    ``review_signal`` scores the text and either lets it proceed or blocks it
    by creating an ``MNPIReviewQueue`` row and flagging the source signal.
    ``decide`` records a reviewer decision, audit-logs it, and on approval
    enqueues the originally computed alert payloads for dispatch.
    """

    def __init__(
        self,
        session: AsyncSession,
        agent: MNPIReviewAgent | None = None,
        guardrail_runner: GuardrailRunner | None = None,
    ) -> None:
        self.session = session
        self.agent = agent or MNPIReviewAgent()
        self.guardrail_runner = guardrail_runner

    async def review_signal(
        self,
        signal_id: str,
        signal_text: str | None,
        ticker: str | None,
        pm_id: str,
        alert_payloads: list[dict[str, Any]],
    ) -> MNPIReviewOutcome:
        """Score ``signal_text`` and block alerts when above the threshold.

        Returns an ``MNPIReviewOutcome`` with ``blocked=True`` when the signal
        is flagged. The alert payloads are stored on the review row for later
        release.
        """
        result = await self.agent.review(signal_text or "", ticker)
        if not result.flagged:
            return MNPIReviewOutcome(blocked=False, result=result)

        # Run the shared guardrail runner so MNPI signals also surface in the
        # compliance escalation queue.
        guardrail_result = None
        guardrail_escalation = None
        if self.guardrail_runner is not None:
            guardrail_result = await self.guardrail_runner.check(
                signal_text,
                metadata={"artifact_type": "signal", "ticker": ticker},
            )
            guardrail_escalation = await self.guardrail_runner.escalate(
                guardrail_result, trace_id=None
            )

        signal = await self.session.get(SignalLog, signal_id)
        if signal is not None:
            signal.mnpi_flag = True
            # If caller didn't pass a ticker, prefer the one on the saved signal.
            ticker = ticker or signal.ticker

        review = MNPIReviewQueue(
            pm_id=pm_id,
            signal_id=signal_id,
            ticker=ticker,
            status="pending",
            mnpi_score=result.mnpi_score,
            materiality_score=result.materiality_score,
            reasoning=result.reasoning,
            alert_payloads=alert_payloads,
            guardrail_result_json=(
                guardrail_result.__dict__ if guardrail_result is not None else None
            ),
            guardrail_escalation_id=(
                guardrail_escalation.id if guardrail_escalation is not None else None
            ),
        )
        self.session.add(review)
        await self.session.flush()
        await self._audit("mnpi_review_created", review)
        return MNPIReviewOutcome(blocked=True, review=review, result=result)

    async def decide(
        self,
        review_id: str,
        decision: Decision,
        reviewer_id: str,
    ) -> MNPIReviewQueue:
        """Approve or reject a pending MNPI review item.

        On approval the source signal is un-flagged and the stored alert
        payloads are enqueued for dispatch. The decision is written to the
        audit trail.
        """
        review = await self.session.get(MNPIReviewQueue, review_id)
        if review is None:
            raise ValueError(f"MNPI review {review_id} not found")
        if review.status != "pending":
            raise ValueError(f"Review {review_id} has already been {review.status}")

        before_state = self._review_state(review)
        review.status = decision
        review.reviewer_id = reviewer_id
        review.decision_at = datetime.now(UTC)

        if decision == "approved" and review.signal_id:
            signal = await self.session.get(SignalLog, review.signal_id)
            if signal is not None:
                signal.mnpi_flag = False
            await self._release_alerts(review)

        await self.session.flush()
        await self._audit(
            f"mnpi_review_{decision}",
            review,
            before_state=before_state,
        )
        return review

    async def _release_alerts(self, review: MNPIReviewQueue) -> None:
        """Enqueue ``send_alert`` tasks for each stored alert payload.

        Looks up the PM's contact details if the stored payload is missing
        them so downstream dispatch has what it needs.
        """
        if not review.alert_payloads:
            return

        result = await self.session.execute(select(PMUser).where(PMUser.id == review.pm_id))
        user = result.scalar_one_or_none()

        for alert in review.alert_payloads:
            payload = dict(alert)
            if user is not None:
                payload.setdefault("slack_user_id", user.slack_user_id)
                payload.setdefault("email", user.email)
            task = RetryQueue(
                pm_id=review.pm_id,
                task_type="send_alert",
                payload=payload,
            )
            self.session.add(task)

    async def _audit(
        self,
        action_type: str,
        review: MNPIReviewQueue,
        before_state: dict[str, Any] | None = None,
    ) -> None:
        ctx = RequestContext.current_or_none()
        audit = AuditService(self.session)
        await audit.log(
            action_type=action_type,
            object_type="mnpi_review_queue",
            object_id=review.id,
            before_state=before_state or {},
            after_state=self._review_state(review),
            pm_id=review.pm_id,
            fund_entity_id=ctx.fund_id if ctx is not None else None,
            source_ip=ctx.client_ip if ctx is not None else None,
            session_id=ctx.request_id if ctx is not None else review.id,
            retention_class="compliance",
            non_blocking=False,
        )

    @staticmethod
    def _review_state(review: MNPIReviewQueue) -> dict[str, Any]:
        return {
            "id": review.id,
            "pm_id": review.pm_id,
            "signal_id": review.signal_id,
            "ticker": review.ticker,
            "status": review.status,
            "mnpi_score": review.mnpi_score,
            "materiality_score": review.materiality_score,
            "reasoning": review.reasoning,
            "reviewer_id": review.reviewer_id,
            "decision_at": review.decision_at.isoformat() if review.decision_at else None,
            "alert_count": len(review.alert_payloads),
        }


__all__ = ["MNPIService", "MNPIReviewOutcome"]
