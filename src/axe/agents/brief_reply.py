"""BriefReplyAgent — classify Slack replies to morning briefs and act on them."""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.llm import LLMProvider, get_default_provider
from axe.db.models import BriefReply, SignalFeedback, SignalLog, ThesisVersion

logger = logging.getLogger(__name__)


class ReplyIntent(BaseModel):
    """Structured intent from a Slack reply to a morning brief."""

    intent: str = Field(
        ...,
        pattern="^(update_thesis|ask_followup|dismiss_signal|unknown)$",
    )
    target_signal_id: str | None = None
    target_thesis_ticker: str | None = None
    target_assumption_id: str | None = None
    new_assumption_text: str | None = None
    follow_up_question: str | None = None
    dismiss_reason: str | None = None
    raw_explanation: str | None = None


class BriefReplyAgent:
    """Classify a Slack reply and apply the intended action."""

    def __init__(
        self,
        session: AsyncSession,
        llm: LLMProvider | None = None,
    ) -> None:
        self.session = session
        self.llm = llm or get_default_provider()

    async def handle(
        self,
        pm_id: str,
        brief_id: str,
        raw_text: str,
        slack_thread_ts: str | None = None,
    ) -> BriefReply:
        """Classify ``raw_text`` and execute the corresponding action."""
        intent = await self._classify(raw_text)
        action_payload: dict[str, Any] = {
            "classified_intent": intent.intent,
            "explanation": intent.raw_explanation,
        }
        action_taken = f"Classified intent: {intent.intent}"

        if intent.intent == "update_thesis" and intent.target_thesis_ticker:
            thesis = await self._get_latest_thesis(pm_id, intent.target_thesis_ticker)
            if thesis:
                new_thesis_id = await self._version_bump_thesis(
                    thesis,
                    intent.target_assumption_id,
                    intent.new_assumption_text or raw_text,
                )
                action_payload["new_thesis_version_id"] = new_thesis_id
                action_taken = f"Created new thesis version for {intent.target_thesis_ticker}"

        elif intent.intent == "dismiss_signal" and intent.target_signal_id:
            feedback = SignalFeedback(
                signal_id=intent.target_signal_id,
                pm_id=pm_id,
                reaction="dismissed",
                reason=intent.dismiss_reason or raw_text,
            )
            self.session.add(feedback)
            action_taken = f"Dismissed signal {intent.target_signal_id}"

        elif intent.intent == "ask_followup":
            action_taken = "Recorded follow-up question"
            action_payload["follow_up_question"] = (
                intent.follow_up_question or "Can you tell me more?"
            )

        reply = BriefReply(
            brief_id=brief_id,
            pm_id=pm_id,
            slack_thread_ts=slack_thread_ts,
            raw_text=raw_text,
            intent=intent.intent,
            action_taken=action_taken,
            action_payload=action_payload,
        )
        self.session.add(reply)
        await self.session.commit()
        return reply

    async def _classify(self, raw_text: str) -> ReplyIntent:
        prompt = (
            "A portfolio manager replied to a morning brief with:\n"
            f"\"{raw_text}\"\n\n"
            "Classify the intent as one of: update_thesis, ask_followup, dismiss_signal, unknown. "
            "Return JSON with keys: intent, target_signal_id (if dismissing a specific signal), "
            "target_thesis_ticker (if updating a thesis), target_assumption_id (optional), "
            "new_assumption_text (paraphrased updated assumption if applicable), "
            "follow_up_question (if asking for more info), dismiss_reason, raw_explanation."
        )
        try:
            response = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_schema=ReplyIntent,
            )
            parsed = response.parsed or {}
            if isinstance(parsed, BaseModel):
                parsed = parsed.model_dump()
            return ReplyIntent(**parsed)
        except Exception:
            logger.exception("Reply classification failed; defaulting to unknown")
            return ReplyIntent(intent="unknown", raw_explanation="Classification failed")

    async def _get_latest_thesis(self, pm_id: str, ticker: str) -> ThesisVersion | None:
        result = await self.session.execute(
            select(ThesisVersion)
            .where(
                ThesisVersion.pm_id == pm_id,
                ThesisVersion.ticker == ticker,
            )
            .order_by(ThesisVersion.version.desc())
        )
        return result.scalars().first()

    async def _version_bump_thesis(
        self,
        thesis: ThesisVersion,
        assumption_id: str | None,
        new_assumption_text: str,
    ) -> str:
        """Create a new immutable thesis version with the updated assumption."""
        assumptions = copy.deepcopy(thesis.key_assumptions or [])
        if assumption_id:
            for assumption in assumptions:
                if isinstance(assumption, dict) and assumption.get("id") == assumption_id:
                    assumption["text"] = new_assumption_text
                    assumption["updated_from_reply"] = True
                    break
            else:
                assumptions.append(
                    {"id": assumption_id, "text": new_assumption_text, "updated_from_reply": True}
                )
        else:
            # If no assumption id, append as a new tracked item.
            assumptions.append(
                {"id": f"from_reply_{datetime.now(timezone.utc).isoformat()}", "text": new_assumption_text}
            )

        new_version = ThesisVersion(
            pm_id=thesis.pm_id,
            ticker=thesis.ticker,
            version=thesis.version + 1,
            is_draft=thesis.is_draft,
            asset_class=thesis.asset_class,
            direction=thesis.direction,
            status=thesis.status,
            bull_case=thesis.bull_case,
            bear_case=thesis.bear_case,
            key_assumptions=assumptions,
            catalysts=copy.deepcopy(thesis.catalysts or []),
            conviction=thesis.conviction,
            unresolved_risks=copy.deepcopy(thesis.unresolved_risks or []),
            fund_entity_id=thesis.fund_entity_id,
            mnpi_flag=thesis.mnpi_flag,
        )
        self.session.add(new_version)
        await self.session.flush()
        return new_version.id
