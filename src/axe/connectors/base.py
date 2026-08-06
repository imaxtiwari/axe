"""Base connector ABC and shared types for AXE ingestion connectors.

A connector converts an external source (broker statement, PDF deck, CRM JSON,
expert-network transcript, research API) into a normalized stream of
``IngestCandidate`` objects. The ``ConnectorService`` then deduplicates,
persists them as ``RawIngest`` rows, and enqueues specialist processing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from axe.ingestion.hashing import content_hash, hash_dict, idempotency_key


class ConnectorError(Exception):
    """Raised when a connector fails in a way the caller should handle.

    ``is_retryable`` hints to the worker whether the run should be retried.
    """

    def __init__(
        self,
        message: str,
        *,
        is_retryable: bool = False,
        source_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.is_retryable = is_retryable
        self.source_type = source_type


@dataclass(frozen=True)
class IngestCandidate:
    """Normalized item produced by a connector before deduplication.

    Fields:
      - external_id: source-native identifier (message id, file name, API id).
      - source_label: human-readable label for provenance (filename, API name).
      - raw_payload_json: the cleaned, serializable payload from the source.
      - extracted_signal_json: optional pre-extracted structured signal.
      - content_text: normalized text used for content hashing.
      - ticker: optional upstream ticker if already known.
      - dedup_key: optional source-specific idempotency key.
    """

    external_id: str | None = None
    source_label: str | None = None
    raw_payload_json: dict[str, Any] = field(default_factory=dict)
    extracted_signal_json: dict[str, Any] = field(default_factory=dict)
    content_text: str = ""
    ticker: str | None = None
    dedup_key: str | None = None

    def content_hash(self) -> str:
        """Return a stable SHA-256 hash of the candidate content."""
        return content_hash(self.content_text)

    def build_dedup_key(self, source_type: str) -> str | None:
        """Return a source-scoped idempotency key if external_id is present."""
        if self.external_id:
            return idempotency_key(source_type, self.external_id)
        return self.dedup_key

    def hash_payload(self) -> str:
        """Return a deterministic hash of the raw payload JSON."""
        return hash_dict(self.raw_payload_json)


@dataclass
class ConnectorResult:
    """Result of a single connector run.

    ``candidates`` lists all items fetched/parsed from the source.
    ``cursor`` is an opaque bookmark the connector can use for incremental runs.
    ``metadata`` carries run-level diagnostics (counts, errors, API limits).
    """

    source_type: str
    candidates: list[IngestCandidate] = field(default_factory=list)
    cursor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.candidates


class BaseConnector(ABC):
    """Abstract base class for AXE ingestion connectors.

    Implementations must provide:
      - ``source_type``: short string identifier.
      - ``fetch``: async generator or batch fetch returning ``ConnectorResult``.
    """

    source_type: str

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    async def fetch(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> ConnectorResult:
        """Fetch and normalize items from the external source.

        Args:
            cursor: Opaque resume token from a previous run.
            limit: Optional cap on the number of candidates returned.

        Returns:
            A ``ConnectorResult`` containing normalized ``IngestCandidate`` rows.

        Raises:
            ConnectorError: on failures the caller should handle.
        """

    def get_config_value(self, key: str, default: Any | None = None) -> Any:
        """Return a value from the connector configuration."""
        return self.config.get(key, default)

    def require_config(self, key: str) -> Any:
        """Return a required config value or raise ``ConnectorError``."""
        value = self.config.get(key)
        if value is None:
            raise ConnectorError(
                f"Missing required connector config: {key}",
                source_type=self.source_type,
                is_retryable=False,
            )
        return value


__all__ = [
    "BaseConnector",
    "ConnectorError",
    "ConnectorResult",
    "IngestCandidate",
]
