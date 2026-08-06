"""ResearchEdge / Smartkarma API adapter.

Accepts a JSON payload (real API response shape or mocked test payload) and
emits one ``IngestCandidate`` per research item/document.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from axe.connectors.base import BaseConnector, ConnectorError, ConnectorResult, IngestCandidate


class ResearchEdgeConnector(BaseConnector):
    """Normalize ResearchEdge / Smartkarma research documents."""

    source_type = "research_edge"

    async def fetch(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConnectorResult:
        """Fetch and normalize research documents.

        Config keys:
          - payload: JSON string or list/dict of research items (required)
          - provider: "research_edge" | "smartkarma" (default "research_edge")
          - items_key: key for list of items when payload is a dict
          - id_field: field for external_id (default "id")
          - ticker_field: field for ticker symbol (default "ticker")
          - title_field: field for title (default "title")
          - body_field: field for body text (default "body")
          - date_field: field for publish date (default "published_at")
        """
        payload = self.require_config("payload")
        provider = self.get_config_value("provider", "research_edge")
        items_key = self.get_config_value("items_key", "items")
        id_field = self.get_config_value("id_field", "id")
        ticker_field = self.get_config_value("ticker_field", "ticker")
        title_field = self.get_config_value("title_field", "title")
        body_field = self.get_config_value("body_field", "body")
        date_field = self.get_config_value("date_field", "published_at")

        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    f"Invalid research payload JSON: {exc}",
                    source_type=self.source_type,
                    is_retryable=False,
                ) from exc
        else:
            data = payload

        if isinstance(data, dict):
            items = data.get(items_key, [data])
        elif isinstance(data, list):
            items = data
        else:
            raise ConnectorError(
                "Research payload must be a JSON object or array",
                source_type=self.source_type,
                is_retryable=False,
            )

        candidates: list[IngestCandidate] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            external_id = str(item.get(id_field) or f"{provider}:{idx}")
            ticker = self._extract_ticker(item, ticker_field)
            title = str(item.get(title_field, "")).strip()
            body = str(item.get(body_field, "")).strip()
            date = item.get(date_field)

            text_parts = [f"provider: {provider}"]
            if ticker:
                text_parts.append(f"ticker: {ticker}")
            if title:
                text_parts.append(f"title: {title}")
            if date:
                text_parts.append(f"date: {date}")
            if body:
                text_parts.append(body)

            candidates.append(
                IngestCandidate(
                    external_id=external_id,
                    source_label=f"research_{provider}",
                    raw_payload_json={"provider": provider, **item},
                    extracted_signal_json={
                        "provider": provider,
                        "ticker": ticker,
                        "title": title,
                        "published_at": date,
                    },
                    content_text="\n\n".join(text_parts),
                    ticker=ticker,
                    dedup_key=external_id,
                )
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

    @staticmethod
    def _extract_ticker(item: dict[str, Any], ticker_field: str) -> str | None:
        raw = item.get(ticker_field)
        if not raw:
            return None
        ticker = str(raw).strip().upper()
        # Remove common exchange suffixes for normalization.
        for suffix in [":US", " US", "-US", ".US"]:
            ticker = ticker.split(suffix)[0]
        return ticker

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()
