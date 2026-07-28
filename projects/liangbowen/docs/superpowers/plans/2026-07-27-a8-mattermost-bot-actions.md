# A8 Mattermost Bot Delivery and Interactive Review Implementation Plan

> **For agentic workers:** Execute each task test-first. Do not broaden A8 into a new auth system or
> frontend. Preserve A5 dispatch and A6/A7 public behavior.

**Goal:** Deliver Agent report cards through Mattermost DMs, process signed teacher actions, and
retain durable, bounded success/final-failure evidence.

**Architecture:** A reusable async client performs bounded Mattermost Bot API requests. Existing A5
notification outbox rows become actual-send leases and finalize with immutable evidence. A strict
signed callback adapter invokes caller-owned A6 review primitives in the same transaction as the A7
integration event.

**Tech Stack:** Python 3.12, FastAPI/Starlette, Pydantic v2, SQLAlchemy async, PostgreSQL 16/18,
Alembic, Celery, httpx, pytest, HMAC-SHA256

---

## Task 1: Pure security, URL, and report-card contracts

**Files:**
- Create: `backend/tests/mattermost/test_action_security.py`
- Create: `backend/tests/mattermost/test_report_card.py`
- Create: `backend/app/integrations/mattermost/actions.py`
- Create: `backend/app/integrations/mattermost/card.py`
- Modify: `backend/app/integrations/mattermost/security.py`
- Modify: `backend/app/integrations/mattermost/schemas.py`

- [ ] Write failing tables for HMAC framing/tampering, strict JSON duplicate/NaN/trailing/unknown
  rejection, ID and 16 KiB boundaries, Markdown/link escaping, grapheme-safe truncation, message and
  props bounds, deterministic output, and exactly three buttons.
- [ ] Run the two focused files and verify RED for missing symbols.
- [ ] Implement closed action types, versioned length-framed signing, strict JSON parsing, safe URL
  joining, and deterministic card rendering with no secrets/submission/reference content.
- [ ] Run focused tests and verify GREEN.

## Task 2: Reusable async Mattermost client

**Files:**
- Create: `backend/tests/mattermost/test_client.py`
- Create: `backend/app/integrations/mattermost/client.py`
- Modify: `backend/pyproject.toml` only if httpx is not already a runtime dependency

- [ ] Write failing MockTransport tests for direct channel/post shape, bearer authentication,
  connection reuse, explicit close, no redirects, timeout configuration, bounded streamed JSON,
  malformed success, 4xx/permanent, 408/429/5xx/network retryable, Retry-After clamping, and complete
  secret/body/URL redaction.
- [ ] Implement one owned `httpx.AsyncClient`, typed safe result/errors, strict IDs, and close
  behavior.
- [ ] Verify focused tests GREEN and run the existing dependency/config tests.

## Task 3: Migration 0006 and model invariants

**Files:**
- Create: `backend/migrations/versions/0006_mattermost_delivery.py`
- Create: `backend/tests/migrations/test_mattermost_delivery_migration.py`
- Create: `backend/tests/evaluations/test_notification_outbox_persistence.py`
- Modify: `backend/app/evaluations/model.py`
- Modify: `backend/app/integrations/mattermost/model.py`

- [ ] Write failing model/direct-SQL tests for notification max-three attempts, terminal mutual
  exclusion, post-ID/error coherence, terminal no-claim state, action/delivery event types, and
  unchanged dispatch attempt behavior.
- [ ] Implement shared SQLAlchemy checks/trigger constants and migration 0006, including the legacy
  delivered-row compatibility rule and guarded downgrade.
- [ ] Run migration fresh upgrade/downgrade/refusal/head/metadata tests and focused persistence tests
  on PostgreSQL 16.
- [ ] Re-run all prior migration and A5 outbox persistence tests.

## Task 4: Actual notification delivery state machine

**Files:**
- Create: `backend/tests/evaluations/test_mattermost_notification_delivery.py`
- Modify: `backend/app/evaluations/outbox.py`
- Modify: `backend/app/evaluations/worker.py`
- Modify: `backend/app/integrations/mattermost/service.py`

- [ ] Write failing tests for one successful DM/post, saved post ID and IntegrationEvent, stable
  outbox correlation prop, duplicate task/idempotent terminal behavior, claim fencing, retryable
  scheduling, Retry-After, permanent terminal failure, third-attempt terminal failure, and no change
  to succeeded job/report.
- [ ] Add a notification-specific callback result/error contract. Keep generic dispatch callback and
  its 1,000-attempt behavior unchanged. Finalize notification outbox + terminal IntegrationEvent in
  one claim-token CAS transaction.
- [ ] Replace the no-op notification task with actual redrive. Make periodic redrive directly send
  due notifications rather than marking a broker publish as delivery.
- [ ] Add a crash-window test: expired post-send lease is reclaimable, stable correlation is reused,
  and documentation asserts at-least-once.
- [ ] Run focused notification, worker, and all existing outbox tests GREEN.

## Task 5: Caller-owned A6 review primitives

**Files:**
- Modify: `backend/tests/reviews/test_service.py`
- Modify: `backend/app/reviews/service.py`

- [ ] Write failing rollback tests for `confirm_in_transaction` and
  `reevaluate_in_transaction`: mutations are visible before commit, disappear on rollback, and no
  Redis/Celery redrive occurs.
- [ ] Extract existing locked business logic into caller-owned methods. Preserve existing REST
  wrappers, exact idempotency, source-report validation, and post-commit dispatch behavior.
- [ ] Run all A6 review, evaluation lineage, and REST tests GREEN.

## Task 6: Interactive action service and endpoint

**Files:**
- Create: `backend/tests/mattermost/test_actions_api.py`
- Create: `backend/tests/mattermost/test_actions_idempotency.py`
- Modify: `backend/app/integrations/mattermost/service.py`
- Modify: `backend/app/integrations/mattermost/router.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/middleware.py`

- [ ] Write failing route tests that prove the 16 KiB streaming boundary and HMAC verification occur
  before DB access. Cover content type/UTF-8/shape errors, wrong signatures, missing/inactive/student
  identities, current/superseded report checks, and safe JSON for every status.
- [ ] Write transaction/idempotency tests for confirm, reevaluate, and openreport; concurrent
  duplicate callbacks must return the exact persisted response and produce one business mutation.
- [ ] Implement action event hashing/claim/replay and one-transaction operation + terminal response.
  Reevaluate must persist only job/outbox work; do not call a broker in the callback.
- [ ] Return HTTP 200 valid Mattermost `update`/`ephemeral_text`/`error` objects for authenticated
  deterministic outcomes; return bounded valid JSON 4xx/5xx elsewhere.
- [ ] Run focused action tests and the complete existing Mattermost A7 suite GREEN.

## Task 7: Configuration, runtime wiring, and least privilege

**Files:**
- Create or modify: `backend/tests/ops/test_mattermost_runtime.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/evaluations/worker.py`
- Modify: `backend/tests/conftest.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

- [ ] Write failing settings/runtime tests for empty-to-None, NUL-safe failures, valid origins,
  HTTPS-in-production, fail-closed missing combinations, A7 slash independence, and secret placement.
- [ ] Add optional settings and wire client construction only in the notification worker path. API
  receives action secret/console URL; worker receives outbound Bot settings; beat and frontend
  receive no Bot token.
- [ ] Verify Compose config contains no default real secrets and runtime logs/errors redact all
  sensitive values.

## Task 8: Packaging, documentation, and acceptance verification

**Files:**
- Modify: `README.md`
- Modify: `backend/scripts/verify_wheel_runtime.py`
- Modify: `backend/tests/ops/test_wheel_runtime.py`
- Modify: `docs/superpowers/specs/2026-07-27-a8-mattermost-bot-actions-design.md` if implementation
  discoveries require precise corrections

- [ ] Document setup, Bot permissions, callback URL, at-least-once crash semantics, three-attempt
  policy, safe failure inspection, and a complete publish → submit → evaluate → DM → action demo.
- [ ] Extend wheel-only verification for client/card/action route imports and valid callback JSON.
- [ ] Run formatter, linter, static checks, full backend suite, migration suite on PostgreSQL 16 and
  18, wheel-only verifier, Compose config validation, and repository secret scan.
- [ ] Conduct two final reviews: requirement trace/security review and compatibility/regression
  review. Resolve all findings and rerun the affected tests before reporting completion.

## Task 9: Second-review compatibility and validation remediation

**Files:**
- Modify: `backend/migrations/versions/0005_mattermost.py`
- Modify: `backend/migrations/versions/0006_mattermost_delivery.py`
- Modify: `backend/app/integrations/mattermost/service.py`
- Create: `backend/app/integrations/mattermost/urls.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/integrations/mattermost/client.py`
- Modify: `backend/app/integrations/mattermost/card.py`
- Modify: `backend/app/integrations/mattermost/schemas.py`
- Modify: `backend/app/evaluations/outbox.py`
- Modify: `backend/tests/migrations/test_mattermost_migration.py`
- Modify: `backend/tests/mattermost/test_actions_service.py`
- Modify: `backend/tests/mattermost/test_actions_api_security.py`
- Modify: `backend/tests/mattermost/test_runtime_config.py`
- Modify: `backend/tests/evaluations/test_outbox.py`

- [x] Restore revision 0005 byte-for-byte to the published A7 version and first prove with a source
  comparison test that any 0005 constraint rewrite is rejected.
- [x] Add a RED migration fixture that creates the physical legacy 0005 ID constraint directly,
  then prove 0006 accepts `0123456789abcdefghijklmnop`, rejects `legacy/user/space` before DDL,
  preserves revision/data/old constraint on failure, and serializes concurrent legacy writes.
- [x] Move the ID constraint transition into 0006 using an `ACCESS EXCLUSIVE` lock and a bounded
  count-only preflight; make downgrade restore the old 0005 constraint.
- [x] Add a RED authorization matrix proving unbound/inactive identities stay HTTP 401 while a
  bound active student receives HTTP 200 with both `error.message` and `ephemeral_text`, creates no
  action/event, and never replays an earlier teacher result.
- [x] Add one shared URL validator and RED tables for NUL/C0/C1 controls, userinfo, query, fragment,
  length, and environment-dependent HTTP policy across Settings, client, action URL, and report URL.
- [x] Apply `validate_mattermost_id` to Settings Bot IDs and demo-binding IDs; map validation to
  configuration/HTTP 422 before database work while retaining the unique-race conflict path.
- [x] Add a RED short-lease double-redriver test using a synchronous `time.sleep` callback; execute
  sync callbacks via `asyncio.to_thread`, then await a returned awaitable so the heartbeat remains
  schedulable.
- [x] Update README/design text to the v2 six-field callback, delivery/user/channel HMAC binding,
  post-delivery DB lookup, request-hash fields, recipient-only DM, and at-least-once crash window.
- [x] Re-run focused tests, PostgreSQL 16 full, PostgreSQL 18 migrations, wheel-only smoke, static
  validation, and secret checks before one atomic commit.
- [ ] Complete both parent final reviews and resolve any resulting findings.

## Task 10: Final quality-review minor remediation

**Files:**
- Modify: `backend/migrations/versions/0006_mattermost_delivery.py`
- Modify: `backend/app/integrations/mattermost/urls.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/integrations/mattermost/card.py`
- Modify: migration and Mattermost URL/card tests

- [x] Add a RED two-connection PostgreSQL test for the legacy reverse lock order, then acquire one
  ordered `ACCESS EXCLUSIVE` lock over outbox, events, and identities as the first 0006 operation.
- [x] Prove a pre-existing delivery-side transaction remains the non-victim and consumes no attempt;
  the migration waits at the first ordered lock, then completes on PostgreSQL 16 and 18.
- [x] Change shared URL limits from code points to UTF-8 bytes and add RED Unicode/ASCII boundaries.
- [x] Validate the three-action URL repetition plus console URL against a conservative shared 32 KiB
  required-card budget during Settings validation, without echoing configuration values.
- [x] Run focused tests, both PostgreSQL migration targets, wheel smoke, Ruff/static checks, and
  commit the two Minor fixes atomically before the parent quality re-review.

## Task 11: Global application/migration transaction gate

**Files:**
- Create: `backend/app/db/migration_gate.py`
- Modify: every production ORM session-factory path
- Modify: `backend/migrations/env.py`
- Modify: `backend/migrations/versions/0006_mattermost_delivery.py`
- Create/modify: DB session, migration concurrency, runtime, and documentation tests

- [x] Add RED tests proving a root ORM transaction takes a stable versioned shared advisory lock as
  its first SQL, nested SAVEPOINTs do not reacquire it, independent app transactions remain
  concurrent, and rollback releases it.
- [x] Add RED static coverage that every production ORM sessionmaker uses the gated factory.
- [x] Add PG16/18 RED topology tests for outbox→identity and identity→outbox application work racing
  migration 0006; require migration to wait only on the gate and both sides to finish correctly.
- [x] Put the key and shared/exclusive statements in one module; acquire the shared transaction lock
  from a custom ORM Session `after_begin` hook and the exclusive lock from Alembic/0006 before DDL.
- [x] Prove failed migration rollback releases the gate and document the per-transaction SELECT cost
  and maintenance-window behavior.
- [x] Correct README wording so URL/card validation is attributed to backend/worker configuration
  startup rather than an API process that does not receive those settings.
- [ ] Run DB/session regressions, PG16 full, PG18 migrations, wheel/static checks, commit atomically,
  and obtain an M=0 reviewer recheck.

## Task 12: Pre-gate legacy-process quiescence contract

**Files:**
- Modify: `backend/app/db/migration_gate.py`
- Modify: `backend/migrations/env.py`
- Modify: migration tests, Makefile, README, and A8 design

- [x] Add RED tests that 0005→0006 refuses a missing or non-exact confirmation before locks/DDL.
- [x] Add PG16/18 tests that confirmed bootstrap still refuses count-only when another client backend
  in the same database is idle or active, leaving revision and data unchanged without leaking
  connection, query, user, DSN, or confirmation values.
- [x] Require exact `MIGRATION_LEGACY_PROCESSES_STOPPED=true`, then query `pg_stat_activity` before
  taking the exclusive migration gate; allow confirmed zero-session bootstrap and preserve the
  steady-state gate for already-upgraded databases.
- [x] Replace the legacy reverse-lock race expectation with proof that preflight refuses immediately
  without deadlock, table locks, or notification attempts.
- [x] Document the stop-confirm-migrate-start runbook, offline SQL prerequisite, maintenance behavior,
  and explicit test-only confirmation without adding any automatic process termination.
- [ ] Run PG16 full, PG16/18 migrations, wheel/static/security checks, commit atomically, and obtain a
  final C0/I0/M0 re-review.
