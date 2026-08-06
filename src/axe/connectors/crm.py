"""Generic CRM activity/contact JSON adapter.

Accepts a JSON payload representing CRM records (activities, contacts,
opportunities, notes) and emits one ``IngestCandidate`` per record.
"""

from __future__ import annotations

import json

from axe.connectors.base import BaseConnector, ConnectorError, ConnectorResult, IngestCandidate


class CRMConnector(BaseConnector):
    """Normalize CRM JSON records into ingest candidates."""

    source_type = "crm"

    async def fetch(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConnectorResult:
        """Fetch and normalize CRM records.

        Config keys:
          - payload: JSON string or list/dict of records (required)
          - record_type: "activity" | "contact" | "opportunity" | "note"
          - id_field: field to use as external_id (default "id")
          - ticker_field: optional field containing a ticker symbol
          - text_fields: fields to concatenate as content_text
          - items_key: key for list of records when payload is a dict
        """
        payload = self.require_config("payload")
        record_type = self.get_config_value("record_type", "activity")
        id_field = self.get_config_value("id_field", "id")
        ticker_field = self.get_config_value("ticker_field")
        text_fields = self.get_config_value("text_fields") or ["subject", "description", "body"]
        items_key = self.get_config_value("items_key", "records")

        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    f"Invalid CRM JSON payload: {exc}",
                    source_type=self.source_type,
                    is_retryable=False,
                ) from exc
        else:
            data = payload

        if isinstance(data, dict):
            records = data.get(items_key, [data])
        elif isinstance(data, list):
            records = data
        else:
            raise ConnectorError(
                "CRM payload must be a JSON object or array",
                source_type=self.source_type,
                is_retryable=False,
            )

        candidates: list[IngestCandidate] = []
        for idx, record in enumerate(records):
            if not isinstance(record, dict):
                continue

            external_id = str(record.get(id_field) or f"crm:{record_type}:{idx}")
            ticker = None
            if ticker_field:
                raw_ticker = record.get(ticker_field)
                if raw_ticker:
                    ticker = str(raw_ticker).strip().upper()

            text_parts = [f"record_type: {record_type}"]
            for field in text_fields:
                value = record.get(field)
                if value:
                    text_parts.append(f"{field}: {value}")

            candidates.append(
                IngestCandidate(
                    external_id=external_id,
                    source_label=f"crm_{record_type}",
                    raw_payload_json={"record_type": record_type, **record},
                    extracted_signal_json={
                        "record_type": record_type,
                        "ticker": ticker,
                    },
                    content_text="\n".join(text_parts),
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
            metadata={"record_type": record_type, "count": len(candidates)},
        )
