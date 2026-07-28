# A6 Teacher Review and Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add transaction-safe teacher confirmation, modification, and re-evaluation with immutable audit evidence, exact source-report lineage, request IDs, and PostgreSQL migration coverage.

**Architecture:** `ReviewService` is the single review state machine: it locks the submission before the report, validates the latest report and active teacher, and writes the state transition plus `ReviewAction` atomically. Manual re-evaluation reuses a transaction-aware `EvaluationService` enqueue helper; the worker supersedes the exact persisted source only after a successful fenced evaluation. PostgreSQL constraints and triggers enforce evidence integrity independently of the API.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async, PostgreSQL 16/18, Alembic, Celery/outbox, pytest/pytest-asyncio

---

## File map

- Create `backend/app/reviews/__init__.py`: package boundary.
- Create `backend/app/reviews/types.py`: review action enum.
- Create `backend/app/reviews/model.py`: append-only `ReviewAction` ORM model and constraints.
- Create `backend/app/reviews/schemas.py`: strict request and request-ID response schemas.
- Create `backend/app/reviews/service.py`: review state machine and typed domain errors.
- Create `backend/app/reviews/router.py`: teacher-only REST adapter.
- Create `backend/migrations/versions/0004_reviews.py`: job source lineage, review evidence, active-teacher and transition triggers, safe downgrade.
- Modify `backend/app/evaluations/model.py`: persist `source_report_id` on manual jobs.
- Modify `backend/app/evaluations/service.py`: transaction-aware enqueue and source-aware success persistence.
- Modify `backend/app/evaluations/api_schemas.py`: expose source report only in the reviewed job response contract where needed.
- Modify `backend/app/main.py`: review router, 16 KiB body limit, request-ID middleware and A6 errors.
- Modify `backend/migrations/env.py`: import review metadata for Alembic comparison.
- Modify `backend/tests/conftest.py`: register `ReviewAction` metadata.
- Create `backend/tests/reviews/helpers.py`: deterministic teachers, students, submissions, reports, and sessions.
- Create `backend/tests/reviews/test_persistence.py`: database constraints, triggers, and evidence immutability.
- Create `backend/tests/reviews/test_workflow.py`: service state machine and rollback behavior.
- Create `backend/tests/reviews/test_api.py`: authentication, validation, privacy, limits, request IDs, and OpenAPI.
- Create `backend/tests/reviews/test_concurrency.py`: serialization and idempotency races.
- Create `backend/tests/migrations/test_review_migration.py`: PostgreSQL 16/18 upgrade, downgrade, refusal, and schema comparison.
- Modify `backend/tests/evaluations/test_jobs.py`: source-aware enqueue and worker result behavior.
- Modify `backend/tests/evaluations/test_worker_recovery.py`: source-race failure safety.

## Task 1: Persist review evidence and source lineage

**Files:**
- Create: `backend/app/reviews/__init__.py`
- Create: `backend/app/reviews/types.py`
- Create: `backend/app/reviews/model.py`
- Create: `backend/migrations/versions/0004_reviews.py`
- Modify: `backend/app/evaluations/model.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/conftest.py`
- Create: `backend/tests/reviews/helpers.py`
- Create: `backend/tests/reviews/test_persistence.py`
- Create: `backend/tests/migrations/test_review_migration.py`

- [x] **Step 1: Write failing ORM and database-contract tests**

Add tests that import `ReviewAction`/`ReviewActionType`, persist a valid confirmation, and prove these exact rules:

```python
assert action.action is ReviewActionType.CONFIRM
assert action.changes == {"review_status": {"before": "proposed", "after": "confirmed"}}
assert manual_job.source_report_id == report.id

with pytest.raises(IntegrityError):
    await persist_review_action(teacher=inactive_teacher)
with pytest.raises(IntegrityError):
    await session.execute(update(ReviewAction).values(comment="rewrite"))
with pytest.raises(IntegrityError):
    await session.execute(delete(ReviewAction))
```

Also test manual jobs without a source, non-manual jobs with a source, cross-submission source references, malformed `changes`, comments above 8,000 UTF-8 bytes, invalid report-status transitions, and teacher-origin reports created by inactive teachers.

- [x] **Step 2: Run the persistence tests and verify RED**

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_persistence.py -q
```

Expected: collection fails because `app.reviews` does not exist.

- [x] **Step 3: Define ORM types and models**

Define the enum and table contract:

```python
class ReviewActionType(StrEnum):
    CONFIRM = "confirm"
    MODIFY = "modify"
    REEVALUATE = "reevaluate"

class ReviewAction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "review_actions"
    report_id: Mapped[UUID]
    teacher_id: Mapped[UUID]
    action: Mapped[ReviewActionType]
    changes: Mapped[dict[str, object]]
    comment: Mapped[str]
    created_at: Mapped[datetime]
```

Add the unique `(report_id, action)` constraint, report/teacher foreign keys, JSON-object and byte-size checks, relationships, and `EvaluationJob.source_report_id` plus the composite source/submission foreign key and reason/source consistency check.

- [x] **Step 4: Implement migration `0004_reviews`**

The upgrade must, in this order:

1. Add nullable `evaluation_jobs.source_report_id`.
2. Backfill each released manual job from the latest same-submission report whose `created_at <= queued_at`; raise if any source is unprovable.
3. Add source/reason check and composite foreign key.
4. Create `review_action_type`, `review_actions`, indexes, checks, and unique constraint.
5. Install a trigger requiring an active teacher and validating exact action-specific `changes` ownership.
6. Install update/delete/truncate rejection triggers for review evidence.
7. Replace evaluation report triggers so teacher-origin creation requires an active teacher and only documented status transitions are allowed.
8. Extend job immutability to include `source_report_id`.

The downgrade must first run one guard block that raises if either `review_actions` has data or any job has `source_report_id`; only an empty A6 schema may remove triggers, table, enum, constraint, and column.

- [x] **Step 5: Write migration lifecycle tests and verify RED-to-GREEN**

Test both configured PostgreSQL majors with:

```python
await upgrade("base", "0004_reviews")
await downgrade("0004_reviews", "0003_evaluation_delivery")
await upgrade("0003_evaluation_delivery", "head")
```

Add a data-bearing downgrade test asserting the downgrade raises and both `alembic_version` and evidence rows remain unchanged. Add Alembic `compare_metadata` assertions with no unexpected diff after importing `app.reviews.model` in `migrations/env.py` and `tests/conftest.py`.

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_persistence.py tests/migrations/test_review_migration.py -q
```

Expected: all persistence and migration tests pass on the available PostgreSQL targets.

## Task 2: Implement confirmation and immutable modification

**Files:**
- Create: `backend/app/reviews/schemas.py`
- Create: `backend/app/reviews/service.py`
- Create: `backend/tests/reviews/test_workflow.py`

- [x] **Step 1: Write strict schema and service tests**

Cover `ConfirmRequest`, `ReportPatch`, full structured-field replacement, copied immutable fields, derived grade, HTML-preserving comments, active teacher checks, current-version checks, pending-retry locks, idempotent same-teacher confirmation, other-teacher confirmation conflicts, and transaction rollback.

Use assertions equivalent to:

```python
confirmed = await service.confirm(report.id, teacher.id, comment="已核验")
assert confirmed.review_status is ReviewStatus.CONFIRMED

modified = await service.modify(
    report.id,
    teacher.id,
    ReportPatch(score=94, limitations=["未运行程序"], comment="<b>原样保存</b>"),
)
assert modified.version == report.version + 1
assert modified.origin is ReportOrigin.TEACHER
assert modified.grade is Grade.A
assert source.review_status is ReviewStatus.SUPERSEDED
assert action.comment == "<b>原样保存</b>"
```

Validate empty patch, unknown fields, booleans as scores, scores outside 0–100, comment over 4,000 characters, and client-supplied `grade` all fail schema validation.

- [x] **Step 2: Run workflow tests and verify RED**

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_workflow.py -q
```

Expected: failures identify missing review schemas/service.

- [x] **Step 3: Implement strict schemas**

Use `ConfigDict(extra="forbid", strict=True)`. `ConfirmRequest` and `ReevaluateRequest` contain only optional `comment`; `ReportPatch` contains optional `completeness`, `correctness`, `major_issues`, `suggestions`, `score`, `limitations`, and `comment` and rejects a request where every field is absent. Normalize CRLF to LF, retain HTML as untrusted text, cap comments at 4,000 characters, and never accept `grade`.

- [x] **Step 4: Implement `ReviewService` locking and transitions**

Expose typed errors `ReviewNotFound`, `ReviewForbidden`, and `ReviewConflict`. Each method must begin its own transaction, re-read an active teacher, lock the submission first, then lock the target report, verify it is the current non-superseded version, and query pending manual jobs before mutation.

`confirm()` changes only `review_status`, inserts the exact before/after action, and returns the existing result for a same-teacher duplicate. `modify()` clones all report evidence, applies validated fields, derives grade through `grade_for_score`, creates a new teacher-origin `modified` report, supersedes the source, and records field-level before/after values plus `result_report_id` and comment.

- [x] **Step 5: Verify service GREEN and rollback isolation**

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_workflow.py -q
```

Expected: confirmation, modification, conflicts, validation, audit, and injected-flush rollback tests pass.

## Task 3: Reuse A5 enqueue and supersede the exact source on success

**Files:**
- Modify: `backend/app/evaluations/service.py`
- Modify: `backend/app/evaluations/api_schemas.py`
- Modify: `backend/app/reviews/service.py`
- Modify: `backend/tests/evaluations/test_jobs.py`
- Modify: `backend/tests/evaluations/test_worker_recovery.py`
- Modify: `backend/tests/reviews/test_workflow.py`

- [x] **Step 1: Write failing source-lineage and re-evaluation tests**

Prove that one review transaction creates exactly one `manual_retry` job, one dispatch outbox, and one review action; retrying the same source returns the same job/action; broker failure leaves all three committed and the outbox pending; job creation failure rolls back all three.

Worker assertions must include:

```python
assert succeeded_report.version == source.version + 1
assert source.review_status is ReviewStatus.SUPERSEDED

await fail_manual_job(job)
assert source.review_status is original_status
assert await report_count(submission.id) == original_count
```

Add a race where a newer report wins before worker persistence; the manual job must finish `failed` with sanitized metadata and neither report may be incorrectly superseded.

- [x] **Step 2: Run focused tests and verify RED**

Run:

```bash
cd backend
.venv/bin/pytest tests/evaluations/test_jobs.py tests/evaluations/test_worker_recovery.py tests/reviews/test_workflow.py -q
```

Expected: source persistence, shared enqueue, or source-aware superseding assertions fail.

- [x] **Step 3: Extract transaction-aware enqueue without changing public A5 behavior**

Refactor `EvaluationService.request()` so its existing transaction wrapper calls an internal helper accepting an active `AsyncSession`, requester, reason, and optional `source_report_id`. The helper owns idempotency-key creation, existing-job lookup, job creation, and outbox creation but never commits or dispatches. The public method commits, then invokes existing outbox redrive exactly as before.

Manual retry keys must include the exact source UUID and manual jobs must persist it; initial/provider retries must keep it null.

- [x] **Step 4: Implement atomic review enqueue**

`ReviewService.reevaluate()` validates and locks the current source, calls the transaction-aware enqueue helper, inserts the `REEVALUATE` action containing the job UUID, commits the combined transaction, and only then invokes outbox redrive. If an action already exists for that source, return its linked job after validating ownership instead of inserting duplicates.

- [x] **Step 5: Fence worker success against source races**

In `_persist_success`, after locking the job and submission, lock `job.source_report_id` for manual jobs and require that it belongs to the submission, is not superseded, and is the current maximum report version. Create the next report and supersede only that exact source in the same transaction. Raise the existing recoverable/sanitized job failure path on mismatch; failed and cancelled jobs never alter source state.

- [x] **Step 6: Verify re-evaluation GREEN**

Run:

```bash
cd backend
.venv/bin/pytest tests/evaluations/test_jobs.py tests/evaluations/test_worker_recovery.py tests/evaluations/test_outbox.py tests/reviews/test_workflow.py -q
```

Expected: all A5 regression, lineage, idempotency, dispatch, and worker-race tests pass.

## Task 4: Add the teacher REST adapter and request IDs

**Files:**
- Create: `backend/app/reviews/router.py`
- Modify: `backend/app/reviews/schemas.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/reviews/test_api.py`

- [x] **Step 1: Write failing endpoint tests**

Test these exact routes and statuses:

```text
POST  /api/v1/reports/{report_id}/confirm     -> 200
PATCH /api/v1/reports/{report_id}             -> 200
POST  /api/v1/reports/{report_id}/reevaluate  -> 202
```

For every A6 success and error, assert `response.headers["X-Request-ID"] == response.json()["request_id"]` and the value parses as UUID. Cover malformed UUID/body `422`, body above 16 KiB `413`, missing/non-enumerable report `404`, state conflict `409`, student role `403`, inactive/invalid auth `401`, and strict unknown-field rejection. Assert existing non-A6 error bodies have not gained `request_id`.

Retest `GET /api/v1/submissions/{submission_id}/reports` for descending `(version, id)`, owner/teacher access, outsider indistinguishable `404`, and absence of raw response/audit fields.

- [x] **Step 2: Run API tests and verify RED**

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_api.py -q
```

Expected: routes and request-ID middleware are absent.

- [x] **Step 3: Implement request-ID middleware and A6 exception bodies**

Add an outer ASGI middleware that accepts a syntactically valid incoming `X-Request-ID` or generates `str(uuid4())`, stores it at `scope["state"]["request_id"]`, and attaches the same response header. Extend the existing HTTP/validation/body-limit error path only for `/api/v1/reports/` so A6 errors include `request_id` while old response contracts remain unchanged. Apply a 16 KiB limit to the three review mutation routes.

- [x] **Step 4: Implement and register review routes**

Require `UserRole.TEACHER` through existing auth dependencies, translate typed service errors to `404/403/409`, serialize `EvaluationReportRead`/`EvaluationJobRead` inside strict response envelopes with `request_id`, and include the router under `/api/v1`.

- [x] **Step 5: Verify API, OpenAPI, and installed-wheel routes**

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_api.py tests/evaluations/test_jobs_api.py tests/auth/test_auth_api.py -q
.venv/bin/python -m build
```

Install the wheel into a temporary virtual environment and assert all three review paths appear in `app.openapi()["paths"]`.

Expected: A6 contracts pass; legacy auth/evaluation bodies remain compatible; wheel build and route discovery pass.

## Task 5: Prove concurrency and database rollback behavior

**Files:**
- Create: `backend/tests/reviews/test_concurrency.py`
- Modify: `backend/tests/reviews/test_workflow.py`

- [x] **Step 1: Write concurrent-operation tests**

Using independent async sessions and a barrier, race:

- confirm vs confirm: one action, one confirmed report, both same-teacher calls idempotently succeed;
- confirm vs modify: exactly one transition wins and the loser receives `ReviewConflict`;
- modify vs modify: exactly one new version/action exists;
- reevaluate vs reevaluate: exactly one job/outbox/action exists and both calls identify the same job;
- reevaluate vs confirm/modify: one serializable result, never both a pending retry and a newly mutated source.

After every race assert contiguous report versions, one current non-superseded report, valid action ownership, and no orphan job/outbox rows.

- [x] **Step 2: Run concurrency tests and verify RED or GREEN for the intended reason**

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_concurrency.py -q
```

Expected before final locking fixes: at least one duplicate/conflict assertion fails. If all pass, inspect SQL logging to confirm both transactions overlapped at the barrier rather than accidentally running sequentially.

- [x] **Step 3: Tighten lock ordering and unique-conflict recovery**

Ensure every review path locks submission then source report. Recover expected `(report_id, action)` unique races by rolling back to a savepoint, loading the winner, and returning it only for documented idempotent same-teacher confirm/reevaluate cases; translate all other integrity races to `ReviewConflict`. Do not catch unrelated `IntegrityError` values.

- [x] **Step 4: Verify concurrency and rollback GREEN**

Run:

```bash
cd backend
.venv/bin/pytest tests/reviews/test_concurrency.py tests/reviews/test_workflow.py -q
```

Expected: every race is deterministic and every injected failure leaves no partial review evidence.

## Task 6: Release verification and one atomic A6 commit

**Files:**
- Modify: `docs/superpowers/specs/2026-07-26-a6-teacher-review-design.md` only if implementation discovered a documented contract mismatch.
- Modify: `docs/superpowers/plans/2026-07-26-a6-teacher-review.md` to check completed steps.

- [x] **Step 1: Run formatting, lint, typing, and the full test suite**

Discover project-native commands from `pyproject.toml`/CI, then run the exact configured checks plus:

```bash
cd backend
.venv/bin/pytest -q
```

Expected: zero failures, zero collection warnings introduced by A6, and no migration drift.

- [x] **Step 2: Run clean-database migration checks**

Against each available PostgreSQL 16 and 18 service, run fresh `alembic upgrade head`, empty `downgrade 0003_evaluation_delivery`, and `upgrade head`; then rerun the data-bearing refusal test.

Expected: empty cycles succeed and evidence-bearing downgrade refuses atomically.

- [x] **Step 3: Inspect the final diff for scope and secrets**

Run:

```bash
git status --short
git diff --check
git diff --stat
git diff -- backend/app backend/tests backend/migrations docs/superpowers
```

Expected: only A6/shared-lineage files are changed; there are no credentials, generated build artifacts, unrelated edits, conflict markers, trailing whitespace, or placeholders.

- [x] **Step 4: Create the user-requested single atomic commit**

The parent task explicitly requires one A6 commit, so this overrides the writing-plans default of per-task commits:

```bash
git add backend/app backend/tests backend/migrations docs/superpowers
git commit -m "feat: add audited teacher review workflow"
```

Expected: one commit containing the approved design, implementation plan, migration, production code, and tests; the worktree is clean afterward.
