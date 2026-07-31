"""Content hashing and source-specific idempotency helpers for ingestion dedup."""

import hashlib
import re
from collections.abc import Mapping


def normalize_text(text: str | None) -> str:
    """Normalize text for stable hashing.

    Lowercases, strips HTML tags, collapses whitespace, and trims.
    """
    if text is None:
        return ""
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain.lower()


def content_hash(content: str | None) -> str:
    """Return SHA-256 hex digest of normalized content."""
    normalized = normalize_text(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def idempotency_key(source_type: str, source_specific_id: str, extra: str | None = None) -> str:
    """Build a source-specific idempotency key.

    Examples:
      - Gmail message: ("gmail", msg_id)
      - Slack event:   ("slack", event_id)
      - Polygon transcript: ("polygon", transcript_id)
    """
    parts = [source_type.lower().strip(), source_specific_id]
    if extra:
        parts.append(extra)
    return ":".join(parts)


def hash_dict(payload: Mapping[str, object]) -> str:
    """Return a deterministic SHA-256 hash of a JSON-serializable dict."""
    import json

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["normalize_text", "content_hash", "idempotency_key", "hash_dict"]
