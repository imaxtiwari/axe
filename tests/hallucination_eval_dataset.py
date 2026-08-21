"""Evaluation dataset for hallucination scoring.

Pairs are intentionally simple so heuristic scoring remains deterministic and
interpretable in tests. Each entry contains an ``output``, a list of raw
``sources``, and a boolean ``should_fail`` flag that indicates whether the
output is expected to exceed the review threshold.

The dataset covers canonical grounding cases plus connector/specialist edge
cases: misattributed values, missing citations, numeric unit mismatches,
multi-source partial grounding, and source-type spoofing.
"""

from __future__ import annotations

from typing import Any

EVAL_PAIRS: list[dict[str, Any]] = [
    {
        "name": "fully_cited_and_verified",
        "output": (
            "Apple's Q1 revenue was $123.9 billion. [1] iPhone revenue grew 6% year over year. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "earnings_release",
                "content": "Apple reported Q1 revenue of $123.9 billion. iPhone revenue grew 6% year over year.",
            }
        ],
        "should_fail": False,
    },
    {
        "name": "unverified_claim_with_marker",
        "output": (
            "Tesla will deliver 2.5 million vehicles in 2025. [1] "
            "Margins are expected to expand to 25%. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "transcript",
                "content": "Tesla has not provided official vehicle delivery guidance for 2025.",
            }
        ],
        "should_fail": True,
    },
    {
        "name": "no_citations_no_sources",
        "output": (
            "NVIDIA is acquiring Arm for $80 billion. "
            "The deal is expected to close in the second half of 2025."
        ),
        "sources": [],
        "should_fail": True,
    },
    {
        "name": "overlap_based_grounding",
        "output": (
            "The Fed raised rates by 25 basis points at the March meeting. "
            "Inflation remains above the 2% target."
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "macro_note",
                "content": "At the March meeting the Federal Reserve raised rates by 25 basis points. Inflation continues to run above the 2% target.",
            }
        ],
        "should_fail": False,
    },
    {
        "name": "partial_overlap_one_unverified",
        "output": ("Microsoft cloud revenue grew 29%. [1] Azure specifically grew 35%. [1]"),
        "sources": [
            {
                "id": "1",
                "source_type": "earnings_release",
                "content": "Microsoft cloud revenue grew 29% this quarter.",
            }
        ],
        "should_fail": True,
    },
    # Connector / specialist signal cases
    {
        "name": "connector_cited_and_verified",
        "output": ("ResearchEdge reports that Snowflake NRR declined to 115% this quarter. [1]"),
        "sources": [
            {
                "id": "1",
                "source_type": "research_edge",
                "content": "Snowflake NRR declined to 115% this quarter.",
            }
        ],
        "should_fail": False,
    },
    {
        "name": "connector_misattributed_value",
        "output": ("BrokerFeed says Snowflake NRR was 124% this quarter. [1]"),
        "sources": [
            {
                "id": "1",
                "source_type": "broker_feed",
                "content": (
                    "Snowflake NRR declined to 115% this quarter. "
                    "Management cited macro headwinds and slower enterprise migrations."
                ),
            }
        ],
        "should_fail": True,
    },
    {
        "name": "specialist_signal_unverified_forward_guidance",
        "output": (
            "The expert network contact expects CRWD endpoint growth to reach 30% next quarter. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "expert_network",
                "content": "CRWD reported non-GAAP EPS of $0.95 in Q3.",
            }
        ],
        "should_fail": True,
    },
    {
        "name": "specialist_signal_fully_cited",
        "output": ("Polygon.io data shows ZS billings grew 31% this quarter. [1]"),
        "sources": [
            {
                "id": "1",
                "source_type": "polygon",
                "content": "ZS billings grew 31% this quarter.",
            }
        ],
        "should_fail": False,
    },
    {
        "name": "mixed_citation_with_unverified_connector_claim",
        "output": (
            "CRM notes show COIN trading volumes totaled $210bn in Q2. [1] "
            "Palantir government revenue grew 25% YoY. [2]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "crm",
                "content": "COIN trading volumes totaled $210bn in Q2.",
            },
            {
                "id": "2",
                "source_type": "research_edge",
                "content": "Palantir government revenue grew 18% YoY in Q3.",
            },
        ],
        "should_fail": True,
    },
    {
        "name": "pdf_deck_excerpt_grounded",
        "output": (
            "The pitch deck excerpt shows UBER mobility EBITDA margin reached 8.1% in Q2. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "pdf_deck",
                "content": "UBER mobility EBITDA margin reached 8.1% in Q2.",
            }
        ],
        "should_fail": False,
    },
    {
        "name": "connector_source_missing_citation_marker",
        "output": (
            "BrokerFeed reports MDB Atlas grew 22%, the slowest pace in three years. "
            "We expect growth to reaccelerate to 35% next quarter."
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "broker_feed",
                "content": "MDB Atlas grew 22%, the slowest pace in three years.",
            }
        ],
        "should_fail": True,
    },
    {
        "name": "numeric_unit_mismatch_millions_vs_billions",
        "output": (
            "CRM notes show COIN quarterly revenue was $1.2 billion. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "crm",
                "content": "COIN quarterly revenue was $1.2 million.",
            }
        ],
        "should_fail": True,
    },
    {
        "name": "numeric_unit_mismatch_pct_points_vs_pct",
        "output": (
            "The expert network contact said gross margin expanded by 300 basis points. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "expert_network",
                "content": "Gross margin expanded by 300%.",
            }
        ],
        "should_fail": True,
    },
    {
        "name": "multi_source_partial_grounding_one_unverified",
        "output": (
            "ResearchEdge reports NET security revenue grew 36% this quarter. [1] "
            "BrokerFeed says SNOW product revenue was $1.02bn in Q4. [2]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "research_edge",
                "content": "NET security revenue grew 36% this quarter.",
            },
            {
                "id": "2",
                "source_type": "broker_feed",
                "content": "SNOW product revenue was $940m in Q4, missing the $1bn mark.",
            },
        ],
        "should_fail": True,
    },
    {
        "name": "source_type_spoofing_wrong_connector_label",
        "output": (
            "ExpertNetwork call: OKTA identity revenue grew 22% YoY on strong enterprise adoption. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "expert_network",
                "content": "BrokerFeed: OKTA identity revenue grew 22% YoY on strong enterprise adoption.",
            }
        ],
        "should_fail": False,
    },
    {
        "name": "multi_source_fully_grounded_connector_claims",
        "output": (
            "ResearchEdge reports NET security revenue grew 36% this quarter. [1] "
            "BrokerFeed says SNOW product revenue was $1.02bn in Q4. [2]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "research_edge",
                "content": "NET security revenue grew 36% this quarter.",
            },
            {
                "id": "2",
                "source_type": "broker_feed",
                "content": "SNOW product revenue was $1.02bn in Q4.",
            },
        ],
        "should_fail": False,
    },
    {
        "name": "qualitative_connector_claim_no_numeric_mismatch",
        "output": (
            "ResearchEdge published a sector note on identity and access management trends. [1]"
        ),
        "sources": [
            {
                "id": "1",
                "source_type": "research_edge",
                "content": "ResearchEdge published a sector note on identity and access management trends.",
            }
        ],
        "should_fail": False,
    },
]

__all__ = ["EVAL_PAIRS"]
