"""Interactive artifact agent: generate decision prompts and actions for artifacts.

This agent turns static artifact outputs (MorningBrief, LPUpdate, DeckOutput,
ICMemo) into decision-driving surfaces by attaching one-click actions and
structured decision prompts scoped to the PM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import DeckOutput, LPUpdate, MorningBrief
from axe.security.isolation import IsolationService


class ActionDefinition(BaseModel):
    """Lightweight action definition emitted by the agent before persistence."""

    action_type: str
    label: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PromptDefinition(BaseModel):
    """Lightweight decision prompt definition emitted by the agent before persistence."""

    prompt_text: str
    options: list[str] = Field(default_factory=list)
    deadline_at: datetime | None = None


class ArtifactActionPlan(BaseModel):
    """Plan of interactive actions and prompts for an artifact."""

    artifact_type: str
    artifact_id: str
    pm_id: str
    actions: list[ActionDefinition] = Field(default_factory=list)
    prompts: list[PromptDefinition] = Field(default_factory=list)


class InteractiveArtifactAgent:
    """Generate artifact-scoped actions and decision prompts.

    The agent is intentionally deterministic: it inspects the artifact content
    and returns a plan of ``ArtifactAction`` and ``DecisionPrompt`` definitions.
    Persistence is delegated to ``InteractiveArtifactService`` so that the
    agent stays side-effect free and easy to unit test.
    """

    SUPPORTED_ARTIFACT_TYPES: set[str] = {
        "morning_brief",
        "lp_update",
        "deck_output",
        "ic_memo",
    }

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def generate_actions(
        self,
        artifact_type: str,
        artifact_id: str,
        pm_id: str,
    ) -> ArtifactActionPlan:
        """Return an action plan for the given artifact and PM."""
        artifact = await self._load_artifact(artifact_type, artifact_id, pm_id)
        if artifact is None:
            return ArtifactActionPlan(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                pm_id=pm_id,
            )

        if isinstance(artifact, MorningBrief):
            return self._morning_brief_actions(artifact, pm_id)
        if isinstance(artifact, LPUpdate):
            return self._lp_update_actions(artifact, pm_id)
        if isinstance(artifact, DeckOutput):
            return self._deck_actions(artifact, pm_id)

        return ArtifactActionPlan(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            pm_id=pm_id,
        )

    async def generate_decision_prompt(
        self,
        artifact_type: str,
        artifact_id: str,
        pm_id: str,
    ) -> ArtifactActionPlan:
        """Return a decision prompt plan for the given artifact and PM."""
        artifact = await self._load_artifact(artifact_type, artifact_id, pm_id)
        if artifact is None:
            return ArtifactActionPlan(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                pm_id=pm_id,
            )

        if isinstance(artifact, MorningBrief):
            return self._morning_brief_prompt(artifact, pm_id)
        if isinstance(artifact, LPUpdate):
            return self._lp_update_prompt(artifact, pm_id)

        return ArtifactActionPlan(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            pm_id=pm_id,
        )

    async def _load_artifact(
        self,
        artifact_type: str,
        artifact_id: str,
        pm_id: str,
    ) -> MorningBrief | LPUpdate | DeckOutput | None:
        if artifact_type not in self.SUPPORTED_ARTIFACT_TYPES:
            return None

        if artifact_type == "morning_brief":
            model = MorningBrief
        elif artifact_type == "lp_update":
            model = LPUpdate
        elif artifact_type == "deck_output":
            model = DeckOutput
        else:
            return None

        if hasattr(model, "pm_id"):
            result = await self.session.execute(
                IsolationService.select_for(model).where(model.id == artifact_id)
            )
        else:
            # Models like LPUpdate are vehicle-scoped; load by primary key and
            # verify ownership via the service/context caller afterwards.
            result = await self.session.execute(select(model).where(model.id == artifact_id))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        if hasattr(model, "pm_id") or hasattr(model, "fund_entity_id"):
            IsolationService.require_isolated(row)
        return row  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Morning brief
    # ------------------------------------------------------------------

    def _morning_brief_actions(
        self,
        brief: MorningBrief,
        pm_id: str,
    ) -> ArtifactActionPlan:
        focus_one = brief.focus_one or {}
        ticker = focus_one.get("ticker", "")
        reason = focus_one.get("reason", "")
        actions: list[ActionDefinition] = []

        if ticker:
            actions.append(
                ActionDefinition(
                    action_type="focus_one_buy_more",
                    label=f"Draft +{ticker} increase",
                    payload={
                        "ticker": ticker,
                        "intent": "increase_position",
                        "note": f"Focus One: {reason}",
                        "draft_only": True,
                    },
                )
            )
            actions.append(
                ActionDefinition(
                    action_type="focus_one_trim",
                    label=f"Draft -{ticker} trim",
                    payload={
                        "ticker": ticker,
                        "intent": "trim_position",
                        "note": f"Focus One: {reason}",
                        "draft_only": True,
                    },
                )
            )

        actions.append(
            ActionDefinition(
                action_type="schedule_call",
                label="Schedule analyst call",
                payload={
                    "topic": (
                        f"Morning Brief follow-up: {ticker or 'portfolio'}"
                        if ticker
                        else "Morning Brief follow-up"
                    ),
                    "draft_only": True,
                },
            )
        )

        return ArtifactActionPlan(
            artifact_type="morning_brief",
            artifact_id=brief.id,
            pm_id=pm_id,
            actions=actions,
        )

    def _morning_brief_prompt(
        self,
        brief: MorningBrief,
        pm_id: str,
    ) -> ArtifactActionPlan:
        focus_one = brief.focus_one or {}
        ticker = focus_one.get("ticker", "")
        reason = focus_one.get("reason", "")

        if not ticker:
            return ArtifactActionPlan(
                artifact_type="morning_brief",
                artifact_id=brief.id,
                pm_id=pm_id,
            )

        prompt_text = (
            f"Today's Focus One is {ticker}. {reason}\n\nHow do you want to act on this signal?"
        )
        options = [
            f"Buy more {ticker}",
            f"Trim {ticker}",
            "Request a specialist call",
            "Dismiss — no action today",
        ]

        return ArtifactActionPlan(
            artifact_type="morning_brief",
            artifact_id=brief.id,
            pm_id=pm_id,
            prompts=[
                PromptDefinition(
                    prompt_text=prompt_text,
                    options=options,
                    deadline_at=datetime.now(UTC).replace(
                        hour=20, minute=0, second=0, microsecond=0
                    ),
                )
            ],
        )

    # ------------------------------------------------------------------
    # LP update
    # ------------------------------------------------------------------

    def _lp_update_actions(
        self,
        update: LPUpdate,
        pm_id: str,
    ) -> ArtifactActionPlan:
        actions: list[ActionDefinition] = []
        if update.status == "approved":
            actions.append(
                ActionDefinition(
                    action_type="send_lp_update",
                    label="Send LP update now",
                    payload={
                        "lp_update_id": update.id,
                        "vehicle_id": update.vehicle_id,
                        "quarter": update.quarter,
                    },
                )
            )
        actions.append(
            ActionDefinition(
                action_type="preview_lp_update",
                label="Preview rendered letter",
                payload={
                    "lp_update_id": update.id,
                    "render_format": "html",
                },
            )
        )
        return ArtifactActionPlan(
            artifact_type="lp_update",
            artifact_id=update.id,
            pm_id=pm_id,
            actions=actions,
        )

    def _lp_update_prompt(
        self,
        update: LPUpdate,
        pm_id: str,
    ) -> ArtifactActionPlan:
        if update.status != "draft":
            return ArtifactActionPlan(
                artifact_type="lp_update",
                artifact_id=update.id,
                pm_id=pm_id,
            )

        prompt_text = (
            f"LP update for {update.quarter} is ready for review. "
            "Approve and send to limited partners?"
        )
        options = [
            "Approve and send",
            "Approve but hold for final review",
            "Request edits",
            "Reject — do not send",
        ]
        return ArtifactActionPlan(
            artifact_type="lp_update",
            artifact_id=update.id,
            pm_id=pm_id,
            prompts=[
                PromptDefinition(
                    prompt_text=prompt_text,
                    options=options,
                    deadline_at=datetime.now(UTC) + timedelta(days=1),
                )
            ],
        )

    # ------------------------------------------------------------------
    # Deck output
    # ------------------------------------------------------------------

    def _deck_actions(
        self,
        deck: DeckOutput,
        pm_id: str,
    ) -> ArtifactActionPlan:
        slides = (deck.content or {}).get("slides", [])
        first_slide_title = ""
        if slides and isinstance(slides[0], dict):
            first_slide_title = slides[0].get("title", "")

        return ArtifactActionPlan(
            artifact_type="deck_output",
            artifact_id=deck.id,
            pm_id=pm_id,
            actions=[
                ActionDefinition(
                    action_type="add_slide_note",
                    label="Add note to first slide",
                    payload={
                        "deck_id": deck.id,
                        "slide_number": 1,
                        "slide_title": first_slide_title,
                    },
                ),
                ActionDefinition(
                    action_type="request_follow_up",
                    label="Request follow-up diligence",
                    payload={
                        "deck_id": deck.id,
                        "topic": first_slide_title or "Deck follow-up",
                    },
                ),
                ActionDefinition(
                    action_type="share_with_team",
                    label="Share with team",
                    payload={
                        "deck_id": deck.id,
                        "channel": "team",
                    },
                ),
            ],
        )


__all__ = [
    "ActionDefinition",
    "ArtifactActionPlan",
    "InteractiveArtifactAgent",
    "PromptDefinition",
]
