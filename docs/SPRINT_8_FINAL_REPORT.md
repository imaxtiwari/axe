# Sprint 8 Final Integration Report — AXE v2.1 Use-Case Expansion

**Date:** 21 August 2026  
**Scope:** Final integration, documentation, evaluation datasets, security/performance hardening, and quality gates for the AXE v2.1 use-case expansion sprint.  
**Branch/Working Tree:** `main` (uncommitted changes in working tree)  
**Git Commit Base:** `1aab34731490a4f77f8b4e234c7548ccc1fc6599`

---

## 1. Executive Summary

Sprint 8 completes the integration of the AXE v2.1 use-case expansion workstreams: unified ingestion connectors, specialist signal agents, interactive artifacts, cross-agent collaboration, identity-aware memory miner, guardrails/anti-hallucination, and the compliance escalation/policy engine.

All functional tests pass (`437 passed, 6 warnings`), `ruff check`, `ruff format --check`, and `mypy src` are clean. This report documents the changes made, architectural decisions, security/performance findings, remaining risks, and recommended future improvements.

---

## 2. Completed Deliverables

### 2.1 Documentation Updates

| File | Update |
|------|--------|
| `CLAUDE.md` | Added module layout, encryption/audit/tenant-isolation conventions, `AgentCollaborationBus`, connector credential schema, persona opt-in, ingestion pipeline with ModelTrace, testing references to the 82-label drift dataset and hallucination eval dataset, production RBAC guidance. |
| `GETTING_STARTED.md` | Added guardrail env vars (`GUARDRAIL_PII_CHECK_ENABLED`, `GUARDRAIL_SECURITIES_REG_CHECK_ENABLED`, `MODEL_TRACE_ENABLED`), lint commands covering `scripts/`, Section 17 “Security & Privacy Checklist”. |
| `README.md` | Expanded capabilities table from 13 to 18 rows (MNPI Review Gate, Brief Reply Actions, Memory Miner, Export & Retention, split Guardrails/Anti-Hallucination, etc.), updated eval dataset descriptions. |

### 2.2 Evaluation Datasets

| File | Update |
|------|--------|
| `tests/drift_eval_dataset.py` | 82 labeled signal/assumption pairs covering connector/specialist contradiction, confirmation, and neutral/uncertain cases. |
| `tests/hallucination_eval_dataset.py` | 19 hardened cases including numeric unit mismatches (millions vs. billions, basis points vs. percent) and source spoofing. |

### 2.3 Agent Hardening (from prior session)

| File | Update |
|------|--------|
| `src/axe/agents/hallucination_guard.py` | Expanded `_numeric_mismatch_penalty` regex to recognize full unit words (`billion`, `million`, `thousand`, `basis points`) and compact forms (`bn`, `m`, `k`, `bps`, `%`, `x`). Normalizes units before flagging mismatch. |
| `src/axe/agents/citation.py` | Updated `_SENTENCE_SPLIT_RE` to split on citation markers (`[1]`, `【1】`, `(source: id)`) so per-sentence/per-claim snippets are returned. |

### 2.4 Security & Performance Fixes (current session)

| File | Issue | Fix |
|------|-------|-----|
| `src/axe/agents/model_trace.py` | Stale `self.last_trace` mutation before assignment. | Removed the dead branch that mutated `self.last_trace` before `create_trace`. The final `human_review_status` is now computed once from hallucination routing and guardrail action before persistence. |
| `src/axe/security/audit.py` | `non_blocking=True` used an `AuditLog` instance bound to the caller session inside a new `AsyncSession`. | Rebuilt the audit entry from a detached kwargs snapshot inside the non-blocking task so it can be safely persisted in a dedicated session. |
| `src/axe/db/models.py` | Several relationships used default lazy loading, risking N+1 in async paths. | Added `lazy="selectin"` to `FundEntity.pm_users`, `PMUser.fund_entity`, `DealRoom.ic_memos`, `ICMemo.deal`, `DealThesisVersion.deal`, `ICSignOff.memo`. |
| `src/axe/services/compliance_escalation.py` | `_next_reviewer` executed one query per compliance officer candidate. | Replaced per-candidate counts with a single grouped `func.count()` query over all candidates, reducing assignment from O(n) queries to one query. |
| `src/axe/db/uow.py` | `ModelTraceRepository.get_by_prompt_hash` was unbounded. | Added `limit: int \| None = 100` parameter with a default cap of 100 traces. |

---

## 3. Verification Results

### 3.1 Test Suite

```text
437 passed, 6 warnings in 40.21s
```

Warnings are pre-existing Starlette/httpx deprecation noise and a SWIG `__module__` warning from PDF parsing; no test failures or new warnings were introduced.

### 3.2 Lint & Type Check

```text
uv run ruff check src tests scripts   # All checks passed!
uv run ruff format --check src tests scripts   # 118 files already formatted
uv run mypy src   # Success: no issues found in 81 source files
```

### 3.3 Security Review

| Area | Finding | Status |
|------|---------|--------|
| Router isolation | `connectors.py`, `deals.py`, `lp.py`, `interactive.py`, `persona.py`, `transcripts.py`, `onboarding.py` enforce `_verify_self_or_admin`, `_ensure_identity`, or `_verify_self_or_bypass`. | ✅ |
| Credential encryption at rest | `ConnectorConfig.credentials_encrypted` and `PMOAuthToken.token_payload` use `EncryptedJSON` (Fernet). `encryption.py` validates key length. | ✅ |
| No secrets in logs / responses | `ConnectorConfigResponse` omits `credentials_encrypted`. Search across routers/services/agents found no logging of credentials, API keys, or tokens. | ✅ |
| Audit append-only | `AuditLog` has ORM events blocking UPDATE/DELETE. | ✅ |
| Audit non-blocking path | Fixed session-bound instance bug. | ✅ |
| Cross-PM update protection | `connectors.py` verifies `config.pm_id != body.pm_id` before updating. | ✅ |

### 3.4 Performance Review

| Area | Finding | Status |
|------|---------|--------|
| N+1 reviewer assignment | Replaced per-candidate count queries with a single grouped query. | ✅ Fixed |
| Unbounded prompt-hash lookup | Added default `limit=100`. | ✅ Fixed |
| Relationship lazy loading | Added `lazy="selectin"` to core deal/IC/user relationships. | ✅ Fixed |
| Missing indexes | Existing indexes cover `pm_id+created_at`, `content_hash`, `fund_entity_id+status`, `prompt_hash`, etc. No new critical gaps identified. | ✅ |
| Caching opportunities | Settings are cached via `@lru_cache`. Chroma vector search, dedup lookups, and policy rule queries are the next caching candidates but out of Sprint 8 scope. | Noted |

---

## 4. Architectural Decisions

1. **Detached audit snapshot for non-blocking mode.** Instead of disabling `non_blocking`, we rebuild the `AuditLog` row from a kwargs snapshot so it can safely be added to a fresh `AsyncSession`. This preserves the fire-and-forget API without session-bound instance errors.
2. **Single grouped query for round-robin reviewer assignment.** Keeping assignment stateless while avoiding N+1 means we still fetch candidates first (fund + role + active), then count open escalations for the candidate set in one query. This is simple and avoids window-function portability issues across SQLite/Postgres.
3. **Default `limit=100` for `get_by_prompt_hash`.** Hash collisions are unlikely, but repeated identical prompts over time could return thousands of rows. The default cap protects the caller while preserving an override for deep replay.
4. **`lazy="selectin"` over `joinedload`.** Select-in loading avoids cartesian product growth when multiple collections are accessed and is well-suited to the async SQLAlchemy + aiosqlite stack.

---

## 5. Risks

| Risk | Mitigation |
|------|------------|
| `EncryptedJSON` falls back to `ENCRYPTION_KEY` env var if `Settings.encryption_key` is unset. | Documented as local/test-only; production should always set `ENCRYPTION_KEY` explicitly. |
| `_get_reviewer` in `ComplianceEscalationService` does not scope by fund before lookup, but `assign_reviewer` validates fund afterwards. | This is a defense-in-depth pattern; the validation after fetch prevents cross-fund assignment. Future hardening could add the fund filter to the initial query. |
| `non_blocking` audit tasks are fire-and-forget. | Callers in high-throughput paths should retain task references to avoid “task destroyed” warnings. |
| `model_trace.py` still performs multiple guardrail/hallucination checks synchronously within the request path. | Latency is acceptable for current volumes; consider async batching if trace volume grows. |

---

## 6. Future Improvements

1. **Caching layer.** Add TTL caching for policy rules, PM personas, and connector metadata to reduce repeated DB hits.
2. **Pagination on list endpoints.** Many repository `list_*` helpers remain unbounded; add consistent `limit`/`offset` parameters.
3. **Fund-scoped reviewer query.** Move the fund filter into `_get_reviewer` so the database does the cross-fund rejection rather than Python.
4. **Async background trace processing.** Offload citation extraction/verification and guardrail checks for high-throughput LLM calls.
5. **Production hardening.** Add structured JSON logging, explicit secret rotation workflow, and runtime validation that `ENCRYPTION_KEY` is set in `app_env=production`.

---

## 7. Modified Files

```text
README.md                                 | 29 +++++++++++++++----------
src/axe/agents/model_trace.py             |  8 -------
src/axe/db/models.py                      | 20 ++++++++++++-----
src/axe/db/uow.py                         |  9 ++++++--
src/axe/security/audit.py                 | 24 +++++++++++++++++++-
src/axe/services/compliance_escalation.py | 27 ++++++++++++++++-------
```

(Note: `CLAUDE.md`, `GETTING_STARTED.md`, `tests/drift_eval_dataset.py`, and `tests/hallucination_eval_dataset.py` were modified in the prior session and are already reflected in the working tree.)

---

## 8. How to Reproduce Verification

```bash
cd "/Users/anshtiwari/Desktop/Email Assistant/Axe/axe"
uv run pytest tests -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src
```

---

**End of Report**
