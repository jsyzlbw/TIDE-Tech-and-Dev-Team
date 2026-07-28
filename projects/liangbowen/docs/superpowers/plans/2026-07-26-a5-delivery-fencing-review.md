# A5 Delivery Fencing Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make evaluation execution and delivery recoverable under real worker loss, broker ambiguity, database outages, cancelled evidence, and invalid notification linkage.

**Architecture:** A dedicated PostgreSQL session advisory lock fences each provider execution for the full external call, while a per-attempt UUID token and monotonic generation fence every terminal write. Outbox rows are claimed in short transactions with expiring UUID leases, published outside transactions, then finalized by token-checked short updates. Database constraints and migration round trips enforce evidence and ownership independently of application code.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL 16/18, Alembic, Celery, pytest/pytest-asyncio.

---

### Task 1: Execution fencing across provider calls

**Files:**
- Modify: `backend/tests/evaluations/test_worker_recovery.py`
- Modify: `backend/tests/evaluations/test_jobs.py`
- Modify: `backend/app/evaluations/model.py`
- Modify: `backend/app/evaluations/service.py`
- Modify: `backend/migrations/versions/0003_evaluation_delivery.py`

- [x] Write a concurrent slow-provider test that starts one execution, submits a redelivered execution while the first PostgreSQL advisory lock is live, and proves the second provider is never invoked.
- [x] Write a worker-loss test that leaves a fresh `running` row, releases the owning database connection without finalizing, and proves redelivery immediately reclaims it.
- [x] Write a stale-token test that advances `execution_token` and proves the old attempt cannot insert a report or finalize the job.
- [x] Run the three tests and confirm they fail because execution lock/token fencing is absent.
- [x] Add nullable `execution_token` and non-negative `execution_generation` to `EvaluationJob`, with the invariant that only `running` rows carry a token.
- [x] Acquire `pg_try_advisory_lock(hashtextextended(job_id::text, 0))` on a dedicated committed connection before claiming; hold it across provider I/O and terminal persistence; always unlock/close in nested `finally`.
- [x] Claim with a fresh UUID and incremented generation, and add the token predicate to every success, failure, cancellation, and compensation write.
- [x] Run the focused tests and existing duplicate/reclaim tests until green.

### Task 2: Fatal compensation must requeue on database failure

**Files:**
- Modify: `backend/tests/evaluations/test_worker_recovery.py`
- Modify: `backend/app/evaluations/service.py`
- Modify: `backend/app/evaluations/worker.py`

- [x] Write a fatal-provider test where the first compensation database write fails and the next succeeds; assert a terminal safe `internal_error` result and no secret leakage.
- [x] Write a persistent compensation failure test; assert the Celery task raises `Reject(requeue=True)` with a fixed safe reason rather than ACKing a `running` job.
- [x] Write a recovery test that restores database writes and redelivers the job, proving it reaches a terminal state.
- [x] Run the tests and confirm current behavior either ACKs or exposes an unfenced running job.
- [x] Retry token-fenced fatal compensation a bounded number of times; on exhaustion raise a private recovery exception.
- [x] Translate only that exception at the Celery boundary into `celery.exceptions.Reject(..., requeue=True)` and sanitize logs/errors.
- [x] Run focused worker recovery tests until green.

### Task 3: Outbox lease claims and transaction-free publishing

**Files:**
- Modify: `backend/tests/evaluations/test_outbox.py`
- Modify: `backend/app/evaluations/model.py`
- Modify: `backend/app/evaluations/outbox.py`
- Modify: `backend/migrations/versions/0003_evaluation_delivery.py`

- [x] Write a callback test that acquires `FOR UPDATE NOWAIT` on its own outbox row during publish, proving no redrive transaction/row lock spans network I/O.
- [x] Write concurrent redriver tests proving one unexpired lease publishes once and competing claims skip it.
- [x] Write lease-expiry and crash-after-publish tests proving an abandoned claim is retried and delivery is at-least-once while downstream job processing remains idempotent.
- [x] Run the tests and confirm they fail against the transaction-held callback implementation.
- [x] Add paired `claim_token`/`claim_expires_at` fields and their consistency constraint.
- [x] Implement a short `SKIP LOCKED` claim transaction, publish each claimed event after commit, and use token-CAS short transactions for delivered/retry finalization.
- [x] Clear claim state on all finalized attempts; keep bounded attempts/backoff and sanitized error types.
- [x] Run outbox and job idempotency tests until green.

### Task 4: Cancelled provider evidence contract

**Files:**
- Modify: `backend/tests/evaluations/test_persistence.py`
- Modify: `backend/tests/evaluations/test_outbox.py`
- Modify: `backend/app/audit/model.py`
- Modify: `backend/app/audit/service.py`
- Modify: `backend/migrations/versions/0003_evaluation_delivery.py`

- [x] Add direct-SQL negative tests for cancelled audits with zero attempts, mismatched provider/model, missing/empty raw output, invalid duration, and missing cancelled error fields.
- [x] Add application validation tests for the same forged `AuditLogCreate` inputs.
- [x] Run the tests and confirm the missing raw-output contract fails.
- [x] Require cancelled evidence to have attempts >=1, bounded non-negative duration, matching job provider/model, and non-empty raw model output in application validation, ORM DDL, and migration DDL.
- [x] Run focused persistence/create-all/migration tests until green.

### Task 5: Data-bearing 0003 downgrade and notification ownership

**Files:**
- Modify: `backend/tests/migrations/test_evaluation_migration.py`
- Modify: `backend/tests/evaluations/test_persistence.py`
- Modify: `backend/app/evaluations/model.py`
- Modify: `backend/migrations/versions/0003_evaluation_delivery.py`

- [x] Add a migration test that upgrades to 0003, inserts genuine cancelled provider evidence, downgrades to 0002, verifies the row is preserved, and upgrades again on PostgreSQL 16 and 18.
- [x] Add direct-SQL tests that reject a notification whose `report_id` belongs to another job and accept the matching job/report pair.
- [x] Run tests and confirm downgrade currently fails on cancelled evidence and mismatched notification ownership currently inserts.
- [x] Keep cancelled-compatible audit constraints/trigger during downgrade so immutable evidence survives without rewrite; ensure the 0002 application remains structurally usable.
- [x] Add a composite `(report_id, job_id)` foreign key backed by the existing report uniqueness, plus an explicit migration precheck for corrupt existing rows before constraint creation.
- [x] Run fresh and data-bearing down/up migration tests until green on both PostgreSQL versions.

### Task 6: Release verification and atomic handoff

**Files:**
- Modify only files required by failures discovered above.

- [x] Run Ruff check/format and `git diff --check`.
- [x] Run focused worker, outbox, persistence, and migration tests.
- [x] Run the entire backend suite on a fresh PostgreSQL 16 database.
- [x] Run data-bearing migration tests on PostgreSQL 18.
- [x] Build a fresh wheel, install it into an independent virtual environment, and smoke Alembic head plus eager REST/outbox behavior against a fresh database.
- [x] Parse Compose statically, dry-run Make targets, and report Docker unavailability honestly if unchanged.
- [x] Stop temporary PostgreSQL clusters, trash generated artifacts, verify a clean intended diff, and create one atomic commit.
