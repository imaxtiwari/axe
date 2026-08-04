"""Deal room and deal document services with isolation and audit logging."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from axe.agents.underwriting import ScenarioOutput, UnderwritingAgent
from axe.db.models import AuditLog, DealDocument, DealRoom, UnderwritingChecklist, UnderwritingScenario
from axe.db.uow import UnitOfWork
from axe.ingestion.hashing import content_hash
from axe.security.audit import _state_dict
from axe.security.context import RequestContext

T = TypeVar("T")


class _ContextHelper:
    """Bind a RequestContext when none is active; no-op otherwise."""

    def __init__(self, pm_id: str, fund_entity_id: str | None) -> None:
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self._token: Any | None = None

    def __enter__(self) -> None:
        if RequestContext.current_or_none() is None:
            self._token = RequestContext.set_current(
                RequestContext(pm_id=self.pm_id, fund_id=self.fund_entity_id)
            )

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            RequestContext.reset_current(self._token)
            self._token = None


class _DealLocks:
    """Process-wide asyncio locks keyed by deal identity."""

    _locks: dict[tuple[str, str], asyncio.Lock] = {}

    @classmethod
    def get(cls, pm_id: str, deal_id: str) -> asyncio.Lock:
        key = (pm_id, deal_id)
        if key not in cls._locks:
            cls._locks[key] = asyncio.Lock()
        return cls._locks[key]


class DealRoomService:
    """CRUD for deal rooms with fund isolation and audit logging."""

    def __init__(self, uow: UnitOfWork, pm_id: str, fund_entity_id: str) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self._context = _ContextHelper(pm_id, fund_entity_id)
        with self._context:
            pass

    async def _with_context(self, coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        with self._context:
            return await coro_factory()

    async def create_deal(
        self,
        name: str,
        *,
        stage: str = "screening",
        asset_class: str = "private_equity",
        target_ticker_or_private_name: str | None = None,
        cim_url: str | None = None,
        status: str = "active",
    ) -> DealRoom:
        deal = DealRoom(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            name=name,
            stage=stage,
            asset_class=asset_class,
            target_ticker_or_private_name=target_ticker_or_private_name,
            cim_url=cim_url,
            status=status,
        )
        self.session.add(deal)
        await self.session.flush()
        await self._audit("deal_create", deal)
        await self.uow.commit()
        return deal

    async def list_deals(self) -> list[DealRoom]:
        async def _build() -> list[DealRoom]:
            return await self.uow.deals.list_deals_for_fund(self.fund_entity_id)

        return await self._with_context(_build)

    async def get_deal(self, deal_id: str) -> DealRoom | None:
        async def _build() -> DealRoom | None:
            return await self.uow.deals.get_by_id(deal_id)

        return await self._with_context(_build)

    async def update_deal(self, deal_id: str, **changes: Any) -> DealRoom:
        async with _DealLocks.get(self.pm_id, deal_id):
            deal = await self.get_deal(deal_id)
            if deal is None:
                raise ValueError(f"Deal {deal_id} not found")
            before = _state_dict(deal)
            allowed = {
                "name",
                "stage",
                "asset_class",
                "target_ticker_or_private_name",
                "cim_url",
                "status",
            }
            for key, value in changes.items():
                if key not in allowed:
                    raise ValueError(f"Cannot update deal field {key}")
                setattr(deal, key, value)
            await self.session.flush()
            await self._audit("deal_update", deal, before)
            await self.uow.commit()
            return deal

    async def delete_deal(self, deal_id: str) -> None:
        deal = await self.get_deal(deal_id)
        if deal is None:
            raise ValueError(f"Deal {deal_id} not found")
        before = _state_dict(deal)
        await self.session.delete(deal)
        await self.session.flush()
        await self._audit("deal_delete", deal, before)
        await self.uow.commit()

    async def _audit(
        self,
        action_type: str,
        deal: DealRoom,
        before_state: dict[str, Any] | None = None,
    ) -> None:
        entry = AuditLog(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            action_type=action_type,
            object_type="deal_room",
            object_id=deal.id,
            before_state=before_state or {},
            after_state=_state_dict(deal),
        )
        self.session.add(entry)
        await self.session.flush()


class DealDocumentService:
    """Idempotent document uploads for a deal room with audit logging."""

    def __init__(self, uow: UnitOfWork, pm_id: str, fund_entity_id: str) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self._context = _ContextHelper(pm_id, fund_entity_id)
        with self._context:
            pass

    async def _with_context(self, coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        with self._context:
            return await coro_factory()

    async def upload_document(
        self,
        deal_id: str,
        source_type: str,
        file_content: bytes,
        *,
        file_path: str | None = None,
        content_url: str | None = None,
        mime_type: str | None = None,
        extracted_entities: dict[str, Any] | None = None,
    ) -> tuple[DealDocument, bool]:
        """Store a document record, deduplicating by content hash per deal.

        Returns the document and a boolean indicating whether it was newly
        created (False means an existing row with the same hash was returned).
        """
        async with _DealLocks.get(self.pm_id, deal_id):

            async def _build() -> tuple[DealDocument, bool]:
                # Verify the deal is reachable in the current isolation scope.
                deal = await self.uow.deals.get_by_id(deal_id)
                if deal is None:
                    raise ValueError(f"Deal {deal_id} not found")

                digest = content_hash(file_content.decode("utf-8", errors="replace"))
                existing = await self.uow.deal_documents.find_by_content_hash(deal_id, digest)
                if existing is not None:
                    return existing, False

                doc = DealDocument(
                    deal_id=deal_id,
                    source_type=source_type,
                    file_path=file_path,
                    content_url=content_url,
                    content_hash=digest,
                    file_size=len(file_content),
                    mime_type=mime_type,
                    extracted_entities=extracted_entities or {},
                    ingestion_status="pending",
                )
                self.session.add(doc)
                await self.session.flush()
                await self._audit("deal_document_create", doc)
                await self.uow.commit()
                return doc, True

            return await self._with_context(_build)

    async def list_documents(self, deal_id: str) -> list[DealDocument]:
        async def _build() -> list[DealDocument]:
            return await self.uow.deal_documents.list_for_deal(deal_id)

        return await self._with_context(_build)

    async def get_document(self, deal_id: str, document_id: str) -> DealDocument | None:
        async def _build() -> DealDocument | None:
            doc = await self.uow.deal_documents.get_by_id(document_id)
            if doc is None or doc.deal_id != deal_id:
                return None
            return doc

        return await self._with_context(_build)

    async def _audit(
        self,
        action_type: str,
        doc: DealDocument,
    ) -> None:
        entry = AuditLog(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            action_type=action_type,
            object_type="deal_document",
            object_id=doc.id,
            before_state={},
            after_state=_state_dict(doc),
        )
        self.session.add(entry)
        await self.session.flush()


class DealUnderwritingService:
    """Checklist generation and scenario analysis for a deal."""

    def __init__(
        self,
        uow: UnitOfWork,
        pm_id: str,
        fund_entity_id: str,
        agent: UnderwritingAgent | None = None,
    ) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self.agent = agent or UnderwritingAgent()
        self._context = _ContextHelper(pm_id, fund_entity_id)
        with self._context:
            pass

    async def _with_context(self, coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        with self._context:
            return await coro_factory()

    async def initialize_checklist(
        self,
        deal_id: str,
        vehicle_type: str,
    ) -> list[UnderwritingChecklist]:
        """Populate the default underwriting checklist for a deal."""
        async with _DealLocks.get(self.pm_id, deal_id):

            async def _build() -> list[UnderwritingChecklist]:
                deal = await self.uow.deals.get_by_id(deal_id)
                if deal is None:
                    raise ValueError(f"Deal {deal_id} not found")

                template_items = UnderwritingAgent.default_checklist(vehicle_type)
                created: list[UnderwritingChecklist] = []
                for item in template_items:
                    row = self.uow.underwriting_checklists.create_item(
                        deal_id=deal_id,
                        category=item.category,
                        question=item.question,
                        required=item.required,
                        sort_order=item.sort_order,
                    )
                    created.append(row)
                await self.session.flush()
                await self._audit(
                    "underwriting_checklist_initialized",
                    deal_id,
                    deal_id,
                    after_state={
                        "vehicle_type": vehicle_type,
                        "items": [
                            {
                                "category": it.category,
                                "question": it.question,
                                "required": it.required,
                                "sort_order": it.sort_order,
                            }
                            for it in template_items
                        ],
                    },
                )
                await self.uow.commit()
                return created

            return await self._with_context(_build)

    async def list_checklist(self, deal_id: str) -> list[UnderwritingChecklist]:
        async def _build() -> list[UnderwritingChecklist]:
            return await self.uow.underwriting_checklists.list_for_deal(deal_id)

        return await self._with_context(_build)

    async def update_checklist_item(
        self,
        deal_id: str,
        checklist_item_id: str,
        *,
        status: str,
        evidence_url: str | None = None,
        answered_by: str | None = None,
    ) -> UnderwritingChecklist:
        """Update a checklist item status."""
        if status not in UnderwritingAgent.VALID_STATUSES:
            raise ValueError(f"Invalid status {status}")

        async with _DealLocks.get(self.pm_id, deal_id):
            item = await self.uow.underwriting_checklists.get_by_id(checklist_item_id)
            if item is None or item.deal_id != deal_id:
                raise ValueError(f"Checklist item {checklist_item_id} not found")

            before = _state_dict(item)
            item.status = status
            if evidence_url is not None:
                item.evidence_url = evidence_url
            if answered_by is not None:
                item.answered_by = answered_by
            await self.session.flush()
            await self._audit(
                "underwriting_checklist_updated",
                deal_id,
                checklist_item_id,
                before_state=before,
                after_state=_state_dict(item),
            )
            await self.uow.commit()
            return item

    async def run_scenarios(
        self,
        deal_id: str,
        thesis_text: str,
        *,
        vehicle_type: str | None = None,
    ) -> tuple[ScenarioOutput, list[UnderwritingScenario]]:
        """Generate and persist scenario analysis for a deal."""
        async with _DealLocks.get(self.pm_id, deal_id):
            deal = await self.uow.deals.get_by_id(deal_id)
            if deal is None:
                raise ValueError(f"Deal {deal_id} not found")

            checklist_rows = await self.uow.underwriting_checklists.list_for_deal(deal_id)
            if not checklist_rows:
                raise ValueError("Checklist must be initialized before running scenarios")

            vehicle_type = (
                vehicle_type
                or self._infer_vehicle_type(deal.asset_class)
                or "equity"
            )

            output = await self.agent.run_scenarios(
                thesis_text=thesis_text,
                vehicle_type=vehicle_type,
                checklist=[_state_dict(row) for row in checklist_rows],
            )

            persisted: list[UnderwritingScenario] = []
            for scenario in output.scenarios:
                row = self.uow.underwriting_scenarios.create_scenario(
                    deal_id=deal_id,
                    scenario_name=scenario.scenario_name,
                    assumptions=scenario.assumptions,
                    output_metrics=scenario.output_metrics,
                    probability_weight=scenario.probability_weight,
                    confidence=scenario.confidence,
                )
                persisted.append(row)
            await self.session.flush()

            after_state: dict[str, Any] = {
                "overall_confidence": output.confidence,
                "scenario_names": [s.scenario_name for s in output.scenarios],
                "thesis_length_chars": len(thesis_text),
            }
            await self._audit(
                "underwriting_scenarios_generated",
                deal_id,
                persisted[0].id if persisted else "",
                after_state=after_state,
            )
            await self.uow.commit()
            return output, persisted

    async def list_scenarios(self, deal_id: str) -> list[UnderwritingScenario]:
        async def _build() -> list[UnderwritingScenario]:
            return await self.uow.underwriting_scenarios.list_for_deal(deal_id)

        return await self._with_context(_build)

    @staticmethod
    def _infer_vehicle_type(asset_class: str | None) -> str | None:
        """Map a deal asset_class to a supported underwriting vehicle type."""
        mapping = {
            "credit": "credit",
            "private_credit": "credit",
            "pe": "equity",
            "private_equity": "equity",
            "vc": "equity",
            "lp": "lp_gp",
            "lp_gp": "lp_gp",
            "fund_commitment": "lp_gp",
        }
        return mapping.get((asset_class or "").lower())

    async def _audit(
        self,
        action_type: str,
        deal_id: str,
        object_id: str,
        *,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
    ) -> None:
        entry = AuditLog(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            action_type=action_type,
            object_type="deal_room",
            object_id=object_id or deal_id,
            before_state=before_state or {},
            after_state=after_state or {},
        )
        self.session.add(entry)
        await self.session.flush()
