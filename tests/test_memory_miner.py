"""Tests for the memory miner agent and privacy guard."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from axe.agents.llm import MockProvider
from axe.agents.memory_miner import (
    MemoryMinerAgent,
    RawMessage,
    build_mock_fetchers,
)


def _msg(
    *,
    source_type: str = "gmail",
    source_id: str | None = None,
    body: str = "",
    is_dm: bool = False,
    participants: list[str] | None = None,
    days_ago: float = 1.0,
) -> RawMessage:
    return RawMessage(
        source_type=source_type,
        source_id=source_id or str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        timestamp=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        participants=participants or [],
        body_text=body,
        is_dm=is_dm,
    )


@pytest.mark.asyncio
async def test_privacy_guard_excludes_dms_by_default() -> None:
    """DMs should be excluded unless include_dms=True and participant is allowed."""
    dm = _msg(
        source_type="slack",
        body="Private DM about AAPL",
        is_dm=True,
        participants=["pm@example.com", "colleague@example.com"],
    )
    channel = _msg(
        source_type="slack",
        body="Channel message about AAPL",
        is_dm=False,
        participants=["#general"],
    )
    fetchers = build_mock_fetchers(slack_messages=[dm, channel])
    agent = MemoryMinerAgent(llm=MockProvider(), settings=None, fetchers=fetchers)

    citations, peers = await agent.mine("pm-1")
    assert len(citations) == 0
    assert len(peers) == 0


@pytest.mark.asyncio
async def test_privacy_guard_allows_opted_in_dms() -> None:
    """DMs with allowed participants are processed when include_dms=True."""
    dm = _msg(
        source_type="slack",
        body="Private DM about AAPL",
        is_dm=True,
        participants=["pm@example.com", "colleague@example.com"],
    )
    fetchers = build_mock_fetchers(slack_messages=[dm])

    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "citations": [
                        {
                            "source_type": "slack",
                            "source_id": dm.source_id,
                            "snippet": "Private DM about AAPL",
                            "linked_ticker": "AAPL",
                            "sentiment": "positive",
                            "confidence": 0.9,
                            "topics": ["AAPL"],
                        }
                    ],
                    "peers": [
                        {
                            "peer_id": "colleague@example.com",
                            "relationship_type": "colleague",
                            "interaction_frequency": "weekly",
                            "trust_level": "high",
                            "topics": ["AAPL"],
                        }
                    ],
                }
            }
        ]
    )
    agent = MemoryMinerAgent(llm=provider, settings=None, fetchers=fetchers)

    citations, peers = await agent.mine(
        "pm-1",
        include_dms=True,
        allowed_dm_participants={"colleague@example.com"},
    )
    assert len(citations) == 1
    assert citations[0].linked_ticker == "AAPL"
    assert len(peers) == 1
    assert peers[0].peer_id == "colleague@example.com"


@pytest.mark.asyncio
async def test_lookback_window_filters_old_messages() -> None:
    """Messages outside the lookback window are ignored."""
    old = _msg(body="Old message", days_ago=120)
    recent = _msg(body="Recent message", days_ago=10)
    fetchers = build_mock_fetchers(gmail_messages=[old, recent])

    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "citations": [
                        {
                            "source_type": "gmail",
                            "source_id": recent.source_id,
                            "snippet": "Recent message",
                            "linked_ticker": "TSLA",
                            "sentiment": "neutral",
                            "confidence": 0.8,
                            "topics": ["TSLA"],
                        }
                    ],
                    "peers": [],
                }
            }
        ]
    )
    agent = MemoryMinerAgent(llm=provider, settings=None, fetchers=fetchers)
    citations, peers = await agent.mine("pm-1", lookback_days=90)
    assert len(citations) == 1
    assert citations[0].snippet == "Recent message"
    assert len(peers) == 0


@pytest.mark.asyncio
async def test_citation_deduplication() -> None:
    """Identical snippets are deduplicated by content hash."""
    body = "Duplicate snippet about META"
    msg1 = _msg(source_type="gmail", body=body)
    msg2 = _msg(source_type="slack", body=body)
    fetchers = build_mock_fetchers(gmail_messages=[msg1], slack_messages=[msg2])

    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "citations": [
                        {
                            "source_type": "gmail",
                            "source_id": msg1.source_id,
                            "snippet": body,
                            "linked_ticker": "META",
                            "sentiment": "positive",
                            "confidence": 0.8,
                            "topics": ["META"],
                        }
                    ],
                    "peers": [],
                }
            },
            {
                "parsed": {
                    "citations": [
                        {
                            "source_type": "slack",
                            "source_id": msg2.source_id,
                            "snippet": body,
                            "linked_ticker": "META",
                            "sentiment": "positive",
                            "confidence": 0.8,
                            "topics": ["META"],
                        }
                    ],
                    "peers": [],
                }
            },
        ]
    )
    agent = MemoryMinerAgent(llm=provider, settings=None, fetchers=fetchers)
    citations, _ = await agent.mine("pm-1", lookback_days=90)
    assert len(citations) == 1


@pytest.mark.asyncio
async def test_peer_merging_accumulates_topics_and_highest_trust() -> None:
    """Peers with the same peer_id are merged across batches, keeping the highest trust level."""
    messages = [_msg(source_type="gmail", body=f"Message {i}") for i in range(9)]
    fetchers = build_mock_fetchers(gmail_messages=messages)

    provider = MockProvider(
        responses=[
            {
                "parsed": {
                    "citations": [],
                    "peers": [
                        {
                            "peer_id": "analyst@example.com",
                            "peer_name": "Senior Analyst",
                            "relationship_type": "expert",
                            "interaction_frequency": "monthly",
                            "trust_level": "medium",
                            "topics": ["AAPL"],
                        }
                    ],
                }
            },
            {
                "parsed": {
                    "citations": [],
                    "peers": [
                        {
                            "peer_id": "analyst@example.com",
                            "peer_name": None,
                            "relationship_type": "colleague",
                            "interaction_frequency": "weekly",
                            "trust_level": "high",
                            "topics": ["GOOGL"],
                        }
                    ],
                }
            },
        ]
    )
    agent = MemoryMinerAgent(llm=provider, settings=None, fetchers=fetchers)
    _, peers = await agent.mine("pm-1", lookback_days=90)
    assert len(provider._calls) == 2
    assert len(peers) == 1
    peer = peers[0]
    assert peer.peer_name == "Senior Analyst"
    assert set(peer.topics) == {"AAPL", "GOOGL"}
    assert peer.trust_level == "high"


@pytest.mark.asyncio
async def test_no_fetchers_returns_empty() -> None:
    """An agent with no fetchers returns empty results gracefully."""
    agent = MemoryMinerAgent(llm=MockProvider(), settings=None, fetchers={})
    citations, peers = await agent.mine("pm-1")
    assert citations == []
    assert peers == []


@pytest.mark.asyncio
async def test_invalid_extraction_results_are_dropped() -> None:
    """Malformed LLM outputs are dropped without raising."""
    msg = _msg(body="Some message")
    fetchers = build_mock_fetchers(gmail_messages=[msg])
    provider = MockProvider(responses=[{"parsed": {"citations": [{}], "peers": [{}]}}])
    agent = MemoryMinerAgent(llm=provider, settings=None, fetchers=fetchers)
    citations, peers = await agent.mine("pm-1", lookback_days=90)
    assert citations == []
    assert peers == []


@pytest.mark.asyncio
async def test_default_settings_used_when_not_overridden() -> None:
    """When arguments are omitted, settings defaults are applied."""
    from axe.config import Settings

    msg = _msg(body="Default settings test", days_ago=10)
    fetchers = build_mock_fetchers(gmail_messages=[msg])
    settings = Settings(memory_mining_default_days=30, memory_mining_include_dms=False)
    agent = MemoryMinerAgent(
        llm=MockProvider(responses=[{"parsed": {"citations": [], "peers": []}}]),
        settings=settings,
        fetchers=fetchers,
    )
    citations, peers = await agent.mine("pm-1")
    assert citations == []
    assert peers == []
