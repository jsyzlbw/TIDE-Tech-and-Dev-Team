# Web Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a polished, responsive React console for teacher assignment management and report review plus a focused student submission experience.

**Architecture:** The frontend is a feature-oriented TypeScript application. A typed API client owns authentication and errors, TanStack Query owns server state, and role-aware routes prevent accidental access. UI components consume domain view models rather than raw API payloads.

**Tech Stack:** React, TypeScript, Vite, React Router, TanStack Query, Zod, CSS variables, Vitest, Testing Library, Playwright

---

## Frozen interface and visual direction

- Routes: `/login`, `/teacher`, `/teacher/assignments/:id`, `/teacher/reports/:id`, `/student`, `/student/assignments/:id`
- Auth storage key: `ai-grading.access-token`
- API base environment key: `VITE_API_BASE_URL`
- Visual direction: academic editorial workbench / annotated paper desk, with a warm paper canvas, archival ink, indigo action color, amber review state, green confirmed state, and red failure state. Avoid permanent sidebars, full-screen floating cards, and decorative purple AI gradients.
- Typography: self-host Source Serif 4 600 for editorial display and IBM Plex Sans 400/600 for interface text; use Songti SC/STSong and PingFang SC/system CJK fallbacks. Scores and timestamps use tabular numerals. The three Latin WOFF2 assets total 68.36 kB uncompressed, require no third-party font request, and leave Chinese glyphs to offline OS fonts instead of shipping a multi-megabyte CJK webfont.
- Text resilience: every dynamic grid/flex child can shrink (`min-width: 0`), user/course/status/copy text may wrap long unbroken tokens, and content must not be silently clipped by decorative paper treatments.
- Contrast: 11 px metadata using muted or review text must retain at least 4.7:1 against both canvas and paper tokens; lighter rule colors are border-only.
- Desktop report review: three columns; tablet: two columns; mobile: stacked sections
- Motion: only opacity/transform transitions at 120–180 ms; color, background, border, and shadow changes are immediate. Respect `prefers-reduced-motion`.

### W1 dependency compatibility decision

These are deliberate stable-major approvals for the scaffold, not incidental upgrades from the earlier examples. Exact versions are locked so later tasks share one reproducible baseline.

| Area | Approved version | Compatibility/audit decision |
| --- | --- | --- |
| Runtime | React / React DOM 19.2.8 | Stable React 19 line. |
| Routing | React Router 8.3.0 | Use the single `react-router` package: v8 exposes the browser and memory router APIs directly, while `react-router-dom` has no 8.3 release. The isolated API/build probe and `npm audit` are clean; this also removes the affected v7 RSC dependency path. Requires Node >=22.22. |
| Server state / validation | TanStack Query 5.101.4; Zod 4.4.3 | Stable major lines, exact-lock approved. |
| Build / test | Vite 8.1.5; Vitest 4.1.10; plugin-react 6.0.4 | Stable published majors. Vitest supports Node 22 and 24+, so Node 23 is not advertised. |
| Language / lint | TypeScript 6.0.2; ESLint 10.0.0; typescript-eslint 8.65.0 | Stable published releases validated by lint, type-check, and production build; do not cross another major without a new compatibility pass. |
| Toolchain | Node `^22.22.0 || >=24 <26`; npm `>=11 <12`; package manager npm 11.12.1 | Covers the target Node 22 LTS and the verified Node 25 workstation while honoring Router and Vitest engine ranges. |

## Task 1: Scaffold React, test runtime, and design tokens

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.app.json`
- Create: `frontend/tsconfig.node.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/eslint.config.js`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/app/App.tsx`
- Create: `frontend/src/styles/tokens.css`
- Create: `frontend/src/styles/global.css`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/app/App.test.tsx`
- Create: `frontend/src/styles/styleFoundation.test.ts`
- Create: `frontend/src/test/packagePolicy.test.ts`

- [x] **Step 1: Create package scripts and dependencies**

Create `frontend/package.json`:

```json
{
  "name": "ai-grading-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "packageManager": "npm@11.12.1",
  "engines": {
    "node": "^22.22.0 || >=24 <26",
    "npm": ">=11 <12"
  },
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "lint": "eslint . --max-warnings=0",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "@fontsource/ibm-plex-sans": "5.3.0",
    "@fontsource/source-serif-4": "5.3.0",
    "@tanstack/react-query": "5.101.4",
    "react": "19.2.8",
    "react-dom": "19.2.8",
    "react-router": "8.3.0",
    "zod": "4.4.3"
  },
  "devDependencies": {
    "@eslint/js": "10.0.1",
    "@testing-library/jest-dom": "7.0.0",
    "@testing-library/react": "16.3.2",
    "@testing-library/user-event": "14.6.1",
    "@types/node": "26.1.1",
    "@types/react": "19.2.17",
    "@types/react-dom": "19.2.3",
    "@vitejs/plugin-react": "6.0.4",
    "eslint": "10.0.0",
    "eslint-plugin-react-hooks": "7.1.1",
    "eslint-plugin-react-refresh": "0.5.3",
    "globals": "17.8.0",
    "jsdom": "29.1.1",
    "typescript": "6.0.2",
    "typescript-eslint": "8.65.0",
    "vite": "8.1.5",
    "vitest": "4.1.10"
  }
}
```

Run:

```bash
cd frontend && npm install
```

Expected: install exits `0` and produces `package-lock.json`.

Create `frontend/vite.config.ts` so Vitest has DOM matchers:

```ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
```

Create `frontend/eslint.config.js`:

```js
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { languageOptions: { globals: { ...globals.browser, ...globals.node } } },
);
```

Create the initial `frontend/src/test/setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";
```

- [x] **Step 2: Write the first render test**

Create `frontend/src/app/App.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App";

describe("App", () => {
  it("renders the product identity", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: "作业评审台" })).toBeInTheDocument();
  });
});
```

- [x] **Step 3: Run and verify failure**

Run:

```bash
cd frontend && npm test -- App.test.tsx
```

Expected: test fails because `App` is missing.

- [x] **Step 4: Implement the app shell and tokens**

Create `frontend/src/styles/tokens.css`:

```css
:root {
  color-scheme: light;
  --canvas: #f5f2eb;
  --surface: #fffdf8;
  --surface-strong: #ffffff;
  --ink: #1c2430;
  --muted: #67717f;
  --line: #ddd8ce;
  --accent: #4f46e5;
  --accent-strong: #3730a3;
  --review: #b45309;
  --success: #15803d;
  --danger: #b42318;
  --radius-sm: 8px;
  --radius-md: 14px;
  --radius-lg: 22px;
  --shadow: 0 18px 60px rgb(38 35 28 / 10%);
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-6: 24px;
  --space-8: 32px;
}
```

Create `frontend/src/app/App.tsx`:

```tsx
export function App() {
  return (
    <main className="app-shell">
      <header className="topbar">
        <p className="eyebrow">TIDE CLUB · DATA STRUCTURES</p>
        <h1>作业评审台</h1>
      </header>
    </main>
  );
}
```

Create `main.tsx` to import tokens and global styles, mount `<App />`, and wrap it in `StrictMode`.

- [x] **Step 5: Verify and commit scaffold**

Run:

```bash
cd frontend && npm test && npm run build
```

Expected: test and production build exit `0`.

```bash
git add frontend
git commit -m "chore: scaffold React web console"
```

## Task 2: Add typed API client and error contract

**Files:**
- Create: `frontend/src/shared/api/client.ts`
- Create: `frontend/src/shared/api/errors.ts`
- Create: `frontend/src/shared/api/schemas.ts`
- Create: `frontend/src/shared/api/client.test.ts`
- Modify: `frontend/src/test/setup.ts`
- Create: `frontend/src/test/fixtures.ts`
- Create: `frontend/src/test/server.ts`

- [ ] **Step 1: Write client behavior tests**

Add `msw` to frontend dev dependencies with `npm install --save-dev msw`.

Create `frontend/src/test/setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./server";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
});
afterAll(() => server.close());
```

Create `frontend/src/test/fixtures.ts`:

```ts
export const USER_FIXTURE = {
  id: "00000000-0000-0000-0000-000000000001",
  username: "teacher-test",
  display_name: "测试教师",
  role: "teacher" as const,
};
```

Create `frontend/src/test/server.ts`:

```ts
import { setupServer } from "msw/node";

export const server = setupServer();
```

Create `frontend/src/shared/api/client.test.ts` with these imports followed by the two tests:

```tsx
import { http, HttpResponse } from "msw";
import { z } from "zod";
import { api } from "./client";
import { currentUserSchema } from "./schemas";
import { USER_FIXTURE } from "../../test/fixtures";
import { server } from "../../test/server";
```

```tsx
it("adds bearer token and parses a typed response", async () => {
  localStorage.setItem("ai-grading.access-token", "token");
  server.use(http.get("*/auth/me", ({ request }) => {
    expect(request.headers.get("authorization")).toBe("Bearer token");
    return HttpResponse.json(USER_FIXTURE);
  }));
  await expect(api.get("/auth/me", currentUserSchema)).resolves.toEqual(USER_FIXTURE);
});

it("throws ApiError with request id", async () => {
  server.use(http.get("*/missing", () => HttpResponse.json(
    { code: "NOT_FOUND", message: "资源不存在", request_id: "req-1" }, { status: 404 }
  )));
  await expect(api.get("/missing", z.never())).rejects.toMatchObject({
    status: 404, code: "NOT_FOUND", requestId: "req-1"
  });
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd frontend && npm test -- client.test.ts
```

Expected: client import fails.

- [ ] **Step 3: Implement `ApiError` and client**

```ts
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId: string,
  ) {
    super(message);
  }
}
```

The client prefixes `VITE_API_BASE_URL ?? "http://localhost:8000/api/v1"`, adds JSON headers and bearer token, validates success payloads with the supplied Zod schema, and throws `ApiError` for non-2xx responses.

- [ ] **Step 4: Define shared schemas**

Create Zod schemas for `CurrentUser`, `AssignmentSummary`, `SubmissionRow`, `EvaluationReport`, `EvaluationJob`, and the standard error. Derive TypeScript types with `z.infer`; do not duplicate interfaces.

- [ ] **Step 5: Verify and commit API layer**

Run:

```bash
cd frontend && npm test -- client.test.ts && npm run build
```

Expected: client tests and type-check build pass.

```bash
git add frontend/src/shared/api
git commit -m "feat: add typed frontend API client"
```

## Task 3: Implement authentication and role-aware routing

**Files:**
- Create: `frontend/src/auth/AuthProvider.tsx`
- Create: `frontend/src/auth/LoginPage.tsx`
- Create: `frontend/src/auth/RequireRole.tsx`
- Create: `frontend/src/auth/auth.test.tsx`
- Create: `frontend/src/test/render.tsx`
- Create: `frontend/src/app/router.tsx`
- Modify: `frontend/src/app/App.tsx`

- [ ] **Step 1: Write login and authorization tests**

Create `frontend/src/test/render.tsx` as the shared route harness:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { RouterProvider } from "react-router";
import { AuthProvider } from "../auth/AuthProvider";
import { createAppRouter } from "../app/router";
import { USER_FIXTURE } from "./fixtures";
import { server } from "./server";

export function mockCurrentUser(overrides: Partial<typeof USER_FIXTURE> = {}) {
  server.use(
    http.get("*/auth/me", () => HttpResponse.json({ ...USER_FIXTURE, ...overrides })),
  );
}

export function mockUnauthenticatedUser() {
  server.use(
    http.get("*/auth/me", () =>
      HttpResponse.json(
        { code: "UNAUTHENTICATED", message: "请先登录", request_id: "test-auth" },
        { status: 401 },
      ),
    ),
  );
}

export function renderAppAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createAppRouter([path]);
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
}
```

Create `frontend/src/auth/auth.test.tsx` with:

```tsx
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockCurrentUser, mockUnauthenticatedUser, renderAppAt } from "../test/render";
```

```tsx
it("redirects an unauthenticated teacher route to login", async () => {
  mockUnauthenticatedUser();
  renderAppAt("/teacher");
  expect(await screen.findByRole("heading", { name: "登录" })).toBeInTheDocument();
});

it("blocks a student from the teacher route", async () => {
  mockCurrentUser({ role: "student" });
  renderAppAt("/teacher");
  expect(await screen.findByText("无权访问此页面")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd frontend && npm test -- auth.test.tsx
```

Expected: authentication components are missing.

- [ ] **Step 3: Implement auth provider**

Expose:

```ts
type AuthContextValue = {
  user: CurrentUser | null;
  loading: boolean;
  login(username: string, password: string): Promise<void>;
  logout(): void;
};
```

Login posts to `/auth/login`, stores the token, then queries `/auth/me`. Logout clears token and query cache.

- [ ] **Step 4: Implement routes**

Export `createAppRouter(initialEntries?: string[])` from `frontend/src/app/router.tsx`. Use `createMemoryRouter` when `initialEntries` is supplied and `createBrowserRouter` otherwise. Configure the six frozen routes. `RequireRole` renders a loading skeleton while resolving auth, navigates missing users to `/login`, and renders a permission panel for the wrong role.

- [ ] **Step 5: Verify and commit auth UI**

Run:

```bash
cd frontend && npm test -- auth.test.tsx && npm run build
```

Expected: login, redirect, role denial, and logout tests pass.

```bash
git add frontend/src/auth frontend/src/app/App.tsx
git commit -m "feat: add role-aware web authentication"
```

## Task 4: Build teacher dashboard and assignment creation

**Files:**
- Create: `frontend/src/assignments/api.ts`
- Create: `frontend/src/assignments/TeacherDashboard.tsx`
- Create: `frontend/src/assignments/AssignmentCard.tsx`
- Create: `frontend/src/assignments/CreateAssignmentDialog.tsx`
- Create: `frontend/src/assignments/TeacherDashboard.test.tsx`

- [ ] **Step 1: Write dashboard tests**

Test that the dashboard renders total assignments, pending evaluations, pending reviews, failed jobs, and assignment cards. Test the create form requires title, question, future due date, and at least one rubric point.

```tsx
expect(screen.getByText("待审核")).toBeInTheDocument();
expect(screen.getByText("HW-0001")).toBeInTheDocument();
expect(await screen.findByText("截止时间必须晚于当前时间")).toBeInTheDocument();
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd frontend && npm test -- TeacherDashboard.test.tsx
```

Expected: dashboard components are missing.

- [ ] **Step 3: Implement query and mutation hooks**

Create hooks:

```ts
useAssignments()
useCreateAssignment()
usePublishAssignment()
useCloseAssignment()
```

After a successful mutation, invalidate `['assignments']` and show a non-blocking status banner.

- [ ] **Step 4: Implement the dashboard and form**

Use a 12-column desktop grid with a compact metric strip, assignment cards, and a single primary “发布新作业” action. Do not use a permanent sidebar. The form includes title, question, notes, due date, required-point chips, and grading notes.

- [ ] **Step 5: Verify and commit dashboard**

Run:

```bash
cd frontend && npm test -- TeacherDashboard.test.tsx && npm run build
```

Expected: metrics, empty state, cards, validation, create, and publish tests pass.

```bash
git add frontend/src/assignments
git commit -m "feat: add teacher assignment dashboard"
```

## Task 5: Build assignment summary and evaluation controls

**Files:**
- Create: `frontend/src/assignments/AssignmentDetailPage.tsx`
- Create: `frontend/src/assignments/SubmissionTable.tsx`
- Create: `frontend/src/assignments/EvaluationProgress.tsx`
- Create: `frontend/src/assignments/AssignmentDetailPage.test.tsx`

- [ ] **Step 1: Write summary interaction tests**

Test:

```tsx
expect(screen.getByText("2 / 3 已提交")).toBeInTheDocument();
expect(screen.getByText("学生甲")).toBeInTheDocument();
await user.click(screen.getByRole("button", { name: "评估全部最新提交" }));
expect(await screen.findByText("已加入 2 个评估任务")).toBeInTheDocument();
```

Also test filtering `待评估`, `评估中`, `待审核`, `已审核`, and `失败`.

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd frontend && npm test -- AssignmentDetailPage.test.tsx
```

Expected: summary page components are missing.

- [ ] **Step 3: Implement summary polling**

Query every two seconds only while any job is `queued` or `running`; stop polling when all jobs are terminal. A repeated bulk-evaluate click is disabled while the mutation is pending and relies on backend idempotency if retried.

- [ ] **Step 4: Implement status table and partial failure state**

The table columns are student, latest version, submitted time, evaluation status, score/grade, and action. A failed row shows its sanitized error and a “重新评估” action without hiding successful rows.

- [ ] **Step 5: Verify and commit assignment detail**

Run:

```bash
cd frontend && npm test -- AssignmentDetailPage.test.tsx && npm run build
```

Expected: summary, filters, polling start/stop, bulk evaluation, and partial failure tests pass.

```bash
git add frontend/src/assignments
git commit -m "feat: add assignment evaluation workspace"
```

## Task 6: Build the three-column report review workspace

**Files:**
- Create: `frontend/src/reports/api.ts`
- Create: `frontend/src/reports/ReportReviewPage.tsx`
- Create: `frontend/src/reports/ReportPanel.tsx`
- Create: `frontend/src/reports/ReportEditForm.tsx`
- Create: `frontend/src/reports/ReportTimeline.tsx`
- Create: `frontend/src/reports/ReportReviewPage.test.tsx`

- [ ] **Step 1: Write report workflow tests**

Cover:

```tsx
expect(screen.getByText("答案完整性")).toBeInTheDocument();
expect(screen.getByText("正确性初步判断")).toBeInTheDocument();
expect(screen.getByText("主要问题")).toBeInTheDocument();
expect(screen.getByText("修改建议")).toBeInTheDocument();
expect(screen.getByText("82 · B")).toBeInTheDocument();
```

Click confirm and assert the status becomes “已确认”. Edit score to 88 and assert grade displays B. Trigger re-evaluation and assert a new pending timeline item appears.

Click “导出报告 JSON”, inspect the downloaded Blob, and assert it contains `schema_version`, `report_id`, `submission_id`, `score`, `grade`, all five required report sections, `limitations`, and the current version metadata. It must not contain the provider API key or JWT.

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd frontend && npm test -- ReportReviewPage.test.tsx
```

Expected: report components are missing.

- [ ] **Step 3: Implement report queries and mutations**

Create hooks for report versions, confirm, modify, and re-evaluate. Invalidate the report timeline and assignment summary after every successful action. Surface `409` as “该报告已被更新，请刷新后重试”. Add `downloadReportJson(report)` that serializes the typed public report DTO with two-space indentation, writes a UTF-8 `application/json` Blob, and downloads `report-<report-id>-v<version>.json`.

- [ ] **Step 4: Implement review layout**

Desktop columns:

```text
28% assignment/rubric | 34% student answer | 38% evaluation/review
```

Score is visually prominent but not the first element. Place limitations and confidence near the teacher action area. The edit form requires a comment when changing score by 10 or more points. Put “导出报告 JSON” beside the version selector as a secondary action; exporting never changes review state.

- [ ] **Step 5: Implement immutable timeline**

Each item shows version, origin, status, score, grade, author, and time. Superseded versions remain readable. Raw model output is collapsed and visible only to teachers.

- [ ] **Step 6: Verify and commit review workspace**

Run:

```bash
cd frontend && npm test -- ReportReviewPage.test.tsx && npm run build
```

Expected: field rendering, confirm, edit, derived grade, re-evaluation, JSON export, conflict, and timeline tests pass.

```bash
git add frontend/src/reports
git commit -m "feat: add teacher report review workspace"
```

## Task 7: Build student assignment and submission experience

**Files:**
- Create: `frontend/src/submissions/api.ts`
- Create: `frontend/src/submissions/StudentHomePage.tsx`
- Create: `frontend/src/submissions/StudentAssignmentPage.tsx`
- Create: `frontend/src/submissions/AnswerEditor.tsx`
- Create: `frontend/src/submissions/StudentAssignmentPage.test.tsx`

- [ ] **Step 1: Write student flow tests**

Test published assignment listing, answer submission, version history, deadline rejection, and report visibility. Assert teacher-only rubric and raw model output never render.

```tsx
expect(screen.queryByText("教师评分注意事项")).not.toBeInTheDocument();
expect(screen.queryByText("raw_model_output")).not.toBeInTheDocument();
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd frontend && npm test -- StudentAssignmentPage.test.tsx
```

Expected: student components are missing.

- [ ] **Step 3: Implement student queries and editor**

The editor offers `text`, `markdown`, and `code` modes, displays a 50,000-character counter, and requires explicit submission. Do not autosubmit. Preserve unsent text in component state during network errors.

- [ ] **Step 4: Implement versions and report view**

Show version number and submitted time. The latest submitted version is visually marked. The report view shows the five required report sections and a banner that the result is an AI-assisted preliminary assessment.

- [ ] **Step 5: Verify and commit student UI**

Run:

```bash
cd frontend && npm test -- StudentAssignmentPage.test.tsx && npm run build
```

Expected: listing, submit, retry, versioning, deadline, ownership, and privacy tests pass.

```bash
git add frontend/src/submissions
git commit -m "feat: add student submission experience"
```

## Task 8: Add responsive, accessible, and error-state coverage

**Files:**
- Create: `frontend/src/shared/ui/AsyncState.tsx`
- Create: `frontend/src/shared/ui/StatusBadge.tsx`
- Create: `frontend/src/shared/ui/ToastRegion.tsx`
- Create: `frontend/src/styles/responsive.css`
- Create: `frontend/src/app/accessibility.test.tsx`
- Modify: all feature pages from Tasks 4–7

- [ ] **Step 1: Write accessibility and state tests**

Assert keyboard-reachable actions, visible focus, labeled form controls, live error regions, and logical heading order. Test loading, empty, permission, network error, processing, and partial failure states.

- [ ] **Step 2: Run and record failures**

Run:

```bash
cd frontend && npm test -- accessibility.test.tsx
```

Expected: tests fail until shared state components and labels are applied.

- [ ] **Step 3: Implement shared states and responsive CSS**

At widths below 900 px, the review layout becomes two columns; below 640 px it becomes one column. Tables receive a compact card representation rather than horizontal overflow. Add:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
  }
}
```

- [ ] **Step 4: Verify full frontend**

Run:

```bash
cd frontend
npm test
npm run lint
npm run build
```

Expected: zero test failures, zero lint errors, and successful production build.

- [ ] **Step 5: Commit UI hardening**

```bash
git add frontend/src
git commit -m "feat: harden responsive and accessible UI states"
```

## Plan 3 completion gate

Run:

```bash
cd frontend
npm test
npm run lint
npm run build
```

Expected:

- All component tests pass.
- TypeScript and Vite production build pass.
- Teacher can create/publish, summarize, evaluate, confirm, modify, and re-evaluate.
- Student can list, submit, see versions, and read only their report.
- Loading, empty, permission, network, processing, and partial-failure states render.
- `git status --short` is empty after the final task commit.
