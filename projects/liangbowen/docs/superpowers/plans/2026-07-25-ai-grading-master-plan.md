# Mattermost AI Grading System Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the complete course acceptance project: a Mattermost-connected AI Agent system that supports assignment publication, versioned student submission, aggregation, structured evaluation, teacher review, repeatable testing, and a polished local demonstration.

**Architecture:** One FastAPI application is the source of truth for domain behavior and exposes both REST and Mattermost adapters. PostgreSQL stores versioned domain records, Redis/Celery runs evaluations, React provides role-focused screens, and provider adapters isolate deterministic Mock evaluation from a real OpenAI-compatible endpoint. Docker Compose and a verification manifest make the whole system reproducible.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL 16, Redis 7, Celery, React, TypeScript, Vite, Mattermost Team Edition, Docker Compose, pytest, Vitest, Playwright

---

## Source of truth

- Approved design: `docs/superpowers/specs/2026-07-25-mattermost-ai-grading-design.md`
- Phase 1: `docs/superpowers/plans/2026-07-25-foundation-domain-api.md`
- Phase 2: `docs/superpowers/plans/2026-07-25-agent-mattermost-review.md`
- Phase 3: `docs/superpowers/plans/2026-07-25-web-console.md`
- Phase 4: `docs/superpowers/plans/2026-07-25-verification-delivery.md`

When this master plan and a phase plan disagree on an implementation detail, the approved design controls product behavior, and the phase plan controls file-by-file execution. Resolve the disagreement in both documents before writing code.

## Non-negotiable acceptance map

| Course requirement | Owning phase | Completion evidence |
|---|---|---|
| Teacher publishes title, prompt, deadline, optional notes | Phase 1 + Phase 3 | Assignment API tests and teacher publish UI test |
| Student submits through web or Mattermost | Phase 1 + Phase 2 + Phase 3 | Versioned submission tests and adapter/browser tests |
| System aggregates by assignment | Phase 1 + Phase 3 | Summary service and assignment detail screen |
| Agent creates structured reports for every submission | Phase 2 | Validated report schema, job state, provider tests |
| Teacher confirms, modifies, or re-evaluates | Phase 2 + Phase 3 | Immutable report history and review UI tests |
| Two role paths are clearly distinguished | Phase 1 + Phase 3 | JWT authorization and role route tests |
| Mattermost Webhook/Bot Token connection is explained | Phase 2 + Phase 4 | Adapter code and `docs/mattermost-setup.md` |
| Three report examples are visible | Phase 4 | Seeded 94/A, 72/C, and 35/D report records |
| Six required test scenarios are covered | Phase 4 | Acceptance pytest suite and manifest |
| Capability boundaries are honest | Phase 2 + Phase 4 | Report limitations and `docs/limitations.md` |
| Another person can run the project | Phase 4 | Clean-room `make demo && make verify` |

## Cross-phase invariants

- IDs are UUIDs in Python/SQL, serialized as UUID strings in JSON, and represented as `string` in TypeScript.
- All timestamps are timezone-aware UTC ISO 8601 values at the API boundary; the UI localizes only for display.
- Roles are exactly `teacher`, `student`, and `admin`.
- Assignment status transitions are `draft → published → closed → archived`; no reverse transition is implicit.
- Each submission creates an immutable numbered version; exactly one version per assignment/student is current.
- Each evaluation job targets one submission version and has an idempotency key.
- Each report is immutable. Confirming changes status; modifying creates a teacher-origin version; re-evaluation creates a new Agent-origin version.
- Provider output is untrusted until Pydantic and semantic validation succeed.
- Provider repair budget is two repairs after the initial attempt; exhausted jobs are failed and audited.
- The backend calculates the grade from score ranges: A 90–100, B 75–89, C 60–74, D 0–59.
- Every AI report says it requires teacher review; only an explicit teacher action confirms it.
- Mattermost and React call the same services and cannot bypass authorization or validation.
- Mock mode is deterministic demo infrastructure, not a claim about model intelligence.

## Task 1: Execute Phase 1 and freeze the domain API

**Files:**
- Follow: `docs/superpowers/plans/2026-07-25-foundation-domain-api.md`
- Produce: `backend/`, `docker-compose.yml`, `.env.example`, `Makefile`

- [ ] **Step 1: Execute every Phase 1 checkbox in order**

Use test-first steps exactly as written. Do not begin provider, Mattermost, or frontend work while migrations, seed data, or domain authorization tests are failing.

- [ ] **Step 2: Run the Phase 1 gate**

```bash
.venv/bin/ruff check backend/app backend/tests
.venv/bin/pytest backend/tests/auth backend/tests/assignments backend/tests/submissions backend/tests/summaries -q
docker compose config --quiet
docker compose up -d --wait postgres test-postgres redis api
curl --fail http://localhost:8000/health/ready
docker compose down
```

Expected: lint exits `0`, all domain tests pass, Compose validates, and readiness returns HTTP 200.

- [ ] **Step 3: Freeze and record the API contract**

```bash
.venv/bin/python -c 'from app.main import app; import json; print(json.dumps(app.openapi(), sort_keys=True))' > /tmp/ai-grading-openapi.json
git status --short
```

Expected: OpenAPI generation exits `0`; all Phase 1 changes are committed before Phase 2 begins.

## Task 2: Execute Phase 2 and freeze evaluation/review behavior

**Files:**
- Follow: `docs/superpowers/plans/2026-07-25-agent-mattermost-review.md`
- Produce: `backend/app/evaluations/`, `backend/app/reports/`, `backend/app/integrations/mattermost/`

- [ ] **Step 1: Execute every Phase 2 checkbox in order**

Use the deterministic Mock provider for all automated tests. Configure a real provider only after schema validation, repair, idempotency, and audit tests pass.

- [ ] **Step 2: Run the Phase 2 gate**

```bash
.venv/bin/pytest backend/tests/evaluations backend/tests/reports backend/tests/integrations -q
.venv/bin/ruff check backend/app/evaluations backend/app/reports backend/app/integrations
```

Expected: provider, validation, retry, version-history, teacher-action, command-parser, token-verification, and interactive-action tests all pass.

- [ ] **Step 3: Prove sensitive boundaries**

Verify with focused tests that a student cannot start evaluations or review reports, raw provider output is not exposed in safe errors, a duplicate job request returns the existing active job, and a Mattermost token mismatch returns HTTP 401/403.

## Task 3: Execute Phase 3 and freeze the role-focused UI

**Files:**
- Follow: `docs/superpowers/plans/2026-07-25-web-console.md`
- Produce: `frontend/`

- [ ] **Step 1: Execute every Phase 3 checkbox in order**

Use accessible labels and stable `data-testid` only for values that have no semantic role, such as the generated assignment code. Keep the API DTO names aligned with OpenAPI and do not invent frontend-only server states.

- [ ] **Step 2: Run the Phase 3 gate**

```bash
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: all component/route tests pass, lint exits `0`, TypeScript has no errors, and Vite emits `frontend/dist/`.

- [ ] **Step 3: Inspect responsive states**

At 390×844, 768×1024, and 1440×900, verify login, teacher dashboard, assignment summary, report review, student assignment, loading, empty, validation-error, and provider-failure states. Keyboard navigation must reach every action and a visible focus indicator must remain present.

## Task 4: Execute Phase 4 and produce acceptance evidence

**Files:**
- Follow: `docs/superpowers/plans/2026-07-25-verification-delivery.md`
- Produce: packaged Compose runtime, E2E tests, scripts, README, acceptance docs, evidence manifest

- [ ] **Step 1: Execute every Phase 4 checkbox in order**

Start with deterministic Mock mode. Run the real provider as an additional demonstration only; its availability must not determine whether acceptance tests pass.

- [ ] **Step 2: Run the complete gate from a clean runtime**

```bash
docker compose --profile mattermost down --volumes
make demo
make verify
make demo-full
```

Expected: the default stack starts, every manifest check passes, and the full profile adds a configured Mattermost workspace without manual database edits.

- [ ] **Step 3: Audit the final requirement map**

Read the approved design’s “验收要求” and “测试与能力边界” sections line by line. For every row in the acceptance map above, open its recorded test or documentation evidence. A missing row blocks submission even when all existing tests pass.

- [ ] **Step 4: Rehearse and submit**

Use `docs/demo-script.md`, keep the walkthrough within 20 minutes, then follow `docs/submission-checklist.md` for the administrator-issued repository path and progress tracker update.

## Final completion gate

- [ ] All four phase plans have every checkbox resolved.
- [ ] `make verify` exits `0` from a clean runtime.
- [ ] `artifacts/acceptance/manifest.json` records all eight checks as passed.
- [ ] The six required scenarios appear in automated test names and `docs/test-results.md`.
- [ ] The seeded demo shows 94/A, 72/C, and 35/D reports from three different student submissions.
- [ ] Teacher confirm, modify, and re-evaluate each preserve immutable history.
- [ ] Mattermost slash commands and interactive actions are both demonstrated.
- [ ] README startup commands were tested by a second clean environment or disposable worktree.
- [ ] No secret, token, student password beyond documented demo credentials, or raw model output is committed.
- [ ] The 15–20 minute rehearsal completed without relying on live provider availability.
