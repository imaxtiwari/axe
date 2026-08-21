# AXE v2.1 — Investment Intelligence OS

AXE is an async FastAPI platform for small/mid-size public-market funds. It connects the dots between investment theses, inbound signals, meeting notes, decks, and compliance, while keeping every PM's data isolated and auditable.

> **Version:** 2.1 (Use-Case Expansion)  
> **Stack:** Python 3.12+, FastAPI, async SQLAlchemy + aiosqlite (WAL), ChromaDB, Alembic, Pydantic, OpenAI/Anthropic-compatible LLM providers.

## What AXE does

| Capability | What it gives you | Key files |
|---|---|---|
| **Thesis & Memory** | Capture versioned theses, track key assumptions, and build a long-term memory profile per PM. | `src/axe/services/thesis.py`, `src/axe/db/models.py` |
| **Signal Ingestion & Connectors** | Pull in broker feeds, research APIs, expert-network transcripts, PDF decks, and CRM activity. Deduplicate, hash, and persist raw payloads. | `src/axe/connectors/`, `src/axe/services/connector.py`, `src/axe/routers/connectors.py` |
| **Specialist Signal Agents** | Convert raw ingestion into structured signals (earnings, research, expert, broker, PDF, CRM) with stance and confidence. | `src/axe/agents/specialist_signal.py` |
| **Drift & Alerts** | Detect when new signals contradict or confirm thesis assumptions; route breakages to morning briefs. | `src/axe/agents/drift_detect.py`, `src/axe/services/alert.py` |
| **Morning Briefs** | Generate a daily focus-one brief with catalysts, decision prompts, and citation links delivered via Slack/email. | `src/axe/agents/morning_brief.py`, `src/axe/services/brief_delivery.py` |
| **Persona Layer** | Opt-in synthesis of a PM's writing style, decision triggers, trusted peers/sources, and confidence language from communications history. | `src/axe/agents/persona.py`, `src/axe/services/persona.py`, `src/axe/routers/persona.py` |
| **Interactive Artifacts** | Turn any artifact (thesis, deck, memo) into executable actions and decision prompts; route cross-agent requests to the PM. | `src/axe/agents/interactive_artifact.py`, `src/axe/services/interactive.py`, `src/axe/routers/interactive.py` |
| **Cross-Agent Collaboration** | Fund-isolated message bus lets agents request input, escalate, or delegate across PMs. | `src/axe/agents/agent_collaboration.py` |
| **Guardrails & Anti-Hallucination** | Every LLM output is checked for MNPI, PII, securities language, self-contradiction, and citation grounding. | `src/axe/agents/guardrails.py`, `src/axe/agents/hallucination_guard.py` |
| **Compliance Escalation** | Auto-open, assign, and audit escalations triggered by guardrails, MNPI review, or hallucination review. | `src/axe/services/compliance_escalation.py`, `src/axe/routers/compliance.py` |
| **IC Memos & Sign-off** | Draft Investment Committee memos, collect e-signature sign-offs, and version content/markdown outputs. | `src/axe/services/ic_memo.py`, `src/axe/routers/deals.py` |
| **LP Communications** | Generate quarterly LP updates with vehicle-scoped content, rendered markdown/HTML, and read receipts. | `src/axe/agents/lp_update.py`, `src/axe/services/lp_comms.py` |
| **Model Tracing & Audit** | Every LLM completion is traced; every state change is append-only audited with `trace_id` correlation. | `src/axe/agents/model_trace.py`, `src/axe/security/audit.py` |

## Quick start

```bash
# 1. Clone and enter the repo
git clone https://github.com/imaxtiwari/axe.git
cd axe

# 2. Install Python dependencies (uv recommended)
uv sync

# 3. Configure environment
cp .env.example .env
# Edit .env and set DATABASE_URL, CHROMA_PATH, and ENCRYPTION_KEY at minimum.

# 4. Run migrations
uv run alembic upgrade head

# 5. Start the API server
uv run python -m axe.main
```

See [GETTING_STARTED.md](GETTING_STARTED.md) for the full onboarding workflow and [CLAUDE.md](CLAUDE.md) for developer conventions.

## API overview

All API routes live under `/api/v1/*` and require identity headers (`X-PM-ID`, `X-Fund-ID`, `X-Role`) in production. Key routers:

- `/api/v1/thesis` — thesis capture and versioning
- `/api/v1/signals` — signal ingestion, drift, and feedback
- `/api/v1/briefs` — morning brief generation and delivery
- `/api/v1/connectors` — connector configuration and manual runs
- `/api/v1/persona` — persona refresh, get, delete, citations, peers
- `/api/v1/artifacts` — interactive artifact actions and decision prompts
- `/api/v1/compliance` — compliance escalation lifecycle
- `/api/v1/deals` — deal rooms, IC memos, sign-offs
- `/api/v1/lp` — LP updates and vehicles

## Security highlights

- **Tenant isolation:** every row is scoped by `pm_id` or `fund_entity_id` via `IsolationService`.
- **Encryption at rest:** OAuth tokens and connector credentials are stored as Fernet-encrypted JSON (`EncryptedJSON`).
- **Audit log:** append-only `audit_log` table blocks ORM UPDATE/DELETE and records before/after state.
- **Role-based access:** routers require `pm`, `compliance`, or `admin` roles as appropriate.
- **No secrets in logs:** connector credentials and token payloads are never logged in plaintext.

## Evaluation datasets

- `tests/drift_eval_dataset.py` — 50+ labeled signal/assumption pairs for stance evaluation.
- `tests/hallucination_eval_dataset.py` — labeled citation/grounding pairs for hallucination scoring.

Run the suites with `pytest tests/test_drift*.py tests/test_hallucination_guard.py tests/test_guardrails.py`.

## License

Proprietary — see repository owner for licensing terms.
