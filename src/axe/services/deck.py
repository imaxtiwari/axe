"""Service layer for deal deck generation."""

from __future__ import annotations

from typing import Any

from axe.agents.deck import DeckBuilderAgent
from axe.db.models import AuditLog, DealThesisVersion, DeckOutput
from axe.db.uow import UnitOfWork
from axe.security.audit import _state_dict
from axe.security.context import RequestContext


class DealDeckService:
    """Generate and persist deal decks from a DealThesisVersion.

    The service wires the UoW, RequestContext isolation, and audit logging.
    Deck generation is deterministic: the same thesis/version + vehicle type
    produces the same slide structure and bullet text.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        pm_id: str,
        fund_entity_id: str,
    ) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self._context = _ContextHelper(pm_id, fund_entity_id)
        with self._context:
            pass

    async def generate_deck(
        self,
        deal_id: str,
        *,
        vehicle_type: str | None = None,
        title: str | None = None,
        source_data: dict[str, Any] | None = None,
    ) -> DeckOutput:
        """Generate a DeckOutput from the latest deal thesis version."""
        with self._context:
            deal = await self.uow.deals.get_by_id(deal_id)
            if deal is None:
                raise ValueError(f"Deal {deal_id} not found")

            thesis = await self.uow.deal_theses.get_latest_for_deal(deal_id)
            if thesis is None:
                raise ValueError(f"No deal thesis found for deal {deal_id}")

            agent = DeckBuilderAgent(self.session)
            output = await agent.build_deck_from_thesis(
                pm_id=self.pm_id,
                thesis=thesis,
                vehicle_type=vehicle_type or deal.asset_class,
                title=title,
                source_data=source_data,
            )
            await self.session.flush()
            await self._audit("deck_output_created", output)
            await self.uow.commit()
            return output

    async def generate_deck_from_thesis(
        self,
        thesis_id: str,
        *,
        vehicle_type: str | None = None,
        title: str | None = None,
        source_data: dict[str, Any] | None = None,
    ) -> DeckOutput:
        """Generate a DeckOutput from a specific deal thesis version."""
        with self._context:
            thesis = await self.uow.deal_theses.get_by_id(thesis_id)
            if thesis is None:
                raise ValueError(f"Thesis {thesis_id} not found")

            deal = await self.uow.deals.get_by_id(thesis.deal_id)
            if deal is None:
                raise ValueError(f"Deal {thesis.deal_id} not found")

            agent = DeckBuilderAgent(self.session)
            output = await agent.build_deck_from_thesis(
                pm_id=self.pm_id,
                thesis=thesis,
                vehicle_type=vehicle_type or deal.asset_class,
                title=title,
                source_data=source_data,
            )
            await self.session.flush()
            await self._audit("deck_output_created", output)
            await self.uow.commit()
            return output

    async def _audit(
        self,
        action_type: str,
        output: DeckOutput,
    ) -> None:
        after = _state_dict(output)
        entry = AuditLog(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            action_type=action_type,
            object_type="deck_output",
            object_id=output.id,
            before_state={},
            after_state=after,
        )
        self.session.add(entry)
        await self.session.flush()


class _ContextHelper:
    """Bind a RequestContext when none is active; no-op otherwise."""

    def __init__(self, pm_id: str, fund_entity_id: str | None) -> None:
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self._token: Any | None = None

    def __enter__(self) -> "_ContextHelper":
        if RequestContext.current_or_none() is None:
            self._token = RequestContext.set_current(
                RequestContext(pm_id=self.pm_id, fund_id=self.fund_entity_id)
            )
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            RequestContext.reset_current(self._token)
            self._token = None
