"""Evaluation dataset for hallucination scoring.

Pairs are intentionally simple so heuristic scoring remains deterministic and
interpretable in tests. Each entry contains an ``output``, a list of raw
``sources``, and a boolean ``should_fail`` flag that indicates whether the
output is expected to exceed the review threshold.
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
]

__all__ = ["EVAL_PAIRS"]
