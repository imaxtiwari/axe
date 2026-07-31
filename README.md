# AXE — Wall Street AI Co-pilot

AXE is an AI co-pilot and investment operating system for portfolio managers, investment teams, and GPs. It tracks investment theses, surfaces thesis-relevant signals, runs adversarial reviews, and builds a compounding long-term memory of how each user thinks.

## What It Does

- **Thesis Capture** — Extract and version key assumptions, bull/bear cases, and price targets from memos, emails, and direct input.
- **Signal Drift Detection** — Compare external signals against thesis assumptions and flag contradictions (`CONFIRMS`, `CONTRADICTS`, `NEUTRAL`, `UNCERTAIN`).
- **Earnings Alerts** — Fire Slack DM + email thesis alerts within 30 minutes of Polygon earnings transcript arrival.
- **Morning Brief** — Push a daily, personalized brief with a focus-one thesis, supporting sections, and a catalyst calendar.
- **Reply-to-Brief Workflow** — PMs can reply via Slack/email to update a thesis or dismiss a stale signal.
- **Security & Compliance** — Audit logging, PM/fund isolation, MNPI flagging, encryption at rest, and zero-retention LLM routing.

## Status

Pre-alpha. Active development on the v1 scaffold. See [`docs/AXE_PRD_v2.1_Delta.md`](docs/AXE_PRD_v2.1_Delta.md) for product requirements and gap analysis.

## Quick Start

```bash
cd axe               # project root
cp .env.example .env # then edit with your keys
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ocr]"
alembic upgrade head
uvicorn axe.main:app --reload
```

Visit `http://localhost:8000/healthz` to verify.

## Docker

```bash
docker compose up --build
```

## Testing

```bash
pytest                  # full suite with coverage
pytest -q               # quiet
pytest tests/test_drift.py  # drift-specific tests
```

Current state: **111 tests passing**.

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

## Key Agents & Services

| Component | Path | Responsibility |
|-----------|------|----------------|
| DriftDetectionAgent | `src/axe/agents/drift_detect.py` | Classify signals vs assumptions with embedding pre-filter |
| ThesisTestAgent | `src/axe/agents/drift_detect.py` | Generate pass/fail tests per assumption |
| MorningBriefAgent | `src/axe/agents/morning_brief.py` | Build daily brief and focus-one thesis |
| BriefReplyAgent | `src/axe/agents/brief_reply.py` | Parse PM reply intents and update thesis or record feedback |
| EarningsAlertService | `src/axe/services/alert.py` | Slack/email alerting with deduplication and SLA tracking |
| Ingestion Worker | `src/axe/ingestion/worker.py` | Route signals from Polygon/email/PDF through extraction and drift detection |

## Compliance Note

AXE is designed for regulated investment workflows. v1 includes audit logging, MNPI flagging, cross-PM isolation, and zero-retention LLM routing via Azure Foundry. Fund-level compliance review is required before production deployment.
