"""User onboarding flow: fund selection, cold-start interview, thesis capture."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from axe.db.models import PMMemory, PMMemoryColdStart, PMUser, TickerRegistry

ONBOARDING_STATES: Sequence[str] = (
    "not_started",
    "fund_selected",
    "cold_start",
    "thesis_capture",
    "complete",
)

COLD_START_PROMPTS: Sequence[tuple[str, str]] = (
    ("q1_hold_period", "What's your typical hold period for a core position?"),
    (
        "q2_cutting_losers",
        "How do you decide when to cut a losing position?",
    ),
    ("q3_edge", "What is your edge as an investor?"),
    (
        "q4_when_wrong",
        "Tell me about a time you were wrong. What did you learn?",
    ),
    ("q5_double_down", "Under what conditions do you double down?"),
)

COLD_START_FIELDS: Sequence[str] = [field for field, _ in COLD_START_PROMPTS]

THESIS_CAPTURE_PROMPT = (
    "Great. Add up to 3 tickers with a one-sentence thesis for each. "
    "Reply with e.g. \"AAPL: services growth thesis\". You can also say \"skip\"."
)

COMPLETION_MESSAGE = "You're all set. AXE is now personalising your workspace."

SKIP_THESIS_MESSAGE = (
    "No problem — you can add tickers anytime. Onboarding complete."
)


def _derive_profile(answers: dict[str, str | None]) -> dict[str, Any]:
    """Deterministically map cold-start answers into an initial memory profile."""
    hold = (answers.get("q1_hold_period") or "").lower()
    cut = (answers.get("q2_cutting_losers") or "").lower()
    edge = (answers.get("q3_edge") or "").lower()
    wrong = (answers.get("q4_when_wrong") or "").lower()
    double = (answers.get("q5_double_down") or "").lower()

    # Time horizon
    if any(word in hold for word in ("day", "week", "month", "< 1", "short")):
        horizon = "short_term"
    elif any(word in hold for word in ("year", "multi", "long", "3", "5")):
        horizon = "long_term"
    else:
        horizon = "medium_term"

    # Risk / conviction profile
    aggressive_signals = sum(
        word in sig
        for sig in (cut, double, edge, wrong)
        for word in ("double down", "pyramid", "conviction", "aggressive", "high")
    )
    conservative_signals = sum(
        word in sig
        for sig in (cut, double, edge, wrong)
        for word in (
            "stop loss",
            "cut",
            "limit",
            "discipline",
            "protect",
            "downside",
            "risk",
        )
    )
    if aggressive_signals > conservative_signals:
        risk_profile = "aggressive"
    elif conservative_signals > aggressive_signals:
        risk_profile = "conservative"
    else:
        risk_profile = "balanced"

    style_tags: list[str] = []
    if horizon == "long_term":
        style_tags.append("long_term")
    if horizon == "short_term":
        style_tags.append("short_term")
    if risk_profile == "aggressive":
        style_tags.append("high_conviction")
    if risk_profile == "conservative":
        style_tags.append("risk_focused")

    return {
        "cold_start": {
            "hold_period": answers.get("q1_hold_period"),
            "cutting_losers": answers.get("q2_cutting_losers"),
            "edge": answers.get("q3_edge"),
            "when_wrong": answers.get("q4_when_wrong"),
            "double_down": answers.get("q5_double_down"),
        },
        "derived": {
            "horizon": horizon,
            "risk_profile": risk_profile,
            "style_tags": style_tags,
        },
        "preferences": {
            "prompt_thesis_drift": True,
            "prompt_post_mortem": True,
        },
    }


class OnboardingService:
    """Drive a PM through the AXE onboarding state machine."""

    def __init__(self, session: AsyncSession, pm_id: str) -> None:
        self.session = session
        self.pm_id = pm_id
        self._user: PMUser | None = None

    async def _load_user(self) -> PMUser:
        if self._user is None:
            result = await self.session.execute(
                select(PMUser).where(PMUser.id == self.pm_id)
            )
            user = result.scalar_one_or_none()
            if user is None:
                raise ValueError("PM user not found")
            self._user = user
        return self._user

    async def start(self) -> dict[str, Any]:
        """Begin onboarding: not_started -> fund_selected -> cold_start."""
        user = await self._load_user()
        if user.onboarding_state != "not_started":
            return await self.get_status()

        user.onboarding_state = "fund_selected"
        await self.session.flush()

        return await self.begin_cold_start()

    async def get_status(self) -> dict[str, Any]:
        """Return current onboarding state and next prompt, if any."""
        user = await self._load_user()
        prompt: str | None = None
        if user.onboarding_state == "cold_start":
            prompt = await self._next_question_prompt()
        elif user.onboarding_state == "thesis_capture":
            prompt = THESIS_CAPTURE_PROMPT
        return {
            "pm_id": self.pm_id,
            "state": user.onboarding_state,
            "onboarding_complete": user.onboarding_complete,
            "prompt": prompt,
        }

    async def begin_cold_start(self) -> dict[str, Any]:
        """Transition fund_selected -> cold_start and present question 1."""
        user = await self._load_user()
        if user.onboarding_state == "fund_selected":
            user.onboarding_state = "cold_start"
            await self.session.flush()

        # Ensure a cold-start row exists for this PM.
        existing = await self.session.execute(
            select(PMMemoryColdStart).where(PMMemoryColdStart.pm_id == self.pm_id)
        )
        if existing.scalar_one_or_none() is None:
            self.session.add(PMMemoryColdStart(pm_id=self.pm_id))
            await self.session.flush()

        return {
            "pm_id": self.pm_id,
            "state": user.onboarding_state,
            "prompt": await self._next_question_prompt(),
        }

    async def submit_answer(self, question_number: int, answer: str) -> dict[str, Any]:
        """Store one cold-start answer and advance the state machine.

        Question numbers are 1-5 and map to the cold-start fields in order.
        """
        if question_number < 1 or question_number > len(COLD_START_FIELDS):
            raise ValueError(f"question_number must be between 1 and {len(COLD_START_FIELDS)}")

        user = await self._load_user()
        if user.onboarding_state != "cold_start":
            raise ValueError(
                f"Cannot submit cold-start answer in state '{user.onboarding_state}'"
            )

        field = COLD_START_FIELDS[question_number - 1]
        cold = await self._get_or_create_cold_start()
        setattr(cold, field, answer)
        await self.session.flush()

        if question_number == len(COLD_START_FIELDS):
            await self._synthesize_initial_memory(cold)
            user.onboarding_state = "thesis_capture"
            await self.session.flush()
            return {
                "pm_id": self.pm_id,
                "state": user.onboarding_state,
                "prompt": THESIS_CAPTURE_PROMPT,
            }

        return {
            "pm_id": self.pm_id,
            "state": user.onboarding_state,
            "prompt": await self._next_question_prompt(),
        }

    async def submit_thesis_capture(
        self, tickers: Sequence[str]
    ) -> dict[str, Any]:
        """Store initial ticker interests and complete onboarding."""
        user = await self._load_user()
        if user.onboarding_state != "thesis_capture":
            raise ValueError(
                f"Cannot capture theses in state '{user.onboarding_state}'"
            )

        for ticker in tickers:
            normalized = ticker.strip().upper()
            if not normalized:
                continue
            self.session.add(
                TickerRegistry(
                    pm_id=self.pm_id,
                    ticker=normalized,
                    asset_class="equity",
                    direction="long",
                )
            )
        await self._complete_onboarding(user)
        return {
            "pm_id": self.pm_id,
            "state": user.onboarding_state,
            "onboarding_complete": user.onboarding_complete,
            "message": COMPLETION_MESSAGE,
            "tickers": [t.strip().upper() for t in tickers if t.strip()],
        }

    async def skip_thesis_capture(self) -> dict[str, Any]:
        """Allow a PM to skip thesis capture and finish onboarding."""
        user = await self._load_user()
        if user.onboarding_state != "thesis_capture":
            raise ValueError(
                f"Cannot skip thesis capture in state '{user.onboarding_state}'"
            )

        await self._complete_onboarding(user)
        return {
            "pm_id": self.pm_id,
            "state": user.onboarding_state,
            "onboarding_complete": user.onboarding_complete,
            "message": SKIP_THESIS_MESSAGE,
        }

    async def _complete_onboarding(self, user: PMUser) -> None:
        user.onboarding_state = "complete"
        user.onboarding_complete = True
        await self.session.flush()

    async def _next_question_prompt(self) -> str:
        cold = await self._get_or_create_cold_start()
        for idx, (field, prompt) in enumerate(COLD_START_PROMPTS, start=1):
            if getattr(cold, field) is None:
                return f"Question {idx}/{len(COLD_START_PROMPTS)}: {prompt}"
        return THESIS_CAPTURE_PROMPT

    async def _get_or_create_cold_start(self) -> PMMemoryColdStart:
        result = await self.session.execute(
            select(PMMemoryColdStart).where(PMMemoryColdStart.pm_id == self.pm_id)
        )
        cold = result.scalar_one_or_none()
        if cold is None:
            cold = PMMemoryColdStart(pm_id=self.pm_id)
            self.session.add(cold)
            await self.session.flush()
        return cold

    async def _synthesize_initial_memory(self, cold: PMMemoryColdStart) -> None:
        answers: dict[str, str | None] = {
            field: getattr(cold, field) for field, _ in COLD_START_PROMPTS
        }
        profile = _derive_profile(answers)
        memory = PMMemory(
            pm_id=self.pm_id,
            version=1,
            profile=profile,
            synthesis_trigger="cold_start",
        )
        self.session.add(memory)
        cold.synthesized = True
        await self.session.flush()


__all__ = [
    "COLD_START_FIELDS",
    "COLD_START_PROMPTS",
    "OnboardingService",
    "THESIS_CAPTURE_PROMPT",
]
