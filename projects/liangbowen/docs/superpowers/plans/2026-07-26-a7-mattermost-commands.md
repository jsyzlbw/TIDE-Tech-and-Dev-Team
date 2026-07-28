# A7 Mattermost Slash Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure, role-aware, transactionally idempotent Mattermost Slash Command adapter for help, publish, list, show, submit, summary, and evaluate.

**Architecture:** A strict raw form boundary verifies the Slash token before database access, then maps the authoritative Mattermost user ID to a freshly validated local user. A single PostgreSQL transaction claims a unique integration event, invokes transaction-aware domain services, and saves the exact terminal Mattermost response; concurrent replays wait and return the saved response, while transient failures roll back for retry.

**Tech Stack:** Python 3.12, FastAPI/Starlette ASGI, Pydantic v2, SQLAlchemy async, PostgreSQL 16/18, Alembic, pytest, shlex, hashlib, hmac

---

## File structure

- `backend/app/integrations/mattermost/schemas.py`: bounded request, response, parsed-command, and demo-binding value objects.
- `backend/app/integrations/mattermost/parser.py`: pure `/hw` grammar and Shanghai deadline conversion.
- `backend/app/integrations/mattermost/security.py`: strict form/media-type parsing, constant-time secret checks, and collision-safe request hashing.
- `backend/app/integrations/mattermost/model.py`: identity/event tables and PostgreSQL immutability triggers.
- `backend/app/integrations/mattermost/service.py`: identity resolution, event claim/replay, command dispatch, domain-service translation, and demo binding.
- `backend/app/integrations/mattermost/router.py`: bounded HTTP adapter and conditional demo route factory.
- `backend/migrations/versions/0005_mattermost.py`: schema migration and downgrade refusal.
- `backend/app/assignments/service.py`: transaction-aware create-and-publish primitive used by REST and Mattermost.
- `backend/app/evaluations/service.py`: transaction-aware assignment batch enqueue primitive; existing REST wrapper retains redrive.
- `backend/app/main.py`, `backend/app/core/config.py`, `.env.example`, `docker-compose.yml`: minimal registration, limits, and secrets.
- `backend/tests/mattermost/`: parser, security/API, command, idempotency, persistence, and migration coverage.

## Task 1: Pure command parser

**Files:**
- Create: `backend/tests/mattermost/test_parser.py`
- Create: `backend/app/integrations/__init__.py`
- Create: `backend/app/integrations/mattermost/__init__.py`
- Create: `backend/app/integrations/mattermost/schemas.py`
- Create: `backend/app/integrations/mattermost/parser.py`

- [ ] **Step 1: Write the failing parser contract tests**

Cover the exact grammar and bounds with table-driven tests. The central happy-path assertion is:

```python
command = parse_command(
    'publish --title "最短路径" --due "2026-07-26 18:00" '
    '--question "解释 Dijkstra" --notes "允许伪代码"'
)
assert command.name == "publish"
assert command.arguments["title"] == "最短路径"
assert command.arguments["due_at"] == datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
```

Parametrize missing/duplicate/unknown flags, unexpected positionals, missing values, unclosed quotes,
unknown command, invalid assignment code, invalid date/timezone suffix, NUL, control-only required
text, Unicode quoting, and every maximum plus maximum+1 boundary.

- [ ] **Step 2: Run the parser tests and verify RED**

Run:

```bash
PYTHONPATH=backend .venv/bin/pytest -q backend/tests/mattermost/test_parser.py
```

Expected: collection fails because `app.integrations.mattermost.parser` does not exist.

- [ ] **Step 3: Implement the closed grammar**

Define `MattermostCommand` with `name: Literal[...]`, one positional tuple, and a typed argument
mapping. Implement `parse_command(text)` with `shlex.split(posix=True)`, an explicit command spec,
single-use flags, `HW-[0-9]{4,10}`, visible-text checks, and:

```python
local_due = datetime.strptime(raw_due, "%Y-%m-%d %H:%M").replace(
    tzinfo=ZoneInfo("Asia/Shanghai")
)
due_at = local_due.astimezone(UTC)
```

Raise a bounded `CommandParseError(public_message)` containing one actionable usage example; do not
include long untrusted values in the message.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run the Task 1 command. Expected: all parser matrix cases pass.

## Task 2: Strict form security and configuration

**Files:**
- Create: `backend/tests/mattermost/test_security.py`
- Create: `backend/app/integrations/mattermost/security.py`
- Modify: `backend/app/integrations/mattermost/schemas.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/tests/conftest.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Write failing strict-boundary tests**

Test exact form decoding for all 12 allowed fields, required fields, malformed `%` escapes, invalid
UTF-8, unsupported/malformed charset, wrong media type, unknown/duplicate keys, NUL, byte/character
bounds, and more than 12 fields. Test that missing/duplicate/wrong token all produce the same error
category and call `hmac.compare_digest` without exposing either value.

Test request hashing with framing, not concatenation:

```python
assert request_hash(payload(team_id="ab", channel_id="c")) != request_hash(
    payload(team_id="a", channel_id="bc")
)
assert request_hash(payload(token="one")) == request_hash(payload(token="two"))
assert request_hash(payload(trigger_id="one")) != request_hash(payload(trigger_id="two"))
```

Add settings tests that empty secrets normalize to `None`, NUL is rejected without secret text in
the error, and no Bot Token setting exists.

- [ ] **Step 2: Run Task 2 tests and verify RED**

Run:

```bash
PYTHONPATH=backend .venv/bin/pytest -q backend/tests/mattermost/test_security.py backend/tests/ops/test_runtime_contract.py
```

Expected: Mattermost security symbols/settings are absent.

- [ ] **Step 3: Implement strict security utilities and secrets**

Implement:

```python
def verify_secret(received: str | None, expected: SecretStr | None) -> bool:
    if received is None or expected is None:
        return False
    return hmac.compare_digest(received.encode("utf-8"), expected.get_secret_value().encode("utf-8"))
```

Parse raw bytes only after validating the media type; reject any `%` not followed by two hex digits,
then use `parse_qsl(..., strict_parsing=True, keep_blank_values=True, encoding="utf-8",
errors="strict", max_num_fields=12)`. Treat duplicate token as authentication failure; validate all
other duplicates after successful token verification.

Hash `b"mattermost-command-v1"` plus an unsigned four-byte big-endian length and UTF-8 bytes for
team/channel/user/command/text/trigger in that order.

Add optional `SecretStr` settings with empty-to-None validators and safe NUL checks. Add commented
replacement placeholders to `.env.example` and API-only Compose environment forwarding.

- [ ] **Step 4: Run Task 2 tests and verify GREEN**

Run the Task 2 command and the existing configuration tests. Expected: all pass with no secret in
captured logs/errors.

## Task 3: Identity and event persistence contract

**Files:**
- Create: `backend/tests/mattermost/test_persistence.py`
- Create: `backend/tests/migrations/test_mattermost_migration.py`
- Create: `backend/app/integrations/mattermost/model.py`
- Create: `backend/migrations/versions/0005_mattermost.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: Write failing model/direct-SQL tests**

Assert identity primary/foreign key and unique authority. Direct SQL must reject empty/oversized IDs,
invalid hashes/source/type/status coherence, array/non-object or oversized response/reference JSON,
terminal event updates, request identity changes, delete, and truncate. Assert the only allowed update
is:

```text
processing + NULL response/completed_at
  -> completed|deterministic_error + object response + completed_at
```

- [ ] **Step 2: Run persistence tests and verify RED**

Run:

```bash
PYTHONPATH=backend TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  .venv/bin/pytest -q backend/tests/mattermost/test_persistence.py
```

Expected: models/tables are absent.

- [ ] **Step 3: Implement models and shared PostgreSQL DDL**

Use bounded `String`, `JSONB`, `DateTime(timezone=True)`, named checks, and indexes. Put event trigger
function/trigger SQL constants in `model.py` and reuse those exact constants from migration 0005.
The trigger rejects delete/truncate, freezes terminal evidence, and permits the single terminal
transition. Response is at most 64 KiB and business refs at most 8 KiB by `pg_column_size`.

- [ ] **Step 4: Write and run migration RED/GREEN tests**

Migration tests must cover fresh upgrade, exact head `0005_mattermost`, empty downgrade/up, downgrade
refusal when either table has evidence, data preservation after refusal, and `compare_metadata == []`.
Run all migration tests on PG16 until green.

## Task 4: Transaction-aware domain service primitives

**Files:**
- Modify: `backend/tests/assignments/test_service.py`
- Modify: `backend/tests/evaluations/test_jobs.py`
- Modify: `backend/app/assignments/service.py`
- Modify: `backend/app/evaluations/service.py`

- [ ] **Step 1: Write failing assignment transaction tests**

Test a new `create_published_assignment_in_transaction(session, data, teacher_id, channel_id)` that
flushes but never commits, produces draft-to-published timestamps/status atomically, stores the
channel, validates active teacher/future due date, and disappears on caller rollback. Existing REST
`create_assignment` and transition behavior must remain unchanged.

- [ ] **Step 2: Verify assignment RED, implement, and verify GREEN**

Extract a no-commit creation primitive; make existing `create_assignment` wrap it with its current
commit/rollback semantics. The Mattermost primitive must validate `AssignmentCreate`, acquire the
existing code advisory lock, create, publish, flush, and return without committing.

- [ ] **Step 3: Write failing evaluation transaction tests**

Test `enqueue_assignment_in_transaction(session, assignment_id, reason)` for zero, mixed existing,
and 10,000 latest submissions; assert jobs/outbox are visible before commit, disappear on rollback,
respect `MAX_EVALUATION_BATCH`, and never call dispatch/provider/Agent.

- [ ] **Step 4: Verify evaluation RED, implement, and verify GREEN**

Extract the current body of `request_assignment` into the caller-owned method. Return an
`EvaluationBatch` plus created job IDs required by the wrapper. Keep `request_assignment` as a
transaction wrapper and perform current bounded redrive only after successful commit. Do not alter
idempotency keys, chunk sizes, latest-submission query, or response counts.

## Task 5: Identity resolution, demo binding, and HTTP registration

**Files:**
- Create: `backend/tests/mattermost/test_demo_bindings.py`
- Create: `backend/tests/mattermost/test_api_security.py`
- Create: `backend/app/integrations/mattermost/service.py`
- Create: `backend/app/integrations/mattermost/router.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing route/security ordering tests**

Create app instances for development/test/production. Assert commands are in OpenAPI, demo route is
registered only for safe environments with a non-empty key, and production is 404 and absent from
OpenAPI. Override the session dependency with a function that fails if called; missing/wrong token
must return uniform 401 without invoking it. Test 64 KiB body limit, request ID header, safe ephemeral
shape, and no request/token text in logs.

- [ ] **Step 2: Run tests and verify RED**

Run Mattermost API security/demo tests. Expected: route/service imports fail.

- [ ] **Step 3: Implement conditional router and demo binding**

Build a router factory using the `Settings` captured by `create_app`. The command endpoint reads raw
bytes, verifies token before requesting a session (use a dependency callable invoked after boundary
verification), then applies the 2.5-second service timeout.

Implement demo binding with `compare_digest`, active teacher/student lookup, exact-pair idempotency,
same-pair username refresh, and 409 for either-direction reassignment. Catch unique races by rollback
and re-read; never auto-promote or bind admin.

- [ ] **Step 4: Run tests and verify GREEN**

Run Task 5 tests and existing global request-ID/OpenAPI tests.

## Task 6: Command service and deterministic error cache

**Files:**
- Create: `backend/tests/mattermost/test_commands.py`
- Modify: `backend/app/integrations/mattermost/service.py`
- Modify: `backend/app/integrations/mattermost/router.py`

- [ ] **Step 1: Write failing seven-command tests**

Seed bound teacher/student identities and assert:

- help returns exact usage;
- publish creates one published assignment with Shanghai deadline normalized to UTC and channel ID;
- list/show role filters and never contain rubric, another student's submission, raw answer, or report;
- submit creates contiguous versions with `source=mattermost` and rejects closed/expired work;
- summary exposes aggregate counts only;
- evaluate returns batch/queued/skipped and no job IDs, provider call, or synchronous dispatch.

Parametrize every command across teacher/student roles and invalid/not-found/conflict paths. Assert
authenticated deterministic failures use HTTP 200 ephemeral messages and a terminal error event.

- [ ] **Step 2: Run command tests and verify RED**

Run:

```bash
PYTHONPATH=backend TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  .venv/bin/pytest -q backend/tests/mattermost/test_commands.py
```

Expected: command dispatch is not implemented.

- [ ] **Step 3: Implement event claim and command dispatch**

Inside `session.begin()`, resolve `MattermostIdentity JOIN User`, require active teacher/student, then:

```python
inserted_id = await session.scalar(
    insert(IntegrationEvent)
    .values(..., status="processing")
    .on_conflict_do_nothing(index_elements=[IntegrationEvent.request_hash])
    .returning(IntegrationEvent.id)
)
```

If no ID is returned, load the terminal event and return its response. Otherwise parse, role-check,
invoke the domain service, bound the response, set refs/status/completed timestamp, and flush. Catch
only typed deterministic domain/parser exceptions inside the transaction and save their safe response.
Let timeout, DB, cancellation, configuration, and unexpected exceptions escape so the transaction
rolls back and router returns 503.

- [ ] **Step 4: Run command tests and verify GREEN**

Run Task 6 tests plus assignments/submissions/evaluations focused suites.

## Task 7: Idempotency, concurrency, and failure semantics

**Files:**
- Create: `backend/tests/mattermost/test_idempotency.py`
- Modify: `backend/app/integrations/mattermost/service.py`

- [ ] **Step 1: Write serial and concurrent replay RED tests**

For publish, submit, and evaluate, send the same trigger/hash twice serially and through two independent
PostgreSQL sessions behind a controlled barrier. Assert byte-equivalent JSON response, one terminal
event, and one business effect. Send identical command text with a new trigger ID and assert it is a
new request subject to normal domain rules.

- [ ] **Step 2: Write rollback/commit failure RED tests**

Force a transient exception before completion and a commit failure after business flush. Assert no
event/business row remains and a retry succeeds once. Force a deterministic parse/role/not-found error
and assert its saved replay performs no parser/domain work on the second request.

- [ ] **Step 3: Implement race-safe replay and verify GREEN**

Handle the `ON CONFLICT` loser only after PostgreSQL releases the unique-index wait; require a terminal
row and bounded dict response. Treat a visible `processing` row as corruption rather than executing
again. Preserve exact saved JSON via deep validation/copy.

- [ ] **Step 4: Verify the three-second/slow-Agent contract**

Install an engine/provider whose execution awaits forever and assert `/hw evaluate` completes well
under three seconds because neither engine nor dispatch is invoked. Separately hold a database lock
past the 2.5-second budget and assert safe 503 plus retryable absence of an event.

## Task 8: Release verification, documentation, and review

**Files:**
- Modify: `backend/scripts/verify_wheel_routes.py`
- Modify: `backend/tests/ops/test_runtime_contract.py`
- Modify: `docs/superpowers/specs/2026-07-26-a7-mattermost-commands-design.md` only if review finds ambiguity

- [ ] **Step 1: Add packaging/static RED tests and implement route verification**

Require the installed wheel OpenAPI to include the command route and safe-environment demo route,
while a production settings subprocess omits demo. Require `.env.example` placeholders and API
Compose forwarding with no Bot Token key.

- [ ] **Step 2: Run focused PG16 verification**

Run Mattermost, assignment, submission, evaluation, migration, config, request-ID, and runtime suites.
Expected: zero failures.

- [ ] **Step 3: Run full release verification**

Run:

```bash
PYTHONPATH=backend TEST_DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/pytest -q backend/tests
.venv/bin/ruff check backend/app backend/migrations backend/scripts backend/tests
.venv/bin/ruff format --check backend/app backend/migrations backend/scripts backend/tests
git diff --check
make build-backend
```

Run all migration/history tests against a fresh PostgreSQL 18 cluster in addition to PG16. Inspect
Compose statically and report Docker runtime unavailability without claiming an image build.

- [ ] **Step 4: Commit implementation**

```bash
git add backend .env.example docker-compose.yml
git commit -m "feat: add Mattermost slash commands"
```

- [ ] **Step 5: Run independent review loops**

Ask one reviewer to check every A7 spec clause and one quality reviewer to inspect security,
transactionality, migrations, concurrency, and tests. Fix every Critical/Important/Major finding with
a new RED→GREEN cycle, amend the implementation commit, repeat release checks, and re-review until
both return Ready Yes.
