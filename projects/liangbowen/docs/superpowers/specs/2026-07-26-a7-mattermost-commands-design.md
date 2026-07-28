# A7 Mattermost Slash Commands and Identity Mapping Design

## Scope

A7 adds the synchronous Mattermost Slash Command adapter only. It covers request verification,
identity binding, command parsing, the seven required commands, durable idempotency, and a
development/test bootstrap route. Bot API calls, interactive message actions, notifications, and
Bot Tokens remain A8 work.

The adapter is deliberately thin: it invokes the existing assignment, submission, summary, and
evaluation services. It does not bypass their role, deadline, versioning, batch-size, or outbox
contracts and never waits for an Agent result.

## Considered approaches

### Chosen: one transaction for event claim, business operation, and saved response

The command service inserts a unique `IntegrationEvent` in `processing` state, executes the domain
operation through transaction-aware service entry points, then stores the terminal response in the
same transaction. A concurrent identical insert waits for the first transaction and then returns
the committed saved response. A transient failure rolls back the event and business operation, so a
Mattermost retry can safely try again.

This is the only approach that provides atomic side effects and an exact replay response without a
recovery lease or distributed transaction.

### Rejected: commit the event claim before the business operation

This prevents concurrent execution but creates a crash window between the claim and the business
commit. Recovering an abandoned `processing` event would require leases, fencing tokens, and an
uncertain-side-effect reconciliation protocol that is unnecessary for A7.

### Rejected: rely only on domain idempotency keys

Submission and assignment publication need adapter-level idempotency, and evaluation batch IDs are
response data rather than stable domain keys. Domain idempotency alone cannot reproduce the exact
Mattermost response or cache deterministic errors.

## HTTP boundary

`POST /api/v1/integrations/mattermost/commands` accepts only
`application/x-www-form-urlencoded`, optionally with `charset=utf-8`. The ASGI body limiter rejects
more than 64 KiB before endpoint parsing. The decoder rejects malformed percent escapes, invalid
UTF-8, unsupported charset parameters, unknown fields, duplicate fields, NUL bytes, excess field
count, and field-specific byte/character limits.

The accepted standard fields are:

- `token`, `team_id`, `team_domain`, `channel_id`, `channel_name`
- `user_id`, `user_name`, `command`, `text`
- `trigger_id`, `response_url`

`token`, `team_id`, `channel_id`, `user_id`, `command`, and `trigger_id` are required. Empty display
fields are allowed within their bounds. `command` must be exactly `/hw`.

Authentication ordering is fixed:

1. enforce media type and body size;
2. decode only enough strict form structure to locate one token;
3. compare the configured Slash Command token with `hmac.compare_digest`;
4. validate all other request fields;
5. compute the request hash;
6. query identity and business data;
7. parse and execute the command.

A missing, duplicated, empty, or incorrect token returns the same HTTP 401 response and never
queries the database. Neither logs nor database rows contain the token, request body, answer text,
or expected secret.

The endpoint uses a 2.5 second internal timeout, leaving headroom under Mattermost's three-second
deadline. Timeout, database outage, provider configuration failure, and commit failure return a
safe HTTP 503 ephemeral response and leave no terminal event, so Mattermost may retry. Auth and
transport failures use 4xx. Authenticated parser, role, not-found, deadline, and state errors are
deterministic: they return HTTP 200 ephemeral responses and are saved for exact replay, preventing
Mattermost from retrying a user-correctable command.

Every response is an object containing only bounded Mattermost fields:

```json
{"response_type": "ephemeral", "text": "..."}
```

Successful publication may use `in_channel`; all other A7 responses are `ephemeral`. The global
`X-Request-ID` middleware remains authoritative and no inbound request ID is trusted.

## Parser grammar

The parser calls `shlex.split(text, posix=True)` and never invokes a shell. Command names and flags
come from closed allowlists. Unknown commands, unknown flags, duplicate flags, extra positionals,
missing values, unclosed quotes, NUL bytes, and arguments beyond their command-specific bounds are
rejected with an actionable example.

The exact grammar is:

```text
help
publish --title TITLE --due "YYYY-MM-DD HH:MM" --question QUESTION [--notes NOTES]
list
show ASSIGNMENT_CODE
submit ASSIGNMENT_CODE --text ANSWER
summary ASSIGNMENT_CODE
evaluate ASSIGNMENT_CODE
```

Flags may appear in any order. Quoted Unicode text is preserved exactly after `shlex` removes the
quote delimiters. `publish` requires title, due, and question exactly once. `submit` requires one
assignment code and one `--text`. Other commands accept no flags. Assignment codes must match
`HW-[0-9]{4,10}`. The deadline format is interpreted explicitly in `Asia/Shanghai`, then normalized
to UTC. Timezone suffixes and ambiguous alternative formats are rejected.

Limits align with domain schemas: title 200 characters, question 50,000, notes 10,000, answer
50,000, command text 60 KiB, and assignment code 13 characters. Validation also rejects Unicode
control-only required text and embedded NUL.

## Identity model and bootstrap policy

`MattermostIdentity` has `user_id` as both primary key and foreign key to `users.id`, a unique
bounded `mattermost_user_id`, a bounded display-only `mattermost_username`, and `bound_at`.
Authentication always looks up by `mattermost_user_id`; the incoming username never selects or
authorizes a local account. Every command joins the local user row and re-checks `is_active` and the
current database role.

Unknown, inactive, or structurally conflicting identities receive the same HTTP 401 ephemeral
binding guidance. No first-use auto-binding or role promotion exists.

`POST /api/v1/integrations/mattermost/demo-bindings` accepts JSON:

```json
{
  "local_username": "teacher",
  "mattermost_user_id": "mattermost-id",
  "mattermost_username": "teacher-mm"
}
```

The route is registered only when `APP_ENV` is `development` or `test` and a non-empty demo setup
key is configured. Therefore it is absent from production routing and OpenAPI. The
`X-Demo-Setup-Key` uses `compare_digest`; missing/incorrect keys return a uniform 401.

Binding is intentionally non-reassignable. An exact local-user/Mattermost-ID repeat is idempotent
and may refresh the display username. A local user already bound to another Mattermost ID or a
Mattermost ID bound to another local user returns 409 with no mutation. Unknown, inactive, admin,
or otherwise unsupported local accounts are rejected. Empty/default keys fail closed.

## Integration event and request hashing

`IntegrationEvent` contains:

- UUID primary key and unique 64-character lowercase SHA-256 `request_hash`;
- fixed `source=mattermost` and `event_type=slash_command`;
- status `processing`, `completed`, or `deterministic_error`;
- `actor_user_id` foreign key;
- bounded JSON object `response` and JSON object `business_refs`;
- `arrived_at` and terminal `completed_at` timestamps.

The hash excludes the secret and uses a versioned, length-prefixed UTF-8 encoding of `team_id`,
`channel_id`, authoritative `user_id`, exact `/hw` command, exact text, and required `trigger_id`.
Length prefixes prevent concatenation collisions, and `trigger_id` distinguishes separate invocations
of otherwise identical commands.

Database checks enforce hash shape, fixed source/type values, response/business-reference JSON
shapes and byte bounds, and status/timestamp/response coherence. A trigger freezes request identity
and arrival data, permits only `processing -> completed|deterministic_error`, makes terminal events
immutable, and rejects delete/truncate. No token or raw form payload is persisted.

The claim uses PostgreSQL `INSERT ... ON CONFLICT DO NOTHING RETURNING`. A loser of a concurrent
race waits for the winner's transaction and then reads its terminal response. A terminal replay
returns the exact saved JSON without parsing the command or invoking a domain service. A transient
exception rolls the entire transaction back, including the claim.

Migration `0005_mattermost` has no legacy backfill because both tables are new. Downgrade refuses
while either table contains evidence, then drops triggers, tables, functions, and constraints in a
dependency-safe order.

## Command-to-domain mapping

### `help`

Returns the seven-command syntax and role notes. It performs no business mutation.

### `publish` — active teacher only

Creates and publishes one assignment through a transaction-aware assignment service entry point.
It stores the current Mattermost channel ID, uses an empty rubric because the Slash grammar has no
rubric field, validates a future deadline, and returns the generated code/title/deadline. Creation,
publication, event response, and event reference commit atomically.

### `list` — active teacher or student

Teachers see up to 20 assignments of all states; students see only published/closed assignments.
Both receive code, title, status, and deadline only. No rubric, question, notes, reference material,
submission, or report data appears.

### `show` — active teacher or student

Lookup is by assignment code. Students may see only published/closed assignments. The response is
limited to code, title, status, deadline, question, and notes. Rubric/reference material and all
submission/report data are omitted for both roles.

### `submit` — active student only

Resolves a student-visible assignment and calls `create_submission` with text content and
`SubmissionSource.MATTERMOST`. Existing row locking supplies contiguous per-student versions and
the authoritative deadline/state check. The submission and completed event commit atomically.

### `summary` — active teacher only

Calls `build_assignment_summary` and returns aggregate counts only: total, submitted, missing,
queued/evaluating, pending review, reviewed, and failed. It never renders student names or answers.

### `evaluate` — active teacher only

Calls a transaction-aware evaluation batch enqueue method using the existing latest-submission,
10,000-subject bound, idempotency keys, job creation, and outbox logic. It returns `batch_id`,
`queued`, and `skipped`; it omits individual job IDs and does not redrive dispatch or call an Agent
inside the request. The existing outbox redriver dispatches after commit.

Role mismatches return deterministic ephemeral guidance. The integration service performs the role
check from the freshly loaded database user before command dispatch.

## Service boundary changes

The adapter never writes assignment, submission, or evaluation rows directly. Minimal domain
changes expose transaction-aware methods:

- assignment creation/publication without an internal commit;
- evaluation batch enqueue without an internal transaction or immediate redrive.

Existing REST entry points retain their current commit and redrive behavior by wrapping these new
primitives. `create_submission` and `build_assignment_summary` already support caller-owned
transactions and require no semantic change.

## Configuration

Settings add only:

- `mattermost_command_token: SecretStr | None`
- `mattermost_demo_setup_key: SecretStr | None`

Empty strings normalize to `None`; NUL-containing secrets are rejected without echoing values.
No Bot Token, Mattermost base URL, or interactive-action secret is introduced in A7. `.env.example`
contains commented replacement placeholders rather than working secrets. Compose injects the two
values into the API service only because workers do not receive Slash Commands or demo requests.

## Test strategy

Tests are written and observed failing before production code for each layer:

1. parser matrix: all grammar, quoting, Unicode, duplicate/unknown flags, positionals, bounds, NUL,
   date format, and Shanghai-to-UTC conversion;
2. strict HTTP/form security: body, media type, charset, percent decoding, duplicate keys, token
   uniformity, `compare_digest`, security-before-database, safe logs, and request IDs;
3. identity and demo binding: environment route registration, secret gate, active role revalidation,
   exact repeat, username refresh, and all conflicts;
4. command behavior: seven happy paths, invalid/auth/role cases, field minimization, Mattermost
   submission source, deadline/version semantics, aggregate summary, and 10,000 batch reuse;
5. idempotency: serial replay, two-connection concurrent replay, deterministic-error caching,
   transient rollback, and commit failure with no duplicate side effect;
6. timing: a deliberately slow Agent/provider is never invoked and enqueue response remains within
   the synchronous budget;
7. persistence/migration: direct constraints, immutable state machine, delete/truncate refusal,
   fresh upgrade, downgrade refusal with evidence, empty down/up, autogenerate parity, PG16 and
   PG18 migration history;
8. packaging/static: OpenAPI route presence/production absence, installed wheel routes, config and
   Compose contracts, Ruff, formatting, and diff checks.

Full backend regression runs on PostgreSQL 16. Docker image execution is reported separately if the
local Docker CLI is unavailable.

## Acceptance criteria

A7 is complete when every required Slash command returns a bounded Mattermost-compatible response,
token and identity checks precede parsing/business access, duplicate requests have exactly one
business effect and exact replay responses, transient failures remain retryable, no command waits
for Agent execution, production exposes no demo bootstrap route, migration/metadata are identical,
and the full release verification plus independent spec/quality review has no Critical, Important,
or Major findings.
