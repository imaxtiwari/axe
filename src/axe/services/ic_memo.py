"""Investment Committee memo service with LLM drafting and sign-off control."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from axe.agents.llm import LLMProvider, get_default_provider
from axe.db.models import AuditLog, DealThesisVersion, ICMemo
from axe.db.uow import UnitOfWork
from axe.security.audit import _state_dict
from axe.security.context import RequestContext

T = TypeVar("T")


class ICMemoContent(BaseModel):
    """Structured IC memo payload produced by the LLM."""

    recommendation: str = Field(..., description="Invest / pass / hold recommendation")
    recommendation_summary: str = Field(
        ..., description="One-paragraph summary of the recommendation"
    )
    investment_thesis: str = Field(..., description="Summary of the deal thesis")
    key_assumptions: list[str] = Field(default_factory=list)
    underwriting_checklist_status: str = Field(..., description="Summary of checklist completion")
    scenario_analysis_summary: str = Field(..., description="Summary of scenario analyses")
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ICMemoService:
    """Draft, version, and sign off on Investment Committee memos.

    - Pulls the latest deal thesis version.
    - Pulls underwriting scenarios and checklist.
    - Drafts a memo using an LLM template (deterministic fallback in tests).
    - Tracks memo version history and sign-off in ``ICMemo`` / ``ICSignOff``.
    - Makes the memo immutable after the required number of sign-offs.
    """

    REQUIRED_SIGNOFF_COUNT = 2

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

    def _with_context(
        self, coro_factory: Callable[[], Coroutine[Any, Any, T]]
    ) -> Coroutine[Any, Any, T]:
        with self._context:
            return coro_factory()

    async def generate_memo(
        self,
        deal_id: str,
        *,
        force_new_version: bool = False,
    ) -> ICMemo:
        """Generate a new IC memo for the deal, incrementing the version if one exists."""
        async with _DealLocks.get(self.pm_id, deal_id):
            deal = await self.uow.deals.get_by_id(deal_id)
            if deal is None:
                raise ValueError(f"Deal {deal_id} not found")

            latest = await self.uow.ic_memos.get_latest_for_deal(deal_id)
            if latest is not None and latest.status == "final_signed":
                raise ValueError("Cannot regenerate a memo after final sign-off")
            if latest is not None and not force_new_version:
                # Re-drafting a memo in draft state overwrites it (version unchanged).
                await self._populate_memo(latest, deal_id)
                await self.session.flush()
                await self._audit("ic_memo_regenerated", latest)
                await self.uow.commit()
                return latest

            new_version = (latest.version + 1) if latest else 1
            memo = self.uow.ic_memos.create_memo(
                deal_id=deal_id,
                pm_id=self.pm_id,
                fund_entity_id=self.fund_entity_id,
                version=new_version,
                status="draft",
            )
            await self._populate_memo(memo, deal_id)
            await self.session.flush()
            await self._audit("ic_memo_created", memo)
            await self.uow.commit()
            return memo

    async def get_memo(self, memo_id: str) -> ICMemo | None:
        return await self.uow.ic_memos.get_by_id(memo_id)

    async def list_memos_for_deal(self, deal_id: str) -> list[ICMemo]:
        return await self.uow.ic_memos.list_for_deal(deal_id)

    async def sign_memo(
        self,
        memo_id: str,
        signer_pm_id: str,
        *,
        role: str = "pm",
        signature_note: str | None = None,
    ) -> ICMemo:
        """Record a sign-off by ``signer_pm_id``. Finalizes after REQUIRED_SIGNOFF_COUNT."""
        async with _DealLocks.get(self.pm_id, memo_id):
            memo = await self.uow.ic_memos.get_by_id(memo_id)
            if memo is None:
                raise ValueError(f"Memo {memo_id} not found")
            if memo.status == "final_signed":
                raise ValueError("Memo is immutable after final sign-off")

            existing = await self.uow.ic_signoffs.list_for_memo(memo_id)
            if any(so.pm_id == signer_pm_id for so in existing):
                raise ValueError(f"User {signer_pm_id} has already signed this memo")

            signoff = self.uow.ic_signoffs.create_signoff(
                memo_id=memo_id,
                pm_id=signer_pm_id,
                fund_entity_id=self.fund_entity_id,
                role=role,
                signature_note=signature_note,
            )
            await self.session.flush()

            updated = await self.uow.ic_signoffs.list_for_memo(memo_id)
            before = _state_dict(memo)
            if len(updated) >= self.REQUIRED_SIGNOFF_COUNT:
                memo.status = "final_signed"
                memo.final_signoff_at = datetime.now(UTC)
            await self.session.flush()
            await self._audit(
                "ic_memo_signed",
                memo,
                before_state=before,
                extra_state={
                    "signoff_id": signoff.id,
                    "signer_pm_id": signer_pm_id,
                    "role": role,
                    "signature_note": signature_note,
                    "signoff_count": len(updated),
                },
            )
            await self.uow.commit()
            return memo

    async def update_memo(self, memo_id: str, **changes: Any) -> ICMemo:
        """Block any mutation after final sign-off; update draft fields otherwise."""
        async with _DealLocks.get(self.pm_id, memo_id):
            memo = await self.uow.ic_memos.get_by_id(memo_id)
            if memo is None:
                raise ValueError(f"Memo {memo_id} not found")
            if memo.status == "final_signed":
                await self._audit(
                    "ic_memo_attempted_edit_after_final_signoff",
                    memo,
                    extra_state={"requested_changes": changes},
                )
                await self.uow.commit()
                raise ValueError("Memo is immutable after final sign-off")

            before = _state_dict(memo)
            allowed = {"content_json", "content_md"}
            for key, value in changes.items():
                if key not in allowed:
                    raise ValueError(f"Cannot update memo field {key}")
                setattr(memo, key, value)
            await self.session.flush()
            await self._audit("ic_memo_updated", memo, before_state=before)
            await self.uow.commit()
            return memo

    async def _populate_memo(self, memo: ICMemo, deal_id: str) -> None:
        """Load deal inputs and produce JSON + markdown content."""
        latest_thesis = await self._latest_deal_thesis(deal_id)
        scenarios = await self.uow.underwriting_scenarios.list_for_deal(deal_id)
        checklist = await self.uow.underwriting_checklists.list_for_deal(deal_id)

        deal = await self.uow.deals.get_by_id(deal_id)
        deal_name = deal.name if deal is not None else deal_id

        content = await self._draft_memo(
            deal_name=deal_name,
            thesis=latest_thesis,
            scenarios=scenarios,
            checklist=checklist,
        )
        memo.content_json = content.model_dump()
        memo.content_md = self._render_markdown(content)

    async def _latest_deal_thesis(self, deal_id: str) -> DealThesisVersion | None:
        from sqlalchemy import desc, select

        result = await self.session.execute(
            select(DealThesisVersion)
            .where(DealThesisVersion.deal_id == deal_id)
            .order_by(desc(DealThesisVersion.version))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _draft_memo(
        self,
        deal_name: str,
        thesis: DealThesisVersion | None,
        scenarios: list[Any],
        checklist: list[Any],
    ) -> ICMemoContent:
        checklist_summary = self._checklist_summary(checklist)
        scenario_summary = self._scenario_summary(scenarios)

        from axe.agents.llm import MockProvider

        if isinstance(self.provider, MockProvider):
            return self._fallback_content(deal_name, thesis, checklist_summary, scenario_summary)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an investment committee memo author. Produce a structured IC memo "
                    "based on the deal thesis, underwriting checklist, and scenario analysis."
                ),
            },
            {
                "role": "user",
                "content": self._build_prompt(
                    deal_name=deal_name,
                    thesis=thesis,
                    checklist_summary=checklist_summary,
                    scenario_summary=scenario_summary,
                ),
            },
        ]
        response = await self.provider.complete(
            messages, temperature=0.2, response_schema=ICMemoContent
        )
        parsed = response.parsed or {}
        if parsed:
            try:
                return ICMemoContent.model_validate(parsed)
            except Exception:  # pragma: no cover - defensive fallback
                pass
        return self._fallback_content(deal_name, thesis, checklist_summary, scenario_summary)

    def _build_prompt(
        self,
        *,
        deal_name: str,
        thesis: DealThesisVersion | None,
        checklist_summary: str,
        scenario_summary: str,
    ) -> str:
        thesis_text = ""
        if thesis is not None:
            thesis_text = (
                f"Stage: {thesis.stage}\n"
                f"Bull case: {thesis.bull_case or 'N/A'}\n"
                f"Bear case: {thesis.bear_case or 'N/A'}\n"
                f"Key assumptions: {thesis.key_assumptions}\n"
                f"Risks: {thesis.risks}"
            )
        return (
            f"Deal: {deal_name}\n\n"
            f"Thesis:\n{thesis_text}\n\n"
            f"Underwriting checklist:\n{checklist_summary}\n\n"
            f"Scenario analysis:\n{scenario_summary}\n\n"
            "Respond with the structured JSON IC memo."
        )

    def _checklist_summary(self, checklist: list[Any]) -> str:
        lines: list[str] = []
        for item in checklist:
            status = getattr(item, "status", "open")
            question = getattr(item, "question", "")
            required = getattr(item, "required", True)
            lines.append(f"- [{status}] {'(required) ' if required else ''}{question}")
        return "\n".join(lines) if lines else "No checklist items."

    def _scenario_summary(self, scenarios: list[Any]) -> str:
        lines: list[str] = []
        for s in scenarios:
            name = getattr(s, "scenario_name", "unnamed")
            assumptions = getattr(s, "assumptions", {})
            metrics = getattr(s, "output_metrics", {})
            weight = getattr(s, "probability_weight", None)
            confidence = getattr(s, "confidence", None)
            lines.append(
                f"- {name}: assumptions={assumptions}, metrics={metrics}, "
                f"weight={weight}, confidence={confidence}"
            )
        return "\n".join(lines) if lines else "No scenarios generated."

    def _fallback_content(
        self,
        deal_name: str,
        thesis: DealThesisVersion | None,
        checklist_summary: str,
        scenario_summary: str,
    ) -> ICMemoContent:
        """Deterministic memo content used when no LLM is configured."""
        recommendation = "Invest"
        if thesis is None or [line for line in checklist_summary.splitlines() if "[open]" in line]:
            recommendation = "Hold"
        investment_thesis = (
            thesis.bull_case
            if thesis is not None and thesis.bull_case is not None
            else "No thesis on file."
        )
        return ICMemoContent(
            recommendation=recommendation,
            recommendation_summary=f"{recommendation} in {deal_name} based on current underwriting.",
            investment_thesis=investment_thesis,
            key_assumptions=(thesis.key_assumptions if thesis is not None else []),
            underwriting_checklist_status=checklist_summary,
            scenario_analysis_summary=scenario_summary,
            risks=(thesis.risks if thesis is not None else []),
            open_questions=["Confirm final IC schedule."],
        )

    def _render_markdown(self, content: ICMemoContent) -> str:
        return (
            "# IC Memo\n\n"
            f"## Recommendation: {content.recommendation}\n\n"
            f"{content.recommendation_summary}\n\n"
            f"## Investment Thesis\n\n{content.investment_thesis}\n\n"
            "## Key Assumptions\n\n" + "\n".join(f"- {a}" for a in content.key_assumptions) + "\n\n"
            "## Underwriting Checklist\n\n"
            f"{content.underwriting_checklist_status}\n\n"
            "## Scenario Analysis\n\n"
            f"{content.scenario_analysis_summary}\n\n"
            "## Risks\n\n" + "\n".join(f"- {r}" for r in content.risks) + "\n\n"
            "## Open Questions\n\n" + "\n".join(f"- {q}" for q in content.open_questions) + "\n"
        )

    async def _audit(
        self,
        action_type: str,
        memo: ICMemo,
        *,
        before_state: dict[str, Any] | None = None,
        extra_state: dict[str, Any] | None = None,
    ) -> None:
        after = _state_dict(memo)
        if extra_state:
            after.update(extra_state)
        entry = AuditLog(
            pm_id=self.pm_id,
            fund_entity_id=self.fund_entity_id,
            action_type=action_type,
            object_type="ic_memo",
            object_id=memo.id,
            before_state=before_state or {},
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


class _DealLocks:
    """Process-wide asyncio locks keyed by (pm_id, deal_id)."""

    _locks: dict[tuple[str, str], asyncio.Lock] = {}

    @classmethod
    def get(cls, pm_id: str, deal_id: str) -> asyncio.Lock:
        key = (pm_id, deal_id)
        if key not in cls._locks:
            cls._locks[key] = asyncio.Lock()
        return cls._locks[key]
