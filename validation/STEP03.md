# Step 03 validation evidence — complete

## Status

**Step 03 is complete as of 2026-09-16.** Step 03A and locked dependency acquisition/offline installation passed previously. After the explicitly authorized narrow worker and isolation-policy remediations, one complete ordered run passed confinement, focused tests, full tests, compliance, lint, formatting, mypy, Pyright, and policy. Results below come from that single run, not a combination of earlier attempts.

The user authorized only the dedicated type-check container to use 2 GiB RAM and 2 GiB combined memory/swap; all other controls remain unchanged and other checks retain 512 MiB. Existing workspace remediations were inspected and preserved on resumption; no additional product edits were needed. No dependency changes, redesign, restriction weakening, migration, deployment, real-data inspection, cleanup, commits, or pushes were performed. The external commit and branch state (ahead one) remain untouched.

## Changes

- [Settings](../src/axe/config.py:21): validation opts out of dotenv and file-secret sources before Pydantic constructs them; normal dotenv behavior remains intact. Adds a configurable readiness directory with the existing default.
- [Application](../src/axe/main.py:33): lifespan/readiness use the configured directory.
- [Dependency image](step03b/Dockerfile.dependencies), [installer](step03b/install_dependencies.py), and [launcher](step03b/install.sh): verify acquired artifacts again, install only hashed binary wheels offline, and compare all installed identities/versions with the selected lock manifest.
- [Application image](step03c/Dockerfile), [runner](step03c/runner.py), [launcher](step03c/run.sh), and [focused tests](step03c/test_settings.py): synthetic pre-import storage/settings, restricted child execution, confined caches/coverage/reports, baseline orchestration.
- Existing acquisition/export/selection evidence remains under [Step 03B](step03b/).

## Commands actually executed

The host launches used an empty environment, PATH limited to system tools, HOME=/var/empty, and COPYFILE_DISABLE=1. Docker calls explicitly used the local socket and empty client configuration; builds used no network, no pulls, and retained intermediates.

- Shell syntax check followed by [dependency launcher](step03b/install.sh:1), then reruns after diagnosed harness failures.
- Shell syntax check followed by [application launcher](step03c/run.sh:1), then complete reruns after hook and runner changes.
- Runtime commands are defined in [installer](step03b/install_dependencies.py:28) and [baseline stage dispatch](step03c/runner.py:35), with executed stage commands/exit codes retained in run logs.
- Final targeted source diff/whitespace check and SHA-256 checks of [lock](../uv.lock) and [project configuration](../pyproject.toml) passed.

## Dependency results

141 Linux ARM64-compatible wheels, 197,793,542 bytes, acquired on the host from approved HTTPS artifact URLs. Each size/hash matched the lock. No package was downloaded in a test container. Installation ran nonroot under the additional socket filter into fresh scratch storage, using the pip bundled in the digest-pinned Python image, no index, binary-only, hashes required, no dependency resolution, no cache, and no bytecode compilation.

[Successful installation report](step03b/evidence-axe-step03b-install-18fb41f9-526b-4423-a7b1-c64b99759f0c/install-report.json) records all 141 exact installed versions, Python 3.12.14 and Node v22.23.2. Export used pinned uv 0.12.15. Key tools: pytest 9.1.1, pytest-cov 7.1.0, Ruff 0.16.1, mypy 2.3.0, Pyright 1.1.411.

Lock SHA-256: c979f5340395926bb699fab93cf8bf76bb3c4f5464f1b0bc7b9b087576e36e88.
Project SHA-256: fd7b84c309dbf949fff24ee0856fc6bf3a52478767e75f50f3eea18cc9802b6f.
Both match the pre-install export evidence.

Retained failed install attempts:

- 0e738bac-5e35-4d6e-9886-a7915eb7ccdb: USTAR omitted long wheel names; verification failed before installation. Corrected to GNU long-name records and separately checked archive creation.
- 493b06e4-e584-4c51-a4d4-2b5e79f8e293 and d4f5bd12-6845-493a-9164-866da07d0e98: uv timed out after resolution, including single-worker retry. Root cause not established; restrictions were not relaxed.
- 317f45b3-3d6e-4e18-a7c2-fd86965cbadc: bundled pip installed successfully but comparison mishandled dotted package names. Corrected canonical normalization; clean full reinstall then passed.

## Original failed baseline (historical)

[Evidence directory](step03c/evidence-axe-step03c-2d8c934c-1b5c-44c3-9121-4fd216190a20/) contains before/after container inspection, build/run logs, context inventory, JUnit/coverage XML, per-stage logs and [exit summary](step03c/evidence-axe-step03c-2d8c934c-1b5c-44c3-9121-4fd216190a20/reports/result.json).

| Check | Executed result |
| --- | --- |
| Container configuration and synthetic parent/child confinement | Passed; source/outside writes EROFS, synthetic secret read EACCES, IPv4/IPv6 socket creation EPERM, UNIX sockets/SQLite permitted in scratch |
| Focused settings/import/readiness | 3 passed |
| Full tests | 436 passed, 1 failed, 1 warning; 43.83 seconds; coverage rounded 79% |
| Separate compliance suite | 88 passed, 1 warning; 91.69% coverage, unchanged 90% threshold passed |
| Ruff lint | Passed |
| Ruff formatting | Passed, 118 files already formatted |
| mypy | Killed by signal 9, no diagnostics; not a pass, cause not conclusively established |
| Pyright | Node heap exhaustion, wrapper exit 250; not a pass |
| Repository isolation-policy checker | Exit 1, 15 findings; not a pass |

Latest image: sha256:e63f1a17a0645e66c0cbf64f1621817d7721a03fac2c80654d6df412cddd0e49.
Container: axe-step03c-2d8c934c-1b5c-44c3-9121-4fd216190a20.
Scratch volume: same name plus -scratch. All retained.

Earlier complete baseline [d4116fd6](step03c/evidence-axe-step03c-d4116fd6-bf62-434a-84c6-fc433c37ec1f/) had an export fallback failure because the harness unnecessarily set an optional export key. That variable was removed and remains absent. The original baseline above instead failed worker start/stop with a PendingRollbackError after shutdown invalidated the shared savepoint connection. At that point cancellation was suspected but not yet established; the authorized diagnosis and remediation are recorded below. These historical failures are not counted as passing results.

The policy findings are retained verbatim in the [policy log](step03c/evidence-axe-step03c-2d8c934c-1b5c-44c3-9121-4fd216190a20/reports/policy.log). They are checker findings requiring review, not proof of exploitable data leaks.

## Isolation and limitations

- Digest-pinned ARM64 base images; local derived images addressed by immutable IDs.
- Runtime: UID/GID 10001, read-only root, sole fresh named scratch volume, network none, all capabilities dropped, no-new-privileges, default Docker seccomp plus inherited additional BPF filter, private PID, IPC none, no host mounts/devices/ports/socket.
- Limits: 1 CPU and 64 PIDs throughout. Baseline/policy containers retain 512 MiB RAM and 512 MiB combined memory/swap. Only the dedicated mypy/Pyright container uses the authorized 2 GiB RAM and 2 GiB combined memory/swap (no additional swap allowance). Named-volume disk quota is not enforced. Standard kernel pseudo-filesystems still exist.
- Additional filtering precedes third-party and AXE imports. Python site initialization disabled; explicitly selected pytest plugins only. Scratch storage/environment configured before application imports. No real dotenv/secrets/data included in image context.
- Parent/child tests prove specified boundaries, not a comprehensive security certification. The host supervisor is best-effort while Docker responds.
- Unit tests use mocks/synthetic data and existing shared-connection fixtures. They do not validate live providers, independent-writer concurrency, pilot authorization readiness, deployment, or backups.
- Packaging/production image build not attempted: Hatchling is not pinned in the acquired closure, and production build/pull/network behavior is outside this harness. Optional system OCR executable behavior is not certified.
- Original Step 03A resources and all failed attempts remain preserved. Historical unsafe macOS tests are not this baseline.

## Authorized remediation and regression evidence

Repository conventions, the implementation plan's testing requirements, the actual original baseline logs, current diffs, and harness were reviewed before further work. The pre-remediation [diff](step03d-review-87d178dd-f288-4248-89d7-6ed80fa6c82e/before.diff) and [dependency hashes](step03d-review-87d178dd-f288-4248-89d7-6ed80fa6c82e/dependency-hashes-before.txt) remain retained.

### Worker cancellation / shared savepoint

The original shutdown cancelled the worker after its handler ran but before database work necessarily finished. Cancellation during database I/O could invalidate the connection shared with the test's outer savepoint. [Worker shutdown](../src/axe/ingestion/worker.py:165) now signals the existing stop event and awaits the in-flight transaction; the same event wakes idle polling immediately. No fixture redesign or database rollback suppression was added.

The [parameterized regression](../tests/test_ingestion.py:277) pauses separately inside the handler and before commit, verifies shutdown waits without cancellation, then checks the persisted success state through the shared connection. Both cases failed in the retained [negative-control run](step03c/evidence-axe-step03c-e3acaa16-8699-4640-ad7f-3074b5b74bc8/worker/reports/worker.log); the [post-fix worker run](step03c/evidence-axe-step03c-a9367495-0a72-4670-bc80-153d123ad5bf/worker/reports/result.json) passed. Both regressions and the original start/stop test also passed in the final full suite. Graceful shutdown waits for in-flight work; this does not establish bounded completion for a permanently hung handler or independent-writer concurrency safety.

### Fifteen isolation-policy findings

The checker and its existing rules were not changed. No model was reclassified as global.

| Original findings | Narrow remediation |
| --- | --- |
| Seven repository queries | [Repositories](../src/axe/db/uow.py:73): thesis/memo reads use context scoping; signoffs require the fund; underwriting children join their scoped deal. |
| Persona scheduler enumeration | [Scheduler](../src/axe/services/brief_scheduler.py:111): documents the existing intentional system-wide enumeration; dispatch retains each active PM/fund identity. |
| Escalation listing and reviewer counts | [Escalations](../src/axe/services/compliance_escalation.py:249): listing requires a fund and inherits the PM role/identity when applicable; reviewer loads explicitly filter the fund. |
| Export audit trail | [Export](../src/axe/services/export.py:86): requires entity scope and filters audit rows by the entity's PM and available fund, in addition to object identity. |
| Memo thesis lookup | [Memo](../src/axe/services/ic_memo.py:210): adds the service PM predicate. |
| Two LP queries | [LP communications](../src/axe/services/lp_comms.py:156): scopes update and recipient queries through the investment vehicle's fund. |
| Retention enumeration | [Retention](../src/axe/services/retention.py:99): documents existing context-free system maintenance; request calls restrict rows to the caller, joining thesis parents for child models without direct tenant columns. |

[Isolation regressions](../tests/test_step03_isolation.py) cover missing-context rejection, generated query predicates, PM-role inheritance, foreign audit exclusion, request-scoped versus system retention, child-model retention joins, LP vehicle scoping, and active-tenant scheduler dispatch. Query-shape tests complement database-backed tests; they are not a comprehensive authorization audit.

Intermediate attempts remain retained: [1ef9165b](step03c/evidence-axe-step03c-1ef9165b-e887-4a12-a05d-ead9f0843060/) exposed retention child models lacking direct scope columns; parent joins fixed that regression. [6572e245](step03c/evidence-axe-step03c-6572e245-ebc8-49cd-b21f-53854379a4aa/) retained formatting and reviewer-count policy failures. The count expression was named separately so the explicit fund predicate fits the unchanged checker's inspection window. [826e0f9a](step03c/evidence-axe-step03c-826e0f9a-e2ed-4384-9b4f-418f97764f7f/) retained Pyright's possibly-unbound loop-local fund finding in the new scheduler test; the test now references the created user instead. None of those attempts establishes completion.

### Type-check resource failures

The original Pyright log establishes Node heap exhaustion; original mypy exited on signal 9 without diagnostics, so its exact kill cause remains unproven. The authorized [launcher](step03c/run.sh:30) runs baseline, types, and policy sequentially from one immutable image, with a fresh volume per group. Only types receives the higher limit. Both type checks now complete successfully; no dependency or type-check configuration was changed. Existing mypy test-module overrides remain as declared in the unchanged project configuration.

## Final complete ordered run

[Final evidence](step03c/evidence-axe-step03c-2d5b22f4-dbf7-4589-b064-5aa553b28c55/) contains the archived inputs, build log, source diff, input hashes, container inspections, stage commands, JUnit/coverage reports, and per-stage logs. Baseline ran 16:33:31–16:34:26 UTC, types 16:34:27–16:36:14 UTC, and policy 16:36:15–16:36:16 UTC on 2026-09-16. Every group exited zero with OOMKilled false. Each group passed the unchanged parent/child confinement self-test before executing its stages (25 passing gate records per group).

Image: sha256:21e7645ecf6b9af4fc60ad88b5b4d0a678c93f8937a8e42b3a6afb29e79850c5.

| Ordered check | Final executed result |
| --- | --- |
| Confinement | Passed in all three containers; inspected limits, mounts, capabilities, identity, network, and security options match the authorized configuration. |
| Focused settings/import/readiness | 3 passed, exit 0. |
| Full suite | 455 passed, exit 0; 28.97 seconds; coverage rounded 80%. Includes 18 added regression cases. |
| Separate compliance | 88 passed, exit 0; 90.16% coverage exceeds the unchanged 90% threshold. |
| Ruff lint | All checks passed, exit 0. |
| Ruff formatting | 119 files already formatted, exit 0. |
| mypy | No issues in 119 source files, exit 0. |
| Pyright | 0 errors, 0 warnings, 0 informations, exit 0. |
| Isolation policy | No violations found, exit 0. |

Exit summaries: [baseline](step03c/evidence-axe-step03c-2d5b22f4-dbf7-4589-b064-5aa553b28c55/baseline/reports/result.json), [types](step03c/evidence-axe-step03c-2d5b22f4-dbf7-4589-b064-5aa553b28c55/types/reports/result.json), [policy](step03c/evidence-axe-step03c-2d5b22f4-dbf7-4589-b064-5aa553b28c55/policy/reports/result.json).

The focused/full/compliance invocations each retain one upstream Starlette TestClient/httpx deprecation warning. No dependency substitution or warning suppression was introduced. Packaging, live integrations, deployment, and broad security certification remain outside this validation, as noted above.

The resumed command was the existing application launcher in an empty host environment after a shell syntax check; application imports and all checks executed only inside the confined containers. The source archive was compared with the current file inputs in [verified snapshot hashes](step03c/evidence-axe-step03c-2d5b22f4-dbf7-4589-b064-5aa553b28c55/source-snapshot-verified.txt). An earlier auxiliary hash comparison concatenated duplicate archive entries for the policy script and failed; that report remains retained, and the corrected comparison used first-occurrence extraction. It did not change the image or validation results. Dependency hashes still match the prechange values above, and the final Git whitespace check passed.

## Task state

- [x] Review instructions, original failure evidence, current changes, and harness.
- [x] Preserve and verify narrow worker/policy fixes and regression coverage.
- [x] Verify types-only 2 GiB authorization and unchanged controls elsewhere.
- [x] Execute and inspect one complete ordered all-passing validation run.
- [x] Review source snapshot, dependency hashes, and retained branch state.
- [x] Update Step03 evidence and completion state.

No Step03 authorization blocker remains. All original, failed, and successful containers, volumes, images, and evidence are retained; no cleanup was performed.
