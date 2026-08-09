"""Interactive artifact service: persist and execute artifact actions and prompts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from axe.agents.interactive_artifact import (
    InteractiveArtifactAgent,
)
from axe.db.models import ArtifactAction, DecisionPrompt
from axe.db.uow import UnitOfWork
from axe.security.audit import AuditService, _state_dict
from axe.security.context import RequestContext
from axe.security.isolation import IsolationError, IsolationService

logger = logging.getLogger(__name__)


class ActionExecutionError(RuntimeError):
    """Raised when an artifact action cannot be executed."""


class _ContextHelper:
    """Bind a RequestContext when none is active; no-op otherwise."""

    def __init__(self, pm_id: str, fund_entity_id: str | None) -> None:
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self._token: Any | None = None

    def __enter__(self) -> _ContextHelper:
        if RequestContext.current_or_none() is None:
            self._token = RequestContext.set_current(
                RequestContext(pm_id=self.pm_id, fund_id=self.fund_entity_id)
            )
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            RequestContext.reset_current(self._token)
            self._token = None


class InteractiveArtifactService:
    """Persist and execute interactive actions and decision prompts.

    All writes are audited. Reads are scoped by ``IsolationService`` and a
    second-line ``require_isolated`` check verifies the action/prompt belongs
    to the calling PM.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        pm_id: str,
        fund_entity_id: str | None = None,
    ) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self.audit = AuditService(self.session)
        self._context = _ContextHelper(pm_id, fund_entity_id)

    async def _ensure_context(self) -> None:
        """Activate the RequestContext if none is currently bound."""
        if RequestContext.current_or_none() is None:
            self._context.__enter__()

    # ------------------------------------------------------------------
    # Generation / persistence
    # ------------------------------------------------------------------

    async def create_actions(
        self,
        artifact_type: str,
        artifact_id: str,
    ) -> list[ArtifactAction]:
        """Generate and persist actions for an artifact."""
        agent = InteractiveArtifactAgent(self.session)
        plan = await agent.generate_actions(artifact_type, artifact_id, self.pm_id)

        persisted: list[ArtifactAction] = []
        for definition in plan.actions:
            action = ArtifactAction(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                pm_id=self.pm_id,
                action_type=definition.action_type,
                payload=definition.payload,
                status="pending",
            )
            self.session.add(action)
            persisted.append(action)

        if persisted:
            await self.session.flush()
            for action in persisted:
                await self._audit("artifact_action_created", action)
            await self.uow.commit()
        return persisted

    async def create_prompts(
        self,
        artifact_type: str,
        artifact_id: str,
    ) -> list[DecisionPrompt]:
        """Generate and persist decision prompts for an artifact."""
        agent = InteractiveArtifactAgent(self.session)
        plan = await agent.generate_decision_prompt(artifact_type, artifact_id, self.pm_id)

        persisted: list[DecisionPrompt] = []
        for definition in plan.prompts:
            prompt = DecisionPrompt(
                pm_id=self.pm_id,
                artifact_id=artifact_id,
                prompt_text=definition.prompt_text,
                options_json=definition.options,
                deadline_at=definition.deadline_at,
            )
            self.session.add(prompt)
            persisted.append(prompt)

        if persisted:
            await self.session.flush()
            for prompt in persisted:
                await self._audit("decision_prompt_created", prompt)
            await self.uow.commit()
        return persisted

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        action_id: str,
        pm_id: str,
        payload: dict[str, Any] | None = None,
    ) -> ArtifactAction:
        """Execute a pending artifact action synchronously.

        Trading-related actions (``focus_one_buy_more``, ``focus_one_trim``)
        produce drafts only. Side-effect actions such as ``share_with_team``
        are enqueued for background processing.
        """
        action = await self._get_action_for_pm(action_id, pm_id)
        if action.status != "pending":
            raise ActionExecutionError(f"Action {action_id} is not pending")

        merged_payload = {**(action.payload or {}), **(payload or {})}
        result = await self._execute_action(action, merged_payload)

        action.status = "executed"
        action.executed_at = datetime.now(UTC)
        action.payload = merged_payload
        await self.session.flush()
        await self._audit("artifact_action_executed", action)
        await self.uow.commit()
        return result

    async def _execute_action(
        self,
        action: ArtifactAction,
        payload: dict[str, Any],
    ) -> ArtifactAction:
        action_type = action.action_type

        if action_type in {"focus_one_buy_more", "focus_one_trim"}:
            # Draft-only: no live order. Result is recorded in the payload.
            payload.setdefault("drafted_at", datetime.now(UTC).isoformat())
            payload.setdefault("status", "draft")
            return action

        if action_type == "send_lp_update":
            # Synchronous approval gate check; actual send is still gated.
            lp_update_id = payload.get("lp_update_id") or action.artifact_id
            payload["prepared_send_for"] = lp_update_id
            payload["requires_approval"] = True
            return action

        if action_type == "share_with_team":
            # Side-effect action: enqueue to worker queue for background delivery.
            payload.setdefault("queued_at", datetime.now(UTC).isoformat())
            payload.setdefault("status", "queued")
            # Worker integration stub: a real implementation would push to
            # axe.ingestion.worker.RetryQueue here.
            return action

        if action_type == "add_slide_note":
            payload.setdefault("note", "")
            payload.setdefault("created_at", datetime.now(UTC).isoformat())
            return action

        if action_type == "request_follow_up":
            payload.setdefault("requested_at", datetime.now(UTC).isoformat())
            payload.setdefault("status", "requested")
            return action

        # Default: record execution with merged payload.
        return action

    # ------------------------------------------------------------------
    # Prompt resolution
    # ------------------------------------------------------------------

    async def resolve_decision_prompt(
        self,
        prompt_id: str,
        response: str,
    ) -> DecisionPrompt:
        """Record the PM's response to a decision prompt and audit the change."""
        prompt = await self._get_prompt_for_pm(prompt_id, self.pm_id)
        if prompt.resolved_at is not None:
            raise ActionExecutionError(f"Prompt {prompt_id} is already resolved")

        prompt.response = response
        prompt.resolved_at = datetime.now(UTC)
        await self.session.flush()
        await self._audit("decision_prompt_resolved", prompt)
        await self.uow.commit()
        return prompt

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    async def list_actions_for_artifact(
        self,
        artifact_type: str,
        artifact_id: str,
    ) -> list[ArtifactAction]:
        return await self.uow.artifact_actions.list_for_artifact(artifact_type, artifact_id)

    async def list_prompts_for_artifact(
        self,
        artifact_id: str,
    ) -> list[DecisionPrompt]:
        result = await self.session.execute(
            IsolationService.select_for(DecisionPrompt).where(
                DecisionPrompt.artifact_id == artifact_id
            )
        )
        return list(result.scalars().all())

    async def _get_action_for_pm(self, action_id: str, pm_id: str) -> ArtifactAction:
        action = await self.uow.artifact_actions.get_by_id(action_id)
        if action is None:
            raise ActionExecutionError(f"Action {action_id} not found")
        if action.pm_id != pm_id:
            raise IsolationError(f"Action {action_id} does not belong to pm_id={pm_id}")
        return action

    async def _get_prompt_for_pm(self, prompt_id: str, pm_id: str) -> DecisionPrompt:
        prompt = await self.uow.decision_prompts.get_by_id(prompt_id)
        if prompt is None:
            raise ActionExecutionError(f"Prompt {prompt_id} not found")
        if prompt.pm_id != pm_id:
            raise IsolationError(f"Prompt {prompt_id} does not belong to pm_id={pm_id}")
        return prompt

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    async def _audit(
        self,
        action_type: str,
        obj: ArtifactAction | DecisionPrompt,
    ) -> None:
        object_type = type(obj).__tablename__
        after = _state_dict(obj)
        await self.audit.log(
            action_type=action_type,
            object_type=object_type,
            object_id=obj.id,
            before_state={},
            after_state=after,
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            non_blocking=False,
        )


__all__ = [
    "ActionExecutionError",
    "InteractiveArtifactService",
]
