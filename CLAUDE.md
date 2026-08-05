# AXE Developer Guide

This document is the source of truth for how the AXE codebase is organized, how to run it, and how to extend it.

## Table of Contents

1. [Guide Map](#guide-map)
2. [Getting Started](#getting-started)
3. [Project Layout](#project-layout)
4. [Conventions](#conventions)
5. [Exceptions & Error Handling](#exceptions--error-handling)
6. [Agents](#agents)
7. [Database & Migrations](#database--migrations)
8. [Ingestion Pipeline](#ingestion-pipeline)
9. [MNPI Review Gate](#mnpi-review-gate)
10. [Alerting](#alerting)
11. [Testing](#testing)
12. [Common Tasks](#common-tasks)
13. [Production Notes](#production-notes)

## Guide Map

This is the developer-facing source of truth. For a step-by-step install and first-use walkthrough, see [`GETTING_STARTED.md`](GETTING_STARTED.md). Product requirements live in [`docs/AXE_PRD_v2.1_Delta.md`](docs/AXE_PRD_v2.1_Delta.md).

## Getting Started

Prerequisites: Python 3.12+

```bash
cd axe
git clone https://github.com/imaxtiwari/axe.git .  # if not already cloned
cp .env.example .env
# Fill in: DATABASE_URL, AZURE_FOUNDRY_ENDPOINT, AZURE_FOUNDRY_API_KEY, AZURE_FOUNDRY_MODEL,
#          SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, RESEND_API_KEY, AXE_EMAIL_DOMAIN,
#          POLYGON_API_KEY, GOOGLE_CLIENT_ID/SECRET (optional for local dev)
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ocr]"
alembic upgrade head
uvicorn axe.main:app --reload
```

Health check: `curl http://localhost:8000/healthz`

Run tests: `pytest`

Docker alternative:

```bash
docker compose up --build
docker compose exec axe alembic upgrade head
```

## Project Layout

| Path | Purpose |
|------|---------|
| `alembic/` | SQLAlchemy/Alembic migrations; one file per schema change |
| `docs/` | Product and design docs, including `AXE_PRD_v2.1_Delta.md` |
| `scripts/` | Operational scripts (backups, seed data, etc.) |
| `src/axe/agents/` | LLM-backed agents. Stateless helpers when possible; DB state passed in explicitly. |
| `src/axe/db/` | Models (`models.py`), base metadata (`base.py`), async session factory (`session.py`) |
| `src/axe/ingestion/` | Parsers, handlers, dedup, retry, and the ingest worker |
| `src/axe/main.py` | FastAPI app factory and middleware wiring |
| `src/axe/models/` | Pydantic request/response models shared by routers and agents |
| `src/axe/routers/` | FastAPI route modules (`onboarding.py`, `transcripts.py`) |
| `src/axe/security/` | Encryption, audit, isolation |
| `src/axe/services/` | Business logic that sits between routers and agents (alerts, onboarding, scheduling) |
| `tests/` | Pytest suite. `conftest.py` holds async DB fixtures. |

## Conventions

- **Formatting**: `ruff` + `black`, line length 100.
- **Imports**: Use `from __future__ import annotations`, then stdlib, third-party, first-party.
- **Types**: Full type hints required. `mypy --strict` in CI.
- **Async**: All DB access is async via `sqlalchemy.ext.asyncio`. Services/agents should be `async` if they touch IO.
- **LLM Providers**: Abstracted behind `axe.agents.llm.LLMProvider`. `MockProvider` is used in tests. Production uses `AzureFoundryProvider`.
- **Settings**: All env vars flow through `axe.config.Settings` (Pydantic settings).
- **Observability**: Metrics via Prometheus, tracing via OpenTelemetry, structured logging with `python-json-logger`.

## Exceptions & Error Handling

All application-level exceptions inherit from `AXEError` in `src/axe/exceptions.py`.

```python
raise IsolationError("cross-pm access attempt")
```

Hierarchy:
- `AXEError` — base; unknown exceptions are normalized to this in the global middleware.
- `AuthError` — HTTP 401, code `auth.failed`.
- `IsolationError` — HTTP 403, code `isolation.violation`; automatically writes an `AuditLog` record.
- `AuditError` — HTTP 500, code `audit.failed`.

Error response envelope:

```json
{
  "request_id": "<trace id>",
  "code": "axe.internal_error",
  "message": "An internal error occurred."
}
```

- `request_id` comes from the active `RequestContext` or a generated UUID.
- Internal details and stack traces are never returned to the client.
- Every handled exception increments the Prometheus counter `axe.errors.total{code}`.
- Register handlers via `create_app()` in `src/axe/main.py` using `register_exception_handlers(app)` and `install_global_error_middleware(app)`.

## Agents

### DriftDetectionAgent

File: `src/axe/agents/drift_detect.py`

Classifies an external signal against a thesis assumption.

```python
pair = await agent.classify(
    signal_text="Q3 revenue fell 5% YoY.",
    assumption_text="Revenue grows at least 10% YoY through 2025.",
)
# pair.stance in {"CONFIRMS", "CONTRADICTS", "NEUTRAL", "UNCERTAIN"}
```

Highlights:
- Embedding pre-filter via `cosine_similarity`. Only calls the LLM when similarity ≥ `similarity_threshold` (default 0.72).
- Structured output parsed into `SignalAssumptionPair`.
- `classify_assumptions` maps a signal to multiple assumptions by `assumption_id`.

### ThesisTestAgent

Also in `src/axe/agents/drift_detect.py`.

For each assumption, generates pass/fail test statements and evaluates a signal against them.

```python
tests = await test_agent.generate_tests(assumptions)
results = await test_agent.evaluate(signal_text, tests)
```

### MorningBriefAgent

File: `src/axe/agents/morning_brief.py`

Builds the daily brief:
1. Fetch active PM and tickers.
2. Collect latest signals and theses.
3. Classify signals vs assumptions.
4. Pick a `focus_one` thesis (contradictions win, then highest confidence confirms).
5. Add a catalyst calendar (earnings, FDA, macro events within 7 days).
6. Persist and optionally deliver via Slack/email.

### BriefReplyAgent

File: `src/axe/agents/brief_reply.py`

Parses a PM reply to a morning brief and either:
- Updates a thesis assumption (creates new `ThesisVersion`).
- Dismisses a signal (`SignalFeedback`).
- Records a follow-up question.

## Database & Migrations

Models: `src/axe/db/models.py`

Key tables:
- `pm_users`, `fund_entities`
- `ticker_registry`
- `thesis_version` (JSON `key_assumptions`, versioned)
- `signal_log` (raw ingested signals, foreign keys to PM/thesis; `mnpi_flag` for review state)
- `mnpi_review_queue` (pending compliance review items; holds blocked alert payloads)
- `signal_feedback` (PM dismissals / confirmations)
- `broken_assumptions` (alert deduplication)
- `morning_briefs`, `brief_replies`

Create a migration after any model change:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

## MNPI Review Gate

Before any alert is dispatched, signals are screened for material non-public information (MNPI).

Components:
- `src/axe/agents/mnpi_review.py` — `MNPIReviewAgent` scores signals.
- `src/axe/services/mnpi.py` — `MNPIService` creates/updates review items and releases alerts on approval.
- `src/axe/routers/mnpi.py` — `POST /api/v1/mnpi/{review_id}/decision` for compliance approve/reject.

`MNPIReviewAgent` returns a JSON structure with `mnpi_score`, `materiality_score`, and `reasoning`. The agent uses a fast keyword heuristic by default; when `LLMProvider` is configured it calls the model for a calibrated score. Flagging threshold is controlled by `MNPI_THRESHOLD` (default `0.7`).

`process_transcript_handler` in `src/axe/ingestion/handlers.py` calls the service after drift detection:
- If flagged, the alert payloads are stored on `mnpi_review_queue`, `signal_log.mnpi_flag` is set, and no alert is enqueued.
- If clean, alerts proceed to `RetryQueue` for dispatch.

Approval/rejection flow:
1. Reviewer calls `POST /api/v1/mnpi/{review_id}/decision` with `{"decision": "approved"|"rejected", "reviewer_id": "..."}`.
2. On approval, `signal_log.mnpi_flag` is cleared and the stored alert payloads are enqueued.
3. On rejection, the flag remains set and the alerts are discarded.
4. Each decision writes an `AuditLog` entry (`mnpi_review_approved` or `mnpi_review_rejected`).

Tests: `tests/test_security.py` contains MNPI service and handler tests under the `mnpi` keyword.

## Ingestion Pipeline

Entry points:
- `src/axe/ingestion/worker.py` — background worker that polls/handles new items.
- `src/axe/routers/transcripts.py` — `POST /transcripts/polygon` for real-time Polygon transcripts.

Flow:
1. Raw input is hashed and deduplicated.
2. Signal extraction: text/earnings transcript → structured signal.
3. Relevant theses are loaded.
4. `DriftDetectionAgent` classifies signal vs each assumption.
5. Polygon + contradiction triggers `AlertService.maybe_fire_earnings_alert`.

## Alerting

File: `src/axe/services/alert.py`

- `AlertService.maybe_fire_earnings_alert(pm, signal, results)` fires a Slack DM and email if `source_type == "polygon"` and any stance is `CONTRADICTS`.
- `BrokenAssumption` records `(pm_id, ticker, assumption_id)` to suppress duplicate alerts.
- Format: `[TICKER] THESIS ALERT — [assumption] may be breaking. Evidence: ... [source link]`
- SLA: alert must be sent within 30 minutes of `signal_log.created_at`.

## Testing

```bash
pytest                  # all tests, with coverage
pytest tests/test_drift.py  # drift eval
pytest -k earnings      # alert SLA tests
```

Evaluation datasets:
- `tests/drift_eval_dataset.py` — 50 labeled signal/assumption pairs used by `test_drift_eval_dataset`.

Coverage is reported in CI but does not currently fail the build; the `fail_under` target is 0 until the suite crosses 85%.

## Common Tasks

### Add a new router

1. Create `src/axe/routers/<name>.py`.
2. Implement a `fastapi.APIRouter`.
3. Import and include in `src/axe/main.py`.

### Add a new ingestion handler

1. Add handler in `src/axe/ingestion/handlers.py`.
2. Register it in `src/axe/ingestion/worker.py`.
3. Add tests in `tests/test_ingestion.py`.

### Change the drift similarity threshold

In `src/axe/agents/drift_detect.py`, the default threshold is 0.72 (calibrated). Override per-instance via:

```python
agent = DriftDetectionAgent(
    provider=provider,
    embedding_model=embed,
    similarity_threshold=0.80,
)
```

## Production Notes

- Azure Foundry endpoint + API key are required.
- Use a managed Postgres instead of SQLite; set `DATABASE_URL` accordingly.
- Run migrations as a startup job.
- Secrets (API keys, SMTP) should be injected via env, never committed.
- Review MNPI flags and audit logs before enabling live trading workflows.
