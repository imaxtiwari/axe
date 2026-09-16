# Step 04 — Identity Trust Boundary Plan (corrected)

**Status: incomplete; validation blocked before the latest rerun. Do not deploy.**

See [the Step04 evidence report](../validation/STEP04.md) for the failed checks and remaining work.

This plan corrects the previous draft against the user's explicit approvals:

- Clerk JWT → verified email → authoritative existing `PMUser` row.
- No raw `X-PM-ID` / `X-Fund-ID` / `X-Role` fallback.
- No dev bypass.
- No `external_id` schema migration.
- Verified email required (`email_verified == true`).
- Required `azp` in configured authorized-party allowlist; no default live/localhost origin.
- Signed role/fund/pm claims never override the database.
- Fail-closed when configuration is missing in all environments.

## Approved identity contract

| Item | Approved value |
| --- | --- |
| Provider | Clerk |
| Token | Session token in `Authorization: Bearer <jwt>` |
| Email claim | `email` (template shortcode `{{user.primary_email_address}}`) |
| Verified ownership | `email_verified == true` (template shortcode `{{user.email_verified}}`) |
| Issuer | Configured `CLERK_ISSUER`, e.g. `https://evolved-moth-9228.clerk.accounts.dev` |
| Algorithm | `RS256` only |
| Key source | JWKS from configured trustworthy public source only; `CLERK_JWKS_JSON` for tests |
| Authorized party (`azp`) | Required and must be in configured `CLERK_AUTHORIZED_PARTIES` allowlist |
| Audience (`aud`) | Validated strictly only if `CLERK_AUDIENCE` is configured |
| Missing configuration | Fail-closed (401) for all business routes |
| Test key injection | `CLERK_JWKS_JSON` accepted only when `APP_ENV=test` |
| Dev bypass | Removed entirely |

## Files to change

- `src/axe/config.py` — add Clerk settings.
- `src/axe/security/jwt.py` — new module: RS256 verification, JWKS cache, offline test keys, strict claim validation.
- `src/axe/security/context.py` — replace header-based identity with JWT + DB membership resolution; remove dev bypass; keep `RequestContext.bind` for internal/test use only.
- `src/axe/security/authz.py` — role checks operate on verified context.
- `src/axe/main.py` — install default-deny middleware covering every HTTP route, including docs and metrics; exempt only GET/HEAD health/readiness/root.
- `src/axe/routers/transcripts.py` — require payload `pm_id` to match authenticated identity for every role; validate destinations against authoritative membership.
- `src/axe/routers/onboarding.py` — remove `_verify_self_or_bypass`; preserve current role gating and require self, including admins.
- `src/axe/routers/connectors.py` — require self for every role; no admin cross-PM impersonation, even within the same fund.
- `src/axe/routers/deals.py`, `lp.py`, `interactive.py`, `persona.py`, `mnpi.py`, `compliance.py`, `audit.py` — rely on verified context.
- `src/axe/ingestion/worker.py` — bind authoritative owner from DB before handlers; reject inactive/missing/mismatched.
- `src/axe/services/mnpi.py` — use authoritative owner for queued alert release.
- `tests/conftest.py` — add synthetic RSA key fixture and authenticated client helper.
- `tests/test_security.py` — replace header tests with JWT tests; add comprehensive negatives.
- `tests/test_compliance.py`, `test_compliance_router.py`, `test_deals.py`, `test_quarterly_simulation.py` — update to use JWT fixtures.
- `validation/STEP04.md` — evidence report.

## Not changing

- No `PMUser.external_id` field or migration.
- No live Clerk network access in tests.
- No schema changes beyond settings.
- No broad redesign of compliance assignment/product gating.

## Business entry points to cover

All FastAPI routers in `src/axe/routers/`:

- `/onboarding/*`
- `/api/v1/persona/*`
- `/api/v1/transcripts/*`
- `/api/v1/mnpi/*`
- `/api/v1/audit/*`
- `/api/v1/compliance/*`
- `/api/v1/deals/*`
- `/api/v1/lp-updates/*`
- `/api/v1/artifacts/*`
- `/api/v1/connectors/*`

Exempt from authentication:

- `/healthz`
- `/ready`
Docs and metrics require authentication. No prefix-based exemption or default frontend origin.
- `/`

## Implementation steps

1. Add Clerk settings to `src/axe/config.py`.
2. Create `src/axe/security/jwt.py` with `JWTVerifier`.
3. Create `src/axe/security/identity.py` with `resolve_identity`.
4. Refactor `src/axe/security/context.py`: middleware verifies once, resolves fresh membership once, and binds one context tied to this HTTP scope. Dependencies consume that context, never independently resolve or accept an internal ContextVar binding as HTTP proof. Reset safely on every exit.
5. Wire default-deny middleware in `src/axe/main.py`. Require exact issuer, RS256, configured public key, integer time claims, valid session/subject, strict boolean verified email, required allowed azp, and configured audience when present. Validate membership ID, existing fund, active state and recognized database role; no provisioning.
6. Update routers to remove raw-header assumptions and body-based identity overrides.
7. Update worker handlers to bind authoritative owner from DB.
8. Update tests to use synthetic JWT fixtures.
9. Add negative tests.
10. Run isolated validation.
11. Document evidence and prerequisites.

## Risks and prerequisites

- Clerk session token template must include `email` and `email_verified` as a valid JSON object.
- `CLERK_AUTHORIZED_PARTIES` must be configured with the real frontend origin before business access is enabled.
- Existing `PMUser` rows must have email addresses matching Clerk user emails.
- Production outbound network must allow JWKS fetch only from the explicitly configured trusted public signing-key URL; the issuer does not implicitly select a URL.
- Public keys are cached for at most 300 seconds. Unknown key IDs fail until the next TTL refresh; expired-cache fetch failures deny access rather than use stale keys. Provider key rotation/revocation behavior still requires validation.
- Strict verified ownership means a JSON boolean true, not a string or integer. Actual Clerk claim rendering has not been certified.
- Actor overrides, queue ownership, delivery destinations, existing test migration, and the full acceptance gates remain unfinished. No admin cross-PM exception is permitted, including within the same fund.
