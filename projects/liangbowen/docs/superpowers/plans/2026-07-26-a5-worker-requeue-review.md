# A5 Worker Requeue Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure recoverable infrastructure failures and exhausted terminal compensation never become acknowledged Celery failures or falsely reported terminal outcomes.

**Architecture:** Keep domain/programming exceptions unchanged, but classify only connection/transaction infrastructure exceptions and `OSError` at the bound Celery task boundary into a fixed `Reject(requeue=True)`. Inside `EvaluationService`, reserve `EvaluationJobFailed` for a terminal state that is already durable and raise `EvaluationRecoveryRequired` when bounded cancellation or withdrawal-evidence persistence is exhausted.

**Tech Stack:** Python 3.12, Celery, FastAPI, SQLAlchemy async, PostgreSQL 16/18, pytest/pytest-asyncio.

---

### Task 1: Recoverable Celery lifecycle failures

**Files:**
- Modify: `backend/tests/evaluations/test_worker_recovery.py`
- Modify: `backend/app/evaluations/worker.py`

- [x] Add direct-boundary tests that inject sanitized `OSError` and SQLAlchemy connection/transaction exceptions from `_run_evaluation`, and assert fixed `Reject("evaluation recovery required", requeue=True)` without secret text.
- [x] Add an eager-task test for persistent failure on redelivery and assert the result is `REJECTED`, contains only the fixed reason, and retains `requeue=True`.
- [x] Add negative tests proving permission/business failures and programming/data-integrity errors are not converted into broker requeues.
- [x] Run the new tests and confirm the current worker produces a normal Celery failure instead of a reject.
- [x] Add a narrow recoverable-infrastructure predicate for `OSError`, SQLAlchemy disconnection/interface/operational, pool-timeout, and pending-rollback failures while excluding permission/integrity/programming faults.
- [x] Convert only the classified failures at the bound task boundary to the fixed broker reject; do not sleep inside the worker.
- [x] Run the focused worker boundary tests until green.

### Task 2: Exhausted terminal compensation

**Files:**
- Modify: `backend/tests/evaluations/test_worker_recovery.py`
- Modify: `backend/tests/evaluations/test_jobs.py`
- Modify: `backend/app/evaluations/service.py`

- [x] Add a cancellation test whose three terminal update transactions fail and assert `EvaluationRecoveryRequired`, never success or `EvaluationJobFailed`.
- [x] Add a withdrawal-after-provider test whose three cancelled-evidence transactions fail and assert `EvaluationRecoveryRequired` with the job remaining safely reclaimable.
- [x] Add redelivery recovery coverage proving restored database writes eventually produce the expected durable terminal state.
- [x] Run the tests and confirm the exhausted paths currently raise `EvaluationJobFailed` or become an acknowledged failure.
- [x] Change only the two bounded compensation exhaustion branches to raise the fixed `EvaluationRecoveryRequired` exception.
- [x] Verify successful compensation still raises `EvaluationJobFailed` only after the terminal row/audit is durable.
- [x] Run focused job and worker recovery tests until green.

### Task 3: Release verification and handoff

**Files:**
- Modify only files required by the failing tests.

- [x] Run all evaluation tests plus Ruff, formatting, compile and diff checks.
- [x] Run the entire backend suite against a fresh PostgreSQL 16 database.
- [x] Run the migration suite against a fresh PostgreSQL 18 database.
- [x] Build the exact current wheel, install it into an independent environment, and smoke the worker rejection and Alembic schema.
- [x] Parse Compose and dry-run Make targets; report Docker availability accurately.
- [x] Stop and trash all temporary databases/build artifacts, verify the intended clean diff, and create one atomic commit.
