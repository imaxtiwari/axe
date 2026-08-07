"""Persona agent: synthesize a PMPersona snapshot from mined citations and peers."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from axe.agents.llm import LLMProvider, get_default_provider
from axe.agents.memory_miner import MinedCitation, MinedPeer
from axe.agents.persona_models import PeerRelationshipSnapshot, PersonaStyleSnapshot

logger = logging.getLogger(__name__)


class _PersonaSynthesis(BaseModel):
    """Structured response from the persona synthesis LLM pass."""

    writing_style_summary: str | None = None
    decision_triggers: dict[str, Any] = Field(default_factory=dict)
    trusted_sources: list[str] = Field(default_factory=list)
    confidence_language: str | None = None


class PersonaAgent:
    """Synthesize a persona snapshot from mined citations and peer maps."""

    SYSTEM_PROMPT = (
        "You are a persona synthesis assistant. Given a portfolio manager's recent "
        "communication citations and peer relationships, infer a concise writing and "
        "decision persona. Output JSON matching the schema."
    )

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm or get_default_provider()

    async def synthesize(
        self,
        pm_id: str,
        citations: list[MinedCitation],
        peers: list[MinedPeer],
    ) -> PersonaStyleSnapshot:
        """Return a synthesized persona snapshot for ``pm_id``."""
        synthesis = await self._synthesize_persona(pm_id, citations, peers)
        peer_snapshots = [
            PeerRelationshipSnapshot(
                peer_id=p.peer_id,
                peer_name=p.peer_name,
                relationship_type=p.relationship_type,
                interaction_frequency=p.interaction_frequency,
                topics=p.topics,
                trust_level=p.trust_level,
            )
            for p in peers
        ]
        return PersonaStyleSnapshot(
            persona_id=str(uuid.uuid4()),
            pm_id=pm_id,
            writing_style_summary=synthesis.writing_style_summary,
            decision_triggers=synthesis.decision_triggers,
            trusted_sources=synthesis.trusted_sources,
            confidence_language=synthesis.confidence_language,
            peer_relationships=peer_snapshots,
        )

    async def _synthesize_persona(
        self,
        pm_id: str,
        citations: list[MinedCitation],
        peers: list[MinedPeer],
    ) -> _PersonaSynthesis:
        prompt = self._build_synthesis_prompt(pm_id, citations, peers)
        try:
            response = await self.llm.complete(
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_schema=_PersonaSynthesis,
            )
        except Exception:
            logger.exception("Persona synthesis LLM failed for pm=%s", pm_id)
            return _PersonaSynthesis()

        parsed = response.parsed
        if not isinstance(parsed, dict):
            return _PersonaSynthesis()

        try:
            return _PersonaSynthesis(**parsed)
        except Exception:
            logger.warning("Invalid persona synthesis dropped: %s", parsed)
            return _PersonaSynthesis()

    @staticmethod
    def _build_synthesis_prompt(
        pm_id: str,
        citations: list[MinedCitation],
        peers: list[MinedPeer],
    ) -> str:
        lines: list[str] = [
            f"Synthesize a persona for portfolio manager {pm_id} based on the following "
            "mined communication history."
        ]
        lines.append("\nCitations:")
        for idx, c in enumerate(citations[:25], 1):
            lines.append(
                f"[{idx}] {c.source_type} | ticker={c.linked_ticker or 'N/A'} | "
                f"sentiment={c.sentiment or 'N/A'} | topics={', '.join(c.topics)}\n{c.snippet}"
            )

        lines.append("\nPeers:")
        for idx, p in enumerate(peers[:15], 1):
            lines.append(
                f"[{idx}] {p.peer_name or p.peer_id} ({p.relationship_type or 'unknown'}) | "
                f"frequency={p.interaction_frequency or 'N/A'} | "
                f"trust={p.trust_level or 'N/A'} | topics={', '.join(p.topics)}"
            )

        lines.append(
            "\nReturn JSON with:\n"
            "- writing_style_summary: a 1-2 sentence description of the PM's writing style\n"
            "- decision_triggers: an object mapping trigger labels to short descriptions\n"
            "- trusted_sources: a list of source names or types the PM seems to trust most\n"
            "- confidence_language: a short phrase describing how the PM expresses conviction"
        )
        return "\n".join(lines)

    @staticmethod
    def snapshot_from_model(model: Any) -> PersonaStyleSnapshot:
        """Convert a SQLAlchemy ``PMPersona`` row into a snapshot.

        The ``model`` argument is typed as ``Any`` to avoid importing the ORM class
        into this module.
        """
        raw_peers = getattr(model, "peer_relationships_json", None) or {}
        if not isinstance(raw_peers, dict):
            raw_peers = {}
        peer_list = raw_peers.get("peers", [])
        if not isinstance(peer_list, list):
            peer_list = []

        peers = []
        for item in peer_list:
            if isinstance(item, dict):
                try:
                    peers.append(PeerRelationshipSnapshot(**item))
                except Exception:
                    logger.warning("Invalid peer snapshot dropped: %s", item)

        return PersonaStyleSnapshot(
            persona_id=getattr(model, "id", None),
            pm_id=getattr(model, "pm_id", None),
            writing_style_summary=getattr(model, "writing_style_summary", None),
            decision_triggers=getattr(model, "decision_triggers", None) or {},
            trusted_sources=getattr(model, "trusted_sources", None) or [],
            confidence_language=getattr(model, "confidence_language", None),
            peer_relationships=peers,
        )

    @staticmethod
    def model_from_snapshot(
        snapshot: PersonaStyleSnapshot,
        model_cls: Any,
    ) -> Any:
        """Convert a snapshot into a new SQLAlchemy ``PMPersona`` instance.

        ``model_cls`` is expected to be ``axe.db.models.PMPersona``.
        """
        return model_cls(
            id=snapshot.persona_id or str(uuid.uuid4()),
            pm_id=snapshot.pm_id or "",
            writing_style_summary=snapshot.writing_style_summary,
            decision_triggers=snapshot.decision_triggers or {},
            peer_relationships_json={
                "peers": [p.model_dump() for p in snapshot.peer_relationships]
            },
            trusted_sources=snapshot.trusted_sources or [],
            confidence_language=snapshot.confidence_language,
            last_refreshed_at=datetime.now(UTC),
        )


__all__ = ["PersonaAgent", "_PersonaSynthesis"]
