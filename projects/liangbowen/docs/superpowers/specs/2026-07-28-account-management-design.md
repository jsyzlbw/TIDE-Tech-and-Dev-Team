# Account Management and Role-Safe Administration Design

**Date:** 2026-07-28
**Status:** Approved direction; written-spec review pending

## Objective

Add secure in-application account provisioning to the existing single-course grading system:

- a bootstrapped local administrator can create teacher and student accounts;
- a teacher can create student accounts only;
- students cannot access account-management operations;
- no web or API caller can create another administrator;
- newly created accounts can sign in through the existing local login flow immediately;
- administrator access does not imply access to assignments, submissions, evaluations, or reviews.

The feature replaces source-code edits as the normal way to provision users while preserving the existing local demo and Mattermost integration.

## Current System Constraints

The database and Python role enum already contain `admin`, `teacher`, and `student`. The login path resolves the authenticated user from the database on every request, so the database role and `is_active` flag remain authoritative even when an older JWT contains a stale role claim.

The system currently has no user-management API or UI. Demo accounts are inserted by `backend/scripts/seed_demo.py`. The frontend recognizes the `admin` value in its API schema but has no administrator landing page or route.

The application currently models one course with a global active-student population. Therefore, every newly created active student immediately counts toward every assignment's total-student and missing-submission metrics. Enrollment and per-class rosters are outside this feature.

## Chosen Approach

Implement a first-class `users` application module with protected list/create endpoints and one shared account-management frontend. This is preferable to a CLI-only workflow because it is demonstrable and usable by teachers, and preferable to treating Mattermost as the source of truth because local login must keep working without Mattermost.

Two alternatives were rejected:

1. **Seed or CLI provisioning only:** smaller implementation, but it cannot satisfy the teacher-facing workflow and makes acceptance demonstrations dependent on terminal access.
2. **Mattermost-owned identity provisioning:** would couple local authentication to an optional external service, require identity synchronization, and incorrectly imply that matching usernames are authoritative bindings.

## Authorization Model

| Capability | Administrator | Teacher | Student |
| --- | --- | --- | --- |
| Create teacher | Allowed | Denied | Denied |
| Create student | Allowed | Allowed | Denied |
| Create administrator | Denied | Denied | Denied |
| List teachers | Allowed | Denied | Denied |
| List students | Allowed | Allowed | Denied |
| Manage assignments and reviews | Denied | Allowed | Denied |
| Submit assignments | Denied | Denied | Allowed |

Backend authorization is authoritative. Hiding role options or routes in the frontend is only a usability measure.

The shared account-management service rechecks that the actor is active and applies the same role matrix inside the transaction. The request body never accepts an actor ID, creator ID, password hash, or active flag. A requested administrator role is recognized as a role value but is always rejected by authorization with `403`; this keeps “cannot create administrators” an explicit permission boundary rather than a syntax accident.

The existing assignment list and detail endpoints must be tightened from implicit “non-student” access to explicit teacher-only access. This prevents the newly usable administrator role from inheriting course-data access accidentally. Existing teacher and student behavior remains unchanged.

## Account Data and Creation Audit

The existing `users` table remains the source of account identity:

- `id`: generated UUID;
- `username`: unique login identifier;
- `display_name`: human-facing name;
- `role`: `teacher`, `student`, or bootstrapped `admin`;
- `password_hash`: bcrypt hash of SHA-256-normalized password material;
- `is_active`: `true` for newly created accounts;
- `created_at`: database creation timestamp.

Do not add a mutable `created_by` column to `users`. Instead, migration `0008` adds an immutable `account_creation_events` table:

- `id`: UUID primary key;
- `actor_user_id`: administrator or teacher that performed the action;
- `target_user_id`: newly created user, unique and foreign-keyed to `users`;
- `target_role`: captured role at creation time;
- `request_id`: request correlation identifier;
- `created_at`: creation timestamp.

Database triggers reject update, delete, and truncate operations on creation events. User insertion and its creation event are committed in the same transaction. The event deliberately excludes plaintext passwords, password hashes, and display names.

Bootstrapped accounts can lack a creation event and are displayed as “系统初始化”. The demo seed creates deterministic attribution for its teacher and student accounts where practical, but the bootstrapped administrator itself has no actor.

## API Contract

### `GET /api/v1/users`

Returns a bounded, paginated account register. Query parameters are `limit` (1–100, default 50) and `offset` (0–10,000, default 0).

- administrators receive administrator, teacher, and student rows;
- teachers receive student rows only;
- students receive `403`;
- missing, invalid, or inactive authentication receives `401`.

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "username": "student4",
      "display_name": "张三",
      "role": "student",
      "is_active": true,
      "created_at": "2026-07-28T12:00:00Z",
      "created_by": {
        "id": "uuid",
        "username": "teacher",
        "display_name": "演示教师"
      }
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

`created_by` is `null` for bootstrapped or historical accounts without an event. No response contains `password_hash` or an initial password.

### `POST /api/v1/users`

Request:

```json
{
  "username": "student4",
  "display_name": "张三",
  "role": "student",
  "password": "Course2026!"
}
```

The endpoint returns the newly created public account record with status `201`.

Stable outcomes:

- `401`: missing, invalid, or inactive authentication;
- `403`: caller cannot use account management or cannot create the requested role;
- `409`: canonical username already exists, including a concurrent insert race;
- `413`: request exceeds the dedicated 4 KiB account-request limit;
- `422`: invalid username, display name, role, or password;
- `500`: unexpected persistence failure, with the user and audit event rolled back together.

Only the username unique-constraint violation maps to `409`; unrelated integrity failures remain server errors. Password hashing runs in a bounded worker thread so bcrypt work does not block the async event loop.

## Input Rules

### Username

- trim surrounding whitespace before validation;
- 3–64 characters;
- canonical lowercase ASCII only;
- pattern: `^[a-z0-9][a-z0-9._-]{2,63}$`;
- no silent lowercasing, because silently changing identity input hides mistakes;
- database uniqueness remains the final concurrency-safe check.

This format is compatible with the project's optional Mattermost account conventions without making a matching Mattermost username an identity binding.

### Display name

- trim surrounding whitespace;
- 1–128 Unicode code points;
- must contain at least one visible, non-control character;
- control characters are rejected.

### Initial password

- 12–128 Unicode code points;
- no leading or trailing whitespace;
- no NUL or control characters;
- must not equal the username;
- accepted only in the create request and password-confirmation field;
- never returned, logged, audited, persisted in client storage, or retained in query caches.

The global login password range remains unchanged so existing accounts continue to authenticate. The stronger policy applies only to newly provisioned accounts. Forced password changes and self-service password reset are explicitly outside this version.

## Frontend Information Architecture

### Administrator

- `/` redirects an administrator to `/admin/users`.
- `/admin/users` renders “账号管理台”.
- The page shows the current administrator, a create-account action, role totals, and a paginated account register.
- The create dialog allows `教师` or `学生`.

### Teacher

- The existing teacher dashboard adds a visible “管理学生账号” link.
- `/teacher/users` renders the same management component in teacher mode.
- The create dialog fixes the target role to `学生`; no teacher/admin role selector is rendered.
- A return link leads to the teacher assignment dashboard.

### Shared interaction design

The page follows the existing editorial ledger visual language and reuses established buttons, asynchronous states, toast regions, modal focus trapping, dirty-form navigation protection, and responsive patterns.

The create form contains username, display name, role where authorized, initial password, and password confirmation. Client validation mirrors the server only for immediate feedback; server validation remains authoritative. On success, the dialog closes, password state is cleared, the list and totals refresh, and a success announcement identifies the created username. Duplicate, validation, timeout, network, contract, and server failures receive safe Chinese messages.

The frontend never stores initial passwords in `localStorage`, React Query data, URLs, navigation state, telemetry, or error messages.

## Routing and Session Behavior

The frontend role type expands to include administrator workspaces. `roleLanding("admin")` returns `/admin/users`, and safe post-login destinations accept only the current role's route namespace.

Route guards support explicit allowed-role sets without weakening existing single-role pages:

- `/admin/users`: administrator only;
- `/teacher/users`: teacher only;
- `/teacher`, assignment details, and report reviews: teacher only;
- `/student` and student assignment details: student only.

An authenticated user who attempts another role's route sees the existing access-denied state and can return to their own landing page.

## Demo Bootstrap

The development/test seed gains one fixed local account:

| Role | Username | Display name | Password |
| --- | --- | --- | --- |
| Administrator | `admin` | 系统管理员 | `Admin123!Secure` |

README labels this credential as local development/test data only. Seed summaries, reset manifests, tests, and acceptance evidence are updated from four to five users. The seed remains idempotent.

The administrator password is intentionally stronger than the historical demo passwords and is never used as a production default. Production administrator bootstrap is an operations concern outside this local assignment.

## Mattermost Boundary

Creating a local teacher or student does not create, discover, or bind a Mattermost account. Same-name accounts are not assumed to be identical.

The existing explicit development binding flow remains responsible for connecting an immutable Mattermost user ID to a local user ID. New teachers and students may be bound later through that flow. Local administrators remain ineligible for Mattermost command identity, and the Mattermost server administrator remains a separate external identity.

## Error Handling and Concurrency

- Creation is idempotent only at the username uniqueness boundary; retrying a successful request returns `409` rather than exposing credentials.
- A pre-insert availability lookup may improve messaging but is never trusted for correctness.
- Concurrent requests for the same username result in one committed user and one committed creation event; all losing requests return `409`.
- Any audit insertion failure rolls back the user insertion.
- Unexpected database errors are logged with request ID and safe metadata only.
- List responses use deterministic ordering by `created_at DESC, id DESC`.

## Testing Strategy

### Backend

- schema boundaries for username, display name, role, and password;
- password hashing and verification without password/hash leakage;
- the full administrator/teacher/student permission matrix;
- inactive and unauthenticated callers;
- teacher attempts to create teacher/admin and administrator attempts to create admin;
- exact and concurrent duplicate usernames;
- atomic user/event commit and rollback;
- immutable audit-table triggers;
- account-list visibility and pagination;
- database role changes and deactivation taking effect with an old JWT;
- administrators receiving `403` from assignment list and detail endpoints;
- seed idempotency and five-user manifest;
- a newly created student increasing existing assignment missing counts;
- no automatic Mattermost binding and continued administrator rejection by Mattermost identity flows.

### Frontend

- administrator login landing and route isolation;
- teacher management entry and teacher-only student role;
- account list, empty, loading, partial, and failure states;
- form validation and password confirmation;
- duplicate, validation, network, timeout, contract, and server errors;
- double-submit prevention, focus restoration, escape behavior, and dirty-form guard;
- success refresh and password-state clearing;
- keyboard and screen-reader accessibility;
- no password persistence in local or query caches.

### End-to-end

1. Administrator signs in and creates a teacher.
2. The new teacher signs in and creates a student.
3. The new student signs in and sees published work.
4. The student cannot access either account-management API or route.
5. The administrator cannot access teacher assignment data.

## Explicit Non-Goals

- public self-registration;
- creating administrators through the application;
- editing roles, usernames, or display names;
- disabling or deleting accounts;
- password reset, forced password change, or password recovery;
- class, team, course enrollment, or per-teacher student ownership;
- automatic Mattermost provisioning or binding;
- production secret distribution or production administrator recovery.

These capabilities can be designed separately without weakening the creation boundary in this version.
