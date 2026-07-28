# A5 DBAPI Invalidation Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Requeue SQLAlchemy DBAPI failures only when SQLAlchemy marks the underlying connection invalidated, without broadening requeue behavior to integrity, programming, or permission faults.

**Architecture:** Extend the existing worker-boundary predicate with one explicit `DBAPIError.connection_invalidated is True` branch. Prove the real driver shape by terminating one dedicated PostgreSQL backend from a separate connection, then cover direct and eager/redelivered Celery boundaries with fixed sanitized rejects.

**Tech Stack:** Python 3.12, Celery, SQLAlchemy async, asyncpg, PostgreSQL 16/18, pytest.

---

### Task 1: RED coverage and minimal classifier fix

**Files:**
- Modify: `backend/tests/evaluations/test_worker_recovery.py`
- Modify: `backend/app/evaluations/worker.py`

- [x] Add a PostgreSQL integration test that obtains one connection backend PID, terminates it from a second connection, performs a query, and asserts the resulting `DBAPIError` has `connection_invalidated is True`.
- [x] Feed that real exception through the direct worker boundary and assert fixed `Reject("evaluation recovery required", requeue=True)` with no exception/DSN leakage.
- [x] Cover first eager delivery and redelivery with a connection-invalidated `DBAPIError`, asserting both are `REJECTED` rather than `FAILURE`.
- [x] Extend negative coverage so `IntegrityError`, `ProgrammingError`, and `PermissionError` are not requeued even when their hierarchy overlaps DBAPI/OSError families.
- [x] Run the focused tests and record the expected RED against the existing classifier.
- [x] Add only `isinstance(error, DBAPIError) and error.connection_invalidated is True` to the recoverable predicate after explicit permission, integrity, and programming exclusions.
- [x] Run the focused worker tests until green.

### Task 2: Release verification and handoff

**Files:**
- Modify only files required by Task 1.

- [x] Run all evaluation tests and static checks.
- [x] Run the full backend suite against a fresh PostgreSQL 16 database.
- [x] Run migrations against a fresh PostgreSQL 18 database.
- [x] Build/install the exact wheel and smoke invalidated-DBAPI rejection plus Alembic head.
- [x] Parse Compose, dry-run Make, and report Docker availability accurately.
- [x] Stop and trash temporary resources, verify the intended diff, and create one atomic commit.
