"""Citation extraction and verification for agent outputs.

``CitationExtractor`` turns an LLM output plus a list of raw sources into a
structured list of ``Citation`` objects. ``CitationVerifier`` checks each
snippet against the source content so downstream hallucination scoring has a
grounded signal.
"""

from __future__ import annotations

import re
import string
from collections.abc import Generator
from difflib import SequenceMatcher
from typing import Any

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A single citation linking an output claim to a source."""

    source_id: str | None = Field(default=None, description="Identifier of the cited source.")
    source_type: str = Field(
        default="unknown", description="Source type, e.g. signal, transcript, document."
    )
    snippet: str = Field(..., description="Claim or quote from the output.")
    span: tuple[int, int] | None = Field(
        default=None, description="Character span (start, end) of the cited snippet in the output."
    )
    verified: bool = Field(
        default=False, description="True when the snippet is found in the source."
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Verification confidence.")


class CitationExtractor:
    """Extract citations from agent output using markers or overlap heuristics."""

    # Match [1], [source-id], 【1】, (source: id)
    _MARKER_RE = re.compile(
        r"(?:\[(?P<bracket>[^\]]+)\]|【(?P<cjk>[^】]+)】|\(source:\s*(?P<src>[^)]+)\))",
        re.IGNORECASE,
    )

    # Real sentence terminators followed by whitespace or end-of-string.
    # Avoid splitting on punctuation inside numbers/currency/percentages.
    _SENTENCE_SPLIT_RE = re.compile(
        r"(?<![a-zA-Z])"  # don't break after abbreviations/short words
        r"(?<![$€£¥₹])"  # don't break immediately after currency symbols
        r"(?<!\d)"  # don't break immediately after a digit
        r"[.!?]+"
        r"(?=\s|$)",
        re.DOTALL,
    )

    def __init__(
        self,
        overlap_threshold: float = 0.5,
        snippet_max_chars: int = 240,
    ) -> None:
        self.overlap_threshold = overlap_threshold
        self.snippet_max_chars = snippet_max_chars

    def extract(
        self,
        output: str,
        raw_sources: list[Any] | None = None,
    ) -> list[Citation]:
        """Return citations found in ``output`` backed by ``raw_sources``.

        When the output contains citation markers such as ``[1]`` they are mapped
        to the corresponding source by index or id. When no markers are present,
        each sentence is matched to the source with the highest token overlap.
        """
        if not output:
            return []

        raw_sources = raw_sources or []
        normalized_sources = self._normalize_sources(raw_sources)

        markers = list(self._MARKER_RE.finditer(output))
        if markers:
            return self._extract_from_markers(output, markers, normalized_sources)
        return self._extract_from_overlap(output, normalized_sources)

    @staticmethod
    def _normalize_sources(raw_sources: list[Any]) -> list[dict[str, Any]]:
        """Coerce raw sources into a common dict shape."""
        normalized: list[dict[str, Any]] = []
        for idx, source in enumerate(raw_sources, start=1):
            if isinstance(source, str):
                normalized.append(
                    {
                        "id": str(idx),
                        "source_type": "text",
                        "content": source,
                    }
                )
            elif isinstance(source, dict):
                sid = source.get("id")
                normalized.append(
                    {
                        "id": str(sid if sid is not None else idx),
                        "source_type": source.get("source_type", "document"),
                        "content": source.get("content") or source.get("text") or "",
                    }
                )
            else:
                normalized.append(
                    {
                        "id": str(idx),
                        "source_type": type(source).__name__,
                        "content": str(source),
                    }
                )
        return normalized

    def _extract_from_markers(
        self,
        output: str,
        markers: list[re.Match[str]],
        sources: list[dict[str, Any]],
    ) -> list[Citation]:
        citations: list[Citation] = []
        for match in markers:
            label = (
                match.group("bracket") or match.group("cjk") or match.group("src") or ""
            ).strip()
            if not label:
                continue

            source = self._resolve_source(label, sources)
            snippet = self._claim_for_marker(output, match)
            citations.append(
                Citation(
                    source_id=source.get("id") if source else label,
                    source_type=source.get("source_type", "unknown") if source else "unknown",
                    snippet=snippet[: self.snippet_max_chars],
                    span=(match.start(), match.end()),
                )
            )
        return citations

    def _resolve_source(self, label: str, sources: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Map a citation label to a source by id or 1-based index."""
        # Try exact id match first.
        for source in sources:
            if str(source.get("id", "")) == label:
                return source

        # Fall back to 1-based positional index.
        try:
            idx = int(label)
            if 1 <= idx <= len(sources):
                return sources[idx - 1]
        except ValueError:
            pass
        return None

    def _split_sentences(self, text: str) -> Generator[tuple[int, int, str], None, None]:
        """Yield (start, end, sentence) slices splitting on real sentence boundaries."""
        # First split on terminal punctuation to get rough sentence chunks.
        parts: list[tuple[int, int, str]] = []
        start = 0
        for split_match in self._SENTENCE_SPLIT_RE.finditer(text):
            end = split_match.end()
            chunk = text[start:end].strip()
            if chunk:
                parts.append((start, end, chunk))
            start = end

        if start < len(text):
            chunk = text[start:].strip()
            if chunk:
                parts.append((start, len(text), chunk))

        if not parts:
            # Degenerate case: no terminators found. Treat whole text as one sentence.
            stripped = text.strip()
            if stripped:
                offset = text.index(stripped)
                yield (offset, offset + len(stripped), stripped)
            return

        for _idx, (seg_start, seg_end, sentence) in enumerate(parts):
            # Don't drop leading terminal punctuation of the next segment; it was
            # consumed by the previous match. This is okay because punctuation is
            # attached to the sentence it terminates.
            yield (seg_start, seg_end, sentence)

    def _claim_for_marker(self, output: str, match: re.Match[str]) -> str:
        """Return the sentence containing or immediately preceding the citation marker."""
        marker_start = match.start()

        # Prefer the sentence whose span contains the marker.
        for sentence_start, sentence_end, sentence in self._split_sentences(output):
            if sentence_start <= marker_start <= sentence_end:
                snippet = self._MARKER_RE.sub("", sentence).strip()
                return snippet

        # Fallback to the sentence that ends closest to (but before) the marker.
        best: tuple[int, int, str] | None = None
        for sentence_start, sentence_end, sentence in self._split_sentences(output):
            if sentence_end <= marker_start:
                best = (sentence_start, sentence_end, sentence)

        if best is not None:
            snippet = self._MARKER_RE.sub("", best[2]).strip()
            return snippet

        # Ultimate fallback: text immediately before the marker.
        snippet = output[max(0, marker_start - self.snippet_max_chars) : marker_start]
        snippet = self._MARKER_RE.sub("", snippet).strip()
        return snippet

    def _extract_from_overlap(self, output: str, sources: list[dict[str, Any]]) -> list[Citation]:
        citations: list[Citation] = []
        if not sources:
            return citations

        for sentence_start, sentence_end, sentence in self._split_sentences(output):
            if len(sentence) < 8:
                continue

            best_source: dict[str, Any] | None = None
            best_score = self.overlap_threshold
            for source in sources:
                score = self._token_overlap(sentence, source.get("content", ""))
                if score > best_score:
                    best_score = score
                    best_source = source

            if best_source is not None:
                citations.append(
                    Citation(
                        source_id=best_source.get("id"),
                        source_type=best_source.get("source_type", "document"),
                        snippet=sentence[: self.snippet_max_chars],
                        span=(sentence_start, sentence_end),
                        confidence=round(best_score, 3),
                    )
                )
        return citations

    @staticmethod
    def _token_overlap(a: str, b: str) -> float:
        """Return token Jaccard overlap between two strings."""
        tokens_a = set(CitationExtractor._normalize(a).split())
        tokens_b = set(CitationExtractor._normalize(b).split())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union)

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercase and strip punctuation."""
        lowered = text.lower()
        return lowered.translate(str.maketrans("", "", string.punctuation))


class CitationVerifier:
    """Verify that extracted citation snippets are actually present in sources."""

    def __init__(self, substring_threshold: float = 0.5, overlap_threshold: float = 0.4) -> None:
        self.substring_threshold = substring_threshold
        self.overlap_threshold = overlap_threshold

    def verify(
        self,
        citations: list[Citation],
        raw_sources: list[Any] | None = None,
    ) -> list[Citation]:
        """Return a new list of citations with ``verified`` and ``confidence`` set."""
        if not citations:
            return []

        sources = CitationExtractor._normalize_sources(raw_sources or [])
        source_map = {str(s.get("id")): s for s in sources if s.get("id") is not None}

        verified: list[Citation] = []
        for citation in citations:
            source = source_map.get(str(citation.source_id)) if citation.source_id else None
            if source is None:
                verified.append(citation.model_copy(update={"verified": False, "confidence": 0.0}))
                continue

            is_verified, confidence = self._verify_snippet(
                citation.snippet, source.get("content", "")
            )
            verified.append(
                citation.model_copy(
                    update={
                        "verified": is_verified,
                        "confidence": round(confidence, 3),
                    }
                )
            )
        return verified

    def _verify_snippet(self, snippet: str, source_content: str) -> tuple[bool, float]:
        """Return (verified, confidence) for a snippet against source content."""
        norm_snippet = CitationExtractor._normalize(snippet)
        norm_source = CitationExtractor._normalize(source_content)

        if not norm_snippet or not norm_source:
            return False, 0.0

        # Direct substring match is strongest evidence.
        if norm_snippet in norm_source:
            return True, 1.0

        # SequenceMatcher handles small rewordings / formatting differences.
        ratio = SequenceMatcher(None, norm_snippet, norm_source).ratio()
        if ratio >= self.substring_threshold:
            return True, ratio

        # Token Jaccard overlap as a fallback.
        overlap = CitationExtractor._token_overlap(snippet, source_content)
        if overlap >= self.overlap_threshold:
            return True, overlap

        return False, max(ratio, overlap)


__all__ = ["Citation", "CitationExtractor", "CitationVerifier"]
