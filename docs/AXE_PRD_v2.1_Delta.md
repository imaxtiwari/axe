# AXE PRD v2.1 — Delta Document
## From Wall Street AI Co-pilot to Investment Operating System

**Owner:** TBD · **Status:** Delta to v2.0 · **Version:** v2.1 · **Last updated:** 2026-07-29

This document does not replace v2.0. It flags every major gap, proposes concrete schema/agent/process changes, and expands AXE's scope from a single-PM public-equity tool into a multi-asset, multi-stakeholder **Investment Operating System** that can support deal flow, LP reporting, investment thesis testing, underwriting, and presentation materials.

---

## A. Executive Summary of Changes

### New strategic scope
AXE v1 was a personal co-pilot for long/short equity PMs. AXE v2.1 explicitly becomes the **system of record for investment judgment** across:
- Public equities (existing)
- Private equity / growth equity / venture (new)
- Credit / distressed / structured products (new)
- LP/GP relationship management (new)
- Internal investment committee and underwriting workflows (new)

The core technical thesis remains: **thesis-aware signal filtering + adversarial review + compounding memory**. The delta adds the workflows, compliance guardrails, and output formats needed for a fund to run on AXE, not just one PM to work faster.

### What stays the same
- Single-PM v1 focus remains the launch wedge.
- Slack + email as primary delivery.
- Azure Foundry zero-retention.
- SQLite + ChromaDB v1 datastore.

### What changes materially
- New `fund_entity`, `investment_vehicle`, `lp_relationship`, `deal_room`, and `dataroom_ingestion` schemas.
- New agents: `DealFlowAgent`, `UnderwritingAgent`, `DeckBuilderAgent`, `LPUpdateAgent`, `ThesisTestAgent`, `ComplianceAgent`.
- Mandatory source citation on every LLM output.
- Audit log as a first-class data store.
- MNPI rules and records-retention policies baked in.
- Memory layer expanded to vehicles, deals, LPs, and IC narratives.
- Build sequence re-sequenced to put compliance and evals first.

---

## B. The Expanded Vision (v2.1)

### Original (v2.0)
> A PM who has used AXE for six months should feel that it knows her better than any colleague does.

### Expanded (v2.1)
> A fund that has used AXE for a year should feel that it cannot originate, underwrite, monitor, or report an investment without AXE knowing the full context — and surfacing the right risk, signal, or stakeholder question at the exact moment it matters.

This means AXE supports five workstreams:

| Workstream | Primary user | Core AXE output |
|---|---|---|
| **Public book management** | PM / trader | Morning brief, drift alerts, sparring, thesis tracker |
| **Deal flow & origination** | Deal lead / sponsor | Deal memo drafts, comparable analysis, IC prep |
| **Private investment thesis testing** | Investment team | Underwriting checklists, scenario analysis, stress tests |
| **LP/investor communications** | IR / CFO / partner | LP update drafts, deck materials, FAQ generation |
| **Post-investment monitoring** | Portfolio manager | Management commitment tracker, KPI drift, exit memo prep |

---

## C. New Use Cases (Deep)

### C.1 Deal Flow Agent
**User story:** A growth-equity partner gets a CIM, VDR link, and three introductory calls. Today she manually builds a one-page memo in three hours. AXE ingests the CIM, call transcripts, and precedent comps and produces a structured first-pass memo with explicit risks.

**Workflow:**
1. PM forwards CIM PDF or VDR URL to AXE Slack.
2. `DealFlowAgent` extracts: target overview, financial summary, ownership structure, key investment thesis, risks, management team, use of proceeds, valuation markers.
3. Agent pulls public comparables from Polygon + private comp database (placeholder for later integration).
4. Output: structured deal memo with sections `Opportunity`, `Thesis`, `Key Risks`, `Open Diligence Questions`, `Precedent Comps`, `Recommended Next Step`.
5. PM can request adversarial review: `sparring on deal [name]` runs the same mandatory sequence as a public-equity thesis.
6. Deal memo is versioned and linked to a new `deal_room` record.

**Schema additions:**
- `deal_rooms` table: id, pm_id, name, stage (sourcing/diligence/IC/passed/invested/exited), asset_class, target_ticker_or_private_name, cim_url, status, created_at.
- `deal_documents` table: id, deal_id, source_type, file_path/content_url, extracted_entities (JSON), ingestion_status.
- `deal_thesis_versions` table: mirrors `thesis_versions` but for deals; adds `stage` and `checkpoints`.

**Agent design:**
- 4-node LangGraph: parse → extract → compare → synthesize memo.
- Memory injection: PMProfile + sector memory.
- Output gated by uncertainty labels: every extracted number must be `confirmed` or `estimated`.

---

### C.2 Underwriting Agent
**User story:** A credit PM is underwriting a private debt deal. She needs a repeatable diligence checklist, scenario-model inputs, and a documented base/bear/bull case. AXE builds the underwriting frame from the term sheet and historical comparables.

**Workflow:**
1. PM uploads term sheet and financial model.
2. `UnderwritingAgent` parses the term sheet (covenants, pricing, security, EBITDA definitions).
3. Agent generates a diligence checklist keyed to the asset class: credit asks about cash flow stability, leverage trajectory, collateral coverage; equity asks about TAM, management quality, exit path.
4. Agent requests missing items: "I need 3 years of audited financials and a capitalization table to complete covenant stress testing."
5. Scenario output: base/bear/bull cases with assumptions and triggers.
6. Sparring agent can stress-test the underwriting thesis.

**Schema additions:**
- `underwriting_checklists` table: id, deal_id, category, question, status, evidence_url, answered_by, updated_at.
- `underwriting_scenarios` table: id, deal_id, scenario_name, assumptions (JSON), output_metrics (JSON), probability_weight.

**Eval requirements:**
- Term-sheet extraction accuracy must be ≥90% on a labeled set of 30 term sheets.
- Diligence checklist completeness measured against expert-created checklists.

---

### C.3 Thesis Testing Agent
**User story:** A biotech PM writes a thesis: "Approval probability for Phase 3 asset X is 70%." She wants AXE to track the experiments, FDA milestones, and competitor data that would confirm or refute each assumption.

**Workflow:**
1. PM enters a thesis with testable hypotheses.
2. `ThesisTestAgent` breaks each key assumption into a set of **falsifiable statements** with pre-defined pass/fail criteria.
3. Agent continuously monitors incoming signals and flags when a test passes, fails, or becomes inconclusive.
4. Outputs a "thesis health dashboard" in Slack: green/yellow/red per assumption, with next expected catalyst.

**Schema additions:**
- `thesis_tests` table: id, thesis_version_id, assumption_id, test_statement, pass_criteria, fail_criteria, status, last_evaluated_at, evidence_id.
- `thesis_test_results` table: id, test_id, result, evidence, evaluated_at.

This formalizes the drift-detection pipeline: instead of a black-box similarity score, every signal is evaluated against explicit test statements.

---

### C.4 Deck Builder Agent
**User story:** The partner needs a one-page investment memo for IC, or the IR person needs an LP update draft. AXE pulls the latest thesis, signals, and performance context into a formatted document.

**Workflow:**
1. User sends: `/axe deck IC memo for NVDA` or `/axe deck LP update Q2`.
2. `DeckBuilderAgent` selects a template based on output type.
3. Agent gathers from memory layer: current thesis, recent signals, sparring open items, management commitments, performance context.
4. Agent generates slides/memo in Markdown/PowerPoint-compatible structure (via `python-pptx` or HTML export).
5. Output is editable; user comments are tracked and can be merged back into the thesis.

**Schema additions:**
- `deck_templates` table: id, name, asset_class, audience, structure (JSON).
- `deck_outputs` table: id, pm_id, type, source_ids (JSON), content (JSON), export_url, created_at.

**Compliance note:** every generated deck must include a footer with version date, source list, and a disclaimer: "Draft — for internal discussion only."

---

### C.5 LP Update Agent
**User story:** A fund needs to produce quarterly LP updates for three vehicles. AXE drafts the letter, performance summary, portfolio updates, and attributable commentary from the quarter's activity.

**Workflow:**
1. CFO/IR triggers `/axe lp update Q2 2026 for Fund I`.
2. `LPUpdateAgent` gathers all public-book changes, private-deal milestones, realized/unrealized marks, cash flows, and management letters.
3. Agent produces: `Executive Summary`, `Portfolio Changes`, `Top Contributors / Detractors`, `Notable Events`, `Outlook`, `Appendix: Full Portfolio`, `Appendix: Definitions`.
4. Numerical data is pulled from accounting system placeholders; AXE does not calculate NAV itself. It drafts narrative around verified numbers.
5. Human reviews numbers before send.

**Schema additions:**
- `investment_vehicles` table: id, name, legal_entity, strategy, vintage, currency, reporting_frequency.
- `lp_relationships` table: id, vehicle_id, lp_name, contact_email, side_letter_flags, preferences.
- `lp_updates` table: id, vehicle_id, quarter, sections (JSON), status, approved_by, sent_at.

**Compliance rule:** LP updates are classified as **regulated communications** in many jurisdictions. All drafts must be reviewed and approved by a human before distribution; AXE cannot send directly.

---

## D. Compliance, Regulatory & Records (Deep)

### D.1 New principles
1. **Every investment-relevant action creates an immutable audit record.**
2. **MNPI is flagged at ingestion, not discovered later.**
3. **All LLM outputs have versioned source citations.**
4. **Human approval is required before any external communication.**
5. **Retention and data residency are configurable per fund entity.**

### D.2 New schema: `audit_log`
```
id: UUID PK
pm_id: FK(pm_users)
fund_entity_id: FK(fund_entities)
action_type: str  [thesis_create, thesis_update, signal_ingest, alert_fire, sparring_complete, deal_memo_create, lp_update_send, memory_correction]
object_type: str
object_id: UUID
before_state: JSON
after_state: JSON
source_ip: str
session_id: str
client_timestamp: datetime
server_timestamp: datetime
retention_class: str  [standard, mnpi, regulatory]
```
- Append-only. No updates, no deletes by application logic.
- Separate encrypted archive after retention period.
- Exportable for regulator/examiner requests.

### D.3 MNPI handling
- Add `mnpi_flag` enum to `signal_log`, `meeting_summaries`, `deal_documents`: `public`, `nonpublic`, `restricted`.
- Ingestion router applies heuristics: expert-network transcripts, NDR-only research notes, management calls before earnings release, VDR materials → default `restricted` pending human review.
- Restricted signals are not used in drift detection or morning brief until a PM explicitly releases them.
- Alert to compliance: if `mnpi_flag=restricted` and `source_type=expert_network`, route a copy to compliance queue.

### D.4 Records retention
- Default retention: 7 years for all investment-related records.
- Add `retention_policy` table per fund_entity.
- Automated purge only after legal hold check passes.

### D.5 Communications archiving
- **Remove BlueBubbles from v1 default entirely.** iMessage cannot be archived by fund compliance tools.
- Slack DMs must route through the fund's existing Slack e-discovery/retention workspace.
- Email delivery must use a fund-controlled email domain. No personal Gmail aliases.
- Add `communication_archive` table logging every Slack DM and email sent with content hash.

### D.6 Cross-border and entity isolation
- New `fund_entities` table: id, legal_name, jurisdiction, tax_id, data_residency, retention_years, mnpi_policy.
- `pm_users` now linked to `fund_entity_id`.
- EU/EEA/SG/CH funds require data residency flags; storage and LLM inference routing must respect these.
- Add GDPR/CCPA data-export and deletion workflows.

### D.7 Compliance agent
New `ComplianceAgent` reviews outputs before externalization:
- LP update draft? Compliance agent flags unsupported performance claims, missing disclaimers, side-letter terms.
- Deal memo with MNPI? Blocks sharing outside restricted user list.
- Thesis alert referencing a restricted research note? Replaces raw signal with "restricted source — see compliance."

---

## E. Explainability, Citations & Trust (Deep)

### E.1 Citation Layer
Every signal and generated output must include:
```json
{
  "source_id": "UUID",
  "source_type": "earnings_transcript|research_email|meeting_recording|polygon|sec_filing|deal_document",
  "source_url": "...",
  "quote_span": "...",
  "page_number": 1,
  "speaker": "...",
  "timestamp": "2026-07-29T10:00:00Z",
  "mnpi_flag": "public|nonpublic|restricted",
  "extraction_confidence": "high|medium|low"
}
```
- Drift detection alert: "Data center capex cycle assumption may be weakening — see MSFT Q4 transcript, CFO quote at 14:23, page 18."
- Sparring challenge: each challenge links to ≥1 contradicting signal or is labeled `inferred`.
- Morning brief: each section links to source signal with 1-tap expand.

### E.2 Model uncertainty labels
Every LLM output gets one of:
- `extracted` (from source)
- `inferred` (reasonable extrapolation)
- `uncertain` (low-confidence)

Agents must downgrade `inferred`/`uncertain` outputs; they do not trigger alerts automatically.

### E.3 Eval expansion beyond sycophancy
Add three eval suites with separate ship gates:
1. **Sparring sycophancy eval** (existing): ≥95% pass.
2. **Drift detection eval**: 50 labeled signals per asset class. Target precision ≥85%, recall ≥80%.
3. **Citation accuracy eval**: 100 generated claims; ≥90% must have a correct, verifiable citation.
4. **Brief usefulness eval**: Human judges rate top 3 signals selected by AXE; NDCG@3 ≥ 0.75.
5. **Memory correctness eval**: 30 PM memory inferences; ≥90% must be supported by evidence or correctly labeled uncertain.

---

## F. Architecture & Operational Resilience (Deep)

### F.1 Database
SQLite is acceptable for v1 but risk-bearing. Mitigations:
- Write-ahead logging (WAL) enabled.
- Hourly encrypted backups to S3/R2-compatible object store.
- Daily backup restore tests in CI.
- Schema migrations versioned with Alembic or similar.
- For multi-PM scale, add a transparent migration path to PostgreSQL by Sprint 11.

### F.2 Idempotency & deduplication
Move content deduplication from v1.1 to v1:
- SHA-256 content hash at ingestion.
- Source-specific idempotency keys (Gmail message ID, Slack event ID, Polygon transcript ID).
- `dedup_log` table: content_hash, source_id, first_seen_at.

### F.3 Message broker & async workers
Replace SQLite `retry_queue` as the primary worker mechanism by Sprint 5 with a proper broker:
- Cloud-native: SQS, Google Pub/Sub, or Cloudflare Queues.
- Self-hosted alternative: Redis Streams + Celery, or a PostgreSQL-backed queue.
- Keep SQLite retry queue as fallback but not the main path.

### F.4 Model fallback
- Primary: Azure Foundry.
- Fallback: secondary Azure deployment in a different region, or OpenAI/Azure OpenAI redundancy.
- If all LLM routes fail, return a clear outage message instead of hallucinating.

### F.5 Polygon & data coverage
Verify:
- Earnings transcript coverage: US listings, ADRs, key international listings.
- WebSocket stability and allowed reconnect rates.
- Pre-market and after-hours earnings handling.
- Add SEC EDGAR ingestion as a backup for earnings transcripts.

### F.6 Staging & chaos testing
Add to build:
- Staging environment mirroring production.
- Chaos tests: Azure timeout, Polygon disconnect, SQLite lock, OAuth expiry, broker failure.
- Load test: 50 active PMs, 50 tickers each, 1,000 signals/day.

---

## G. User Workflow Edge Cases (Deep)

### G.1 Position lifecycle
Add `direction` and `status` to `thesis_versions`:
```
direction: long|short|neutral|arbitrage
status: active|reduced|closed|paused|watchlist
position_size_bucket: tiny|small|medium|large|max_conviction
```
- Short theses: drift logic inverts; a bull signal "breaks" a short thesis.
- Closed theses trigger a **Post-Mortem Agent**.

### G.2 Post-Mortem Agent
When a PM closes a thesis, AXE prompts:
- Was the thesis right or wrong?
- What assumption broke first?
- What signal did you ignore?
- Outcome logged to memory and `thesis_post_mortems` table.
This closes the feedback loop for behavioral pattern tracking.

### G.3 Quiet hours, urgency tiers, Do Not Disturb
- `pm_quiet_hours` table: start_time, end_time, timezone, override_keywords.
- Urgency tiers:
  - `breaking`: overrides quiet hours.
  - `high`: Slack + email, respects quiet hours.
  - `routine`: batched into morning brief or next business window.
- PM can set keywords: only interrupt for ticker X or "take-private" deals.

### G.4 Ticker/deal lifecycle events
- Delistings, ticker changes, M&A, spin-offs: `corporate_actions` table.
- AXE must handle successor tickers and inherited thesis assumptions.
- Acquired positions: thesis auto-archived with `resolution: acquired`.

### G.5 Draft vs. published theses
- Add `is_draft` flag to `thesis_versions`.
- Drafts are not used for drift detection or alerts until published.
- Sparring can run against drafts.

### G.6 Multi-asset support
- `asset_class` enum: public_equity, fixed_income, private_equity, venture, credit, crypto, macro.
- Each asset class has asset-specific thesis fields:
  - Credit: covenant package, recovery analysis, yield/carry.
  - Venture: ownership %, valuation methodology, board rights, follow-on reserves.
  - Macro: regime thesis, instrument exposure, hedge structure.

---

## H. Memory & Personalization (Deep)

### H.1 Expanded PMMemory object
Keep the three components but add:
- **FundContext:** fund-specific style, compliance rules, IC preferences.
- **AssetClassMemory:** per-asset-class patterns.
- **DealMemory:** per-deal room.citations to deals; open diligence items.

### H.2 Uncertainty labels in memory
Every inferred field in PMProfile must be:
- `evidence_based` with citations, or
- `inferred` with confidence score, or
- `unknown`

If PM corrects a field twice, future synthesis requires stronger evidence to overwrite.

### H.3 Long-horizon memory
In addition to last 3 synthesized versions, keep:
- Quarterly compressed snapshots for 2 years.
- Annual "narrative of evolution" summaries.
- This allows "how has my thinking on X evolved over 18 months?"

### H.4 Ticker memory triggers
Beyond high-signal events, add:
- Every 6 hours during market hours for active positions.
- Every material signal arrival (>0.85 relevance).
- Nightly full synthesis.

### H.5 Cross-PM learning (future)
- Anonymized pattern extraction across ≥10 PMs.
- Only after explicit fund opt-in and legal review.
- Never expose individual books or positions.

---

## I. Evals, Metrics & Iteration (Deep)

### I.1 New eval suites
| Eval | Target | When to run |
|---|---|---|
| Sparring sycophancy | ≥95% | Every system prompt/model change |
| Drift detection precision/recall | P≥85%, R≥80% | Weekly on new golden set |
| Citation accuracy | ≥90% | Every agent change |
| Brief usefulness (NDCG@3) | ≥0.75 | Weekly |
| Memory correctness | ≥90% | Every synthesis prompt change |
| Term-sheet extraction | ≥90% | Every doc parser change |
| Cross-PM isolation | 100/100 | Every deploy |

### I.2 Human-in-the-loop feedback
- One-tap reactions to every brief and alert: ✅ useful, ⚠️ wrong ticker, ❌ irrelevant, 🚫 stop sending this type.
- Feedback logged to `signal_feedback` table; used to retrain relevance scoring.
- Weekly "memory audit" prompt: AXE asks PM to confirm or correct one inferred memory field.

### I.3 Iteration thresholds
Add new thresholds:
- If drift alerts have false-positive rate >20% for 2 weeks: pause auto-alerts and revert to queued review.
- If PM corrects memory >3 times in 7 days: flag synthesis conservatism review.
- If brief NDCG@3 < 0.5 for any PM: trigger format A/B test.

---

## J. Go-to-Market & Pricing (Deep)

### J.1 Target expansion
| Tier | User | Price anchor | Features |
|---|---|---|---|
| **Individual PM** | Single long/short equity PM | $300-500/month | Thesis + brief + sparring |
| **Fund seat** | Multi-PM fund, no enterprise features | $500-800/seat/month | + audit logs + basic compliance |
| **Enterprise GP** | Fund with LP reporting/private deals | Custom ($20K+/month minimum) | + deal flow + LP updates + SOC 2 |

### J.2 Pilot terms
- 90-day paid pilot with 3-5 PMs.
- Success: ≥80% retention, ≥3 thesis updates/week, ≥50% morning brief open rate, zero compliance incidents.
- Design partner agreement required for Gmail/Calendar OAuth and future OMS.

### J.3 Competitive positioning
Differentiation vs. ChatGPT + Notion:
| Capability | AXE | Generic AI + Notes |
|---|---|---|
| Signal-to-thesis relevance | Native | Manual |
| Adversarial discipline | Enforced sequence | Optional |
| Compounding memory | Synthesized + versioned | Static notes |
| Compliance/audit | Built-in | None |
| Deal flow/LP workflows | Integrated | Manual |

---

## K. Build Sequencing v2.1

The 13-prompt plan remains but is reordered and expanded to 15 prompts. Security/compliance and evals move earlier.

```
SPRINT 1 (Week 1-2) — Foundation + Compliance
  Prompt 0:  Project scaffold + tooling + CI skeleton
  Prompt 1:  SQLite schema v2.1 (all 32 tables) + Alembic
  Prompt 2:  Encryption, audit logging, cross-PM isolation
  Prompt 3:  Idempotency, deduplication, retry queue
  Prompt 4:  Thesis store (CRUD + versioning + async write lock)
  Prompt 5:  Onboarding, cold start, PM profile foundation

SPRINT 2 (Week 3-4) — Ingestion + Agents
  Prompt 6:  Ingestion pipeline (Gmail, Slack, Polygon, PDF/OCR)
  Prompt 7:  LLM abstraction, thesis extraction, sparring agent + evals
  Prompt 8:  Drift detection, thesis testing, earnings alerts

SPRINT 3 (Week 5-6) — Orchestration + Delivery
  Prompt 9:  Morning brief + scheduler
  Prompt 10: Meeting intelligence + citation layer

SPRINT 4 (Week 7) — Integration Gate
  Prompt 11: Integration tests + all eval gates + README
             All evals must pass before Sprint 5.

SPRINT 5 (Week 8) — Auth + Multi-Asset Expansion
  Prompt 12: Clerk auth + OAuth + Gmail/Calendar poller + deal flow schema

SPRINT 6 (Week 9-10) — Private Markets + Infrastructure
  Prompt 13: DealFlowAgent + UnderwritingAgent + DeckBuilderAgent
  Prompt 14: Polygon subscription manager + PDF/OCR pipeline + message broker + Fly deploy

SPRINT 7 (Week 11-12) — Memory + LP/GP
  Prompt 15: PM memory layer + LPUpdateAgent + profile correction commands + memory injection wiring
```

### New gating rules
- **Eval gate (Sprint 4):** sparring ≥95%, drift precision/recall ≥85/80, citation accuracy ≥90, isolation 100/100.
- **Compliance gate (Sprint 1):** audit log writes on every action, MNPI flagging present, isolation tests pass.
- **Scale gate (Sprint 6):** load test passes, backup/restore verified, broker fallback works.

---

## L. Updated/Extended Schema Reference

### New tables
- `fund_entities`
- `audit_log`
- `dedup_log`
- `pm_quiet_hours`
- `thesis_tests`, `thesis_test_results`
- `thesis_post_mortems`
- `corporate_actions`
- `deal_rooms`, `deal_documents`, `deal_thesis_versions`
- `underwriting_checklists`, `underwriting_scenarios`
- `deck_templates`, `deck_outputs`
- `investment_vehicles`, `lp_relationships`, `lp_updates`
- `communication_archive`
- `signal_feedback`

### Modified tables
- `pm_users`: add `fund_entity_id`, `role`, `compliance_approved`.
- `ticker_registry`: add `asset_class`, `direction`, `status`, `position_size_bucket`.
- `thesis_versions`: add `direction`, `status`, `asset_class`, `is_draft`, `fund_entity_id`, `mnpi_flag`.
- `signal_log`: add `citation`, `mnpi_flag`, `extraction_confidence`, `idempotency_key`.
- `sparring_sessions`: add `output_format`, `citation_list`, `deal_id`.
- `meeting_summaries`: add `mnpi_flag`, `speaker_roles`, `citation_timestamps`.
- `pm_memory`: add `fund_context`, `asset_class_memories`, `deal_memories`, `uncertainty_labels`.
- `retry_queue`: add `broker_attempted`, `dead_letter_at`.

---

## M. Phase 2 / Phase 3 Backlog Expansion

### Phase 2 (Months 4-9)
- OMS/EMS integration
- Bloomberg B-PIPE
- Behavioral pattern tracking
- Multi-PM fund accounts with attribution
- Natural language order drafting (human-in-the-loop)
- Web dashboard read-only viewer
- Content deduplication engine v2
- PostgreSQL migration path
- Private-market data integrations (PitchBook, CapIQ, Preqin)

### Phase 3 (Months 10-18)
- Cross-PM anonymized pattern learning (opt-in)
- On-prem/private cloud deployment
- Mobile app
- Voice interface for hands-free sparring
- Advanced scenario Monte Carlo engine
- LP-facing secure portal
- Regulator-ready export package

---

## N. Decision Log Additions

| Date | Decision | Rationale | Rejected alternatives |
|---|---|---|---|
| 2026-07-29 | Expand scope from single-PM equity tool to multi-asset Investment OS | User demand for deal flow, LP updates, underwriting support | Stay pure equity-only |
| 2026-07-29 | Compliance and audit logging built into v1 foundation, not bolted on at Sprint 6 | Compliance incidents are catastrophic and hard to retrofit | Add later as config |
| 2026-07-29 | BlueBubbles/iMessage removed from v1 default | Cannot meet fund records/retention requirements | Keep as optional add-on |
| 2026-07-29 | Mandatory source citations and uncertainty labels on every LLM output | Finance users require explainability and trust | Model-only outputs |
| 2026-07-29 | Cross-PM learning deferred to Phase 3 with explicit opt-in | Privacy and legal complexity | Build anonymized learning in v1 |
| 2026-07-29 | SQLite retained for v1 but with migration path to PostgreSQL by v1.5 | Faster v1 build without painting into a corner | Immediate PostgreSQL only (slower v1) |
| 2026-07-29 | Post-mortem agent added to close feedback loop | Behavioral memory requires outcome data | Manual post-mortem only |
| 2026-07-29 | LP updates require human approval before send | Regulated communication; liability | Auto-send LP updates |
| 2026-07-29 | Deal flow and underwriting agents use asset-class-specific templates | One-size-fits-all templates produce low-quality outputs | Generic investment memo template |

---

## O. Open Questions Additions

| Question | Owner | Target |
|---|---|---|
| Which audit firm and SOC 2 scope? | Founder/Legal | 14 days post-v2.1 sign-off |
| Can we get a zero-retention rider covering multi-modal inputs (audio, images, PDFs) and not just text? | Legal/Engineering | Sprint 1, Week 1 |
| How do we handle VDR-provider authentication for deal document ingestion? | Engineering | Sprint 6 |
| What is the fund-level licensing model for LP update and deal flow modules? | Founder/GTM | Sprint 5 |
| Which accounting/performance system will AXE read from for LP updates? | Engineering/Product | Sprint 7 |
| Do we need a FINRA-registered entity or RIA status to support LP communication drafting? | Legal | 14 days |
| What jurisdictions require local data residency and can Azure Foundry satisfy all of them? | Legal/Engineering | Sprint 1 |
| Should the sparring agent have different tone/profile for private-market vs public-market theses? | Product | Sprint 7 |

---

## P. What to Cut or Deprioritize

1. **BlueBubbles/iMessage:** completely remove from v1 scope. Revisit if compliance can certify it.
2. **Electron desktop app:** keep out of v1 and v1.1. Web dashboard only.
3. **50-ticker truncation:** instead, set a soft cap at 30 tickers in onboarding and study signal quality.
4. **Mobile app:** Slack app + email responsive design is sufficient until Phase 3.
5. **Cross-PM learning:** Phase 3 only.
6. **Advanced Monte Carlo:** Phase 3; underwriting agent starts with scenario tables, not simulation.

---

## Q. Step-by-Step Build Prompts Reference

See `docs/BUILD_PROMPTS.md` for the full ordered list of implementation prompts with done-when gates and tests for each.
