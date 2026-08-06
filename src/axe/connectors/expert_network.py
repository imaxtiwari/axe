"""Expert network transcript connector (GLG / AlphaSights / Third Bridge).

Accepts either a raw transcript string or a structured JSON payload containing
interview metadata, questions, and answers. Emits one candidate per transcript
or per Q&A turn depending on config.
"""

from __future__ import annotations

import json
import re
from typing import Any

from axe.connectors.base import BaseConnector, ConnectorError, ConnectorResult, IngestCandidate


class ExpertNetworkConnector(BaseConnector):
    """Normalize expert network transcripts into ingest candidates."""

    source_type = "expert_network"

    async def fetch(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConnectorResult:
        """Fetch and normalize expert network transcripts.

        Config keys:
          - payload: raw transcript string or structured JSON object (required)
          - provider: "glg" | "alphasights" | "third_bridge" | generic
          - transcript_id: optional external identifier
          - ticker: optional ticker symbol
          - per_turn: if True, emit one candidate per Q&A turn (default False)
          - questions_key: JSON key for question list
          - answer_key: JSON key for answer text
        """
        payload = self.require_config("payload")
        provider = self.get_config_value("provider", "generic")
        transcript_id = self.get_config_value("transcript_id")
        ticker = self.get_config_value("ticker")
        per_turn = self.get_config_value("per_turn", False)
        questions_key = self.get_config_value("questions_key", "questions")
        answer_key = self.get_config_value("answer_key", "answer")

        structured: dict[str, Any]
        if isinstance(payload, str):
            try:
                structured = json.loads(payload)
            except json.JSONDecodeError:
                structured = {"raw_text": payload}
        elif isinstance(payload, dict):
            structured = payload
        else:
            raise ConnectorError(
                "Expert network payload must be a string or dict",
                source_type=self.source_type,
                is_retryable=False,
            )

        candidates: list[IngestCandidate] = []
        if per_turn and questions_key in structured:
            questions = structured[questions_key]
            if isinstance(questions, list):
                for idx, turn in enumerate(questions):
                    candidates.append(
                        self._build_turn_candidate(
                            turn, idx, provider, transcript_id, ticker, answer_key
                        )
                    )
        else:
            candidates.append(
                self._build_document_candidate(structured, provider, transcript_id, ticker)
            )

        if cursor and cursor.isdigit():
            candidates = candidates[int(cursor) :]
        if limit is not None:
            candidates = candidates[:limit]

        next_cursor: str | None = None
        if limit is not None and len(candidates) == limit:
            next_cursor = str((int(cursor) if cursor and cursor.isdigit() else 0) + limit)

        return ConnectorResult(
            source_type=self.source_type,
            candidates=candidates,
            cursor=next_cursor,
            metadata={"provider": provider, "count": len(candidates)},
        )

    def _build_document_candidate(
        self,
        structured: dict[str, Any],
        provider: str,
        transcript_id: Any | None,
        ticker: str | None,
    ) -> IngestCandidate:
        raw_text = structured.get("raw_text", "")
        title = structured.get("title", "")
        date = structured.get("date") or structured.get("transcript_date")
        external_id = str(transcript_id) if transcript_id else f"{provider}:doc"

        text_parts = [f"provider: {provider}"]
        if title:
            text_parts.append(f"title: {title}")
        if date:
            text_parts.append(f"date: {date}")
        if raw_text:
            text_parts.append(raw_text)

        return IngestCandidate(
            external_id=external_id,
            source_label=f"expert_network_{provider}",
            raw_payload_json={"provider": provider, **structured},
            extracted_signal_json={
                "provider": provider,
                "ticker": ticker,
                "transcript_date": date,
            },
            content_text="\n\n".join(text_parts),
            ticker=ticker,
            dedup_key=external_id,
        )

    def _build_turn_candidate(
        self,
        turn: Any,
        idx: int,
        provider: str,
        transcript_id: Any | None,
        ticker: str | None,
        answer_key: str,
    ) -> IngestCandidate:
        if isinstance(turn, dict):
            question = str(turn.get("question", ""))
            answer = str(turn.get(answer_key, ""))
            turn_ticker = str(turn.get("ticker", "")).upper() or ticker
            turn_id = str(turn.get("id", f"{idx}"))
        else:
            question = ""
            answer = str(turn)
            turn_ticker = ticker
            turn_id = str(idx)

        external_id = (
            f"{transcript_id}:turn:{turn_id}" if transcript_id else f"{provider}:turn:{turn_id}"
        )
        text_parts = [f"provider: {provider}"]
        if question:
            text_parts.append(f"Q: {question}")
        if answer:
            text_parts.append(f"A: {answer}")

        return IngestCandidate(
            external_id=external_id,
            source_label=f"expert_network_{provider}_turn",
            raw_payload_json={"provider": provider, "question": question, "answer": answer},
            extracted_signal_json={
                "provider": provider,
                "ticker": turn_ticker,
            },
            content_text="\n\n".join(text_parts),
            ticker=turn_ticker,
            dedup_key=external_id,
        )

    @staticmethod
    def _strip_noise(text: str) -> str:
        """Remove boilerplate headers/footers from raw transcript strings."""
        lines = text.splitlines()
        cleaned: list[str] = []
        for line in lines:
            if re.search(r"confidential|proprietary|copyright", line, re.IGNORECASE):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)
