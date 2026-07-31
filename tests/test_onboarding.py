"""Tests for the AXE onboarding state machine and cold-start profile generation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import FundEntity, PMMemory, PMMemoryColdStart, PMUser
from axe.routers.onboarding import router as onboarding_router
from axe.services.onboarding import (
    COLD_START_PROMPTS,
    THESIS_CAPTURE_PROMPT,
    OnboardingService,
)

# pylint: disable=redefined-outer-name


async def _fund_entity(session: AsyncSession) -> FundEntity:
    fund = FundEntity(
        id=str(uuid.uuid4()),
        legal_name=f"Test Fund {uuid.uuid4().hex[:8]}",
        data_residency="US",
    )
    session.add(fund)
    await session.flush()
    return fund


async def _pm_user(session: AsyncSession, fund_id: str) -> PMUser:
    user = PMUser(
        id=str(uuid.uuid4()),
        fund_entity_id=fund_id,
        email=f"test_{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(user)
    await session.flush()
    return user


async def _setup_pm(session: AsyncSession) -> tuple[FundEntity, PMUser]:
    fund = await _fund_entity(session)
    user = await _pm_user(session, fund.id)
    await session.commit()
    return fund, user


@pytest.mark.asyncio
async def test_onboarding_state_transitions(db_session: AsyncSession) -> None:
    """Full happy path moves through every onboarding state."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)

    assert user.onboarding_state == "not_started"

    start = await service.start()
    assert start["state"] == "cold_start"
    assert "Question 1" in (start["prompt"] or "")

    for idx in range(1, len(COLD_START_PROMPTS) + 1):
        res = await service.submit_answer(idx, f"Answer for question {idx}")
        if idx < len(COLD_START_PROMPTS):
            assert res["state"] == "cold_start"
            assert f"Question {idx + 1}" in (res["prompt"] or "")
        else:
            assert res["state"] == "thesis_capture"
            assert res["prompt"] == THESIS_CAPTURE_PROMPT

    capture = await service.submit_thesis_capture(["AAPL", "MSFT"])
    assert capture["state"] == "complete"
    assert capture["onboarding_complete"] is True
    assert capture["tickers"] == ["AAPL", "MSFT"]

    await db_session.refresh(user)
    assert user.onboarding_state == "complete"
    assert user.onboarding_complete is True


@pytest.mark.asyncio
async def test_cold_start_answers_create_memory(db_session: AsyncSession) -> None:
    """Answering all 5 cold-start questions synthesizes a PMMemory v1."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)

    await service.start()
    answers: dict[str, str] = {
        "q1_hold_period": "3-5 years",
        "q2_cutting_losers": "I cut when the thesis breaks.",
        "q3_edge": "Network in enterprise software.",
        "q4_when_wrong": "I re-size quickly and document learnings.",
        "q5_double_down": "When the price is lower and the thesis is intact.",
    }
    for idx, field in enumerate(answers, start=1):
        await service.submit_answer(idx, answers[field])

    result = await db_session.execute(select(PMMemory).where(PMMemory.pm_id == user.id))
    memory = result.scalar_one_or_none()
    assert memory is not None
    assert memory.version == 1
    assert memory.synthesis_trigger == "cold_start"
    assert memory.profile["cold_start"]["edge"] == answers["q3_edge"]
    assert memory.profile["derived"]["horizon"] == "long_term"

    cold_result = await db_session.execute(
        select(PMMemoryColdStart).where(PMMemoryColdStart.pm_id == user.id)
    )
    cold = cold_result.scalar_one()
    assert cold.synthesized is True


@pytest.mark.asyncio
async def test_skip_thesis_capture_allowed(db_session: AsyncSession) -> None:
    """PM can decline thesis capture; onboarding completes without re-prompt."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)

    await service.start()
    for idx in range(1, len(COLD_START_PROMPTS) + 1):
        await service.submit_answer(idx, f"Answer {idx}")

    skip = await service.skip_thesis_capture()
    assert skip["state"] == "complete"
    assert skip["onboarding_complete"] is True
    assert "complete" in (skip["message"] or "").lower()

    # A follow-up status check should not prompt thesis capture again.
    status = await service.get_status()
    assert status["state"] == "complete"
    assert status["onboarding_complete"] is True
    assert status["prompt"] is None


@pytest.mark.asyncio
async def test_duplicate_onboarding_start_is_idempotent(db_session: AsyncSession) -> None:
    """Starting onboarding twice returns the current status without resetting."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)

    await service.start()
    await service.submit_answer(1, "first answer")

    second = await service.start()
    assert second["state"] == "cold_start"
    # We already answered question 1, so the next prompt should be question 2.
    assert "Question 2" in (second["prompt"] or "")


@pytest.mark.asyncio
async def test_submit_answer_out_of_range(db_session: AsyncSession) -> None:
    """Question numbers outside 1-5 raise ValueError."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)
    await service.start()

    with pytest.raises(ValueError, match="question_number"):
        await service.submit_answer(0, "answer")
    with pytest.raises(ValueError, match="question_number"):
        await service.submit_answer(len(COLD_START_PROMPTS) + 1, "answer")


@pytest.mark.asyncio
async def test_submit_answer_wrong_state(db_session: AsyncSession) -> None:
    """submit_answer refuses to run outside cold_start state."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)
    await service.start()
    for idx in range(1, len(COLD_START_PROMPTS) + 1):
        await service.submit_answer(idx, f"Answer {idx}")

    with pytest.raises(ValueError, match="Cannot submit cold-start answer"):
        await service.submit_answer(1, "too late")


@pytest.mark.asyncio
async def test_thesis_capture_wrong_state(db_session: AsyncSession) -> None:
    """Thesis capture is rejected outside the thesis_capture state."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)
    await service.start()

    with pytest.raises(ValueError, match="Cannot capture theses"):
        await service.submit_thesis_capture(["AAPL"])


@pytest.mark.asyncio
async def test_skip_thesis_capture_wrong_state(db_session: AsyncSession) -> None:
    """Skipping thesis capture is rejected outside the thesis_capture state."""
    _fund, user = await _setup_pm(db_session)
    service = OnboardingService(db_session, user.id)
    await service.start()

    with pytest.raises(ValueError, match="Cannot skip thesis capture"):
        await service.skip_thesis_capture()


@pytest.mark.asyncio
async def test_load_user_missing(db_session: AsyncSession) -> None:
    """Loading a missing PM raises ValueError."""
    service = OnboardingService(db_session, str(uuid.uuid4()))
    with pytest.raises(ValueError, match="PM user not found"):
        await service.get_status()


@pytest.mark.asyncio
async def test_router_routes_exist() -> None:
    """The onboarding router exposes expected endpoints."""
    from fastapi.routing import APIRoute

    paths = {r.path for r in onboarding_router.routes if isinstance(r, APIRoute)}
    assert onboarding_router.prefix == "/onboarding"
    assert "/onboarding/start" in paths
    assert "/onboarding/{pm_id}/status" in paths
    assert "/onboarding/answer" in paths
    assert "/onboarding/thesis-capture" in paths
