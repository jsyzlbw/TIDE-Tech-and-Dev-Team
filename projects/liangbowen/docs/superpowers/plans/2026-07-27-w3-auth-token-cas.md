# W3 Authentication Token CAS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stale session and login responses for token A from deleting or overwriting a newer persisted token B, and focus the first invalid login field.

**Architecture:** `AuthProvider` owns an `activeToken` string instead of a boolean and keys each TanStack session query by that token. Every `/auth/me` call uses a client whose bearer reader is pinned to the attempt token; completion reconciles against the current sanitized storage value before deleting storage or publishing user data. Login validation schedules one focus action only when submission validation fails.

**Tech Stack:** React 19, TypeScript, TanStack Query 5, React Router 8, Vitest, Testing Library, MSW.

---

### Task 1: Pin restored-session attempts and reconcile fatal results

**Files:**
- Modify: `frontend/src/auth/AuthProvider.tsx`
- Test: `frontend/src/auth/auth.test.tsx`

- [x] **Step 1: Write failing deterministic session tests**

Use a deferred MSW `/auth/me` response for bearer A, write bearer B directly to local storage without dispatching an event, then release A as 401. Assert B remains, a second request uses `Bearer B`, and B's user is rendered. Add timely and late storage-event variants and a throwing `removeItem` case.

- [x] **Step 2: Verify RED**

Run:

```bash
cd frontend && npm test -- src/auth/auth.test.tsx -t "session token compare-and-swap" --reporter=verbose
```

Expected: stale A deletes B or prevents B revalidation.

- [x] **Step 3: Implement pinned session attempts**

Represent the current credential as `activeToken: string | null`, build the query key as:

```ts
const sessionQueryKey = (token: string) => [...AUTH_SESSION_QUERY_KEY, token] as const;
```

Create the request client with:

```ts
createApiClient({ getToken: () => attemptToken }).get("/auth/me", currentUserSchema, { signal });
```

On fatal A, sanitize and read storage. Remove only when storage still equals A; otherwise switch to legal B and let B's distinct query run. Cancel and remove all queries with the `AUTH_SESSION_QUERY_KEY` prefix on logout or credential replacement.

- [x] **Step 4: Verify GREEN**

Run the focused command from Step 2 and require all session CAS cases to pass.

### Task 2: Pin login verification and guard publication with CAS

**Files:**
- Modify: `frontend/src/auth/AuthProvider.tsx`
- Test: `frontend/src/auth/auth.test.tsx`

- [x] **Step 1: Write failing login CAS tests**

Return token A from `/auth/login`, defer A's `/auth/me`, write token B without an event, then release A once as 503 and once as a successful A user. In both tests assert B remains, `Bearer B` is reverified, and A's user never becomes B's cached session.

- [x] **Step 2: Verify RED**

Run:

```bash
cd frontend && npm test -- src/auth/auth.test.tsx -t "login token compare-and-swap" --reporter=verbose
```

Expected: the current generation authorizes unconditional deletion or publication for stale A.

- [x] **Step 3: Implement pinned login verification**

Verify A with `createApiClient({ getToken: () => tokenA })`. Before publishing success, require both the current generation and sanitized storage to still equal A. On failure or stale completion, remove storage only through an exact-value CAS; when legal B exists, switch `activeToken` to B and start B's query.

- [x] **Step 4: Verify GREEN**

Run the focused command from Step 2 and require both failure and success races to pass.

### Task 3: Focus the first invalid login field and run release gates

**Files:**
- Modify: `frontend/src/auth/LoginPage.tsx`
- Test: `frontend/src/auth/auth.test.tsx`

- [x] **Step 1: Write failing keyboard focus tests**

Submit with both fields empty and assert `document.activeElement` is username. Submit with a valid username and empty password and assert password is focused. Trigger both submissions through keyboard Enter.

- [x] **Step 2: Verify RED**

Run:

```bash
cd frontend && npm test -- src/auth/auth.test.tsx -t "focuses the first invalid" --reporter=verbose
```

Expected: focus remains on the submit origin instead of the invalid field.

- [x] **Step 3: Implement one-shot focus**

Attach refs to both inputs. Only in the invalid submit branch, schedule one microtask that focuses the username ref when it has an error, otherwise the password ref. Do not focus from render or a persistent effect.

- [x] **Step 4: Run fresh verification and commit**

Run:

```bash
cd frontend && npm test && npm run lint && npm run build && npm audit --audit-level=high
PYTHONPATH=backend .venv/bin/pytest backend/tests/auth -q
git diff --check
```

Expected: every command exits zero. Commit only W3 CAS, focus, tests, and this plan as one atomic fix.
