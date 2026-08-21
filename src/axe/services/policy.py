"""Fund-scoped policy engine for guardrails and compliance automation.

``PolicyEngine.evaluate`` matches an artifact or event against enabled
``PolicyRule`` rows and returns a list of ``PolicyAction`` recommendations.
CRUD helpers make it easy to create, read, update, and disable rules per fund.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from axe.config import Settings, get_settings
from axe.db.models import PolicyRule
from axe.db.uow import UnitOfWork


class PolicyEvent(BaseModel):
    """Input event evaluated by the policy engine."""

    pm_id: str | None = Field(default=None, description="PM who generated the artifact.")
    fund_entity_id: str | None = Field(
        default=None, description="Fund entity scope for the rule lookup."
    )
    artifact_type: str = Field(default="unknown", description="Type of artifact being checked.")
    content: str = Field(default="", description="Text content of the artifact.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extra structured context.")


class PolicyAction(BaseModel):
    """Action recommended by a matched policy rule."""

    rule_id: str
    rule_type: str
    action: str
    severity: str = "medium"  # low|medium|high|critical
    message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyEngine:
    """Evaluate policy rules and provide CRUD helpers."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def evaluate(
        self,
        event: PolicyEvent,
        uow: UnitOfWork | None = None,
    ) -> list[PolicyAction]:
        """Return actions triggered by ``event`` against enabled fund rules."""
        if uow is None:
            return []

        rules = await uow.policy_rules.list_enabled()
        if event.fund_entity_id is not None:
            rules = [r for r in rules if r.fund_entity_id == event.fund_entity_id]

        actions: list[PolicyAction] = []
        for rule in rules:
            if not self._matches(rule, event):
                continue
            actions.append(
                PolicyAction(
                    rule_id=rule.id,
                    rule_type=rule.rule_type,
                    action=rule.action,
                    severity=rule.conditions_json.get("severity", "medium"),
                    message=rule.conditions_json.get("message"),
                    metadata={"scope": rule.scope, "priority": rule.priority},
                )
            )
        return actions

    @staticmethod
    def _matches(rule: PolicyRule, event: PolicyEvent) -> bool:
        """Return True when ``event`` satisfies the rule conditions."""
        conditions = rule.conditions_json or {}
        content = event.content.lower()

        # Artifact type condition
        if (
            "artifact_types" in conditions
            and event.artifact_type not in conditions["artifact_types"]
        ):
            return False

        # Keyword / phrase presence condition
        if "contains_any" in conditions:
            terms = conditions["contains_any"]
            if not any(str(term).lower() in content for term in terms):
                return False

        if "contains_all" in conditions:
            terms = conditions["contains_all"]
            if not all(str(term).lower() in content for term in terms):
                return False

        # Metadata equality condition (exact key matches)
        if "metadata" in conditions:
            for key, value in conditions["metadata"].items():
                if event.metadata.get(key) != value:
                    return False

        return True

    async def create_rule(
        self,
        uow: UnitOfWork,
        *,
        fund_entity_id: str,
        rule_type: str,
        scope: str,
        action: str,
        conditions_json: dict[str, Any] | None = None,
        priority: int = 0,
        enabled: bool = True,
    ) -> PolicyRule:
        """Create a new policy rule."""
        return uow.policy_rules.create_rule(
            fund_entity_id=fund_entity_id,
            rule_type=rule_type,
            scope=scope,
            action=action,
            conditions_json=conditions_json or {},
            priority=priority,
            enabled=enabled,
        )

    async def get_rule(self, uow: UnitOfWork, rule_id: str) -> PolicyRule | None:
        """Fetch a single policy rule by id."""
        return await uow.policy_rules.get_by_id(rule_id)

    async def list_rules(
        self,
        uow: UnitOfWork,
        *,
        enabled_only: bool = False,
    ) -> list[PolicyRule]:
        """List rules for the current fund scope."""
        if enabled_only:
            return await uow.policy_rules.list_enabled()
        return await uow.policy_rules.list_for_fund()

    async def update_rule(
        self,
        uow: UnitOfWork,
        rule_id: str,
        **changes: Any,
    ) -> PolicyRule | None:
        """Update mutable fields on a policy rule."""
        rule = await uow.policy_rules.get_by_id(rule_id)
        if rule is None:
            return None

        allowed = {"rule_type", "scope", "action", "conditions_json", "priority", "enabled"}
        for key, value in changes.items():
            if key in allowed:
                setattr(rule, key, value)
        return rule

    async def delete_rule(self, uow: UnitOfWork, rule_id: str) -> bool:
        """Disable a policy rule (soft delete)."""
        rule = await uow.policy_rules.get_by_id(rule_id)
        if rule is None:
            return False
        rule.enabled = False
        return True


__all__ = ["PolicyEngine", "PolicyEvent", "PolicyAction"]
