# A6 Teacher Review Design

## Scope and contracts

A6 implements only teacher review and the smallest shared infrastructure needed by its API. The binding sources are Task 6 in `2026-07-25-agent-mattermost-review.md` and design sections 2.4, 5.5, 8.2, 9.3, and 10.4. A5 remains the sole owner of evaluation-job creation, idempotency, outbox dispatch, execution fencing, and report persistence.

The codebase already has immutable, gap-free `EvaluationReport` versions and a safe owner/teacher report-list endpoint. It does not contain the planned `ReviewAction` model/table. Migration `0004_reviews` therefore introduces that evidence table and the missing persistent manual-retry source relationship without editing released migrations.

## Review states

- Confirm accepts only the current, non-superseded `proposed` report. Repeating the same confirmation by the same teacher returns the existing confirmed report and does not duplicate the action. Another teacher or any historical/modified report receives `409`.
- Modify accepts only the current, non-superseded `proposed` report. It creates a teacher-origin `modified` version, copies every unchanged evaluation field, derives `grade` from the effective score, and changes the source to `superseded` in the same transaction. It never edits immutable report contents in place.
- Re-evaluate accepts the current `proposed`, `modified`, or `confirmed` report. The request creates one idempotent `manual_retry` job plus dispatch outbox and one review action in a single transaction. It does not supersede the source. A successful fenced worker execution locks and revalidates the exact source, creates the new report, and supersedes that source atomically. Failed or cancelled jobs leave the source unchanged.
- A pending re-evaluation freezes its source against confirm or modify. Repeating re-evaluation of the same source returns the existing job and action without duplication.
- Re-evaluation records the source version/status before entering the row-lock sequence. Without an existing idempotent action, the locked row must still match that observation. Sequential confirmed-report re-evaluation remains valid, while an overlapping confirm-versus-re-evaluate request has exactly one winner.
- If the source is no longer current when a manual-retry worker finalizes, the job fails safely and cannot overwrite or supersede a newer version.

## Data model and migration

`EvaluationJob.source_report_id` is nullable for non-manual jobs and mandatory for `manual_retry`. A composite foreign key `(source_report_id, submission_id)` targets the corresponding report. Existing manual jobs are backfilled from the latest same-submission report created no later than the job queue time, and each provable job receives an immutable re-evaluation action with its original source status; upgrade aborts atomically if the source or active-teacher reviewer cannot be proven.

`ReviewAction` contains `report_id`, `teacher_id`, enum `action`, JSONB `changes` bounded at 2 MiB, bounded NUL-free `comment`, and `created_at`. The larger evidence bound accommodates the worst-case before-images of otherwise valid report fields while the HTTP mutation body remains capped at 16 KiB. `(report_id, action)` is unique so confirmation and re-evaluation remain idempotent under concurrency. Re-evaluation evidence records the unchanged source review status as an immutable marker. For modifications, the database computes the complete set of actual editable-field differences between source and result, requires that set to equal the declared change keys exactly, verifies every before/after value, and rejects divergence in copied immutable evidence (`schema_version`, `confidence`, and `validation_status`). The ORM and migration validation SQL are parity-tested. Database checks enforce both `char_length(comment) <= 4000` and `octet_length(comment) <= 8000`.

Three PostgreSQL `DEFERRABLE INITIALLY DEFERRED` row constraint triggers form a bidirectional state-machine boundary over review actions, report versions, and manual jobs. At commit, confirmation must target the current report without a pending retry; modification must link the previous current agent report to the exact current teacher version; and each manual job must have its same-transaction action. Queued/running/failed/cancelled retries preserve the recorded source state. Only a succeeded matching job with audit evidence and the exact new current report may supersede it. The reverse checks reject direct status updates without evidence, orphan actions/jobs/reports, historical confirmation, late evidence insertion, and pending-action conflicts. The frozen trigger SQL is shared by ORM DDL and migration `0004`.

The report-lineage trigger is upgraded to require an active teacher for new teacher-origin reports. The report immutability trigger permits only documented review-status transitions while preserving all other evidence fields.

Downgrade succeeds for an empty A6 schema. If review evidence or source-linked manual jobs exist, downgrade aborts before destructive DDL, leaving revision and evidence intact; a data-bearing test proves this atomic refusal. Fresh and empty down/up paths are tested on PostgreSQL 16 and 18.

## Editable fields and untrusted text

The patch schema accepts optional `completeness`, `correctness`, `major_issues`, `suggestions`, `score`, `limitations`, and `comment`. At least one field is required. Existing strict evaluation schemas validate structured fields. `score` is a strict integer from 0 to 100; clients cannot submit `grade`, which is always derived with `grade_for_score`. `comment` is normalized for line endings, bounded to 4,000 characters and 8,000 UTF-8 bytes in the database, and stored as plain untrusted text. HTML is neither executed nor silently stripped.

Confirm and re-evaluate accept an optional strict body containing only `comment`. Unknown fields are `422`. All review request bodies are limited to 16 KiB and oversized payloads are `413`.

## Service and transaction boundaries

`ReviewService` owns role revalidation, submission-first/report-second row-lock ordering, latest-version checks, status checks, action writes, and report cloning. It uses a transaction-aware A5 job-request helper rather than reproducing idempotency or outbox logic. Dispatch redrive occurs only after the combined transaction commits; broker failure leaves the action, job, and pending outbox durable.

The service raises typed not-found, conflict, and permission errors for Web and later Mattermost adapters. It never commits unrelated state from the caller's session.

## REST API and request IDs

- `POST /api/v1/reports/{id}/confirm` returns `200` with the reviewed report.
- `PATCH /api/v1/reports/{id}` returns `200` with the new teacher report.
- `POST /api/v1/reports/{id}/reevaluate` returns `202` with the idempotent job.
- `GET /api/v1/submissions/{id}/reports` remains owner/teacher only, version-descending with an ID tie-breaker, and never exposes raw model output or audit evidence.

Only active teachers may mutate reviews. Malformed UUID/body is `422`, missing resources are `404`, invalid state/history/races are `409`, wrong role is `403`, and invalid/inactive authentication is `401`. A minimal request-ID middleware generates a fresh random server UUID for every request and never treats a client `X-Request-ID` as canonical. A6 success and error bodies contain the same ID emitted in the response header, including safe `500` responses; existing non-A6 response bodies remain unchanged while receiving the server-generated header.

## Test strategy

Tests cover service and API happy paths, full patch copying, score-to-grade derivation, comment bounds and untrusted HTML preservation, active-role enforcement, non-enumerating authorization, historical/cross-submission conflicts, concurrent confirm/modify/re-evaluate serialization, repeated request idempotency, commit rollback, durable dispatch failure, review-evidence immutability, worker success/failure source behavior, PostgreSQL 16/18 migration fresh/down/up/refusal/autogenerate checks, installed-wheel routes, OpenAPI, request limits, and existing report-list privacy.

`make build-backend` performs the installed-wheel route smoke automatically: it installs the newest built wheel into a temporary target, removes source-path environment overrides, verifies that `app` resolves inside that target, and checks all three review routes in the packaged OpenAPI document.
