"""Tests for citation extraction and verification."""

from __future__ import annotations

import pytest

from axe.agents.citation import Citation, CitationExtractor, CitationVerifier


SAMPLE_SOURCE = {
    "id": "1",
    "source_type": "earnings_release",
    "content": "Apple reported Q1 revenue of $123.9 billion. iPhone revenue grew 6% year over year.",
}


class TestCitationExtractor:
    def test_extract_bracket_markers(self) -> None:
        output = "Apple's revenue was $123.9 billion. [1]"
        citations = CitationExtractor().extract(output, [SAMPLE_SOURCE])

        assert len(citations) == 1
        assert citations[0].source_id == "1"
        assert citations[0].source_type == "earnings_release"
        assert "$123.9 billion" in citations[0].snippet
        assert citations[0].span is not None
        assert citations[0].span[0] < citations[0].span[1]

    def test_extract_cjk_markers(self) -> None:
        output = "iPhone revenue grew 6%.【1】"
        citations = CitationExtractor().extract(output, [SAMPLE_SOURCE])

        assert len(citations) == 1
        assert citations[0].source_id == "1"
        assert "6%" in citations[0].snippet

    def test_extract_source_prefix_markers(self) -> None:
        output = "Margins compressed. (source: 1)"
        citations = CitationExtractor().extract(output, [SAMPLE_SOURCE])

        assert len(citations) == 1
        assert citations[0].source_id == "1"
        assert "Margins compressed" in citations[0].snippet

    def test_extract_falls_back_to_overlap(self) -> None:
        output = "Apple reported Q1 revenue of $123.9 billion."
        citations = CitationExtractor().extract(output, [SAMPLE_SOURCE])

        assert len(citations) == 1
        assert citations[0].source_id == "1"
        assert citations[0].confidence > 0.5

    def test_extract_no_sources_returns_empty(self) -> None:
        output = "Some unsubstantiated claim about the market."
        citations = CitationExtractor().extract(output, [])

        assert citations == []

    def test_extract_empty_output(self) -> None:
        assert CitationExtractor().extract("", [SAMPLE_SOURCE]) == []

    def test_resolve_by_index_and_id(self) -> None:
        extractor = CitationExtractor()
        sources = [SAMPLE_SOURCE, {"id": "xyz", "source_type": "note", "content": "hello"}]

        assert extractor._resolve_source("1", sources) == SAMPLE_SOURCE
        assert extractor._resolve_source("xyz", sources) == sources[1]
        assert extractor._resolve_source("99", sources) is None


class TestCitationVerifier:
    def test_verifies_substring_match(self) -> None:
        citation = Citation(
            source_id="1",
            source_type="earnings_release",
            snippet="Apple reported Q1 revenue of $123.9 billion",
        )
        verified = CitationVerifier().verify([citation], [SAMPLE_SOURCE])

        assert len(verified) == 1
        assert verified[0].verified is True
        assert verified[0].confidence == 1.0

    def test_rejects_unrelated_snippet(self) -> None:
        citation = Citation(
            source_id="1",
            source_type="earnings_release",
            snippet="Tesla delivered 500,000 vehicles in Q4",
        )
        verified = CitationVerifier().verify([citation], [SAMPLE_SOURCE])

        assert verified[0].verified is False
        assert verified[0].confidence < 0.5

    def test_missing_source_is_unverified(self) -> None:
        citation = Citation(
            source_id="99",
            source_type="unknown",
            snippet="Some claim",
        )
        verified = CitationVerifier().verify([citation], [SAMPLE_SOURCE])

        assert verified[0].verified is False
        assert verified[0].confidence == 0.0

    def test_empty_citations(self) -> None:
        assert CitationVerifier().verify([], [SAMPLE_SOURCE]) == []

    def test_fuzzy_match(self) -> None:
        citation = Citation(
            source_id="1",
            source_type="earnings_release",
            snippet="Apple Q1 revenue $123.9 billion",
        )
        verified = CitationVerifier().verify([citation], [SAMPLE_SOURCE])

        assert verified[0].verified is True
        assert 0.0 < verified[0].confidence < 1.0


class TestCitationModel:
    def test_citation_defaults(self) -> None:
        citation = Citation(snippet="A claim")

        assert citation.source_id is None
        assert citation.source_type == "unknown"
        assert citation.verified is False
        assert citation.confidence == 0.0

    def test_citation_confidence_bounds(self) -> None:
        with pytest.raises(ValueError):
            Citation(snippet="claim", confidence=1.5)

        with pytest.raises(ValueError):
            Citation(snippet="claim", confidence=-0.1)
