# Getting Started with AXE

This guide walks you through installing, configuring, and running AXE from scratch. AXE is a pre-alpha AI co-pilot and investment operating system for portfolio managers, investment teams, and GPs.

> **Status:** Pre-alpha (v0.1.0). This is an active development build. Fund-level compliance review is required before any production deployment.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone the Repository](#2-clone-the-repository)
3. [Configure Environment Variables](#3-configure-environment-variables)
4. [Option A: Local Python Install](#option-a-local-python-install)
5. [Option B: Docker Compose](#option-b-docker-compose)
6. [Run Database Migrations](#6-run-database-migrations)
7. [Verify the Installation](#7-verify-the-installation)
8. [Run the Ingestion Worker](#8-run-the-ingestion-worker)
9. [First-Time Use: Create a PM & Capture a Thesis](#9-first-time-use-create-a-pm--capture-a-thesis)
10. [Trigger a Drift Alert](#10-trigger-a-drift-alert)
11. [Generate a Morning Brief](#11-generate-a-morning-brief)
12. [Run the Test Suite](#12-run-the-test-suite)
13. [Linting, Formatting & Type Checks](#13-linting-formatting--type-checks)
14. [Troubleshooting](#14-troubleshooting)
15. [Next Steps](#15-next-steps)

---

## 1. Prerequisites

You need the following before you begin:

- **Python 3.12+**
- **Git**
- **Docker + Docker Compose** (optional, but recommended for a consistent environment)
- **Make** or a POSIX-compatible shell
- A **Unix-like shell** (macOS / Linux / WSL). Windows PowerShell is untested.
- Accounts and API keys for the services you want to use:
  - **Azure Foundry** (required for LLM calls)
  - **Slack** (required for Slack alerts)
  - **Resend** (required for email alerts)
  - **Polygon.io** (required for earnings transcript ingestion)
  - **Google Cloud Console** (required for Gmail / Calendar OAuth)

> For pure local testing you can skip the external services and rely on the `MockProvider`, but core features such as drift detection, brief generation, and alerts will not run end to end without an LLM provider.

---

## 2. Clone the Repository

```bash
git clone https://github.com/imaxtiwari/axe.git
cd axe
```

---

## 3. Configure Environment Variables

Copy the example environment file and fill in your keys.

```bash
cp .env.example .env
```

Open `.env` in your editor and update every `replace-with-...` or `your-...` value. The most important keys are listed below.

| Variable | Purpose | Required? |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy database URL. Defaults to local SQLite. | Yes |
| `CHROMA_PERSIST_DIR` | Directory for the ChromaDB vector store. | Yes |
| `ENCRYPTION_KEY` | Fernet key for encrypting sensitive data at rest. | Strongly recommended |
| `AZURE_FOUNDRY_ENDPOINT` | Azure AI Foundry endpoint. | Yes for LLM features |
| `AZURE_FOUNDRY_API_KEY` | Azure AI Foundry API key. | Yes for LLM features |
| `AZURE_FOUNDRY_MODEL` | Model deployment, e.g. `gpt-4o-mini`. | Yes for LLM features |
| `SLACK_BOT_TOKEN` | Slack bot OAuth token. | Yes for Slack alerts |
| `SLACK_SIGNING_SECRET` | Slack request signing secret. | Yes for Slack webhooks |
| `RESEND_API_KEY` | Resend API key. | Yes for email alerts |
| `AXE_EMAIL_DOMAIN` | Domain used for outbound email. | Yes for email alerts |
| `POLYGON_API_KEY` | Polygon.io API key. | Yes for market data |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID. | Yes for Gmail / Calendar |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret. | Yes for Gmail / Calendar |

### Generate an encryption key

Use the helper script (or any Fernet key generator):

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

Copy the printed value into `.env` as `ENCRYPTION_KEY`.

---

## Option A: Local Python Install

### A.1 Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### A.2 Install the package and dependencies

```bash
pip install --upgrade pip setuptools wheel
pip install -e ".[dev,ocr]"
```

- `".[dev]"` installs pytest, ruff, black, mypy, and coverage tools.
- `".[ocr]"` installs PDF/OCR dependencies (`pymupdf`, `pytesseract`, `pillow`).

### A.3 Run migrations

```bash
alembic upgrade head
```

This creates the SQLite database and all required tables in `./data/axe.db`.

### A.4 Start the API server

```bash
uvicorn axe.main:app --reload --host 127.0.0.1 --port 8000
```

Leave this terminal running and open a new one for the worker.

---

## Option B: Docker Compose

This is the simplest way to run the API, worker, and an optional ChromaDB server together.

### B.1 Build and start the stack

```bash
docker compose up --build
```

Services that start:

- `axe` — FastAPI server on `http://localhost:8000` (with hot reload)
- `worker` — ingestion worker (`axe.ingestion.cli`)
- `chroma` — local ChromaDB server on `http://localhost:8001` (optional)

Make sure `.env` exists first; docker-compose mounts it into every container.

### B.2 Run migrations inside the container

On first run (or after pulling new migrations):

```bash
docker compose exec axe alembic upgrade head
```

### B.3 Stopping the stack

```bash
docker compose down
```

Add `-v` if you also want to delete the persisted `./data` volume.

---

## 6. Run Database Migrations

Whether you use local Python or Docker, apply migrations before first use:

```bash
# Local Python
alembic upgrade head

# Docker Compose
docker compose exec axe alembic upgrade head
```

To create a new migration after editing `src/axe/db/models.py`:

```bash
alembic revision --autogenerate -m "describe your change"
alembic upgrade head
```

---

## 7. Verify the Installation

Open a second terminal and run:

```bash
curl http://localhost:8000/healthz
```

Expected response:

```json
{"status":"ok","env":"development"}
```

Also check readiness:

```bash
curl http://localhost:8000/ready
```

Expected response:

```json
{"status":"ready"}
```

If both return successfully, AXE is installed correctly.

---

## 8. Run the Ingestion Worker

The worker processes incoming signals (Polygon transcripts, emails, PDFs, Slack messages).

### Local Python

In a new terminal with the virtual environment activated:

```bash
python -m axe.ingestion.cli
```

### Docker Compose

```bash
docker compose up -d worker
```

The worker polls for new ingestion jobs and routes them through extraction, drift detection, and alerting.

---

## 9. First-Time Use: Create a PM & Complete Onboarding

The current onboarding path is API-based. Before you can use it, a PM user must exist in the database. In production that is handled by an upstream identity/account flow; for local testing, seed one directly (or import the user model via a small script) so that you have a valid `pm_id` for the steps below. The examples below use `httpie`; swap for `curl` if you prefer.

### 9.1 Start onboarding

```bash
http POST http://localhost:8000/api/v1/onboarding/start \
  pm_id=<pm_id>
```

Save the returned `pm_id` and current `state`. The router requires the request context to match this PM, so include the relevant identity header (or run in dev bypass mode) as documented in `src/axe/security/context.py`.

### 9.2 Answer the cold-start questions

```bash
http POST http://localhost:8000/api/v1/onboarding/answer \
  pm_id=<pm_id> \
  question_number=1 \
  answer="3-5 years"
```

Repeat for `question_number` 2 through 5. The service returns the next prompt each time; after question 5 the state advances to `thesis_capture`.

There are five fixed questions, in order:

1. **q1_hold_period** — *What's your typical hold period for a core position?*
2. **q2_cutting_losers** — *How do you decide when to cut a losing position?*
3. **q3_edge** — *What is your edge as an investor?*
4. **q4_when_wrong** — *Tell me about a time you were wrong. What did you learn?*
5. **q5_double_down** — *Under what conditions do you double down?*

### 9.3 Capture initial tickers

The onboarding thesis-capture step is deliberately lightweight: it records the tickers you want to follow, not a full investment thesis.

```bash
http POST http://localhost:8000/api/v1/onboarding/thesis-capture \
  pm_id=<pm_id> \
  tickers:='["AAPL","MSFT","NVDA"]'
```

To skip ticker capture and finish onboarding:

```bash
http POST http://localhost:8000/api/v1/onboarding/thesis-capture \
  pm_id=<pm_id> \
  skip:=true
```

On completion the PM's `onboarding_complete` flag is set to `true`. AXE is now ready to use.

### 9.4 Create a full investment thesis (separate from onboarding)

Onboarding does **not** currently create a versioned public-market thesis. To capture one with assumptions for drift detection, use the deal-creation flow:

```bash
# 1. Create a deal room
http POST http://localhost:8000/api/v1/deals \
  name="NVDA Long" \
  asset_class="equity" \
  target_ticker_or_private_name="NVDA"

# 2. Add a versioned thesis with falsifiable assumptions
http POST http://localhost:8000/api/v1/deals/<deal_id>/thesis \
  stage="conviction" \
  bull_case="Data center capex remains strong through 2025." \
  key_assumptions:='[
    "Revenue grows at least 10% YoY through 2025",
    "Data center revenue share stays above 40%"
  ]'
```

> Tips for the best drift-detection results:
> - Make assumptions falsifiable (contain numbers, dates, or clear conditions).
> - Keep each assumption to a single claim.
> - Avoid ambiguity words like "strong" or "good".

---

## 10. Trigger a Drift Alert

If Polygon earnings transcripts are configured, AXE will automatically detect contradictions and fire alerts. To test locally without Polygon, call the transcript router directly (the route is `POST /api/v1/transcripts`):

```bash
http POST http://localhost:8000/api/v1/transcripts \
  pm_id=<pm_id> \
  ticker="NVDA" \
  source_type="polygon" \
  signal_text="Q3 revenue fell 5% year over year, missing consensus."
```

If the signal contradicts one of your assumptions and passes the MNPI screen, an alert will be queued to Slack and/or email.

To run drift detection synchronously and see the result immediately:

```bash
http POST http://localhost:8000/api/v1/transcripts \
  pm_id=<pm_id> \
  ticker="NVDA" \
  source_type="polygon" \
  signal_text="Q3 revenue fell 5% year over year, missing consensus." \
  sync:=true
```

---

## 11. Generate a Morning Brief

The brief scheduler runs on APScheduler, but you can also trigger generation manually by using the underlying agent or, depending on the build, a scheduler endpoint. The standard path is to schedule the job at market open.

Check your configured Slack channel or email inbox for the brief. A brief contains:

- One focus thesis
- Top supporting / contradicting signals
- A catalyst calendar for the next 7 days
- Open follow-up items

---

## 12. Run the Test Suite

```bash
# Run the full test suite with coverage
pytest

# Quick mode
pytest -q

# Run a specific module
pytest tests/test_drift.py

# Run compliance / isolation tests
pytest tests/test_security.py
```

The repository currently targets 111+ passing tests. Coverage is reported but does not fail the build until the suite passes 85%.

---

## 13. Linting, Formatting & Type Checks

```bash
# Format code
black src tests

# Lint and fix imports
ruff check --fix src tests
ruff format src tests

# Strict type check
mypy --strict src
```

CI runs these automatically on every pull request.

---

## 14. Troubleshooting

### `ModuleNotFoundError: No module named 'axe'`

Make sure you installed the package in editable mode and that you are inside the virtual environment:

```bash
source .venv/bin/activate
pip install -e ".[dev,ocr]"
```

Also confirm `pythonpath` includes `src` for pytest runs (configured in `pyproject.toml`).

### `alembic` command not found

Install with the dev extras, or pin the path:

```bash
pip install -e ".[dev]"
# or
python -m alembic upgrade head
```

### SQLite `database is locked` errors in Docker

SQLite with WAL mode is used by default. Avoid running the worker and API in separate processes that write to the same DB file simultaneously in production. For local Docker Compose this is acceptable, but for real workloads move to PostgreSQL.

### LLM calls fail or hang

Verify:

- `AZURE_FOUNDRY_ENDPOINT` and `AZURE_FOUNDRY_API_KEY` are set.
- The deployed model name matches `AZURE_FOUNDRY_MODEL`.
- Your endpoint has zero-retention enabled if required by your compliance policy.

### Slack alerts not arriving

- Confirm `SLACK_BOT_TOKEN` has `chat:write` and `users:read` scopes.
- Verify `ALERTS_SLACK_USER_ID` points to a valid Slack member ID.
- For webhook verification, `SLACK_SIGNING_SECRET` must also be set.

### Email not sending

- Confirm `RESEND_API_KEY` and `AXE_EMAIL_DOMAIN` are set and the domain is verified in Resend.
- The `AXE_FROM_EMAIL` must be from your verified domain.

---

## 15. Next Steps

After the basics are working:

1. Read the full architecture and development guide in [`CLAUDE.md`](CLAUDE.md).
2. Review the product requirements and gap analysis in [`docs/AXE_PRD_v2.1_Delta.md`](docs/AXE_PRD_v2.1_Delta.md).
3. Explore the API routes in `src/axe/routers/`.
4. Configure the ingestion worker for Polygon, Gmail, and Slack sources.
5. Review compliance settings: audit logging, MNPI thresholds, retention policies, and cross-PM isolation.
6. For production deployment, see [`fly.toml`](fly.toml) and the Dockerfile, or deploy to your own Kubernetes cluster.

If you hit issues not covered here, open an issue on GitHub or check the existing test files for usage examples.
