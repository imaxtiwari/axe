"""PersonaService orchestrates memory mining, persona synthesis, and persistence.

The service is intentionally thin: it wires ``MemoryMinerAgent`` and
``PersonaAgent`` to the Unit-of-Work repositories, persists mined citations,
peer maps, and the synthesized ``PMPersona``, and exposes a small public API for
routers and scheduled jobs.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import cast

from axe.agents.memory_miner import MemoryMinerAgent, MinedCitation, MinedPeer, SourceFetcher
from axe.agents.persona import PersonaAgent
from axe.agents.persona_models import PersonaStyleSnapshot
from axe.config import Settings, get_settings
from axe.db.models import PMPersona
from axe.db.uow import UnitOfWork
from axe.security.context import RequestContext

logger = logging.getLogger(__name__)


class PersonaService:
    """Orchestrate mining + synthesis and persist persona artifacts."""

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        miner: MemoryMinerAgent | None = None,
        persona_agent: PersonaAgent | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.uow = uow
        self.settings = settings or get_settings()
        self.miner = miner or MemoryMinerAgent(settings=self.settings)
        self.persona_agent = persona_agent or PersonaAgent()

    @classmethod
    def with_fetchers(
        cls,
        uow: UnitOfWork,
        fetchers: dict[str, SourceFetcher],
        *,
        settings: Settings | None = None,
    ) -> PersonaService:
        """Create a service with an explicit set of fetchers (useful for tests)."""
        settings = settings or get_settings()
        miner = MemoryMinerAgent(settings=settings, fetchers=fetchers)
        return cls(uow, miner=miner, settings=settings)

    async def refresh_persona(
        self,
        pm_id: str,
        *,
        lookback_days: int | None = None,
        include_dms: bool | None = None,
        allowed_dm_participants: set[str] | None = None,
    ) -> PersonaStyleSnapshot:
        """Mine history, synthesize a persona, and persist everything.

        Returns the synthesized snapshot. The previous ``PMPersona`` row for the
        PM is soft-deleted by replacement: we insert a new row and rely on the
        unique constraint on ``pm_id`` to raise if a concurrent refresh wins.
        """
        citations, peers = await self.miner.mine(
            pm_id,
            lookback_days=lookback_days,
            include_dms=include_dms,
            allowed_dm_participants=allowed_dm_participants,
        )

        snapshot = await self.persona_agent.synthesize(pm_id, citations, peers)
        await self._persist(snapshot, citations, peers)
        return snapshot

    async def get_current_persona(self, pm_id: str) -> PersonaStyleSnapshot | None:
        """Return the latest persisted persona snapshot for ``pm_id``."""
        model = await self.uow.pm_personas.get_current()
        if model is None or model.pm_id != pm_id:
            return None
        return PersonaAgent.snapshot_from_model(model)

    async def delete_persona_and_mined_data(self, pm_id: str) -> int:
        """Delete the current PM's persona, citations, and peer maps.

        Returns the total number of rows deleted.
        """
        persona = await self.uow.pm_personas.get_current()
        if persona is not None and persona.pm_id == pm_id:
            await self.uow.session.delete(persona)

        citations = await self.uow.memory_citations.list_for_pm()
        for citation in citations:
            if citation.pm_id == pm_id:
                await self.uow.session.delete(citation)

        peers = await self.uow.pm_peer_maps.list_for_pm()
        for peer in peers:
            if peer.pm_id == pm_id:
                await self.uow.session.delete(peer)

        await self.uow.commit()
        return (
            (1 if persona is not None and persona.pm_id == pm_id else 0)
            + sum(1 for c in citations if c.pm_id == pm_id)
            + sum(1 for p in peers if p.pm_id == pm_id)
        )

    async def _persist(
        self,
        snapshot: PersonaStyleSnapshot,
        citations: list[MinedCitation],
        peers: list[MinedPeer],
    ) -> PMPersona:
        """Persist persona, citations, and peers within the current UoW."""
        # Delete previous persona row for this PM so we always have one current row.
        existing = await self.uow.pm_personas.get_current()
        if existing is not None and existing.pm_id == snapshot.pm_id:
            await self.uow.session.delete(existing)
            await self.uow.session.flush()

        persona_model = PersonaAgent.model_from_snapshot(snapshot, PMPersona)
        self.uow.pm_personas.create_persona(
            id=persona_model.id,
            pm_id=persona_model.pm_id,
            writing_style_summary=persona_model.writing_style_summary,
            decision_triggers=persona_model.decision_triggers,
            peer_relationships_json=persona_model.peer_relationships_json,
            trusted_sources=persona_model.trusted_sources,
            confidence_language=persona_model.confidence_language,
            last_refreshed_at=persona_model.last_refreshed_at,
        )

        for citation in citations:
            self.uow.memory_citations.create_citation(
                pm_id=snapshot.pm_id or "",
                source_type=citation.source_type,
                source_id=citation.source_id,
                snippet=citation.snippet,
                linked_ticker=citation.linked_ticker,
                linked_deal_id=citation.linked_deal_id,
                sentiment=citation.sentiment,
                extracted_at=dt.datetime.now(dt.UTC),
            )

        for peer in peers:
            existing_peer = await self.uow.pm_peer_maps.get_by_peer_id(peer.peer_id)
            if existing_peer is not None:
                # Merge topics and keep highest trust level.
                merged_topics = list(set(existing_peer.topics + peer.topics))
                trust_rank = {"high": 3, "medium": 2, "low": 1, None: 0}
                trust = (
                    existing_peer.trust_level
                    if trust_rank.get(existing_peer.trust_level, 0)
                    >= trust_rank.get(peer.trust_level, 0)
                    else peer.trust_level
                )
                existing_peer.peer_name = peer.peer_name or existing_peer.peer_name
                existing_peer.relationship_type = (
                    peer.relationship_type or existing_peer.relationship_type
                )
                existing_peer.interaction_frequency = (
                    peer.interaction_frequency or existing_peer.interaction_frequency
                )
                existing_peer.topics = merged_topics
                existing_peer.trust_level = trust
            else:
                self.uow.pm_peer_maps.create_peer(
                    pm_id=snapshot.pm_id or "",
                    peer_email_or_slack_id=peer.peer_id,
                    peer_name=peer.peer_name,
                    relationship_type=peer.relationship_type,
                    interaction_frequency=peer.interaction_frequency,
                    topics=peer.topics,
                    trust_level=peer.trust_level,
                )

        await self.uow.commit()
        return cast(PMPersona, persona_model)


async def refresh_persona_for_pm(
    pm_id: str,
    *,
    fund_id: str | None = None,
    fetchers: dict[str, SourceFetcher] | None = None,
    settings: Settings | None = None,
) -> PersonaStyleSnapshot:
    """Standalone helper for scheduled jobs / background workers.

    Binds a ``RequestContext`` so isolation works outside of HTTP requests.
    """
    settings = settings or get_settings()
    ctx = RequestContext(pm_id=pm_id, fund_id=fund_id, role="system")
    token = RequestContext.set_current(ctx)
    try:
        async with UnitOfWork() as uow:
            if fetchers:
                service = PersonaService.with_fetchers(uow, fetchers, settings=settings)
            else:
                service = PersonaService(uow, settings=settings)
            return await service.refresh_persona(pm_id)
    finally:
        RequestContext.reset_current(token)


__all__ = [
    "PersonaService",
    "refresh_persona_for_pm",
]
