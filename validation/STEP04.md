# Step04 — partial implementation; acceptance NOT passed

## Status

Stopped at an offline Docker build failure on 2026-09-16. This is not a completed identity-boundary delivery and must not be deployed. No commit or push was performed. No real sign-in origin was configured.

## Changes present

- [Settings](../src/axe/config.py): explicit issuer, authorized origins, optional audience, trusted public JWKS URL, bounded key cache, and test-only synthetic public keys.
- [Verifier](../src/axe/security/jwt.py): RS256 signature and issuer verification, required session/subject/time claims, strict boolean verified email, and required authorized party. Token-directed key URLs are rejected.
- [Membership resolver](../src/axe/security/identity.py): exact email lookup of an existing active member joined to an existing fund, with recognized role validation; no provisioning.
- [HTTP context](../src/axe/security/context.py): middleware resolves one context tied to the actual HTTP scope. Internal context binding does not authenticate HTTP. Default deny includes docs, metrics, unknown routes, and onboarding; only GET/HEAD root, health and readiness are exempt.
- [Application wiring](../src/axe/main.py) passes its explicit settings to the boundary. [Role dependency](../src/axe/security/authz.py) requires the same request-specific verified context.
- [Synthetic tests](../tests/test_step04_identity.py) and an identity-only stage in the retained [runner](step03c/runner.py) and [shell harness](step03c/run.sh).
- [Plan](../plans/STEP04_PLAN.md) corrected to prohibit admin impersonation, protect metrics, require explicit key source, and disclose incomplete status.

## Commands and evidence

First identity run:

```sh
/usr/bin/env -i PATH=/usr/bin:/bin HOME=/var/empty COPYFILE_DISABLE=1 /bin/bash -c '/bin/bash -n validation/step03c/run.sh && /bin/bash validation/step03c/run.sh identity'
```

Retained [run log](step03c/evidence-axe-step03c-e71bf73c-431b-403e-a33a-587a39a1a1fd/identity/run.log), [test log](step03c/evidence-axe-step03c-e71bf73c-431b-403e-a33a-587a39a1a1fd/identity/reports/identity.log), and [format log](step03c/evidence-axe-step03c-e71bf73c-431b-403e-a33a-587a39a1a1fd/identity/reports/format.log).

Image: sha256:d3fe90402432319da6cfd2f888c0518596c8689dcf7d8a7f785795e2fa659e47.
Container: 2ca402d5903f2b1d21fd1bdcb4f35c366a4a148fd912fab95f5914407c3572b5.

| Gate | Observed result |
| --- | --- |
| Parent and child confinement self-tests | Passed |
| Focused settings checks | Exit 0 |
| Identity tests | 41 passed, 1 failed during fixture setup |
| Ruff lint | Exit 0 |
| Ruff format | Exit 1; context, verifier, new test module need formatting |
| Overall | Exit 1 |

The failed fixture used an invalid fund constructor field. It was changed to the actual legal-name field. The corrected membership test has NOT run successfully; neither positive HTTP membership nor fresh revocation is certified by this run.

Attempted rerun:

```sh
/usr/bin/env -i PATH=/usr/bin:/bin HOME=/var/empty COPYFILE_DISABLE=1 /bin/bash validation/step03c/run.sh identity
```

The retained [build log](step03c/evidence-axe-step03c-3ee2b11d-0088-49b0-b4b5-bcac8c4ecb2d/build.log) records exit 1 before container validation. At dependency-copy step Docker reported a missing cached image:

```text
unable to find image "sha256:0d6b953a7e69edb9088aedfb0f06ffc082d908c1845f0e804bf0816f754771da"
```

No image pulls, cache cleanup, resource deletion, network enablement, or host AXE imports were attempted in response. A minimal possible recovery is an explicitly approved cache-disabled offline build using the same digest-pinned bases and unchanged confinement; this was not attempted or verified.

## Confinement

The identity stage uses the same retained Step03 harness: clean Docker environment, network-none builds and execution, read-only root, UID/GID 10001, no capabilities, no-new-privileges, IPC none, private PID namespace, no host mounts/devices/ports, one CPU, 64 PIDs, and 512 MiB memory plus combined memory/swap limit. Parent and child seccomp tests deny non-UNIX sockets and retain the existing io_uring restrictions. Synthetic storage, readiness, caches, temporary files and coverage remain under container scratch storage. Validation mode is set before AXE imports and disables dotenv and file-secret loading. No dependency or lock changes were made.

## Trust model and real-ingress prerequisites

A signed session proves verified email ownership, not AXE membership or role. The database supplies member ID, fund and role. Tokens and legacy identity headers cannot override them. Membership must be freshly resolved on each HTTP request. Internal bindings are not HTTP proof.

Missing authorized-origin configuration denies business access in every environment. The actual issuer alone is insufficient. Before real ingress, configure a trusted public signing-key URL and the exact real sign-in origin, verify Clerk emits JSON boolean verified-email claims, ensure existing membership emails match, and complete all acceptance gates. No provider access or compatibility certification has been performed.

Keys expire from cache within at most 300 seconds. Unknown IDs deny until refresh; failed refresh does not use expired keys. Offline signed-token verification does not establish immediate provider-session revocation. These semantics and rotation behavior still need explicit tests and deployment review.

## Unfinished security and regression work

- Remove existing onboarding references to the deleted development-bypass field; authenticated onboarding is currently broken on that path.
- Remove remaining router admin cross-PM exceptions and reject actor/body/fund overrides, including reviewer, signer, approver and checklist actor identities. Review-assignment targets are resources, not actors.
- Scope audit, MNPI and escalation reads without redesigning compliance assignment or enabling deferred deal/LP behavior.
- Remove raw-header fallback from isolation-failure audit metadata.
- Bind workers to fresh authoritative queue owners and reject missing, inactive or mismatching queue/row/payload identity.
- Trace and secure transcript delivery destinations and released alerts.
- Complete downstream source inspection and existing raw-header test migration. Existing tests referring to removed context APIs are not yet updated.
- Add signature/key/configuration/cache failures, concurrency, scope-proof spoofing, invalid membership, actor override and queue negative tests.
- Fix formatting and run fresh focused checks, then ordered full suite, compliance coverage at least 90%, Ruff lint/format, mypy, Pyright and isolation policy. None of the full Step04 acceptance gates has passed.

Business access must remain blocked while these issues are unresolved. Passing 41 synthetic tests is not acceptance of the boundary or business authorization.
