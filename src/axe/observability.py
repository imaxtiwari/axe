"""Production observability: structured logging, Sentry, OpenTelemetry, metrics."""

from __future__ import annotations

import logging
import sys
from typing import Any

# Optional third-party observability libraries. Code falls back gracefully if they
# are not installed (e.g. in lightweight test environments).
try:
    from pythonjsonlogger.json import JsonFormatter
except ImportError:  # pragma: no cover
    JsonFormatter = None  # type: ignore

try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
except ImportError:  # pragma: no cover
    sentry_sdk = None  # type: ignore
    FastApiIntegration = None  # type: ignore

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
except ImportError:  # pragma: no cover
    trace = None  # type: ignore
    Resource = None  # type: ignore
    TracerProvider = None  # type: ignore

try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
except ImportError:  # pragma: no cover
    FastAPIInstrumentor = None  # type: ignore

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )
except ImportError:  # pragma: no cover
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    Counter = None  # type: ignore
    Histogram = None  # type: ignore
    generate_latest = None  # type: ignore


class _NoOpCounter:
    """In-memory counter used when prometheus_client is unavailable."""

    def __init__(self, store: dict[str, int], name: str) -> None:
        self._store = store
        self._name = name
        store.setdefault(name, 0)

    def inc(self, amount: float = 1) -> None:
        self._store[self._name] += int(amount)

    def labels(self, **_kwargs: Any) -> _NoOpCounter:
        return self


class _NoOpHistogram:
    """In-memory histogram used when prometheus_client is unavailable."""

    def __init__(self, store: dict[str, list[float]], name: str) -> None:
        self._store = store
        self._name = name
        store.setdefault(name, [])

    def observe(self, amount: float) -> None:
        self._store[self._name].append(amount)

    def labels(self, **_kwargs: Any) -> _NoOpHistogram:
        return self


class _NoOpMetrics:
    """Fallback metrics collector when prometheus_client is not installed."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, list[float]] = {}

    def counter(
        self, name: str, description: str = "", labels: tuple[str, ...] = ()
    ) -> _NoOpCounter:
        return _NoOpCounter(self._counters, name)

    def histogram(
        self, name: str, description: str = "", labels: tuple[str, ...] = ()
    ) -> _NoOpHistogram:
        return _NoOpHistogram(self._histograms, name)


_METRICS_REGISTRY: Any = _NoOpMetrics()
_PROMETHEUS_AVAILABLE = Counter is not None


def get_metrics() -> Any:
    """Return a metrics registry/counter factory."""
    return _METRICS_REGISTRY


def configure_logging(level: str = "INFO") -> None:
    """Configure structured JSON logging for production or plain logs for dev/test."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    if JsonFormatter is not None:
        handler.setFormatter(
            JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"levelname": "level", "asctime": "timestamp"},
            )
        )
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def init_sentry(dsn: str | None = None, app_env: str = "development") -> None:
    """Initialise Sentry when a DSN is configured."""
    if not dsn or sentry_sdk is None:
        return
    integration = FastApiIntegration() if FastApiIntegration is not None else None
    kwargs: dict[str, Any] = {
        "dsn": dsn,
        "environment": app_env,
        "traces_sample_rate": 0.2 if app_env == "production" else 1.0,
    }
    if integration is not None:
        kwargs["integrations"] = [integration]
    sentry_sdk.init(**kwargs)


def init_tracing(service_name: str) -> Any:
    """Initialise OpenTelemetry tracing if available."""
    if trace is None or TracerProvider is None:
        return None
    resource = Resource({"service.name": service_name}) if Resource is not None else None
    provider = TracerProvider(resource=resource) if resource else TracerProvider()
    trace.set_tracer_provider(provider)
    return provider


def instrument_fastapi(app: Any) -> None:
    """Instrument a FastAPI app with OpenTelemetry when available."""
    if FastAPIInstrumentor is not None:
        FastAPIInstrumentor.instrument_app(app)


def render_metrics() -> tuple[bytes, str]:
    """Render Prometheus metrics or a simple fallback payload."""
    if generate_latest is not None:
        return generate_latest(), CONTENT_TYPE_LATEST

    lines: list[str] = []
    for name, value in _METRICS_REGISTRY._counters.items():
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    for name, values in _METRICS_REGISTRY._histograms.items():
        lines.append(f"# TYPE {name} histogram")
        lines.append(f"{name}_count {len(values)}")
        if values:
            lines.append(f"{name}_sum {sum(values)}")
    return ("\n".join(lines).encode("utf-8"), CONTENT_TYPE_LATEST)
