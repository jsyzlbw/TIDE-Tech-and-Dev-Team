# A8 Mattermost Bot Delivery and Interactive Review Design

## Scope

A8 completes the Mattermost side of the grading workflow. It sends every successful Agent report to
the responsible teacher as a direct-message report card, accepts the three interactive review
actions, and records durable success or final-failure evidence. It reuses A5 evaluation outbox
leases, A6 review semantics, and A7 Mattermost identity/event persistence. It does not add a new
permission system, expose answer/reference material in notifications, or make the evaluation job
depend on Mattermost availability.

The complete path is:

```text
successful EvaluationReport -> notification outbox -> worker -> Mattermost DM/post
                                                        |
                                                        v
teacher button -> signed callback -> current-report check -> A6 review mutation
```

## Considered approaches

### Chosen: extend the existing outbox and integration-event evidence

Migration `0006_mattermost_delivery` adds terminal-delivery fields to `evaluation_outbox` and widens
the closed `integration_events.event_type` set. A notification outbox row remains the only delivery
lease. The worker performs the external request outside a database transaction, then uses a short
compare-and-swap transaction to finalize the lease and insert immutable terminal evidence.

This preserves A5's concurrency design and keeps one source of truth for retry scheduling. Dispatch
rows retain their existing 1,000-attempt behavior; only notification rows have the three-attempt
policy.

### Rejected: encode delivery state in existing nullable fields

Overloading `delivered_at` and `last_error_type` cannot distinguish pending from terminal failure or
store the Mattermost post ID with database-enforced coherence. It would make operational and
acceptance evidence ambiguous.

### Rejected: add a second Mattermost delivery table

A dedicated table could express the state well, but would duplicate the existing outbox lease,
retry, and claim fencing machinery and introduce cross-table recovery rules without improving the
required behavior.

## Outbound client and trust boundary

`MattermostClient` owns one reusable `httpx.AsyncClient`. It creates a direct channel with
`POST /api/v4/channels/direct` using an exact two-user-ID array, then posts the card with
`POST /api/v4/posts`. Requests use `Authorization: Bearer <bot token>`, JSON content, explicit
connect/read/write/pool timeouts, default TLS verification, and no redirects.

Configuration validates the Mattermost base URL before the first request: only absolute `http` or
`https`, no userinfo, query, or fragment, and HTTPS is mandatory outside development/test. Paths are
constructed from the validated origin rather than accepting arbitrary endpoints. Every URL uses a
shared 2,048 UTF-8-byte limit rather than a code-point limit. User IDs use the bounded Mattermost
identifier grammar. The client never logs or raises the token, URL, request body, response body,
teacher identity, or report content.

Responses are streamed into a small bounded buffer. A successful direct-channel response must be a
bounded JSON object with a valid channel `id`; a successful post response must contain a valid post
`id`. Network errors, HTTP 408, 429, and 5xx are retryable. Other 4xx responses and malformed success
responses are permanent. `Retry-After` accepts only bounded integer seconds and is clamped to
1–300 seconds. All public exceptions expose only a fixed safe error type and retry classification.
Client close errors are logged by class name and never mask a primary operation error.

## Deterministic report card

The renderer is a pure function of assignment, submission, report, action URL, console URL, and the
action secret. It emits one Mattermost attachment containing a concise Chinese report summary and
exactly three actions:

- `confirm` — confirm the current Agent report;
- `reevaluate` — enqueue an A6 re-evaluation;
- `openreport` — return the safe web-console link.

Action IDs contain only ASCII letters and numbers, matching Mattermost's documented action-ID
restriction. Each v2 action context contains exactly six fields: `action`, `report_id`,
`delivery_id`, `expected_user_id`, `expected_channel_id`, and `signature`. The signature is
HMAC-SHA256 over the v2 domain and a length-framed UTF-8 encoding of the first five fields, including
the canonical report/delivery UUIDs and the expected recipient/channel IDs. The callback verifies it
with `hmac.compare_digest` before any database access, then requires the callback `user_id` and
`channel_id` to match the signed expectations.

The message labels the result as `AI 基础评估 · 待教师审核` and shows score/grade, completeness,
major issues, suggestions, and limitations. It never renders the submission answer, rubric,
reference material, provider prompts, or secrets. Markdown metacharacters and link destinations are
escaped. The report URL is resolved only under the configured web-console origin; untrusted values
cannot select a host or scheme.

Every text field and the final message/props JSON have conservative limits below Mattermost's post
limit. Truncation is deterministic and does not split combining-mark, variation-selector,
emoji-modifier, regional-indicator, or zero-width-joiner sequences. If optional detail cannot fit,
the renderer removes whole sections in a fixed order and retains the review label and all three
actions. Settings validation reserves 24 KiB of the 32 KiB props budget for fixed/non-URL card
content and bounds the JSON-escaped cost of three repeated action URLs plus one console URL to the
remaining 8 KiB, so an accepted URL combination can always fit the required card.

The post includes a stable correlation property derived from the outbox UUID. Delivery is
recipient-only: the direct channel is created from exactly `[bot_user_id, recipient_user_id]`, and
the signed recipient/channel pair is bound back to that delivery. This supports operations and
duplicate diagnosis. Mattermost does not provide a documented exactly-once post contract for this
flow, so the delivery guarantee is explicitly at-least-once across the external send/SQL-ack crash
window. No claim of reliable `pending_post_id` deduplication is made.

## Notification state, retries, and evidence

The A5 notification outbox is redefined as an actual Mattermost delivery record, not a record that a
Celery task was published. `deliver_evaluation_notification(job_id)` asks the redriver to claim that
job's notification row and performs the HTTP delivery. The periodic redriver directly performs due
notification sends as well; it does not publish a task and prematurely mark the row delivered.
Dispatch rows and callbacks remain unchanged.

For notification rows:

- attempt count starts at zero and increments once per completed failed HTTP attempt;
- retryable failures schedule a non-blocking retry using bounded `Retry-After` or existing exponential
  delay;
- permanent failures become terminal immediately;
- retryable failures become terminal after the third failed attempt;
- terminal success stores `delivered_at`, the Mattermost post ID in `delivery_ref`, and no error;
- terminal failure stores `failed_at`, a fixed safe error type and bounded safe summary, and no post ID;
- delivered or terminal-failed rows can never be claimed again.

The database constraints distinguish dispatch and notification semantics so migration 0006 cannot
weaken or cap A5 dispatch retries. A successful or final-failed notification inserts an immutable
`IntegrationEvent` in the same short transaction that finalizes the claimed outbox row. Event types
are `notification_delivered` and `notification_failed`; their deterministic hash is derived from the
outbox UUID and terminal outcome, and business references contain only outbox/job/report/post IDs.
Failures never alter the succeeded evaluation job or its report.

The worker commits the claim, sends the HTTP request, then finalizes by claim token. A crash before
the send causes a later retry. A crash after Mattermost accepted the post but before SQL finalize can
produce a duplicate after lease expiry; the stable outbox correlation property makes this visible.
The documentation and tests therefore assert at-least-once, not exactly-once, delivery.

## Interactive callback boundary

`POST /api/v1/integrations/mattermost/actions` is always registered, independently of the A7 slash
token. The ASGI middleware enforces a 16 KiB streaming body limit before buffering. The endpoint
accepts only `application/json` with optional UTF-8 charset and rejects malformed UTF-8, duplicate
keys, NaN/infinity, trailing data, unknown fields, NUL, invalid IDs, oversized strings, and invalid
context shape.

Boundary ordering is fixed:

1. enforce media type and streaming byte bound;
2. parse strict bounded JSON;
3. validate the closed action/context shape;
4. verify the HMAC signature in constant time;
5. acquire a database session and resolve the fresh authoritative identity;
6. return HTTP 200 authorization JSON for a bound active non-teacher, while an unknown, unbound, or
   inactive identity remains HTTP 401;
7. lock and require the delivered notification row identified by `delivery_id` to match the signed
   `report_id`, callback `post_id`, recipient actor, and a non-failed delivered state;
8. require the report to be the current non-superseded report;
9. claim/replay the action `IntegrationEvent`;
10. execute the A6 transaction-aware operation and persist the terminal response in one transaction.

The action request hash uses a versioned, length-framed encoding of exactly the authoritative
`user_id`, `post_id`, `channel_id`, `team_id`, `action`, canonical `report_id`, and canonical
`delivery_id`. It excludes the HMAC and redundant signed expectation fields. A concurrent identical
callback waits for the winning transaction and returns its exact saved JSON. Transient failures roll
back both the event and business change so Mattermost can retry.

Malformed/authentication failures return safe 4xx JSON. Authenticated business outcomes—including
already-final state, superseded report, and role errors—return HTTP 200 with valid bounded JSON.
Successful responses use Mattermost's documented `update` and/or `ephemeral_text` fields. A
deterministic action error includes both `error.message` and `ephemeral_text` so modern clients show
the structured error while older behavior still gives feedback. Unexpected/transient failures use
a valid safe JSON 5xx response.

## Action semantics and atomicity

`confirm` calls a new caller-owned A6 confirmation primitive. It writes the `ReviewAction` and the
completed integration event in one transaction, then updates the original card to a reviewed state
with no remaining actions. Exact same-state confirmation remains idempotent.

`reevaluate` calls a new caller-owned A6 re-evaluation primitive. It writes the review action,
evaluation job, dispatch outbox row, and completed integration event in one transaction. It never
calls Redis/Celery or an Agent inside the HTTP callback; the existing periodic outbox redriver
dispatches after commit. The card is updated to show the queued re-evaluation and removes actions.

`openreport` performs no review mutation. It still locks/validates the current report and records the
completed integration event, then returns an ephemeral link under the configured console origin.

Existing A6 REST methods retain their current behavior by wrapping the new transaction-aware
primitives in their existing transaction and post-commit redrive boundaries.

## Schema and immutability

Migration `0006_mattermost_delivery`:

- leaves the published 0005 file unchanged, takes the exclusive side of the stable versioned
  application/migration advisory gate, and only then takes one ordered `ACCESS EXCLUSIVE` lock over
  `evaluation_outbox`, `integration_events`, and `mattermost_identities` (the same order as
  downgrade). Every production ORM root transaction takes the shared side as its first SQL, while
  nested SAVEPOINTs reuse that root lock. Existing transactions can finish before migration DDL and
  new transactions wait at the gate instead of holding business-table locks. The gate adds one
  `SELECT` per SQL-producing root transaction and is released automatically on commit or rollback;
  deployments should still reserve a maintenance window for long-running transactions;
- closes the one-time pre-gate binary gap with a fail-closed bootstrap contract. Any upgrade path
  entering 0006 requires exact `MIGRATION_LEGACY_PROCESSES_STOPPED=true`, then performs a count-only
  `pg_stat_activity` check scoped to the current database, excluding its own PID and including every
  other `client backend` regardless of state. A missing confirmation or any old idle/active session
  aborts before the exclusive advisory gate, business-table locks, or DDL. Operators must stop API,
  worker, beat, shells, and pools before confirming; neither Make nor Compose kills them. Offline SQL
  generation requires the same confirmation and emits the prerequisite as a comment;
- performs a count-only preflight of legacy user IDs before any DDL; invalid legacy data aborts the
  transaction without echoing identity values, while valid data is upgraded to the shared strict
  identifier constraint;
- adds `failed_at`, `delivery_ref`, and bounded `last_error_summary` to `evaluation_outbox`;
- replaces outbox checks/triggers so notification terminal states and three-attempt bounds are
  enforced without changing dispatch behavior;
- widens the integration event type allowlist to `slash_command`, `interactive_action`,
  `notification_delivered`, and `notification_failed`;
- preserves the A7 event transition/immutability/delete/truncate rules.

Legacy A5 notification rows remain pending unless already delivered; existing delivered rows cannot
have a post ID reconstructed and are explicitly represented as legacy delivery evidence. New
notification successes require a post ID. Migration tests cover this compatibility path. Downgrade
refuses while any A8-only evidence exists, restores the exact A5/A7 constraints, and preserves
legacy data.

## Configuration and least privilege

Settings add optional, fail-closed values:

- `mattermost_url`
- `mattermost_bot_token` (`SecretStr`)
- `mattermost_bot_user_id`
- `mattermost_action_secret` (`SecretStr`)
- `mattermost_action_url`
- `web_console_url`

Empty values normalize to `None`; NUL and invalid URLs fail without echoing values. Slash commands
continue to work when Bot settings are absent. The API receives the action secret and console URL,
but not the Bot token. The worker receives outbound URL, Bot token/user ID, action secret/action URL,
and console URL. Beat only schedules maintenance and receives no Bot token. This is the narrowest
runtime secret distribution for the deployed responsibilities.

## Verification

Tests use `httpx.MockTransport`; they never require a live Mattermost server. Coverage includes
strict URL and response parsing, redaction, timeouts, retry classification, `Retry-After`, client
reuse/close, deterministic and size-safe cards, Unicode/Markdown safety, exact button/context
shape, signature tampering, boundary-before-DB ordering, identity/role/current-report checks,
concurrent replay, all three actions, transactional rollback, outbox claim fencing, max-three retry,
permanent failure, success/failure evidence, crash recovery semantics, and unchanged A5 dispatch
behavior. Migration and metadata tests run on PostgreSQL 16 and 18. The wheel-only verifier imports
and exercises the A8 routes without the source tree.
