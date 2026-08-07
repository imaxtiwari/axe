"""Shared Pydantic models for PM persona snapshots and rendering helpers.

These models are used by the memory miner, persona agent, and artifact
personalization hooks. They are intentionally separate from the SQLAlchemy
ORM so agents can pass lightweight snapshots around without a database
session.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PeerRelationshipSnapshot(BaseModel):
    """A lightweight peer relationship derived from PMPeerMap."""

    peer_id: str = Field(..., description="email or slack id")
    peer_name: str | None = None
    relationship_type: str | None = None
    interaction_frequency: str | None = None
    topics: list[str] = Field(default_factory=list)
    trust_level: str | None = None


class PersonaStyleSnapshot(BaseModel):
    """Serializable PM writing/decision persona used to personalize artifacts."""

    persona_id: str | None = None
    pm_id: str | None = None
    writing_style_summary: str | None = None
    decision_triggers: dict[str, Any] = Field(default_factory=dict)
    trusted_sources: list[str] = Field(default_factory=list)
    confidence_language: str | None = None
    peer_relationships: list[PeerRelationshipSnapshot] = Field(default_factory=list)

    def render_system_prompt_snippet(self) -> str:
        """Return a compact system-prompt sized description of this persona."""
        lines: list[str] = []
        if self.writing_style_summary:
            lines.append(f"Writing style: {self.writing_style_summary}")
        triggers = self.decision_triggers
        if triggers:
            items = "; ".join(f"{k}: {v}" for k, v in triggers.items())
            lines.append(f"Decision triggers: {items}")
        if self.confidence_language:
            lines.append(f"Confidence language: {self.confidence_language}")
        if self.trusted_sources:
            lines.append(f"Trusted sources: {', '.join(self.trusted_sources)}")
        if self.peer_relationships:
            peers = ", ".join(
                f"{p.peer_name or p.peer_id} ({p.relationship_type or 'peer'})"
                for p in self.peer_relationships[:5]
            )
            lines.append(f"Key peers: {peers}")
        return "\n".join(lines) if lines else "No persona snapshot available."


__all__ = ["PeerRelationshipSnapshot", "PersonaStyleSnapshot"]
