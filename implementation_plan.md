# AXE Use-Case Expansion — Implementation Plan

> Status: PLANNING  
> Target personas: (1) the solo public-equity PM already working in AXE, and (2) the GP / IR operator managing LP communications and fundraising.  
> Method: expand what AXE can ingest first so existing agents get richer signal, then make generated artifacts (briefs, memos, decks, LP updates) more interactive and decision-driving. Add specialist agents, cross-agent collaboration, identity-aware memory mining, and stronger guardrails / anti-hallucination controls. Defer multi-asset generalization until public-equity and LP workflows are tight.

---

## 1. Overview

### 1.1 Current state
AXE v2.1 is a FastAPI Investment OS with:

- Async SQLAlchemy + aiosqlite (WAL), ChromaDB vector store, Alembic migrations.
- Identity model: `FundEntity` → `PMUser`, header-based `RequestContext` (`X-PM-ID`, `X-Fund-ID`, `X-Role`), `IsolationService` enforcing PM/fund-scoped reads.
- Public-equity core: `TickerRegistry`, immutable `ThesisVersion`, `SignalLog`, `BrokenAssumption`, `MNPIReviewQueue`, drift detection, morning briefs, brief replies.
- Private-market scaffold: `DealRoom`, `DealDocument`, `DealThesisVersion`, `UnderwritingChecklist`, `UnderwritingScenario`, `ICMemo`, `ICSignOff`, `DeckOutput`, `LPUpdate`, `CommunicationArchive`.
- Ingestion: transcript handler, retry worker, dedup/hashing, alert delivery (Slack/Resend), scheduled briefs.
- Compliance: append-only `AuditLog`, `@audit_action`, retention, encryption, MNPI gate.

### 1.2 Strategic priorities (from user direction)
1. **Inbound expansion** — broker feeds, PDF decks, CRM, expert networks, ResearchEdge so agents get richer signal.
2. **Artifact interactivity** — make briefs, memos, decks, LP updates decision-driving and interactive.
3. **Identity & communication memory** — when a user connects Slack/email, agents study history to learn how the PM writes, thinks, and talks with peers.
4. **Guardrails & anti-hallucination** — explicit controls so agents never cross lines and hallucination is minimized.
5. **Agent-to-agent collaboration** — agents of respective people can interact in their own ways.
6. **Compliance depth** — propose a pragmatic, audit-friendly escalation path; let user decide.
7. **Defer** broad multi-asset urgency until equity + LP workflows are excellent.

### 1.3 What this plan covers
Six implementation workstreams grouped into two phases:

| Phase | Workstream | Why now |
|-------|------------|---------|
| Phase 1 — Ingestion & memory | A. Unified ingestion connectors | Required before any agent can get smarter. |
| | B. Identity-aware memory miner | Enables personalization, better briefs, better LP updates. |
| Phase 2 — Agents & artifacts | C. Specialist signal agents | Convert raw feed data into structured signal for existing agents. |
| | D. Interactive artifact layer | Turn static outputs into decision-driving surfaces. |
| | E. Cross-agent collaboration | Let PM agents, deal agents, LP agents collaborate safely. |
| Phase 3 — Trust & compliance | F. Guardrails, citations, anti-hallucination | Required before scaling ingestion volume. |
| | G. Compliance escalations & policy engine | Close the compliance-depth question. |

---

## 2. Types (Pydantic / schema additions)

### 2.1 New domain models

```python
# Ingestion sources
class ConnectorConfig(Base):
    id, pm_id, source_type, credentials_encrypted, schedule, enabled, last_cursor, created_at


class RawIngest(Base):
    (
        id,
        pm_id,
        source_type,
        external_id,
        content_hash,
        raw_payload_json,
        extracted_signal_json,
    )
    extracted_at, status, dedup_key


# Memory / identity
class PMPersona(Base):
    (
        id,
        pm_id,
        writing_style_summary,
        decision_triggers,
        peer_relationships_json,
    )
    trusted_sources, confidence_language, last_refreshed_at


class MemoryCitation(Base):
    (
        id,
        pm_id,
        source_type,
        source_id,
        snippet,
        linked_ticker,
        linked_deal_id,
    )
    sentiment, extracted_at


class PMPeerMap(Base):
    (
        id,
        pm_id,
        peer_email_or_slack_id,
        peer_name,
        relationship_type,
        interaction_frequency,
    )
    topics, trust_level


# Signal specialists
class SpecialistSignal(Base):
    (
        id,
        pm_id,
        ticker,
        source_type,
        specialist_agent,
        signal_type,
        summary,
        stance,
    )
    confidence, evidence_json, assumptions_touched, created_at


# Artifact interactivity
class ArtifactAction(Base):
    id, artifact_type, artifact_id, pm_id, action_type, payload, created_at, executed_at, status


class DecisionPrompt(Base):
    id, pm_id, artifact_id, prompt_text, options_json, response, deadline_at, resolved_at


# Guardrails
class ModelTrace(Base):
    (
        id,
        pm_id,
        agent,
        prompt_hash,
        model,
        response_schema,
        latency_ms,
        token_usage,
    )
    citations_json, hallucination_score, human_review_status


class PolicyRule(Base):
    id, fund_entity_id, rule_type, scope, conditions_json, action, priority, enabled


# Compliance escalation
class ComplianceEscalation(Base):
    id, pm_id, fund_entity_id, trigger_type, severity, status, reviewer_id, opened_at, closed_at
```

### 2.2 New enums / string constants
- `source_type`: `polygon`, `gmail`, `slack`, `broker_feed`, `pdf_deck`, `crm`, `expert_network`, `research_edge`, `transcript`, `manual`
- `specialist_agent`: `earnings_specialist`, `research_edge_specialist`, `expert_network_specialist`, `broker_specialist`, `pdf_deck_specialist`, `crm_specialist`
- `signal_type`: `earnings`, `estimate_revision`, `price_target`, `management_change`, `macro`, `mos`, `sentiment`
- `artifact_type`: `morning_brief`, `ic_memo`, `lp_update`, `deck`, `thesis`, `signal`
- `action_type`: `buy_more`, `trim`, `close`, `update_thesis`, `request_follow_up`, `schedule_call`, `share_with_team`, `generate_post_mortem`, `approve_lp_update`
- `human_review_status`: `not_required`, `pending`, `approved`, `rejected`

### 2.3 Schema changes to existing models
- `SignalLog`: add `source_id` (foreign id in source system), `specialist_signal_id`, `parent_signal_id`, `chain_id` for threading.
- `ThesisVersion`: add `pm_persona_snapshot_id` (optional) to record which persona snapshot informed the thesis language.
- `MorningBrief`: add `decision_prompts_json`, `actions_json`, `citation_links_json`.
- `LPUpdate`: add `feedback_json`, `read_receipts_json`.
- `AuditLog`: add `trace_id` for correlation with `ModelTrace`.

---

## 3. Files

### 3.1 New files
| File | Responsibility |
|------|----------------|
| `src/axe/connectors/__init__.py` | Connector registry. |
| `src/axe/connectors/base.py` | `BaseConnector`, `ConnectorResult` ABC. |
| `src/axe/connectors/broker_feed.py` | Generic broker statement / trade-confirm ingestion. |
| `src/axe/connectors/pdf_deck.py` | PDF pitch deck / CIM extraction (pdfplumber / PyMuPDF fallback). |
| `src/axe/connectors/crm.py` | Generic CRM activity/contact ingestion. |
| `src/axe/connectors/expert_network.py` | GLG / AlphaSights / Third Bridge transcript ingestion. |
| `src/axe/connectors/research_edge.py` | ResearchEdge / Smartkarma / similar API adapter. |
| `src/axe/agents/memory_miner.py` | Identity-aware history mining agent. |
| `src/axe/agents/persona.py` | Persona extraction and `PMPersona` maintenance. |
| `src/axe/agents/specialist_signal.py` | Specialist signal agents registry. |
| `src/axe/agents/interactive_artifact.py` | Convert artifacts → decision prompts + actions. |
| `src/axe/agents/agent_collaboration.py` | Cross-agent message bus / collaboration protocol. |
| `src/axe/agents/guardrails.py` | Guardrail checks: MNPI, policy, self-correction, refusal. |
| `src/axe/agents/citation.py` | Citation extraction and verification. |
| `src/axe/agents/hallucination_guard.py` | Hallucination scoring + human-review routing. |
| `src/axe/services/connector.py` | Orchestrate connector runs, dedup, retry. |
| `src/axe/services/persona.py` | CRUD + refresh scheduling for personas. |
| `src/axe/services/interactive.py` | Artifact action execution and decision prompt lifecycle. |
| `src/axe/services/policy.py` | Policy rule engine. |
| `src/axe/services/compliance_escalation.py` | Escalation workflow + reviewer assignment. |
| `src/axe/routers/connectors.py` | Connector config & run endpoints. |
| `src/axe/routers/persona.py` | Persona viewing / refresh endpoints. |
| `src/axe/routers/interactive.py` | Artifact actions / decision prompt endpoints. |
| `src/axe/routers/compliance.py` | Escalation review endpoints. |
| `alembic/versions/xxx_add_connector_persona_interactive_compliance_tables.py` | Migration. |
| `tests/test_connectors.py` | Connector base + specific adapters. |
| `tests/test_persona.py` | Persona extraction. |
| `tests/test_specialist_signal.py` | Specialist agents. |
| `tests/test_interactive_artifact.py` | Decision prompts & actions. |
| `tests/test_guardrails.py` | Guardrail & hallucination tests. |
| `tests/test_compliance_escalation.py` | Escalation workflow. |

### 3.2 Files to modify
| File | Changes |
|------|---------|
| `src/axe/db/models.py` | Add new tables; extend columns as listed above. |
| `src/axe/db/uow.py` | Add repositories: connectors, raw_ingest, persona, memory_citation, specialist_signal, artifact_action, decision_prompt, model_trace, policy_rule, compliance_escalation. |
| `src/axe/config.py` | Add connector settings, feature flags, review thresholds. |
| `src/axe/ingestion/handlers.py` | Route non-transcript ingestion through connector pipeline; call specialist agents. |
| `src/axe/ingestion/worker.py` | Add tasks: `run_connector`, `mine_memory`, `specialize_signal`, `execute_artifact_action`, `escalation_review`. |
| `src/axe/agents/morning_brief.py` | Consume persona, specialist signals, citations, produce decision prompts. |
| `src/axe/agents/drift_detect.py` | Consume specialist signals; attach `ModelTrace`; route to review if hallucination score high. |
| `src/axe/agents/lp_update.py` | Use persona + CRM activity + LP feedback; produce interactive approval prompts. |
| `src/axe/agents/deck.py` | Accept deck source PDFs; add interactive annotation actions. |
| `src/axe/services/mnpi.py` | Use guardrails module; escalate to compliance queue. |
| `src/axe/services/alert.py` | Include decision-prompt deep links in Slack/Email alerts. |
| `src/axe/services/brief_scheduler.py` | Trigger persona refresh weekly; pre-compute citations. |
| `src/axe/routers/deals.py` | Expose interactive artifact endpoints for IC memos/decks. |
| `src/axe/routers/lp.py` | Expose LP update decision-prompt endpoints. |
| `src/axe/main.py` | Register new routers. |
| `src/axe/security/audit.py` | Add `trace_id` support in `AuditService.log`. |

---

## 4. Functions

### 4.1 Ingestion connectors (`src/axe/connectors/`)

```python
# src/axe/connectors/base.py
class BaseConnector(ABC):
    source_type: ClassVar[str]

    def __init__(self, config: ConnectorConfig, encryption_service): ...

    @abstractmethod
    async def fetch(self, *, cursor: Any = None, limit: int = 100) -> ConnectorResult: ...

    @abstractmethod
    async def parse(self, raw: dict) -> IngestCandidate: ...

    async def normalize(self, candidate: IngestCandidate) -> SignalLog | None: ...


class ConnectorResult(NamedTuple):
    items: list[dict]
    next_cursor: Any | None
    has_more: bool
```

### 4.2 Memory miner (`src/axe/agents/memory_miner.py`)

```python
class MemoryMinerAgent:
    def __init__(self, provider: LLMProvider, embedding_service): ...

    async def mine_email_history(
        self, pm_id: str, token: OAuthToken, max_messages: int = 5000
    ) -> MiningSummary: ...

    async def mine_slack_history(
        self, pm_id: str, token: OAuthToken, channels: list[str]
    ) -> MiningSummary: ...

    async def build_persona(self, pm_id: str, snippets: list[MemoryCitation]) -> PMPersona: ...

    async def map_peers(self, pm_id: str, snippets: list[MemoryCitation]) -> list[PMPeerMap]: ...
```

### 4.3 Specialist signal agents (`src/axe/agents/specialist_signal.py`)

```python
class SpecialistSignalAgent(ABC):
    agent_id: ClassVar[str]

    @abstractmethod
    async def process(self, raw: RawIngest, ctx: AgentContext) -> SpecialistSignal | None: ...


class EarningsSpecialist(SpecialistSignalAgent): ...


class ResearchEdgeSpecialist(SpecialistSignalAgent): ...


class ExpertNetworkSpecialist(SpecialistSignalAgent): ...


class BrokerSpecialist(SpecialistSignalAgent): ...


class PDFDeckSpecialist(SpecialistSignalAgent): ...


class CRMSpecialist(SpecialistSignalAgent): ...
```

### 4.4 Interactive artifact layer (`src/axe/agents/interactive_artifact.py`)

```python
class InteractiveArtifactAgent:
    async def generate_actions(
        self, artifact_type: str, artifact_id: str, pm_id: str
    ) -> list[ArtifactAction]: ...

    async def generate_decision_prompt(
        self, artifact_type: str, artifact_id: str, pm_id: str
    ) -> DecisionPrompt | None: ...
```

### 4.5 Cross-agent collaboration (`src/axe/agents/agent_collaboration.py`)

```python
class AgentMessage(BaseModel):
    sender_agent: str
    recipient_agent: str
    intent: str
    payload: dict
    required_confidence: float


class AgentCollaborationBus:
    async def publish(self, message: AgentMessage) -> None: ...
    async def subscribe(self, agent_id: str, handler: Callable): ...
    async def route_to_pm(self, message: AgentMessage) -> DecisionPrompt | None: ...
```

### 4.6 Guardrails & anti-hallucination (`src/axe/agents/guardrails.py`, `src/axe/agents/hallucination_guard.py`)

```python
class GuardrailRunner:
    async def check(self, content: str, ctx: AgentContext) -> GuardrailResult: ...

    # checks: mnpi, policy, privacy, securities_regulation, self_consistency


class HallucinationGuard:
    async def score(
        self, output: str, citations: list[Citation], raw_sources: list[dict]
    ) -> HallucinationScore: ...

    async def route_for_review(
        self, score: HallucinationScore, trace: ModelTrace
    ) -> HumanReviewStatus: ...
```

### 4.7 Policy & compliance escalation (`src/axe/services/policy.py`, `src/axe/services/compliance_escalation.py`)

```python
class PolicyEngine:
    async def evaluate(self, event: PolicyEvent) -> list[PolicyAction]: ...


class ComplianceEscalationService:
    async def open(self, trigger: EscalationTrigger) -> ComplianceEscalation: ...
    async def assign_reviewer(self, escalation_id: str, reviewer_id: str): ...
    async def resolve(self, escalation_id: str, decision: str, note: str): ...
```

---

## 5. Classes

### 5.1 New service classes
| Class | Module | Purpose |
|-------|--------|---------|
| `ConnectorService` | `services/connector.py` | Run connectors, dedup, normalize, enqueue specialist processing. |
| `PersonaService` | `services/persona.py` | Manage persona lifecycle and refresh. |
| `InteractiveArtifactService` | `services/interactive.py` | Persist and execute artifact actions. |
| `PolicyEngine` | `services/policy.py` | Evaluate policy rules against agent outputs. |
| `ComplianceEscalationService` | `services/compliance_escalation.py` | Open/assign/resolve escalations. |

### 5.2 New agent classes
| Class | Module | Purpose |
|-------|--------|---------|
| `MemoryMinerAgent` | `agents/memory_miner.py` | Mine email/Slack and build persona/peer map. |
| `PersonaAgent` | `agents/persona.py` | Summarize writing style, decision triggers. |
| `SpecialistSignalAgent` (base) | `agents/specialist_signal.py` | Convert raw ingest → structured signal. |
| `InteractiveArtifactAgent` | `agents/interactive_artifact.py` | Generate actions/prompts from artifacts. |
| `AgentCollaborationBus` | `agents/agent_collaboration.py` | Cross-agent publish/subscribe. |
| `GuardrailRunner` | `agents/guardrails.py` | Multi-layer policy checks. |
| `HallucinationGuard` | `agents/hallucination_guard.py` | Score and route for review. |

### 5.3 New repository classes
| Class | Module | Purpose |
|-------|--------|---------|
| `ConnectorConfigRepo` | `db/uow.py` | CRUD for connector configs. |
| `RawIngestRepo` | `db/uow.py` | Deduplicated raw ingestion store. |
| `PersonaRepo` | `db/uow.py` | Persona snapshots. |
| `MemoryCitationRepo` | `db/uow.py` | Cited snippets. |
| `SpecialistSignalRepo` | `db/uow.py` | Specialist outputs. |
| `ArtifactActionRepo` | `db/uow.py` | Action records. |
| `DecisionPromptRepo` | `db/uow.py` | Decision prompt lifecycle. |
| `ModelTraceRepo` | `db/uow.py` | Per-generation traces. |
| `PolicyRuleRepo` | `db/uow.py` | Fund-scoped rules. |
| `ComplianceEscalationRepo` | `db/uow.py` | Escalation records. |

---

## 6. Dependencies

### 6.1 Existing dependencies (from `pyproject.toml`)
- Python 3.12+, FastAPI, SQLAlchemy async, aiosqlite, ChromaDB, httpx, cryptography, APScheduler, Pydantic, pydantic-settings.

### 6.2 New dependencies to evaluate
| Package | Use | Install command |
|---------|-----|-----------------|
| `pdfplumber` or `pymupdf` | PDF deck/CIM extraction | `uv add pdfplumber` |
| `beautifulsoup4` | HTML email / research page parsing | `uv add beautifulsoup4` |
| `markdownify` | Convert HTML to markdown | `uv add markdownify` |
| `slack-sdk` (already likely present) | Slack history mining | verify in `pyproject.toml` |
| `google-auth-oauthlib` | Gmail OAuth history sync | verify / add |

### 6.3 External integrations
- **Broker feeds**: Alpaca, Interactive Brokers, or generic OFX/CSV statement upload.
- **CRM**: HubSpot / Salesforce via OAuth.
- **Expert networks**: GLG, AlphaSights, Third Bridge via transcript email / portal scrape.
- **ResearchEdge / Smartkarma**: REST API with API key.
- **Slack**: conversations.history, users.info.
- **Gmail**: history.list + messages.get via OAuth2.

---

## 7. Testing

### 7.1 Unit tests
- `test_connectors.py` — base contract, broker CSV, PDF deck, CRM JSON, expert-network transcript.
- `test_persona.py` — persona extraction with `MockProvider`.
- `test_specialist_signal.py` — each specialist returns valid `SpecialistSignal`.
- `test_interactive_artifact.py` — action generation, decision prompt lifecycle.
- `test_guardrails.py` — MNPI/policy/refusal checks.
- `test_hallucination_guard.py` — citation coverage scoring, review routing.
- `test_compliance_escalation.py` — open/assign/resolve workflow.

### 7.2 Integration tests
- `test_ingestion_pipeline.py` — end-to-end connector → raw ingest → specialist → signal log.
- `test_memory_mining.py` — mock Gmail/Slack history → persona.
- `test_cross_agent_collaboration.py` — two agents publish messages and produce a decision prompt.

### 7.3 Compliance tests
- Isolation: persona / memory citations never leak across PMs.
- Audit: every connector run, specialist signal, artifact action, and model trace has an `AuditLog`.
- Retention: soft-delete applies to new tables unless exempt.

### 7.4 Eval datasets
- Extend `tests/drift_eval_dataset.py` with connector-specific signal examples.
- Add `tests/hallucination_eval_dataset.py` with known-good and known-bad citation pairs.

---

## 8. Implementation Order

### Sprint 0 — Foundation (migration + config + repositories)
1. Migration: add `ConnectorConfig`, `RawIngest`, `PMPersona`, `MemoryCitation`, `PMPeerMap`, `SpecialistSignal`, `ArtifactAction`, `DecisionPrompt`, `ModelTrace`, `PolicyRule`, `ComplianceEscalation`; extend `SignalLog`, `ThesisVersion`, `MorningBrief`, `LPUpdate`.
2. Update `src/axe/config.py` with connector/guardrail/feature flags.
3. Add repositories to `UnitOfWork`.
4. Add `ModelTrace` writing to every LLM provider call path.
5. Add `trace_id` to `AuditLog` and `AuditService.log`.

### Sprint 1 — Connectors + ingestion pipeline
6. Implement `BaseConnector` and registry.
7. Implement `broker_feed`, `pdf_deck`, `crm`, `expert_network`, `research_edge` connectors.
8. Implement `ConnectorService`: run, dedup, normalize, enqueue.
9. Wire connector tasks into `RetryWorker`.
10. Add `routers/connectors.py` and register in `main.py`.
11. Tests for each connector and service.

### Sprint 2 — Specialist signal agents
12. Implement `SpecialistSignalAgent` base + `EarningsSpecialist`.
13. Implement `ResearchEdgeSpecialist`, `ExpertNetworkSpecialist`, `BrokerSpecialist`, `PDFDeckSpecialist`, `CRMSpecialist`.
14. Update `drift_detect.py` to consume `SpecialistSignal`.
15. Update `morning_brief.py` to include specialist-curated sections.
16. Tests for specialists.

### Sprint 3 — Memory miner + persona
17. Implement `MemoryMinerAgent` for Gmail/Slack history.
18. Implement `PersonaAgent` to synthesize `PMPersona`.
19. Implement `PersonaService` with refresh schedule.
20. Add `routers/persona.py`.
21. Use persona snapshot when drafting theses, briefs, LP updates.
22. Tests for memory miner and persona.

### Sprint 4 — Interactive artifact layer
23. Implement `InteractiveArtifactAgent`.
24. Implement `InteractiveArtifactService`.
25. Update morning brief, IC memo, LP update, deck outputs to embed decision prompts/actions.
26. Add `routers/interactive.py`.
27. Update `AlertDelivery` to include deep links to decision prompts.
28. Tests for interactive artifacts.

### Sprint 5 — Cross-agent collaboration
29. Implement `AgentCollaborationBus`.
30. Add collaboration hooks in drift, brief, deal, LP update agents.
31. Add `agent_collaboration` worker task.
32. Tests for agent-to-agent messaging and routing.

### Sprint 6 — Guardrails + anti-hallucination
33. Implement `GuardrailRunner` with MNPI, policy, privacy, regulation checks.
34. Implement `HallucinationGuard` with citation verification.
35. Integrate guards into all agent output paths; route high-risk outputs to human review.
36. Add `PolicyEngine` + `PolicyRule` CRUD.
37. Tests for guardrails and hallucination guard.

### Sprint 7 — Compliance escalation
38. Implement `ComplianceEscalationService`.
39. Add `routers/compliance.py` for reviewers.
40. Wire MNPI review, guardrail failures, hallucination review into escalation queue.
41. Tests for escalation workflow.

### Sprint 8 — Documentation + eval hardening
42. Update `CLAUDE.md` with new conventions.
43. Add eval datasets for connectors, specialists, hallucination.
44. Run full test suite, fix regressions.
45. User review of compliance-depth choices before any production deployment.

---

## 9. Open decisions for user

1. **Compliance reviewer model**: should escalations route to a designated compliance officer user role, or to an external reviewer via email/Slack?
2. **Memory mining scope**: should we default to mining only the last 90 days of email/Slack, or let the PM set a range? Should DMs be excluded by default?
3. **Broker feed priority**: which broker(s) should we support first — Alpaca, Interactive Brokers, or generic CSV upload?
4. **Expert network / ResearchEdge access**: do we have API keys / sample payloads to build against, or should we start with email-forward ingestion?
5. **Artifact action execution**: should actions like `buy_more` / `trim` produce an order draft for review, or only notify the PM with a pre-filled email/Slack message?

---

## 10. Success metrics

- **Ingestion**: 5 new source types connected; >90% deduplication accuracy; <1% connector failure rate.
- **Signal quality**: specialist signals improve drift alert precision by ≥10 points on eval dataset.
- **Personalization**: persona used in ≥3 artifact types; brief LP update NPS-style feedback ≥4/5.
- **Interactivity**: ≥1 decision prompt generated per morning brief; ≥50% of prompts resolved within 24h in test cohort.
- **Trust**: 100% of LLM outputs traced; hallucination review rate <5%; zero cross-PM isolation regressions.
- **Compliance**: every escalation has an audit trail; mean time to reviewer assignment <1 business day.

---

*Plan prepared for AXE deep-planning phase. Next step: user review, then switch to Act mode to implement Sprint 0.*
