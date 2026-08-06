"""Generic broker feed connector for CSV/OFX/JSON statements.

Supports:
  - CSV with a configurable ticker column and optional date/quantity/price columns.
  - OFX (Open Financial Exchange) via xml.etree when available.
  - JSON arrays/objects with a configurable ticker/date/value mapping.

Credentials and column mappings live in ``ConnectorConfig.config``.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

from axe.connectors.base import BaseConnector, ConnectorError, ConnectorResult, IngestCandidate


class BrokerFeedConnector(BaseConnector):
    """Parse broker statements into ingest candidates."""

    source_type = "broker_feed"

    async def fetch(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConnectorResult:
        """Read a broker statement payload and yield normalized candidates.

        Config keys:
          - format: "csv" | "ofx" | "json" (required)
          - payload: raw statement content as a string or dict/list (required)
          - ticker_column / ticker_path: where to extract the ticker symbol
          - date_column / date_path: optional date field
          - quantity_column / quantity_path: optional quantity
          - price_column / price_path: optional price
          - text_columns / text_paths: optional list of columns to include as text
        """
        fmt = self.require_config("format")
        payload = self.require_config("payload")

        try:
            if fmt == "csv":
                candidates = list(self._parse_csv(payload))
            elif fmt == "ofx":
                candidates = list(self._parse_ofx(payload))
            elif fmt == "json":
                candidates = list(self._parse_json(payload))
            else:
                raise ConnectorError(
                    f"Unsupported broker feed format: {fmt}",
                    source_type=self.source_type,
                    is_retryable=False,
                )
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(
                f"Broker feed parse error: {exc}",
                source_type=self.source_type,
                is_retryable=False,
            ) from exc

        if cursor and cursor.isdigit():
            offset = int(cursor)
            candidates = candidates[offset:]

        if limit is not None:
            candidates = candidates[:limit]

        next_cursor: str | None = None
        if limit is not None and len(candidates) == limit:
            next_cursor = str((int(cursor) if cursor and cursor.isdigit() else 0) + limit)

        return ConnectorResult(
            source_type=self.source_type,
            candidates=candidates,
            cursor=next_cursor,
            metadata={"format": fmt, "count": len(candidates)},
        )

    def _parse_csv(self, payload: Any) -> list[IngestCandidate]:
        if not isinstance(payload, str):
            raise ConnectorError(
                "CSV broker feed payload must be a string",
                source_type=self.source_type,
                is_retryable=False,
            )
        ticker_column = self.require_config("ticker_column")
        text_columns = self.get_config_value("text_columns") or []
        date_column = self.get_config_value("date_column")
        quantity_column = self.get_config_value("quantity_column")
        price_column = self.get_config_value("price_column")

        reader = csv.DictReader(io.StringIO(payload))
        candidates: list[IngestCandidate] = []
        for idx, row in enumerate(reader):
            ticker = row.get(ticker_column, "").strip().upper()
            if not ticker:
                continue

            raw = dict(row)
            if date_column:
                raw["date"] = row.get(date_column)
            if quantity_column:
                raw["quantity"] = row.get(quantity_column)
            if price_column:
                raw["price"] = row.get(price_column)

            text_parts = [f"{ticker_column}: {ticker}"]
            for col in text_columns:
                if row.get(col):
                    text_parts.append(f"{col}: {row[col]}")

            candidates.append(
                IngestCandidate(
                    external_id=f"csv:{idx}:{ticker}",
                    source_label="broker_csv",
                    raw_payload_json=raw,
                    extracted_signal_json={
                        "ticker": ticker,
                        "date": raw.get("date"),
                        "quantity": raw.get("quantity"),
                        "price": raw.get("price"),
                    },
                    content_text="\n".join(text_parts),
                    ticker=ticker,
                )
            )
        return candidates

    def _parse_json(self, payload: Any) -> list[IngestCandidate]:
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    f"Invalid JSON broker feed payload: {exc}",
                    source_type=self.source_type,
                    is_retryable=False,
                ) from exc
        else:
            data = payload

        ticker_path = self.require_config("ticker_path")
        date_path = self.get_config_value("date_path")
        quantity_path = self.get_config_value("quantity_path")
        price_path = self.get_config_value("price_path")
        text_paths = self.get_config_value("text_paths") or []

        rows: list[dict[str, Any]]
        if isinstance(data, dict):
            rows = data.get(self.get_config_value("items_key", "items"), [data])
        elif isinstance(data, list):
            rows = data
        else:
            raise ConnectorError(
                "JSON broker feed payload must be an object or array",
                source_type=self.source_type,
                is_retryable=False,
            )

        candidates: list[IngestCandidate] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            ticker = self._extract_path(row, ticker_path)
            if not ticker:
                continue

            raw = dict(row)
            extracted: dict[str, Any] = {"ticker": ticker}
            if date_path:
                extracted["date"] = self._extract_path(row, date_path)
            if quantity_path:
                extracted["quantity"] = self._extract_path(row, quantity_path)
            if price_path:
                extracted["price"] = self._extract_path(row, price_path)

            text_parts = [f"ticker: {ticker}"]
            for path in text_paths:
                value = self._extract_path(row, path)
                if value:
                    text_parts.append(f"{path}: {value}")

            candidates.append(
                IngestCandidate(
                    external_id=f"json:{idx}:{ticker}",
                    source_label="broker_json",
                    raw_payload_json=raw,
                    extracted_signal_json=extracted,
                    content_text="\n".join(text_parts),
                    ticker=ticker,
                )
            )
        return candidates

    def _parse_ofx(self, payload: Any) -> list[IngestCandidate]:
        if not isinstance(payload, str):
            raise ConnectorError(
                "OFX broker feed payload must be a string",
                source_type=self.source_type,
                is_retryable=False,
            )
        try:
            import xml.etree.ElementTree as ET
        except ImportError as exc:  # pragma: no cover
            raise ConnectorError(
                "OFX parsing requires xml.etree",
                source_type=self.source_type,
                is_retryable=False,
            ) from exc

        # OFX files are often SGML-ish but usually parseable by ElementTree.
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ConnectorError(
                f"OFX parse error: {exc}",
                source_type=self.source_type,
                is_retryable=False,
            ) from exc

        ticker_tag = self.get_config_value("ticker_tag", "TICKER")
        text_tags = self.get_config_value("text_tags") or ["NAME", "MEMO"]

        candidates: list[IngestCandidate] = []
        for idx, invpos in enumerate(root.iter("INVPOS") or root.iter("POSSTOCK")):
            ticker_el = invpos.find(f"./{ticker_tag}")
            ticker = (ticker_el.text or "").strip().upper() if ticker_el is not None else ""
            if not ticker:
                continue

            raw: dict[str, Any] = {}
            text_parts = [f"ticker: {ticker}"]
            for tag in text_tags:
                el = invpos.find(f"./{tag}")
                if el is not None and el.text:
                    raw[tag.lower()] = el.text
                    text_parts.append(f"{tag.lower()}: {el.text}")

            candidates.append(
                IngestCandidate(
                    external_id=f"ofx:{idx}:{ticker}",
                    source_label="broker_ofx",
                    raw_payload_json=raw,
                    extracted_signal_json={"ticker": ticker},
                    content_text="\n".join(text_parts),
                    ticker=ticker,
                )
            )
        return candidates

    @staticmethod
    def _extract_path(data: dict[str, Any], path: str) -> Any:
        """Extract a dotted or simple key path from a dict."""
        value: Any = data
        for part in path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return None
        return value

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()
