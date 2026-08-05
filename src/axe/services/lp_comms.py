"""LP communication service: draft, approve, send and archive LP updates."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select

from axe.agents.llm import LLMProvider, get_default_provider
from axe.agents.lp_update import ComplianceGateError, LPUpdateAgent, send_lp_update
from axe.db.models import (
    CommunicationArchive,
    InvestmentVehicle,
    LPRelationship,
    LPUpdate,
)
from axe.db.uow import UnitOfWork
from axe.security.audit import _state_dict
from axe.security.context import RequestContext


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


class LPCommsService:
    """Draft, send, and archive LP updates with audit and isolation."""

    def __init__(
        self,
        uow: UnitOfWork,
        pm_id: str,
        fund_entity_id: str,
        provider: LLMProvider | None = None,
    ) -> None:
        self.uow = uow
        self.session = uow.session
        self.pm_id = pm_id
        self.fund_entity_id = fund_entity_id
        self.provider = provider or get_default_provider()
        self._context = _ContextHelper(pm_id, fund_entity_id)
        with self._context:
            pass

    async def draft_update(
        self,
        vehicle_id: str,
        quarter: str,
        *,
        activity: dict[str, Any] | None = None,
    ) -> LPUpdate:
        """Generate and persist a draft LP update for a vehicle and quarter."""
        # Ensure the vehicle belongs to the current fund.
        vehicle = await self.session.get(InvestmentVehicle, vehicle_id)
        if vehicle is None or vehicle.fund_entity_id != self.fund_entity_id:
            raise ValueError(f"Vehicle {vehicle_id} not found")

        agent = LPUpdateAgent(
            self.session,
            provider=self.provider,
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
        )
        update = await agent.draft_update(
            vehicle_id=vehicle_id,
            quarter=quarter,
            activity=activity,
        )
        await self.session.flush()
        await self._audit("lp_update_drafted", update)
        await self.uow.commit()
        return update

    async def get_update(self, update_id: str) -> LPUpdate | None:
        """Fetch a single LP update if it belongs to the current fund scope."""
        update = await self.session.get(LPUpdate, update_id)
        if update is None:
            return None
        vehicle = await self.session.get(InvestmentVehicle, update.vehicle_id)
        if vehicle is None or vehicle.fund_entity_id != self.fund_entity_id:
            return None
        return update

    async def approve_update(
        self,
        update_id: str,
        approved_by: str,
    ) -> LPUpdate:
        """Mark an LP update as approved and ready to send."""
        update = await self.get_update(update_id)
        if update is None:
            raise ValueError(f"LP update {update_id} not found")
        if update.status != "draft":
            raise ValueError(f"LP update {update_id} is not in draft status")
        update.status = "approved"
        update.approved_by = approved_by
        await self.session.flush()
        await self._audit("lp_update_approved", update)
        await self.uow.commit()
        return update

    async def send_update(
        self,
        update_id: str,
        approved_by: str,
        *,
        sent_at: datetime | None = None,
    ) -> LPUpdate:
        """Send an approved LP update and archive it.

        The actual email delivery is a no-op; AXE records the final content and
        recipient list in the ``CommunicationArchive`` for compliance.
        """
        update = await self.get_update(update_id)
        if update is None:
            raise ValueError(f"LP update {update_id} not found")

        update = await send_lp_update(update, approved_by=approved_by, sent_at=sent_at)
        await self.session.flush()

        archive = await self._archive(update)
        await self.session.flush()

        await self._audit(
            "lp_update_sent",
            update,
            extra_state={"archive_id": archive.id},
        )
        await self.uow.commit()
        return update

    async def list_updates_for_vehicle(self, vehicle_id: str) -> list[LPUpdate]:
        vehicle = await self.session.get(InvestmentVehicle, vehicle_id)
        if vehicle is None or vehicle.fund_entity_id != self.fund_entity_id:
            raise ValueError(f"Vehicle {vehicle_id} not found")

        result = await self.session.execute(
            select(LPUpdate)
            .where(LPUpdate.vehicle_id == vehicle_id)
            .order_by(desc(LPUpdate.created_at))
        )
        return list(result.scalars().all())

    async def _archive(self, update: LPUpdate) -> CommunicationArchive:
        """Persist the final LP update content to the communication archive."""
        raw_content = (update.content_md or "").strip()
        if not raw_content:
            raw_content = str(update.sections)
        content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()

        result = await self.session.execute(
            select(LPRelationship.contact_email).where(
                LPRelationship.vehicle_id == update.vehicle_id,
                LPRelationship.contact_email.is_not(None),
            )
        )
        recipient_emails = list(result.scalars().all())

        archive = CommunicationArchive(
            pm_id=self.pm_id,
            channel="lp_update",
            message_type="quarterly_letter",
            content_hash=content_hash,
            raw_content=raw_content,
            sent_at=update.sent_at,
        )
        # Store recipient list in the JSON-friendly state via audit only to keep
        # the archive row schema simple. It could also be written to a dedicated
        # column when one is added.
        self.session.add(archive)
        await self.session.flush()

        # Update the archive with recipient metadata stored in archive_metadata.
        archive.archive_metadata = {
            "recipients": recipient_emails,
            "lp_update_id": update.id,
            "vehicle_id": update.vehicle_id,
            "quarter": update.quarter,
        }
        return archive

    async def _audit(
        self,
        action_type: str,
        update: LPUpdate,
        *,
        extra_state: dict[str, Any] | None = None,
    ) -> None:
        from axe.db.models import AuditLog

        after = _state_dict(update)
        if extra_state:
            after.update(extra_state)
        entry = AuditLog(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            action_type=action_type,
            object_type="lp_update",
            object_id=update.id,
            before_state={},
            after_state=after,
        )
        self.session.add(entry)
        await self.session.flush()


__all__ = ["ComplianceGateError", "LPCommsService"]
