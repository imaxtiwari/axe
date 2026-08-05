# AXE — Wall Street AI Co-pilot

[![Tests](https://github.com/imaxtiwari/axe/actions/workflows/ci.yml/badge.svg)](https://github.com/imaxtiwari/axe/actions/workflows/ci.yml)

> **AI co-pilot for portfolio managers, investment teams, and GPs.**
>
> AXE tracks your investment theses, monitors thesis-breaking signals, and surfaces a daily morning brief — all while keeping each PM's memory, permissions, and audit trail isolated.

---

## What It Does

| Capability | Description |
|------------|-------------|
| **Thesis Capture** | Turn memos, emails, or direct input into versioned theses with falsifiable assumptions. |
| **Signal Drift Detection** | Compare transcripts, filings, and news against your assumptions and flag contradictions. |
| **Earnings Alerts** | Get Slack + email alerts within 30 minutes of a conflicting Polygon earnings transcript. |
| **Morning Brief** | Receive a daily brief with one focus thesis, supporting signals, and a 7-day catalyst calendar. |
| **Reply-to-Brief** | Reply via Slack/email to update a thesis or dismiss a stale signal. |
| **Compliance First** | Audit logging, cross-PM isolation, MNPI review gate, encryption at rest, and zero-retention LLM routing. |

---

## Status

**Pre-alpha (v0.1.0).** This is an active development scaffold. Do not use for production trading or compliance-critical workflows without a fund-level review.

Read the product requirements: [`docs/AXE_PRD_v2.1_Delta.md`](docs/AXE_PRD_v2.1_Delta.md).

---

## Installation

### Requirements

- Python 3.12+
- Git
- Docker + Docker Compose (recommended)

### 1. Clone the repository

```bash
git clone https://github.com/imaxtiwari/axe.git
cd axe
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and add your keys. The minimum set is:

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/axe.db          # default SQLite
CHROMA_PERSIST_DIR=./data/chroma
ENCRYPTION_KEY=<your-fernet-key>

AZURE_FOUNDRY_ENDPOINT=https://<your-resource>.services.ai.azure.com
AZURE_FOUNDRY_API_KEY=<your-key>
AZURE_FOUNDRY_MODEL=gpt-4o-mini

SLACK_BOT_TOKEN=xoxb-<your-token>
SLACK_SIGNING_SECRET=<your-secret>
RESEND_API_KEY=<your-key>
AXE_EMAIL_DOMAIN=<your-domain>

POLYGON_API_KEY=<your-key>
GOOGLE_CLIENT_ID=<your-client-id>
GOOGLE_CLIENT_SECRET=<your-secret>
```

Generate a Fernet key:

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

### 3. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ocr]"
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Start the server

```bash
uvicorn axe.main:app --reload
```

Check health:

```bash
curl http://localhost:8000/healthz
```

---

## Quick Start Workflow

After the server is running:

### 1. Create a PM user

In production this is handled by an upstream identity flow. For local testing, seed a PM directly or use a small script that inserts into `pm_users`.

### 2. Onboard the PM

```bash
http POST http://localhost:8000/api/v1/onboarding/start \
  pm_id=<pm_id>
```

Answer the five cold-start questions:

```bash
http POST http://localhost:8000/api/v1/onboarding/answer \
  pm_id=<pm_id> \
  question_number=1 \
  answer="3-5 years"
```

Repeat for `question_number` 2 through 5.

### 3. Capture tickers

```bash
http POST http://localhost:8000/api/v1/onboarding/thesis-capture \
  pm_id=<pm_id> \
  tickers:='["AAPL","MSFT","NVDA"]'
```

### 4. Create a thesis with assumptions

```bash
# Create a deal room
http POST http://localhost:8000/api/v1/deals \
  name="NVDA Long" \
  asset_class="equity" \
  target_ticker_or_private_name="NVDA"

# Add a versioned thesis
http POST http://localhost:8000/api/v1/deals/<deal_id>/thesis \
  stage="conviction" \
  key_assumptions:='[
    "Revenue grows at least 10% YoY through 2025",
    "Data center revenue share stays above 40%"
  ]'
```

> **Tip:** Make assumptions falsifiable. Use numbers, dates, or clear thresholds, and avoid words like "strong" or "good".

### 5. Trigger a drift alert

```bash
http POST http://localhost:8000/api/v1/transcripts \
  pm_id=<pm_id> \
  ticker="NVDA" \
  source_type="polygon" \
  signal_text="Q3 revenue fell 5% year over year, missing consensus."
```

If the signal contradicts an assumption and passes the MNPI screen, a Slack DM and email alert will fire.

For the complete walkthrough — Docker Compose, ingestion worker, troubleshooting, and more — see [`GETTING_STARTED.md`](GETTING_STARTED.md).

---

## Docker Compose

Run the API, worker, and optional ChromaDB server in one command:

```bash
docker compose up --build
```

Then apply migrations:

```bash
docker compose exec axe alembic upgrade head
```

The API is available at `http://localhost:8000`.

---

## Testing

```bash
pytest                  # full suite with coverage
pytest -q               # quiet mode
pytest tests/test_drift.py  # drift-specific tests
```

Current state: **111 tests passing**.

---

## Project Structure

```
axe/
├── alembic/              Database migrations
├── docs/                 PRD and design documents
├── scripts/              Helper scripts
├── src/axe/
│   ├── agents/           LLM agents (thesis extraction, drift detection, morning brief, brief replies)
│   ├── config.py         Pydantic settings
│   ├── db/               SQLAlchemy models, sessions, migrations
│   ├── ingestion/        Gmail, Slack, Polygon, PDF ingestion + ingest worker
│   ├── main.py           FastAPI entry point
│   ├── memory/           PM memory synthesis and injection
│   ├── models/           Pydantic domain models
│   ├── routers/          API routes
│   ├── security/         Audit, encryption, isolation
│   └── services/         Business logic services (alerts, onboarding, scheduling)
└── tests/                Test suite and evaluation datasets
```

---

## Documentation

- **[Getting Started](GETTING_STARTED.md)** — Full install, run, and first-use walkthrough.
- **[Developer Guide](CLAUDE.md)** — Architecture, conventions, agents, ingestion, and production notes.
- **[Product Requirements](docs/AXE_PRD_v2.1_Delta.md)** — v2.1 PRD and gap analysis.

---

## Compliance Note

AXE is designed for regulated investment workflows. v1 includes audit logging, MNPI flagging, cross-PM isolation, and zero-retention LLM routing via Azure Foundry. Fund-level compliance review is required before production deployment.
