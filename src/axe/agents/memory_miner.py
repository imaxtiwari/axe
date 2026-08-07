"""Memory miner agent: extract citations and peer maps from Gmail/Slack history.

This module provides an abstract fetcher interface plus in-memory mock fetchers
for tests and offline development. Production fetchers can be implemented on top
of the same ``SourceFetcher`` protocol without changing the agent logic.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field

from axe.agents.llm import LLMProvider, get_default_provider
from axe.config import Settings, get_settings

logger = logging.getLogger(__name__)


class PrivacyGuardError(RuntimeError):
    """Raised when a fetcher request violates the privacy guard."""


@dataclass
class RawMessage:
    """A normalized message from Gmail, Slack, or any other source."""

    source_type: str  # gmail, slack, etc.
    source_id: str
    thread_id: str | None
    timestamp: dt.datetime
    participants: list[str] = field(default_factory=list)
    subject_or_topic: str | None = None
    body_text: str = ""
    is_dm: bool = False
    channel_id: str | None = None
    channel_name: str | None = None


class SourceFetcher(Protocol):
    """Protocol for fetching historical messages for memory mining."""

    async def fetch(
        self,
        pm_id: str,
        lookback_days: int,
        include_dms: bool,
        allowed_dm_participants: set[str] | None = None,
    ) -> list[RawMessage]:
        """Return normalized messages for the given PM within the lookback window."""
        ...


class _MockFetcherBase:
    """Base class for in-memory mock fetchers with privacy guard enforcement."""

    source_type: str = "mock"

    def __init__(self, messages: list[RawMessage] | None = None) -> None:
        self._messages = list(messages or [])

    def _apply_privacy_guard(
        self,
        messages: list[RawMessage],
        include_dms: bool,
        allowed_dm_participants: set[str] | None,
    ) -> list[RawMessage]:
        allowed = allowed_dm_participants or set()
        filtered: list[RawMessage] = []
        for msg in messages:
            if msg.is_dm:
                if not include_dms:
                    continue
                # Only keep DMs with explicitly opted-in participants.
                if not any(p in allowed for p in msg.participants):
                    continue
            filtered.append(msg)
        return filtered


class MockGmailFetcher(_MockFetcherBase):
    """In-memory mock Gmail fetcher for tests."""

    source_type = "gmail"

    async def fetch(
        self,
        pm_id: str,
        lookback_days: int,
        include_dms: bool,
        allowed_dm_participants: set[str] | None = None,
    ) -> list[RawMessage]:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=lookback_days)
        messages = [m for m in self._messages if m.source_type == "gmail" and m.timestamp >= cutoff]
        return self._apply_privacy_guard(messages, include_dms, allowed_dm_participants)


class MockSlackFetcher(_MockFetcherBase):
    """In-memory mock Slack fetcher for tests."""

    source_type = "slack"

    async def fetch(
        self,
        pm_id: str,
        lookback_days: int,
        include_dms: bool,
        allowed_dm_participants: set[str] | None = None,
    ) -> list[RawMessage]:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=lookback_days)
        messages = [m for m in self._messages if m.source_type == "slack" and m.timestamp >= cutoff]
        return self._apply_privacy_guard(messages, include_dms, allowed_dm_participants)


class MinedCitation(BaseModel):
    """A citation extracted from a raw communication."""

    source_type: str
    source_id: str
    snippet: str
    linked_ticker: str | None = None
    linked_deal_id: str | None = None
    sentiment: str | None = Field(default=None, pattern="^(positive|negative|neutral|uncertain)$")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    topics: list[str] = Field(default_factory=list)


class MinedPeer(BaseModel):
    """A peer relationship inferred from communication patterns."""

    peer_id: str  # email or slack id
    peer_name: str | None = None
    relationship_type: str | None = Field(
        default=None, pattern="^(colleague|expert|lp|management|other)$"
    )
    interaction_frequency: str | None = Field(
        default=None, pattern="^(daily|weekly|monthly|occasional)$"
    )
    topics: list[str] = Field(default_factory=list)
    trust_level: str | None = Field(default=None, pattern="^(high|medium|low)$")
    evidence_message_ids: list[str] = Field(default_factory=list)


class _ExtractionBatch(BaseModel):
    """Structured response from the LLM citation extraction pass."""

    citations: list[MinedCitation] = Field(default_factory=list)
    peers: list[MinedPeer] = Field(default_factory=list)


class MemoryMinerAgent:
    """Mine historical email/Slack for citations and peer relationships."""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        settings: Settings | None = None,
        fetchers: dict[str, SourceFetcher] | None = None,
    ) -> None:
        self.llm = llm or get_default_provider()
        self.settings = settings or get_settings()
        self.fetchers = fetchers or {}

    async def mine(
        self,
        pm_id: str,
        *,
        lookback_days: int | None = None,
        include_dms: bool | None = None,
        allowed_dm_participants: set[str] | None = None,
    ) -> tuple[list[MinedCitation], list[MinedPeer]]:
        """Run the memory-mining pipeline for ``pm_id``.

        Returns mined citations and peer relationships after applying the privacy
        guard. ``include_dms`` defaults to the application setting.
        """
        lookback = lookback_days or self.settings.memory_mining_default_days
        include_dms = (
            include_dms if include_dms is not None else self.settings.memory_mining_include_dms
        )

        all_messages: list[RawMessage] = []
        for fetcher in self.fetchers.values():
            try:
                msgs = await fetcher.fetch(pm_id, lookback, include_dms, allowed_dm_participants)
                all_messages.extend(msgs)
            except Exception:
                logger.exception("Fetcher failed for pm=%s", pm_id)

        if not all_messages:
            return [], []

        # Sort oldest-first so context builds naturally.
        all_messages.sort(key=lambda m: m.timestamp)

        citations, peers = await self._extract(all_messages)

        # Deduplicate citations by content hash.
        seen_hashes: set[str] = set()
        deduped_citations: list[MinedCitation] = []
        for c in citations:
            h = hashlib.sha256(c.snippet.encode()).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            deduped_citations.append(c)

        # Merge peer entries by peer_id, accumulating topics and evidence.
        peer_index: dict[str, MinedPeer] = {}
        for p in peers:
            existing = peer_index.get(p.peer_id)
            if existing:
                merged_topics = list(set(existing.topics + p.topics))
                merged_evidence = list(set(existing.evidence_message_ids + p.evidence_message_ids))
                # Keep the highest trust level.
                trust_rank = {"high": 3, "medium": 2, "low": 1, None: 0}
                trust = (
                    existing.trust_level
                    if trust_rank.get(existing.trust_level, 0) >= trust_rank.get(p.trust_level, 0)
                    else p.trust_level
                )
                peer_index[p.peer_id] = MinedPeer(
                    peer_id=p.peer_id,
                    peer_name=p.peer_name or existing.peer_name,
                    relationship_type=p.relationship_type or existing.relationship_type,
                    interaction_frequency=p.interaction_frequency or existing.interaction_frequency,
                    topics=merged_topics,
                    trust_level=trust,
                    evidence_message_ids=merged_evidence,
                )
            else:
                peer_index[p.peer_id] = p

        return deduped_citations, list(peer_index.values())

    async def _extract(
        self, messages: list[RawMessage]
    ) -> tuple[list[MinedCitation], list[MinedPeer]]:
        """Run LLM extraction over batched messages."""
        citations: list[MinedCitation] = []
        peers: list[MinedPeer] = []

        # Batch to avoid enormous prompts; overlap by one message for continuity.
        batch_size = 8
        for i in range(0, len(messages), batch_size):
            batch = messages[i : i + batch_size]
            batch_citations, batch_peers = await self._extract_batch(batch)
            citations.extend(batch_citations)
            peers.extend(batch_peers)

        return citations, peers

    async def _extract_batch(
        self, messages: list[RawMessage]
    ) -> tuple[list[MinedCitation], list[MinedPeer]]:
        prompt = self._build_extraction_prompt(messages)
        try:
            response = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_schema=_ExtractionBatch,
            )
        except Exception:
            logger.exception("LLM extraction failed for memory miner batch")
            return [], []

        parsed = response.parsed
        if not isinstance(parsed, dict):
            return [], []

        raw_citations = parsed.get("citations") or []
        raw_peers = parsed.get("peers") or []

        citations: list[MinedCitation] = []
        for item in raw_citations:
            if not isinstance(item, dict):
                continue
            try:
                citations.append(MinedCitation(**item))
            except Exception:
                logger.warning("Invalid mined citation dropped: %s", item)

        peers: list[MinedPeer] = []
        for item in raw_peers:
            if not isinstance(item, dict):
                continue
            try:
                peers.append(MinedPeer(**item))
            except Exception:
                logger.warning("Invalid mined peer dropped: %s", item)

        # Attach source message IDs to peers for traceability.
        source_ids = [m.source_id for m in messages]
        peers = [p.model_copy(update={"evidence_message_ids": source_ids}) for p in peers]

        return citations, peers

    @staticmethod
    def _build_extraction_prompt(messages: list[RawMessage]) -> str:
        lines: list[str] = [
            "You are analyzing historical investment-related communications for a portfolio manager. "
            "Extract concise, factual citations and infer peer relationships."
        ]
        lines.append("\nMessages:")
        for idx, msg in enumerate(messages, 1):
            lines.append(f"\n[{idx}] {msg.source_type.upper()} id={msg.source_id}")
            if msg.subject_or_topic:
                lines.append(f"Subject/topic: {msg.subject_or_topic}")
            lines.append(f"Participants: {', '.join(msg.participants)}")
            body = msg.body_text.strip()
            if len(body) > 2000:
                body = body[:2000] + "..."
            lines.append(body)

        lines.append(
            "\nReturn JSON with two arrays:"
            "\n- citations: objects with source_type, source_id, snippet, linked_ticker (optional), "
            "linked_deal_id (optional), sentiment (positive|negative|neutral|uncertain), "
            "confidence (0.0-1.0), topics (list of strings)."
            "\n- peers: objects with peer_id (email/slack id), peer_name (optional), "
            "relationship_type (colleague|expert|lp|management|other), "
            "interaction_frequency (daily|weekly|monthly|occasional), topics (list), "
            "trust_level (high|medium|low)."
        )
        return "\n".join(lines)


def build_mock_fetchers(
    gmail_messages: list[RawMessage] | None = None,
    slack_messages: list[RawMessage] | None = None,
) -> dict[str, SourceFetcher]:
    """Convenience helper to build a pair of mock Gmail + Slack fetchers."""
    return {
        "gmail": MockGmailFetcher(messages=gmail_messages),
        "slack": MockSlackFetcher(messages=slack_messages),
    }


__all__ = [
    "MemoryMinerAgent",
    "MockGmailFetcher",
    "MockSlackFetcher",
    "MinedCitation",
    "MinedPeer",
    "RawMessage",
    "SourceFetcher",
    "PrivacyGuardError",
    "build_mock_fetchers",
]
