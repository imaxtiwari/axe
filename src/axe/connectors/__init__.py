"""AXE connector registry and public exports."""

from __future__ import annotations

from typing import Any

from axe.connectors.base import BaseConnector, ConnectorError, ConnectorResult, IngestCandidate
from axe.connectors.broker_feed import BrokerFeedConnector
from axe.connectors.crm import CRMConnector
from axe.connectors.expert_network import ExpertNetworkConnector
from axe.connectors.pdf_deck import PDFDeckConnector
from axe.connectors.research_edge import ResearchEdgeConnector

# Registry mapping source_type strings to connector classes.
_CONNECTOR_REGISTRY: dict[str, type[BaseConnector]] = {
    BrokerFeedConnector.source_type: BrokerFeedConnector,
    PDFDeckConnector.source_type: PDFDeckConnector,
    CRMConnector.source_type: CRMConnector,
    ExpertNetworkConnector.source_type: ExpertNetworkConnector,
    ResearchEdgeConnector.source_type: ResearchEdgeConnector,
}


def register_connector(source_type: str, connector_cls: type[BaseConnector]) -> None:
    """Register a connector implementation for ``source_type``."""
    if not issubclass(connector_cls, BaseConnector):
        raise TypeError("Connector must subclass BaseConnector")
    _CONNECTOR_REGISTRY[source_type] = connector_cls


def get_connector_class(source_type: str) -> type[BaseConnector]:
    """Return the connector class registered for ``source_type``.

    Raises:
        ConnectorError: if no connector is registered.
    """
    try:
        return _CONNECTOR_REGISTRY[source_type]
    except KeyError as exc:
        raise ConnectorError(
            f"No connector registered for source_type '{source_type}'",
            is_retryable=False,
        ) from exc


def build_connector(source_type: str, config: dict[str, Any]) -> BaseConnector:
    """Instantiate the connector registered for ``source_type``."""
    connector_cls = get_connector_class(source_type)
    return connector_cls(config)


def list_connector_types() -> list[str]:
    """Return all registered source_type identifiers."""
    return list(_CONNECTOR_REGISTRY.keys())


__all__ = [
    "BaseConnector",
    "BrokerFeedConnector",
    "CRMConnector",
    "ConnectorError",
    "ConnectorResult",
    "ExpertNetworkConnector",
    "IngestCandidate",
    "PDFDeckConnector",
    "ResearchEdgeConnector",
    "build_connector",
    "get_connector_class",
    "list_connector_types",
    "register_connector",
]
