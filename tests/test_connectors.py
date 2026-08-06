"""Tests for AXE ingestion connectors."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from axe.connectors import (
    BaseConnector,
    BrokerFeedConnector,
    ConnectorError,
    ConnectorResult,
    CRMConnector,
    ExpertNetworkConnector,
    IngestCandidate,
    PDFDeckConnector,
    ResearchEdgeConnector,
    build_connector,
    get_connector_class,
    list_connector_types,
    register_connector,
)


# -----------------------------------------------------------------------------
# Base contract
# -----------------------------------------------------------------------------
def test_connector_error_fields() -> None:
    err = ConnectorError("boom", is_retryable=True, source_type="x")
    assert str(err) == "boom"
    assert err.is_retryable is True
    assert err.source_type == "x"


def test_ingest_candidate_hashing_and_dedup() -> None:
    c1 = IngestCandidate(content_text="AAPL beats", external_id="e1")
    c2 = IngestCandidate(content_text="  aapl BEATS  ", external_id="e1")
    assert c1.content_hash() == c2.content_hash()
    assert c1.build_dedup_key("src") == "src:e1"

    explicit = IngestCandidate(external_id=None, dedup_key="k")
    assert explicit.build_dedup_key("src") == "k"

    ordered1 = IngestCandidate(raw_payload_json={"a": 1, "b": 2})
    ordered2 = IngestCandidate(raw_payload_json={"b": 2, "a": 1})
    assert ordered1.hash_payload() == ordered2.hash_payload()


def test_connector_result_is_empty() -> None:
    result = ConnectorResult(source_type="s")
    assert result.is_empty() is True
    assert result.candidates == []

    populated = ConnectorResult(source_type="s", candidates=[IngestCandidate()])
    assert populated.is_empty() is False


@pytest.mark.asyncio
async def test_base_connector_config_helpers() -> None:
    class DummyConnector(BaseConnector):
        source_type = "dummy"

        async def fetch(
            self,
            *,
            cursor: str | None = None,
            limit: int | None = None,
        ) -> ConnectorResult:
            return ConnectorResult(source_type=self.source_type)

    conn = DummyConnector({"a": 1})
    assert conn.get_config_value("a") == 1
    assert conn.get_config_value("missing") is None
    with pytest.raises(ConnectorError):
        conn.require_config("missing")


def test_base_connector_cannot_instantiate_abstract() -> None:
    class Incomplete(BaseConnector):  # type: ignore[valid-type, misc]
        source_type = "incomplete"

    with pytest.raises(TypeError):
        Incomplete({})  # type: ignore[call-arg]


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------
def test_list_connector_types() -> None:
    types = list_connector_types()
    for source_type in (
        "broker_feed",
        "pdf_deck",
        "crm",
        "expert_network",
        "research_edge",
    ):
        assert source_type in types


def test_get_connector_class_unknown() -> None:
    with pytest.raises(ConnectorError):
        get_connector_class("not_registered")


@pytest.mark.asyncio
async def test_register_and_build_custom_connector() -> None:
    class DummyConnector(BaseConnector):
        source_type = "dummy_test"

        async def fetch(
            self,
            *,
            cursor: str | None = None,
            limit: int | None = None,
        ) -> ConnectorResult:
            return ConnectorResult(source_type=self.source_type)

    register_connector("dummy_test", DummyConnector)
    built = build_connector("dummy_test", {"x": 1})
    assert isinstance(built, DummyConnector)
    result = await built.fetch()
    assert result.source_type == "dummy_test"


def test_register_connector_requires_subclass() -> None:
    class NotAConnector:
        source_type = "bad"

    with pytest.raises(TypeError):
        register_connector("bad", NotAConnector)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Broker feed
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_broker_feed_csv() -> None:
    payload = "ticker,date,quantity,price\nAAPL,2024-01-01,100,150.00\n,2024-01-02,50,160.00\n"
    conn = BrokerFeedConnector(
        {
            "format": "csv",
            "payload": payload,
            "ticker_column": "ticker",
            "date_column": "date",
            "quantity_column": "quantity",
            "price_column": "price",
            "text_columns": ["date"],
        }
    )
    result = await conn.fetch()
    assert result.source_type == "broker_feed"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.ticker == "AAPL"
    assert candidate.external_id == "csv:0:AAPL"
    assert "ticker: AAPL" in candidate.content_text
    assert "date: 2024-01-01" in candidate.content_text
    assert candidate.extracted_signal_json["quantity"] == "100"
    assert candidate.extracted_signal_json["price"] == "150.00"


@pytest.mark.asyncio
async def test_broker_feed_json() -> None:
    payload = [
        {"symbol": "NVDA", "trade_date": "2024-01-01", "shares": "10", "avg_price": "500"},
        {"symbol": "", "trade_date": "2024-01-02", "shares": "5", "avg_price": "505"},
    ]
    conn = BrokerFeedConnector(
        {
            "format": "json",
            "payload": payload,
            "ticker_path": "symbol",
            "date_path": "trade_date",
            "quantity_path": "shares",
            "price_path": "avg_price",
            "text_paths": ["trade_date"],
        }
    )
    result = await conn.fetch()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.ticker == "NVDA"
    assert candidate.external_id == "json:0:NVDA"
    assert candidate.extracted_signal_json["date"] == "2024-01-01"


@pytest.mark.asyncio
async def test_broker_feed_pagination() -> None:
    payload = "ticker\nA\nB\nC\nD\n"
    conn = BrokerFeedConnector(
        {
            "format": "csv",
            "payload": payload,
            "ticker_column": "ticker",
        }
    )
    page1 = await conn.fetch(limit=2)
    assert len(page1.candidates) == 2
    assert page1.cursor == "2"

    page2 = await conn.fetch(cursor="2", limit=2)
    assert len(page2.candidates) == 2
    assert page2.cursor == "4"

    page3 = await conn.fetch(cursor="4", limit=2)
    assert len(page3.candidates) == 0


@pytest.mark.asyncio
async def test_broker_feed_errors() -> None:
    with pytest.raises(ConnectorError):
        await BrokerFeedConnector({"format": "csv"}).fetch()

    with pytest.raises(ConnectorError):
        await BrokerFeedConnector({"format": "xml", "payload": ""}).fetch()

    with pytest.raises(ConnectorError):
        await BrokerFeedConnector(
            {"format": "csv", "payload": 123, "ticker_column": "ticker"}
        ).fetch()


# -----------------------------------------------------------------------------
# CRM
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crm_json_records() -> None:
    payload = [
        {"id": "c1", "subject": "met mgmt", "description": "positive", "ticker": "MSFT"},
        {"id": "c2", "subject": "follow up", "body": "send model", "ticker": "amzn"},
    ]
    conn = CRMConnector(
        {
            "payload": payload,
            "record_type": "activity",
            "ticker_field": "ticker",
            "text_fields": ["subject", "description", "body"],
        }
    )
    result = await conn.fetch()
    assert len(result.candidates) == 2
    assert result.candidates[0].ticker == "MSFT"
    assert result.candidates[1].ticker == "AMZN"
    assert result.candidates[1].dedup_key == "c2"


@pytest.mark.asyncio
async def test_crm_dict_payload() -> None:
    payload = {
        "records": [
            {"id": "c3", "subject": "call", "description": "earnings preview"},
        ]
    }
    conn = CRMConnector({"payload": payload, "items_key": "records"})
    result = await conn.fetch()
    assert len(result.candidates) == 1
    assert result.candidates[0].external_id == "c3"


# -----------------------------------------------------------------------------
# ResearchEdge
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_research_edge_strips_exchange_suffixes() -> None:
    payload = [
        {
            "id": "r1",
            "ticker": "AAPL:US",
            "title": "Bullish note",
            "body": "Body text",
            "published_at": "2024-01-01",
        },
        {
            "id": "r2",
            "ticker": "MSFT US",
            "title": "Bearish note",
            "body": "More text",
            "published_at": "2024-01-02",
        },
    ]
    conn = ResearchEdgeConnector({"payload": payload})
    result = await conn.fetch()
    assert len(result.candidates) == 2
    assert result.candidates[0].ticker == "AAPL"
    assert result.candidates[1].ticker == "MSFT"


@pytest.mark.asyncio
async def test_research_edge_smartkarma_dict() -> None:
    payload = {
        "items": [{"id": "s1", "ticker": "TSLA-US", "title": "T", "body": "B", "published_at": "x"}]
    }
    conn = ResearchEdgeConnector({"payload": payload, "provider": "smartkarma"})
    result = await conn.fetch()
    assert len(result.candidates) == 1
    assert result.candidates[0].ticker == "TSLA"


# -----------------------------------------------------------------------------
# Expert network
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_expert_network_document_candidate() -> None:
    payload = {
        "title": "Expert call",
        "date": "2024-01-01",
        "raw_text": "Management sounded bullish on guidance.",
    }
    conn = ExpertNetworkConnector(
        {"payload": payload, "provider": "glg", "transcript_id": "t1", "ticker": "TSLA"}
    )
    result = await conn.fetch()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.external_id == "t1"
    assert candidate.ticker == "TSLA"
    assert "Management sounded bullish" in candidate.content_text


@pytest.mark.asyncio
async def test_expert_network_per_turn_candidates() -> None:
    payload = {
        "questions": [
            {"question": "Q1", "answer": "A1", "ticker": "amzn", "id": "1"},
            {"question": "Q2", "answer": "A2", "id": "2"},
        ]
    }
    conn = ExpertNetworkConnector(
        {"payload": payload, "provider": "alphasights", "transcript_id": "t2", "per_turn": True}
    )
    result = await conn.fetch()
    assert len(result.candidates) == 2
    assert result.candidates[0].external_id == "t2:turn:1"
    assert result.candidates[0].ticker == "AMZN"


@pytest.mark.asyncio
async def test_expert_network_raw_string() -> None:
    conn = ExpertNetworkConnector({"payload": "Some raw text", "provider": "generic"})
    result = await conn.fetch()
    assert len(result.candidates) == 1
    assert result.candidates[0].content_text == "provider: generic\n\nSome raw text"


# -----------------------------------------------------------------------------
# PDF deck
# -----------------------------------------------------------------------------
def _minimal_pdf_bytes() -> bytes:
    """Return a minimal valid PDF with one text page.

    Uses a hand-built PDF containing a single "Hello PDF" text object so tests
    do not require external PDF libraries to be installed.
    """
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<< /Type /Catalog /Pages 2 0 R >>\n"
        b"endobj\n"
        b"2 0 obj\n"
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"
        b"endobj\n"
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\n"
        b"endobj\n"
        b"4 0 obj\n"
        b"<< /Length 44 >>\n"
        b"stream\n"
        b"BT\n"
        b"/F1 12 Tf\n"
        b"100 700 Td\n"
        b"(Hello PDF) Tj\n"
        b"ET\n"
        b"endstream\n"
        b"endobj\n"
        b"5 0 obj\n"
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
        b"endobj\n"
        b"xref\n"
        b"0 6\n"
        b"0000000000 65535 f\n"
        b"0000000009 00000 n\n"
        b"0000000058 00000 n\n"
        b"0000000115 00000 n\n"
        b"0000000264 00000 n\n"
        b"0000000358 00000 n\n"
        b"trailer\n"
        b"<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n"
        b"438\n"
        b"%%EOF\n"
    )


@pytest.mark.asyncio
async def test_pdf_deck_base64_per_page(tmp_path: Any) -> None:
    pdf_bytes = _minimal_pdf_bytes()
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    conn = PDFDeckConnector({"file_content_b64": encoded, "per_page": True})
    result = await conn.fetch()
    assert result.source_type == "pdf_deck"
    assert len(result.candidates) == 1
    assert "Hello PDF" in result.candidates[0].content_text
    assert result.candidates[0].source_label == "pdf_page_1"
    assert result.metadata["pages"] == 1


@pytest.mark.asyncio
async def test_pdf_deck_file_path_document(tmp_path: Any) -> None:
    pdf_path = tmp_path / "deck.pdf"
    pdf_path.write_bytes(_minimal_pdf_bytes())
    conn = PDFDeckConnector({"file_path": str(pdf_path), "per_page": False})
    result = await conn.fetch()
    assert len(result.candidates) == 1
    assert "Hello PDF" in result.candidates[0].content_text
    assert result.candidates[0].source_label == "pdf_document"


@pytest.mark.asyncio
async def test_pdf_deck_missing_source() -> None:
    with pytest.raises(ConnectorError):
        await PDFDeckConnector({}).fetch()


@pytest.mark.asyncio
async def test_pdf_deck_invalid_base64() -> None:
    with pytest.raises(ConnectorError):
        await PDFDeckConnector({"file_content_b64": "!!!"}).fetch()


@pytest.mark.asyncio
async def test_pdf_deck_missing_file() -> None:
    with pytest.raises(ConnectorError):
        await PDFDeckConnector({"file_path": "/tmp/does_not_exist_xyz.pdf"}).fetch()
