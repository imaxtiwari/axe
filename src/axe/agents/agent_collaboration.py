"""Cross-agent collaboration bus for AXE.

Agents communicate via typed messages. The bus enforces PM/fund isolation,
writes an audit log entry for every published message, and can surface
high-value messages as ``DecisionPrompt`` artifacts.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel, Field, model_validator

from axe.db.models import AgentMessage as AgentMessageRow
from axe.db.models import DecisionPrompt
from axe.exceptions import IsolationError
from axe.security.context import RequestContext

logger = logging.getLogger(__name__)

COLLABORATION_RETENTION_DAYS = 30

AgentMessageHandler = Callable[["AgentMessage"], Awaitable[None]]


class AgentMessage(BaseModel):
    """A typed message exchanged between AXE agents."""

    id: str | None = None
    sender_agent: str
    recipient_agent: str | None = None
    sender_pm_id: str
    recipient_pm_id: str | None = None
    fund_entity_id: str
    intent: str
    payload: dict[str, Any] = Field(default_factory=dict)
    scope: str = "pm"  # pm | fund
    allowed_other_pm_ids: set[str] = Field(default_factory=set)
    required_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_decision: bool = False
    expires_at: datetime | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _normalize_scope(self) -> AgentMessage:
        if self.scope not in {"pm", "fund"}:
            raise ValueError(f"Invalid scope: {self.scope}")
        return self

    def model_dump_for_worker(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender_agent": self.sender_agent,
            "recipient_agent": self.recipient_agent,
            "sender_pm_id": self.sender_pm_id,
            "recipient_pm_id": self.recipient_pm_id,
            "fund_entity_id": self.fund_entity_id,
            "intent": self.intent,
            "payload": self.payload,
            "scope": self.scope,
            "allowed_other_pm_ids": list(self.allowed_other_pm_ids),
            "required_confidence": self.required_confidence,
            "requires_decision": self.requires_decision,
        }

    @classmethod
    def model_validate_from_worker(cls, data: dict[str, Any]) -> AgentMessage:
        copy = dict(data)
        copy["allowed_other_pm_ids"] = set(copy.get("allowed_other_pm_ids") or [])
        # Legacy worker payloads may use fund_id; normalize to fund_entity_id.
        if "fund_id" in copy and "fund_entity_id" not in copy:
            copy["fund_entity_id"] = copy.pop("fund_id")
        return cls.model_validate(copy)


class AgentCollaborationBus:
    """In-memory pub/sub + persistence-aware routing for agent messages."""

    def __init__(
        self,
        uow: Any | None = None,
        enqueue: Callable[[str, dict[str, Any], str | None], Awaitable[Any]] | None = None,
    ) -> None:
        self.uow = uow
        self.session = uow.session if uow is not None else None
        self._subscribers: dict[str, list[AgentMessageHandler]] = {}
        self._in_memory: dict[str, AgentMessage] = {}
        self._audit_repo = uow.audit if uow is not None else None
        self._enqueue = enqueue

    def subscribe(self, agent_id: str, handler: AgentMessageHandler) -> None:
        self._subscribers.setdefault(agent_id, []).append(handler)

    async def publish(self, message: AgentMessage) -> AgentMessage:
        if message.id is None:
            message.id = str(uuid.uuid4())

        self._validate_isolation(message)
        self._in_memory[message.id] = message

        if self.uow is not None:
            await self._persist_message(message)

        await self._audit_message(message, action="agent_message_published")

        target = message.recipient_agent or "*"
        await self._notify(target, message)
        if target != "*":
            await self._notify("*", message)

        if message.requires_decision and self._enqueue is not None:
            await self._enqueue(
                "route_agent_message",
                message.model_dump_for_worker(),
                message.sender_pm_id,
            )

        return message

    async def _notify(self, target: str, message: AgentMessage) -> None:
        for handler in self._subscribers.get(target, []):
            try:
                await handler(message)
            except Exception:
                logger.exception(
                    "Agent message handler failed for target=%s message=%s",
                    target,
                    message.id,
                )

    async def route_to_pm(self, message: AgentMessage) -> DecisionPrompt | None:
        if not message.requires_decision:
            return None

        if message.id is None:
            message.id = str(uuid.uuid4())

        self._validate_isolation(message)

        target_pm_id = self._resolve_target_pm_id(message)
        if target_pm_id is None:
            return None

        if self.session is not None and message.id is not None:
            existing = await self._find_existing_prompt(message.id, target_pm_id)
            if existing is not None:
                return existing

        prompt = DecisionPrompt(
            pm_id=target_pm_id,
            artifact_id=message.id,
            prompt_text=self._render_prompt_text(message),
            options_json=self._render_options(message),
        )

        if self.session is not None:
            self.session.add(prompt)
            await self.session.flush()
            await self._audit_message(message, action="agent_message_routed_to_pm")

        return prompt

    def _validate_isolation(self, message: AgentMessage) -> None:
        if not message.fund_entity_id:
            raise IsolationError("AgentMessage requires fund_entity_id")
        if not message.sender_pm_id:
            raise IsolationError("AgentMessage requires sender_pm_id")

        # Validate against ambient context only when the context belongs to the
        # message sender. This catches true cross-fund sends by the same PM
        # while ignoring stale HTTP request contexts that belong to a different
        # PM/fund than the service-layer publish.
        ctx = RequestContext.current_or_none()
        if (
            ctx is not None
            and ctx.pm_id == message.sender_pm_id
            and ctx.fund_id
            and ctx.fund_id != message.fund_entity_id
        ):
            raise IsolationError(
                f"Cross-fund agent message rejected: expected fund_id={ctx.fund_id}, "
                f"got fund_id={message.fund_entity_id}"
            )

        recipient_pm_id = message.recipient_pm_id
        if recipient_pm_id is None or recipient_pm_id == message.sender_pm_id:
            return

        if message.scope == "fund":
            return
        if recipient_pm_id in message.allowed_other_pm_ids:
            return

        raise IsolationError(
            f"Cross-PM agent message from {message.sender_pm_id} to "
            f"{recipient_pm_id} is not allow-listed"
        )

    async def recent_messages_for_pm(
        self,
        pm_id: str,
        fund_entity_id: str | None,
        *,
        limit: int = 20,
    ) -> list[AgentMessage]:
        """Return recent agent messages visible to ``pm_id`` in ``fund_entity_id``.

        Visibility rules:
          - Same-PM messages (sender or recipient).
          - Fund-scoped messages without an explicit recipient.
          - Cross-PM messages where this PM is the explicit recipient and the
            message is allow-listed or fund-scoped.
        """
        if fund_entity_id is None:
            ctx = RequestContext.current_or_none()
            if ctx is not None and ctx.fund_id:
                fund_entity_id = ctx.fund_id

        if self.session is None:
            return self._recent_in_memory(pm_id, fund_entity_id, limit=limit)

        from sqlalchemy import select

        stmt = (
            select(AgentMessageRow)
            .where(AgentMessageRow.fund_entity_id == fund_entity_id)
            .where(
                (AgentMessageRow.sender_pm_id == pm_id)
                | (AgentMessageRow.recipient_pm_id == pm_id)
                | ((AgentMessageRow.scope == "fund") & (AgentMessageRow.recipient_pm_id.is_(None)))
            )
            .where(AgentMessageRow.expires_at > datetime.now(UTC))
            .order_by(AgentMessageRow.created_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        rows = result.scalars().all()
        return [
            AgentMessage(
                id=row.id,
                sender_agent=row.sender_agent,
                recipient_agent=row.recipient_agent,
                sender_pm_id=row.sender_pm_id,
                recipient_pm_id=row.recipient_pm_id,
                fund_entity_id=row.fund_entity_id,
                intent=row.intent,
                payload=row.payload,
                scope=row.scope,
                allowed_other_pm_ids=set(row.allowed_other_pm_ids or []),
                required_confidence=row.required_confidence,
                requires_decision=row.requires_decision,
            )
            for row in rows
        ]

    def _recent_in_memory(
        self,
        pm_id: str,
        fund_entity_id: str | None,
        *,
        limit: int,
    ) -> list[AgentMessage]:
        now = datetime.now(UTC)
        filtered: list[AgentMessage] = []
        for message in self._in_memory.values():
            if fund_entity_id is not None and message.fund_entity_id != fund_entity_id:
                continue
            if message.sender_pm_id != pm_id and message.recipient_pm_id != pm_id:
                continue
            if message.expires_at is not None and message.expires_at <= now:
                continue
            filtered.append(message)
        filtered.sort(key=lambda m: m.created_at or now, reverse=True)
        return filtered[:limit]

    async def _persist_message(self, message: AgentMessage) -> None:
        if self.session is None:
            return

        row = AgentMessageRow(
            id=message.id,
            sender_agent=message.sender_agent,
            recipient_agent=message.recipient_agent,
            sender_pm_id=message.sender_pm_id,
            recipient_pm_id=message.recipient_pm_id,
            fund_entity_id=message.fund_entity_id,
            intent=message.intent,
            payload=message.payload,
            scope=message.scope,
            allowed_other_pm_ids=list(message.allowed_other_pm_ids),
            required_confidence=message.required_confidence,
            requires_decision=message.requires_decision,
            expires_at=datetime.now(UTC) + timedelta(days=COLLABORATION_RETENTION_DAYS),
        )
        self.session.add(row)
        await self.session.flush()

    async def _audit_message(self, message: AgentMessage, action: str) -> None:
        if self._audit_repo is None:
            return
        await self._audit_repo.log(
            action_type=action,
            object_type="agent_message",
            object_id=message.id or "unknown",
            before_state={},
            after_state=message.model_dump_for_worker(),
            pm_id=message.sender_pm_id,
            fund_entity_id=message.fund_entity_id,
            trace_id=message.id,
        )

    def _resolve_target_pm_id(self, message: AgentMessage) -> str | None:
        if message.recipient_pm_id:
            return message.recipient_pm_id
        ctx = RequestContext.current_or_none()
        if ctx is not None and ctx.pm_id:
            return ctx.pm_id
        return message.sender_pm_id

    async def _find_existing_prompt(
        self,
        message_id: str,
        pm_id: str,
    ) -> DecisionPrompt | None:
        if self.session is None:
            return None

        from sqlalchemy import select

        result = await self.session.execute(
            select(DecisionPrompt).where(
                DecisionPrompt.artifact_id == message_id,
                DecisionPrompt.pm_id == pm_id,
            )
        )
        return cast(DecisionPrompt | None, result.scalar_one_or_none())

    def _render_prompt_text(self, message: AgentMessage) -> str:
        intent_labels = {
            "conflict_alert": "Cross-agent conflict alert",
            "opportunity_share": "Cross-agent opportunity",
            "question_forward": "Agent question for you",
        }
        label = intent_labels.get(message.intent, "Agent message")
        summary = message.payload.get("summary") or message.payload.get("message") or ""
        parts = [f"[{label}] {message.sender_agent}"]
        if summary:
            parts.append(str(summary))
        return "\n\n".join(parts)

    def _render_options(self, message: AgentMessage) -> list[str]:
        defaults = {
            "conflict_alert": ["Acknowledge", "Escalate to team"],
            "opportunity_share": ["Review details", "Dismiss"],
            "question_forward": ["Answer", "Assign to colleague", "Dismiss"],
        }
        return defaults.get(message.intent, ["Acknowledge", "Dismiss"])


__all__ = [
    "AgentCollaborationBus",
    "AgentMessage",
    "COLLABORATION_RETENTION_DAYS",
]
