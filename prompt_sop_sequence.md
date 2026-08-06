# AXE Use-Case Expansion — Prompt SOP Sequence

> This document contains a sequence of self-contained, production-grade prompts. Each prompt is designed to be given to the agent one at a time. Every prompt includes all required context, assumes the role of senior Staff Software Engineer / systems architect / QA engineer / security reviewer / prompt engineer, and enforces exhaustive validation loops.

---

## Meta-Prompt (apply to every sub-prompt below)

When executing any prompt in this sequence, assume the following universal requirements unless explicitly overridden:

1. **Role**: Senior Staff Software Engineer, systems architect, QA engineer, security reviewer, and prompt engineer.
2. **Code quality**: production-grade, modular, scalable, readable, extensible. Follow existing project conventions in `CLAUDE.md` and `README.md`.
3. **Assumption analysis**: explicitly list all assumptions made before implementing. Flag any missing requirements or ambiguous product behavior and propose defaults with justification.
4. **Architectural decisions**: explain major choices, compare alternatives, justify the selected approach, and document trade-offs.
5. **Edge cases & technical debt**: identify edge cases, failure modes, and technical debt introduced. Mitigate where feasible; document the rest.
6. **Error handling & logging**: robust error handling, structured logging, observability hooks. No silent failures.
7. **Security**: validate inputs, enforce isolation/authorization where applicable, avoid secrets in logs, use encryption at rest for credentials, prevent injection/SQLi/common vulnerabilities.
8. **Performance**: analyze hot paths, batching, N+1 risks, async patterns, caching opportunities. Optimize without sacrificing clarity.
9. **Testing**: write unit, integration, and regression tests as appropriate. Run the full test suite after implementation. Fix failures, rerun, repeat until green.
10. **Lint/format/type check**: run `ruff check`, `ruff format`, `mypy` (or equivalent configured in the project) and fix all issues.
11. **Self-review**: after implementation, review your own code for simplification, maintainability, and consistency. Perform a second validation pass against the original objective.
12. **Final report**: deliver a concise report covering what was completed, files touched, architectural decisions, trade-offs, remaining risks, and future improvements.
13. **Verification loops**: never assume correctness after the first attempt. Continuously review, validate, fix, re-run.

---

## Prompt 1 — Foundation Sprint: Schema, Config, Repositories, and Model Tracing

### Objective
Lay the production foundation for the entire use-case expansion. Introduce the new database schema, configuration, `UnitOfWork` repositories, and model-tracing infrastructure that every later prompt will depend on.

### Context
- Read `implementation_plan.md` Sections 1, 2, 3, 5, 8 (Sprint 0).
- Current stack: FastAPI, async SQLAlchemy + aiosqlite, ChromaDB, Alembic, Pydantic.
- Existing identity model: `PMUser`, `FundEntity`, header-based `RequestContext`, `IsolationService`.
- Existing audit model: `AuditLog` is append-only.
- Existing LLM abstraction: `LLMProvider` in `src/axe/agents/llm.py`; `get_default_provider()`.

### Deliverables
1. Alembic migration adding:
   - `connector_config`, `raw_ingest`
   - `pm_persona`, `memory_citation`, `pm_peer_map`
   - `specialist_signal`
   - `artifact_action`, `decision_prompt`
   - `model_trace`
   - `policy_rule`
   - `compliance_escalation`
   - Extend `signal_log`, `thesis_versions`, `morning_briefs`, `lp_updates`, `audit_log` columns as described in Section 2.3 of the implementation plan.
2. Update `src/axe/db/models.py` with new SQLAlchemy model classes and column extensions.
3. Update `src/axe/config.py` with new settings:
   - connector feature flags
   - guardrail thresholds
   - model-trace retention
   - human-review score thresholds
   - memory-mining scope defaults
4. Add repositories to `src/axe/db/uow.py` for every new table.
5. Implement `ModelTrace` capture:
   - New file `src/axe/agents/model_trace.py` with `TraceableProvider` wrapper.
   - Wrap the default provider so every `complete()` call writes a `ModelTrace` row.
   - Ensure `trace_id` is returned in `LLMResponse` and propagated to `AuditLog`.
6. Update `src/axe/security/audit.py`:
   - Add optional `trace_id` parameter to `AuditService.log` and `audit_action` decorator.
7. Tests:
   - `tests/test_model_trace.py` — trace written on every completion, trace_id propagated.
   - `tests/test_db_schema.py` — new tables/columns exist.
   - `tests/test_uow_repositories.py` — each new repo can create/list items.

### Execution Order
1. Read `implementation_plan.md`, `src/axe/db/models.py`, `src/axe/db/uow.py`, `src/axe/config.py`, `src/axe/agents/llm.py`, `src/axe/security/audit.py`.
2. Design the schema additions. List assumptions (e.g., string lengths, JSON default values, isolation scopes).
3. Write the Alembic migration first; ensure it is reversible.
4. Update `models.py`.
5. Update `config.py`.
6. Add repositories to `uow.py`; ensure isolation scopes are honored.
7. Implement `TraceableProvider`.
8. Update audit logging.
9. Write tests.
10. Run `alembic upgrade head`, then `pytest tests/test_model_trace.py tests/test_db_schema.py tests/test_uow_repositories.py`.
11. Run full test suite. Fix failures.
12. Run lint/format/type checks. Fix issues.
13. Self-review and final report.

### Acceptance Criteria
- [ ] Migration applies cleanly and is reversible.
- [ ] All new tables/columns are queryable from SQLAlchemy.
- [ ] Every LLM completion writes a `ModelTrace` row with `trace_id`.
- [ ] `AuditService.log` accepts and stores `trace_id`.
- [ ] All repositories are accessible via `UnitOfWork` and pass isolation checks.
- [ ] New tests pass; full test suite passes.
- [ ] No lint/format/type errors.

### Ambiguities to Resolve
- Should `ModelTrace` store the full prompt text or only a hash? Default: store hash for privacy, plus truncated preview (first 500 chars) for debugging.
- Should `ConnectorConfig.credentials_encrypted` use the existing `EncryptedJSON` type? Default: yes.
- Should `PolicyRule` be scoped by `fund_entity_id` or global? Default: fund-scoped with optional global fallback.

---

## Prompt 2 — Unified Ingestion Connector Framework

### Objective
Build a generic, extensible connector framework that unifies ingestion from broker feeds, PDF decks, CRMs, expert networks, and ResearchEdge. The framework must deduplicate, normalize, and enqueue specialist processing.

### Context
- Depends on Prompt 1 (schema, repositories, config).
- Existing ingestion: `src/axe/ingestion/handlers.py` (transcript), `src/axe/ingestion/worker.py` (retry tasks), `src/axe/ingestion/dedup.py`, `src/axe/ingestion/hashing.py`.
- Existing models: `SignalLog`, `RawIngest`, `ConnectorConfig`.
- Existing services: `AlertDelivery`, `MNPIService`.

### Deliverables
1. New module `src/axe/connectors/`:
   - `__init__.py` with registry.
   - `base.py` with `BaseConnector`, `ConnectorResult`, `IngestCandidate`, `ConnectorError`.
   - `broker_feed.py` — generic CSV/OFX/JSON broker statement parser.
   - `pdf_deck.py` — PDF pitch deck / CIM text extraction (use `pdfplumber` or `pymupdf`).
   - `crm.py` — generic CRM activity/contact JSON adapter.
   - `expert_network.py` — GLG/AlphaSights/Third Bridge transcript email/JSON adapter.
   - `research_edge.py` — ResearchEdge / Smartkarma API adapter.
2. New service `src/axe/services/connector.py`:
   - `ConnectorService.run(source_type, pm_id, *, limit=None)`
   - `ConnectorService.run_all(pm_id)`
   - Dedup using `content_hash` + `dedup_key`.
   - Normalize to `RawIngest`.
   - Enqueue `specialize_signal` worker task for each new raw ingest.
3. Update `src/axe/ingestion/worker.py`:
   - Add `run_connector` task.
   - Add `specialize_signal` task (initially no-op or dispatch to specialist registry created in Prompt 3).
4. Update `src/axe/ingestion/handlers.py`:
   - Route non-transcript payloads to connector pipeline.
5. New router `src/axe/routers/connectors.py`:
   - `POST /connectors/{source_type}` — create/update config.
   - `POST /connectors/{source_type}/run` — trigger a run (admin or self).
   - `GET /connectors` — list configs.
6. Register router in `src/axe/main.py`.
7. Tests:
   - `tests/test_connectors.py` — base contract, each connector implementation.
   - `tests/test_connector_service.py` — dedup, normalize, enqueue.

### Execution Order
1. Read `implementation_plan.md` Sections 2, 3, 4, 8 (Sprint 1), existing ingestion files.
2. Design the connector ABC and registry. List assumptions (e.g., PDF parser fallback, credential storage, rate limits).
3. Implement `base.py` and registry.
4. Implement each connector with deterministic fallback for tests.
5. Implement `ConnectorService`.
6. Wire worker tasks.
7. Implement router and register it.
8. Write tests and sample fixtures in `tests/fixtures/`.
9. Run connector tests, then full suite.
10. Run lint/format/type checks.
11. Self-review and final report.

### Acceptance Criteria
- [ ] All 5 connector types implement `BaseConnector` and pass contract tests.
- [ ] `ConnectorService` deduplicates identical payloads and creates one `RawIngest` per unique item.
- [ ] Each new `RawIngest` enqueues a `specialize_signal` task.
- [ ] Router endpoints enforce PM/fund isolation.
- [ ] Tests pass; full suite passes; no lint/type errors.

### Ambiguities to Resolve
- Which PDF library to use? Default: `pdfplumber` (pure Python) with graceful fallback if unavailable.
- Should connectors run on a schedule? Default: manual + worker task only; scheduler added later.
- How are credentials provided? Default: stored encrypted in `ConnectorConfig.credentials_encrypted`.

---

## Prompt 3 — Specialist Signal Agents

### Objective
Convert raw ingestion outputs into structured, thesis-aware signals that existing agents (drift detection, morning brief) can consume.

### Context
- Depends on Prompts 1 and 2.
- Existing agents: `src/axe/agents/drift_detect.py`, `src/axe/agents/morning_brief.py`.
- Existing models: `SignalLog`, `SpecialistSignal`, `TickerRegistry`, `ThesisVersion`.

### Deliverables
1. New file `src/axe/agents/specialist_signal.py`:
   - `SpecialistSignalAgent` ABC.
   - `AgentContext` dataclass (pm_id, fund_id, persona, active_tickers, recent_theses).
   - Implementations:
     - `EarningsSpecialist`
     - `ResearchEdgeSpecialist`
     - `ExpertNetworkSpecialist`
     - `BrokerSpecialist`
     - `PDFDeckSpecialist`
     - `CRMSpecialist`
   - `SpecialistSignalRegistry` to map `source_type` → agent class.
2. Update `src/axe/agents/drift_detect.py`:
   - Consume `SpecialistSignal` records when classifying drift.
   - Attach `ModelTrace` and route to human review if hallucination score exceeds threshold.
3. Update `src/axe/agents/morning_brief.py`:
   - Include a "Specialist Signals" section curated by the highest-confidence specialist signals.
4. Update `src/axe/ingestion/worker.py`:
   - Implement `specialize_signal` task using the registry.
5. Update `src/axe/services/thesis.py`:
   - Provide `alertable_latest_theses_with_signals()` that joins theses with recent specialist signals.
6. Tests:
   - `tests/test_specialist_signal.py` — each specialist returns valid `SpecialistSignal`.
   - `tests/test_drift_with_specialist.py` — drift classification improves with specialist input.
   - `tests/test_morning_brief_specialist.py` — brief includes specialist section.

### Execution Order
1. Read implementation plan Sections 2, 4, 8 (Sprint 2), existing drift/morning-brief agents.
2. Define `SpecialistSignal` schema and `AgentContext`.
3. Implement the ABC and registry.
4. Implement each specialist with LLM schema + deterministic fallback.
5. Wire into drift detection.
6. Wire into morning brief.
7. Implement worker task.
8. Write tests.
9. Run specialist tests, then full suite.
10. Lint/format/type checks.
11. Self-review and final report.

### Acceptance Criteria
- [ ] Every specialist implements the ABC and produces validated `SpecialistSignal` output.
- [ ] Drift detection consumes specialist signals and produces `ModelTrace` rows.
- [ ] Morning brief includes a specialist-curated section.
- [ ] Worker `specialize_signal` task dispatches correctly.
- [ ] Tests pass; full suite passes; no lint/type errors.

### Ambiguities to Resolve
- Should a specialist signal always link to a ticker? Default: yes where possible; nullable for macro/CRM signals.
- How many specialist signals should a brief show? Default: top 5 by confidence, filtered by relevance to active tickers.

---

## Prompt 4 — Identity-Aware Memory Miner and Persona Agent

### Objective
When a user connects Slack or Gmail, mine their historical interactions to build a `PMPersona` and peer map. Use the persona to personalize theses, briefs, and LP updates.

### Context
- Depends on Prompts 1 and 3 (for `pm_persona` schema and signal consumption patterns).
- Existing OAuth token model: `PMOAuthToken` in `src/axe/db/models.py`.
- Existing embedding service: `src/axe/agents/embedding.py`.
- Existing LLM provider: `src/axe/agents/llm.py`.

### Deliverables
1. New file `src/axe/agents/memory_miner.py`:
   - `MemoryMinerAgent` with:
     - `mine_email_history(pm_id, token, max_messages=5000)`
     - `mine_slack_history(pm_id, token, channels)`
     - `build_persona(pm_id, snippets)`
     - `map_peers(pm_id, snippets)`
   - Privacy guard: exclude DMs by default; only process channels/DMs the user explicitly opts into.
2. New file `src/axe/agents/persona.py`:
   - `PersonaAgent` to synthesize `PMPersona` from citations.
3. New service `src/axe/services/persona.py`:
   - `PersonaService.get_or_create(pm_id)`
   - `PersonaService.refresh(pm_id)`
   - Schedule weekly refresh in `src/axe/services/brief_scheduler.py`.
4. New router `src/axe/routers/persona.py`:
   - `GET /persona` — view own persona.
   - `POST /persona/refresh` — trigger refresh.
   - `DELETE /persona` — opt out and delete mined data.
5. Update `src/axe/agents/morning_brief.py`, `src/axe/agents/lp_update.py`, `src/axe/agents/thesis_extract.py`:
   - Accept optional persona snapshot and adjust tone/summary style.
6. Update `src/axe/db/models.py`:
   - Add `pm_persona_snapshot_id` to `ThesisVersion` if not already done in Prompt 1.
7. Tests:
   - `tests/test_memory_miner.py` — mock Gmail/Slack history → citations.
   - `tests/test_persona.py` — persona extraction.
   - `tests/test_persona_isolation.py` — no cross-PM leakage.

### Execution Order
1. Read implementation plan Sections 2, 4, 8 (Sprint 3), existing models/scheduler.
2. Design citation schema and mining scope. Document privacy assumptions.
3. Implement mock Gmail/Slack fetchers for tests.
4. Implement `MemoryMinerAgent`.
5. Implement `PersonaAgent` and `PersonaService`.
6. Add scheduler refresh.
7. Add router.
8. Personalize existing artifact agents.
9. Write tests.
10. Run tests, full suite, lint/format/type checks.
11. Self-review and final report.

### Acceptance Criteria
- [ ] Memory miner produces `MemoryCitation` rows from mocked email/Slack data.
- [ ] Persona is built and stored per PM.
- [ ] Artifact agents can consume persona snapshot.
- [ ] Router enforces PM isolation; deletion removes persona and citations.
- [ ] Tests pass; full suite passes; no lint/type errors.

### Ambiguities to Resolve
- Default lookback window? Default: 90 days, configurable via `PERSONA_MINING_LOOKBACK_DAYS`.
- Include direct messages? Default: no; require explicit opt-in.
- Should persona be shared within a fund? Default: no; strictly PM-scoped.

---

## Prompt 5 — Interactive Artifact Layer

### Objective
Transform static generated artifacts (morning brief, IC memo, LP update, deck) into decision-driving surfaces by attaching actionable prompts and one-click actions.

### Context
- Depends on Prompts 1, 3, 4.
- Existing artifacts: `MorningBrief`, `ICMemo`, `LPUpdate`, `DeckOutput`.
- Existing alert delivery: `src/axe/services/alert.py`.

### Deliverables
1. New file `src/axe/agents/interactive_artifact.py`:
   - `InteractiveArtifactAgent` with:
     - `generate_actions(artifact_type, artifact_id, pm_id)`
     - `generate_decision_prompt(artifact_type, artifact_id, pm_id)`
   - Action types: `update_thesis`, `trim`, `close`, `request_follow_up`, `schedule_call`, `share_with_team`, `generate_post_mortem`, `approve_lp_update`, etc.
2. New service `src/axe/services/interactive.py`:
   - `InteractiveArtifactService.create_actions(artifact_type, artifact_id, pm_id)`
   - `InteractiveArtifactService.execute_action(action_id, pm_id, payload)`
   - `InteractiveArtifactService.resolve_decision_prompt(prompt_id, response)`
3. Update artifact agents to generate actions/prompts:
   - `src/axe/agents/morning_brief.py` — add `Focus One` decision prompt.
   - `src/axe/agents/lp_update.py` — add approval prompt.
   - `src/axe/agents/deck.py` — add annotation actions.
4. Update `src/axe/services/alert.py`:
   - Include deep links to decision prompts in Slack/Email alerts.
5. New router `src/axe/routers/interactive.py`:
   - `GET /artifacts/{artifact_type}/{artifact_id}/actions`
   - `POST /artifacts/actions/{action_id}/execute`
   - `POST /artifacts/decision-prompts/{prompt_id}/resolve`
6. Tests:
   - `tests/test_interactive_artifact.py` — action generation, execution, prompt lifecycle.

### Execution Order
1. Read implementation plan Sections 2, 4, 8 (Sprint 4), existing artifact agents.
2. Design action/prompt schemas and execution semantics.
3. Implement `InteractiveArtifactAgent`.
4. Implement `InteractiveArtifactService` with audit logging.
5. Integrate into artifact generation paths.
6. Update alert delivery with deep links.
7. Add router.
8. Write tests.
9. Run tests, full suite, lint/format/type checks.
10. Self-review and final report.

### Acceptance Criteria
- [ ] Every morning brief has at least one decision prompt.
- [ ] LP updates have an approval decision prompt before sending.
- [ ] Actions are auditable and isolated to the owning PM.
- [ ] Slack/Email alerts include deep links.
- [ ] Tests pass; full suite passes; no lint/type errors.

### Ambiguities to Resolve
- Should `buy_more`/`trim` actions create actual orders or drafts? Default: produce a draft notification/pre-filled message; no live trading.
- Should action execution be synchronous or enqueued? Default: synchronous for simple actions, worker task for side effects (e.g., share_with_team).

---

## Prompt 6 — Cross-Agent Collaboration Bus

### Objective
Enable agents to communicate with each other across PM/fund boundaries in a controlled, auditable way, surfacing conflicts and opportunities as decision prompts.

### Context
- Depends on Prompts 1, 3, 4, 5.
- Existing agents: drift, brief, deal, LP update.

### Deliverables
1. New file `src/axe/agents/agent_collaboration.py`:
   - `AgentMessage` Pydantic model.
   - `AgentCollaborationBus`:
     - `publish(message)`
     - `subscribe(agent_id, handler)`
     - `route_to_pm(message) → DecisionPrompt | None`
   - Guarantees: isolation-aware routing, audit log per message, no cross-PM leakage.
2. New worker task in `src/axe/ingestion/worker.py`: `route_agent_message`.
3. Add collaboration hooks in:
   - `src/axe/agents/drift_detect.py` — notify when a signal affects multiple tickers or conflicts with a peer's thesis.
   - `src/axe/agents/morning_brief.py` — incorporate cross-agent messages.
   - `src/axe/agents/lp_update.py` — flag LP questions that another agent can answer.
4. Update `src/axe/services/interactive.py` to render cross-agent messages as decision prompts.
5. Tests:
   - `tests/test_cross_agent_collaboration.py` — publish/subscribe, isolation, routing to PM.

### Execution Order
1. Read implementation plan Sections 2, 4, 8 (Sprint 5).
2. Design message schema and routing rules. Document isolation guarantees.
3. Implement the bus.
4. Add worker task.
5. Add hooks in existing agents.
6. Write tests.
7. Run tests, full suite, lint/format/type checks.
8. Self-review and final report.

### Acceptance Criteria
- [ ] Agents can publish and subscribe to messages.
- [ ] Messages never leak across PM/fund scopes.
- [ ] Relevant messages surface as decision prompts.
- [ ] Every message is audited.
- [ ] Tests pass; full suite passes; no lint/type errors.

### Ambiguities to Resolve
- Should agents communicate within one PM or across PMs in the same fund? Default: both allowed if explicitly allow-listed; default is same-PM only.
- Message persistence duration? Default: 30 days, aligned with retention policy.

---

## Prompt 7 — Guardrails, Citations, and Anti-Hallucination Controls

### Objective
Add a multi-layer guardrail system and hallucination scoring to ensure agent outputs are safe, cited, and routed for human review when uncertain.

### Context
- Depends on Prompts 1–6.
- Existing MNPI service: `src/axe/services/mnpi.py`.
- Existing `ModelTrace` from Prompt 1.
- Existing `PolicyRule` schema from Prompt 1.

### Deliverables
1. New file `src/axe/agents/guardrails.py`:
   - `GuardrailRunner` with checks:
     - `mnpi_check`
     - `policy_check` (uses `PolicyEngine`)
     - `privacy_check`
     - `securities_regulation_check`
     - `self_consistency_check`
   - `GuardrailResult` with severity, violated rules, suggested action.
2. New file `src/axe/agents/citation.py`:
   - `Citation` model.
   - `CitationExtractor.extract(output, raw_sources)`
   - `CitationVerifier.verify(citations, raw_sources)`
3. New file `src/axe/agents/hallucination_guard.py`:
   - `HallucinationGuard.score(output, citations, raw_sources)`
   - `HallucinationGuard.route_for_review(score, trace)`
4. New service `src/axe/services/policy.py`:
   - `PolicyEngine.evaluate(event) → list[PolicyAction]`
   - CRUD helpers for `PolicyRule`.
5. Integrate into all agent output paths:
   - Every LLM-generated artifact runs `GuardrailRunner.check()` and `HallucinationGuard.score()`.
   - High-risk outputs create `ComplianceEscalation` and set `ModelTrace.human_review_status = pending`.
6. Update `src/axe/services/mnpi.py` to use `GuardrailRunner`.
7. Tests:
   - `tests/test_guardrails.py`
   - `tests/test_hallucination_guard.py`
   - `tests/test_citation.py`
   - Add `tests/hallucination_eval_dataset.py`.

### Execution Order
1. Read implementation plan Sections 2, 4, 8 (Sprint 6), existing MNPI service.
2. Design guardrail check interface and severity levels.
3. Implement citation extraction/verification.
4. Implement hallucination scoring heuristic.
5. Implement `GuardrailRunner` and `PolicyEngine`.
6. Integrate into artifact generation.
7. Update MNPI service.
8. Write tests and eval dataset.
9. Run tests, full suite, lint/format/type checks.
10. Self-review and final report.

### Acceptance Criteria
- [ ] Every LLM artifact can be traced to a `ModelTrace` with citations and hallucination score.
- [ ] Guardrail violations route to `ComplianceEscalation`.
- [ ] MNPI review uses the shared guardrail runner.
- [ ] Tests include known-good and known-bad citation pairs.
- [ ] Tests pass; full suite passes; no lint/type errors.

### Ambiguities to Resolve
- Hallucination scoring algorithm: default heuristic based on citation coverage + direct quote overlap; pluggable for future LLM-as-judge.
- Default review threshold: hallucination score ≥ 0.7 triggers review.

---

## Prompt 8 — Compliance Escalation and Policy Engine

### Objective
Close the compliance-depth loop by implementing an escalation queue, reviewer assignment, and resolution workflow.

### Context
- Depends on Prompts 1 and 7.
- Existing audit service: `src/axe/security/audit.py`.
- Existing models: `ComplianceEscalation`, `PolicyRule`, `ModelTrace`.

### Deliverables
1. New service `src/axe/services/compliance_escalation.py`:
   - `ComplianceEscalationService.open(trigger)`
   - `ComplianceEscalationService.assign_reviewer(escalation_id, reviewer_id)`
   - `ComplianceEscalationService.resolve(escalation_id, decision, note)`
   - `ComplianceEscalationService.list_open(pm_id=None, fund_id=None, role=None)`
2. New router `src/axe/routers/compliance.py`:
   - `GET /compliance/escalations` — list (compliance role).
   - `POST /compliance/escalations/{id}/assign` — assign reviewer.
   - `POST /compliance/escalations/{id}/resolve` — resolve.
3. Update `src/axe/security/authz.py`:
   - Add `compliance_officer` role and `require_role(["admin", "compliance_officer"])` helper.
4. Wire escalations from:
   - MNPI review queue
   - Guardrail failures
   - Hallucination review routing
5. Tests:
   - `tests/test_compliance_escalation.py` — open/assign/resolve lifecycle.
   - `tests/test_compliance_router.py` — authorization/isolation.

### Execution Order
1. Read implementation plan Sections 2, 4, 8 (Sprint 7), existing authz.
2. Define escalation severity levels and reviewer assignment rules.
3. Implement service.
4. Add role and router.
5. Wire escalation triggers.
6. Write tests.
7. Run tests, full suite, lint/format/type checks.
8. Self-review and final report.

### Acceptance Criteria
- [ ] Escalations can be opened, assigned, and resolved.
- [ ] Only authorized roles can view/assign/resolve.
- [ ] Every resolution is audited with before/after state.
- [ ] Escalation triggers from MNPI, guardrails, and hallucination review work end-to-end.
- [ ] Tests pass; full suite passes; no lint/type errors.

### Ambiguities to Resolve
- Reviewer model: default to a `compliance_officer` role within the same fund.
- Auto-assign rules: default round-robin among active compliance officers.

---

## Prompt 9 — Final Integration, Documentation, and Hardening

### Objective
Integrate all previous prompts, update documentation, harden eval datasets, and ensure the system is production-ready.

### Context
- Depends on Prompts 1–8.
- Full implementation plan is in `implementation_plan.md`.

### Deliverables
1. Update `CLAUDE.md` and `GETTING_STARTED.md` with:
   - New module conventions.
   - Connector setup.
   - Persona opt-in.
   - Compliance escalation workflow.
2. Update `README.md` capabilities table.
3. Add eval datasets:
   - Extend `tests/drift_eval_dataset.py` with connector/specialist examples.
   - Add `tests/hallucination_eval_dataset.py`.
4. Run full test suite and fix all regressions.
5. Run `ruff check`, `ruff format`, `mypy` and fix all issues.
6. Run a security review pass:
   - Check all new routers for isolation.
   - Verify credentials are encrypted at rest.
   - Verify no secrets in logs.
7. Run a performance review pass:
   - Identify N+1 queries.
   - Add indexes where missing.
   - Document caching opportunities.
8. Final report:
   - What was completed across all sprints.
   - Files created/modified.
   - Architectural decisions and trade-offs.
   - Remaining risks.
   - Future improvements.

### Execution Order
1. Read `implementation_plan.md`, `CLAUDE.md`, `GETTING_STARTED.md`, `README.md`.
2. Update documentation.
3. Add eval datasets.
4. Run full test suite.
5. Run lint/format/type checks.
6. Security review.
7. Performance review.
8. Final report.

### Acceptance Criteria
- [ ] All tests pass.
- [ ] No lint/format/type errors.
- [ ] Documentation reflects the new capabilities.
- [ ] Security review checklist completed with no critical findings.
- [ ] Performance review documented with actionable next steps.

---

## How to Use This Sequence

1. Give Prompt 1 to the agent and wait for the final report.
2. Only proceed to Prompt N+1 after Prompt N's acceptance criteria are fully satisfied and the final report is delivered.
3. Each prompt is self-contained; if a new session starts, include the prompt plus the relevant files from the previous prompt's final report.
4. Do not skip prompts. The schema/config/repos in Prompt 1 are prerequisites for everything else.
