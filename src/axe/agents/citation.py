"""Citation extraction and verification for agent outputs.

``CitationExtractor`` turns an LLM output plus a list of raw sources into a
structured list of ``Citation`` objects. ``CitationVerifier`` checks each
snippet against the source content so downstream hallucination scoring has a
grounded signal.
"""

from __future__ import annotations

import re
import string
from difflib import SequenceMatcher
from typing import Any

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A single citation linking an output claim to a source."""

    source_id: str | None = Field(
        default=None, description="Identifier of the cited source."
    )
    source_type: str = Field(
        default="unknown", description="Source type, e.g. signal, transcript, document."
    )
    snippet: str = Field(..., description="Claim or quote from the output.")
    span: tuple[int, int] | None = Field(
        default=None, description="Character span (start, end) of the cited snippet in the output."
    )
    verified: bool = Field(default=False, description="True when the snippet is found in the source.")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Verification confidence."
    )


class CitationExtractor:
    """Extract citations from agent output using markers or overlap heuristics."""

    # Match [1], [source-id], 【1】, (source: id)
    _MARKER_RE = re.compile(
        r"(?:\[(?P<bracket>[^\]]+)\]|【(?P<cjk>[^】]+)】|\(source:\s*(?P<src>[^)]+)\))",
        re.IGNORECASE,
    )

    # Split text into sentences without destroying the marker positions.
    _SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?(?=\s|$)", re.DOTALL)

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

    def _resolve_source(
        self, label: str, sources: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
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

    def _claim_for_marker(self, output: str, match: re.Match[str]) -> str:
        """Return the sentence containing the citation marker."""
        # Walk backward from the character just before the marker, skipping any
        # whitespace so the marker's own padding does not terminate the search,
        # then continue back to the previous sentence boundary.
        sentence_start = match.start()
        passed_whitespace = False
        while sentence_start > 0:
            ch = output[sentence_start - 1]
            if ch.isspace():
                sentence_start -= 1
                passed_whitespace = True
                continue
            if passed_whitespace and ch in ".!?\n":
                # We reached the boundary that terminates the claim sentence;
                # stop before it.
                break
            sentence_start -= 1

        # Walk forward from the character just after the marker to find the end
        # of the sentence. Include any trailing terminator.
        sentence_end = match.end()
        while sentence_end < len(output) and output[sentence_end] not in ".!?\n":
            sentence_end += 1
        if sentence_end < len(output) and output[sentence_end] in ".!":
            sentence_end += 1

        # Slice the whole sentence and remove the marker afterwards.
        snippet = output[sentence_start:sentence_end].strip()
        snippet = self._MARKER_RE.sub("", snippet).strip()
        return snippet

    def _extract_from_overlap(
        self, output: str, sources: list[dict[str, Any]]
    ) -> list[Citation]:
        citations: list[Citation] = []
        if not sources:
            return citations

        for sentence_match in self._SENTENCE_RE.finditer(output):
            sentence = sentence_match.group().strip()
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
                        span=(sentence_match.start(), sentence_match.end()),
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

    def __init__(self, substring_threshold: float = 0.55, overlap_threshold: float = 0.5) -> None:
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
