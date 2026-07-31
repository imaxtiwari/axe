"""Tests for IC memo deck-builder and LP update agents."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.agents.fund_comms import (
    ComplianceGateError,
    DeckBuilderAgent,
    LPUpdateAgent,
    send_lp_update,
)
from axe.db.models import DeckOutput, DeckTemplate, FundEntity, InvestmentVehicle, LPUpdate, PMUser


@pytest_asyncio.fixture
async def pm_and_vehicle(db_session: AsyncSession):
    fund = FundEntity(legal_name="AXE Test Fund")
    db_session.add(fund)
    await db_session.flush()

    pm = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        email="pm@axe.fund",
    )
    db_session.add(pm)
    await db_session.flush()

    vehicle = InvestmentVehicle(
        id=str(uuid.uuid4()),
        fund_entity_id=fund.id,
        name="AXE Venture I",
        legal_entity="AXE Venture I LP",
        strategy="Growth equity",
        vintage=2024,
        currency="USD",
    )
    db_session.add(vehicle)
    await db_session.flush()

    return pm, vehicle


@pytest_asyncio.fixture
async def ic_template(db_session: AsyncSession, pm_and_vehicle):
    template = DeckTemplate(
        id=str(uuid.uuid4()),
        name="Public Equity IC Memo",
        asset_class="public_equity",
        audience="ic_committee",
        structure=[
            {"title": "Investment Thesis", "bullets": ["Position summary", "Key catalysts"]},
            {"title": "Risks", "bullets": ["Downside scenarios"]},
            {"title": "Valuation", "bullets": ["Model outputs"]},
        ],
    )
    db_session.add(template)
    await db_session.flush()
    return template


async def test_deck_builder_uses_template(
    db_session: AsyncSession,
    pm_and_vehicle,
    ic_template,
) -> None:
    """DeckBuilderAgent selects matching template and persists a versioned output."""
    pm, _vehicle = pm_and_vehicle
    agent = DeckBuilderAgent(db_session)

    template = await agent.select_template(asset_class="public_equity", audience="ic_committee")
    assert template is not None
    assert template.id == ic_template.id

    output = await agent.build_deck(
        pm_id=pm.id,
        asset_class="public_equity",
        audience="ic_committee",
        title="NVDA Long Thesis",
        source_ids=["internal_model_001", "transcript_042"],
        source_data={"ticker": "NVDA", "position_size": 5.0},
    )

    assert isinstance(output, DeckOutput)
    assert output.pm_id == pm.id
    assert output.type == "ic_memo"
    assert output.content["template_id"] == ic_template.id
    assert output.content["title"] == "NVDA Long Thesis"

    sections = output.content["sections"]
    titles = [s["title"] for s in sections]
    assert "Investment Thesis" in titles
    assert "Risks" in titles
    assert "Valuation" in titles

    footer = output.content["footer"]
    assert "Draft — internal only." in footer
    assert "internal_model_001" in footer
    assert "transcript_042" in footer
    assert "markdown" in output.content

    result = await db_session.execute(select(DeckOutput).where(DeckOutput.id == output.id))
    stored = result.scalar_one()
    assert stored.content["title"] == "NVDA Long Thesis"


async def test_lp_update_sections_present(
    db_session: AsyncSession,
    pm_and_vehicle,
) -> None:
    """LPUpdateAgent drafts a quarterly letter with all required sections and footer."""
    _pm, vehicle = pm_and_vehicle
    agent = LPUpdateAgent(db_session)

    update = await agent.draft_update(
        vehicle_id=vehicle.id,
        quarter="2026-Q2",
        activity={"sources": ["Fund admin report", "Portfolio dashboard"]},
    )

    assert isinstance(update, LPUpdate)
    assert update.vehicle_id == vehicle.id
    assert update.quarter == "2026-Q2"
    assert update.status == "draft"

    headings = [section["heading"] for section in update.sections]
    for required in LPUpdateAgent.REQUIRED_SECTIONS:
        assert required in headings
    assert "Footer" in headings

    footer = next(section["body"] for section in update.sections if section["heading"] == "Footer")
    assert "Draft — internal only." in footer
    assert "Fund admin report" in footer
    assert "Portfolio dashboard" in footer

    result = await db_session.execute(select(LPUpdate).where(LPUpdate.id == update.id))
    stored = result.scalar_one()
    assert stored.status == "draft"


async def test_lp_update_blocks_auto_send(
    db_session: AsyncSession,
    pm_and_vehicle,
) -> None:
    """AXE cannot send LP updates without human approval; auto-send is blocked."""
    _pm, vehicle = pm_and_vehicle
    agent = LPUpdateAgent(db_session)

    update = await agent.draft_update(vehicle_id=vehicle.id, quarter="2026-Q2")

    with pytest.raises(ComplianceGateError):
        await send_lp_update(update, approved_by=None)

    with pytest.raises(ComplianceGateError):
        update.status = "pending_review"
        await send_lp_update(update, approved_by="compliance_officer_1")

    update.status = "approved"
    approved = await send_lp_update(update, approved_by="compliance_officer_1")
    assert approved.status == "sent"
    assert approved.approved_by == "compliance_officer_1"
    assert approved.sent_at is not None
