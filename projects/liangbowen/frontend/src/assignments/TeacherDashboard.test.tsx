import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ACCESS_TOKEN_STORAGE_KEY } from "../shared/api/client";
import type { AssignmentListItem } from "../shared/api/schemas";
import { ids } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const API_BASE = "http://localhost:8000/api/v1";
const TOKEN_A = "teacher-a.jwt.token";
const TOKEN_B = "teacher-b.jwt.token";
const teacherA = {
  id: ids.user,
  username: "ada",
  display_name: "Ada Lovelace",
  role: "teacher" as const,
};
const teacherB = {
  id: "77777777-7777-4777-8777-777777777777",
  username: "barbara",
  display_name: "Barbara Liskov",
  role: "teacher" as const,
};

const assignments = [
  {
    id: ids.assignment,
    code: "HW-0001",
    title: "图的最短路径",
    due_at: "2026-08-01T12:00:00Z",
    status: "draft",
    created_at: "2026-07-20T08:00:00Z",
    published_at: null,
  },
  {
    id: "88888888-8888-4888-8888-888888888888",
    code: "HW-0002",
    title: "平衡搜索树",
    due_at: "2026-07-24T12:00:00Z",
    status: "published",
    created_at: "2026-07-21T08:00:00Z",
    published_at: "2026-07-22T08:00:00Z",
  },
] as const;

const createdAssignment = {
  ...assignments[0],
  question: "说明 Dijkstra 算法的松弛过程。",
  notes: "请给出复杂度。",
  rubric: {
    required_points: ["正确说明松弛条件"],
    grading_notes: "优先检查负权边限制。",
  },
  mattermost_channel_id: null,
  created_by: ids.user,
} as const;

function summary(
  assignmentId: string,
  counts: { pending_evaluation?: number; pending_review?: number; failed?: number } = {},
) {
  return {
    assignment_id: assignmentId,
    total_students: 5,
    submitted_students: 4,
    missing_students: 1,
    latest_submission_at: "2026-07-23T08:30:00Z",
    latest_submission_versions: {},
    pending_evaluation: counts.pending_evaluation ?? 0,
    queued: 0,
    evaluating: 0,
    pending_review: counts.pending_review ?? 0,
    reviewed: 0,
    failed: counts.failed ?? 0,
    limit: 1,
    offset: 0,
    students: [],
  };
}

function authenticateTeacher(token = TOKEN_A) {
  window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
  server.use(
    http.get(`${API_BASE}/auth/me`, ({ request }) =>
      HttpResponse.json(
        request.headers.get("Authorization") === `Bearer ${TOKEN_B}` ? teacherB : teacherA,
      ),
    ),
  );
}

describe("teacher assignment dashboard", () => {
  it("links teachers to student account management", async () => {
    authenticateTeacher();
    server.use(http.get(`${API_BASE}/assignments`, () => HttpResponse.json([])));

    renderApp(["/teacher"]);

    const accountLink = await screen.findByRole("link", { name: "管理学生账号" });
    expect(accountLink).toHaveAttribute(
      "href",
      "/teacher/users",
    );
    expect(accountLink).toHaveClass("teacher-dashboard__account-link");
  });

  it("renders the first 50 assignments, aggregate metrics, status actions, and detail links", async () => {
    authenticateTeacher();
    server.use(
      http.get(`${API_BASE}/assignments`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("limit")).toBe("50");
        expect(url.searchParams.get("offset")).toBe("0");
        return HttpResponse.json(assignments);
      }),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params, request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("limit")).toBe("1");
        expect(url.searchParams.get("offset")).toBe("0");
        return HttpResponse.json(
          params.id === ids.assignment
            ? summary(String(params.id), { pending_evaluation: 2, pending_review: 1 })
            : summary(String(params.id), { pending_evaluation: 1, pending_review: 3, failed: 2 }),
        );
      }),
    );

    renderApp(["/teacher"]);

    expect(await screen.findByRole("heading", { name: "作业发布与评阅" })).toBeInTheDocument();
    expect(await screen.findByText("HW-0001")).toBeInTheDocument();
    expect(screen.getByText("HW-0002")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /图的最短路径/u })).toHaveAttribute(
      "href",
      `/teacher/assignments/${ids.assignment}`,
    );
    expect(screen.getByRole("button", { name: "发布 HW-0001" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭 HW-0002" })).toBeInTheDocument();
    expect(await screen.findByText("3", { selector: "[data-metric='pending-evaluation']" })).toBeInTheDocument();
    expect(screen.getByText("4", { selector: "[data-metric='pending-review']" })).toBeInTheDocument();
    expect(screen.getByText("2", { selector: "[data-metric='failed']" })).toBeInTheDocument();
    expect(screen.getByText("2", { selector: "[data-metric='visible-assignments']" })).toBeInTheDocument();
    expect(screen.getByText("统计覆盖 2 / 2 · 首批最多 50 份作业")).toBeInTheDocument();
  });

  it("shows an explicit loading state without rendering stale assignment rows", async () => {
    authenticateTeacher();
    server.use(http.get(`${API_BASE}/assignments`, () => new Promise(() => undefined)));

    renderApp(["/teacher"]);

    expect(await screen.findByText("正在读取作业登记…")).toBeInTheDocument();
    expect(screen.queryByText("HW-0001")).not.toBeInTheDocument();
  });

  it("shows an empty register with the single create call to action", async () => {
    authenticateTeacher();
    server.use(http.get(`${API_BASE}/assignments`, () => HttpResponse.json([])));

    renderApp(["/teacher"]);

    expect(await screen.findByText("还没有作业记录")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "发布新作业" })).toHaveLength(1);
    await waitFor(() =>
      expect(screen.getByTestId("metric-coverage")).toHaveTextContent(
        "统计覆盖 0 / 0 · 首批最多 50 份作业",
      ),
    );
  });

  it("labels a full 50-row response as the first batch instead of a global total", async () => {
    authenticateTeacher();
    const firstBatch = Array.from({ length: 50 }, (_, index) => ({
      ...assignments[0],
      id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
      code: `HW-${String(index + 1).padStart(4, "0")}`,
      title: `首批作业 ${index + 1}`,
    }));
    server.use(
      http.get(`${API_BASE}/assignments`, () => HttpResponse.json(firstBatch)),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        HttpResponse.json(summary(String(params.id))),
      ),
    );

    renderApp(["/teacher"]);

    expect(await screen.findByText("HW-0050")).toBeInTheDocument();
    const metrics = screen.getByRole("region", { name: "作业统计" });
    expect(within(metrics).getByText("首批作业")).toBeInTheDocument();
    expect(within(metrics).queryByText("作业总数")).not.toBeInTheDocument();
    expect(
      within(metrics).getByText("50", { selector: "[data-metric='visible-assignments']" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("metric-coverage")).toHaveTextContent(
        "统计覆盖 50 / 50 · 首批最多 50 份作业",
      );
    });
  });

  it("recovers a failed list request through an explicit retry", async () => {
    authenticateTeacher();
    let attempts = 0;
    server.use(
      http.get(`${API_BASE}/assignments`, () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json({ detail: "database unavailable" }, { status: 503 })
          : HttpResponse.json([assignments[0]]);
      }),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        HttpResponse.json(summary(String(params.id))),
      ),
    );
    const user = userEvent.setup();

    renderApp(["/teacher"]);

    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取作业列表");
    await user.click(screen.getByRole("button", { name: "重试读取作业" }));
    expect(await screen.findByText("HW-0001")).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it("limits summary fan-out to four concurrent requests and reports partial coverage", async () => {
    authenticateTeacher();
    const manyAssignments = Array.from({ length: 6 }, (_, index) => ({
      ...assignments[0],
      id: `${String(index + 1).padStart(8, "0")}-0000-4000-8000-00000000000${index}`,
      code: `HW-${String(index + 1).padStart(4, "0")}`,
    }));
    let active = 0;
    let maximumActive = 0;
    const releases: Array<() => void> = [];
    const started: string[] = [];
    server.use(
      http.get(`${API_BASE}/assignments`, () => HttpResponse.json(manyAssignments)),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        new Promise((resolve) => {
          active += 1;
          maximumActive = Math.max(maximumActive, active);
          started.push(String(params.id));
          releases.push(() => {
            active -= 1;
            const isFailed = String(params.id) === manyAssignments[5]?.id;
            resolve(
              isFailed
                ? HttpResponse.json({ detail: "summary unavailable" }, { status: 503 })
                : HttpResponse.json(summary(String(params.id), { pending_review: 1 })),
            );
          });
        }),
      ),
    );

    renderApp(["/teacher"]);

    await waitFor(() => expect(started).toHaveLength(4));
    expect(maximumActive).toBe(4);
    act(() => releases.shift()?.());
    await waitFor(() => expect(started).toHaveLength(5));
    act(() => releases.shift()?.());
    await waitFor(() => expect(started).toHaveLength(6));
    while (releases.length > 0) act(() => releases.shift()?.());

    expect(await screen.findByRole("alert")).toHaveTextContent("部分统计暂不可用");
    expect(screen.getByText("统计覆盖 5 / 6 · 首批最多 50 份作业")).toBeInTheDocument();
    expect(screen.getByText("HW-0006")).toBeInTheDocument();
    expect(maximumActive).toBe(4);
  });

  it("never shows teacher A assignment cache while teacher B is loading", async () => {
    authenticateTeacher();
    let releaseTeacherB!: () => void;
    server.use(
      http.get(`${API_BASE}/assignments`, ({ request }) => {
        const authorization = request.headers.get("Authorization");
        if (authorization === `Bearer ${TOKEN_A}`) return HttpResponse.json([assignments[0]]);
        return new Promise((resolve) => {
          releaseTeacherB = () => resolve(HttpResponse.json([{ ...assignments[1], code: "B-ONLY" }]));
        });
      }),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        HttpResponse.json(summary(String(params.id))),
      ),
    );

    renderApp(["/teacher"]);
    expect(await screen.findByText("HW-0001")).toBeInTheDocument();

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN_B);
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ACCESS_TOKEN_STORAGE_KEY,
          oldValue: TOKEN_A,
          newValue: TOKEN_B,
          storageArea: window.localStorage,
        }),
      );
    });
    await waitFor(() => expect(screen.queryByText("HW-0001")).not.toBeInTheDocument());
    expect(await screen.findByText("Barbara Liskov")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("正在读取作业登记…");
    act(() => releaseTeacherB());
    expect(await screen.findByText("B-ONLY")).toBeInTheDocument();
    expect(screen.queryByText("HW-0001")).not.toBeInTheDocument();
  });
});

function emptyDashboard() {
  authenticateTeacher();
  server.use(http.get(`${API_BASE}/assignments`, () => HttpResponse.json([])));
}

async function openCreateDialog(initialEntries: string[] = ["/teacher"]) {
  const user = userEvent.setup();
  const { router } = renderApp(initialEntries);
  const trigger = await screen.findByRole("button", { name: "发布新作业" });
  await user.click(trigger);
  return { user, trigger, router, dialog: screen.getByRole("dialog", { name: "新建作业草稿" }) };
}

async function fillRequiredCreationFields(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("作业标题"), "图的最短路径");
  await user.type(screen.getByLabelText("题目内容"), "说明 Dijkstra 算法的松弛过程。");
  await user.type(screen.getByLabelText("提交截止时间"), "2026-08-01T12:30");
  await user.type(screen.getByLabelText("新增评分要点"), "正确说明松弛条件{Enter}");
}

describe("create assignment dialog", () => {
  it("opens as a labelled modal and focuses the title", async () => {
    emptyDashboard();

    const { dialog } = await openCreateDialog();

    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleDescription("先保存草稿，确认内容后再从作业登记发布。所有字段均不会预填演示数据。");
    expect(document.activeElement).toBe(screen.getByLabelText("作业标题"));
    expect(screen.getByLabelText("作业标题")).toHaveValue("");
    expect(screen.getByLabelText("题目内容")).toHaveValue("");
  });

  it("gives every submitted field a stable name and disables form autofill", async () => {
    emptyDashboard();

    const { dialog } = await openCreateDialog();
    expect(dialog.querySelector("form")).toHaveAttribute("autocomplete", "off");
    const fields = [
      ["作业标题", "title"],
      ["题目内容", "question"],
      ["学生说明", "notes"],
      ["提交截止时间", "due_at"],
      ["新增评分要点", "rubric_point"],
      ["教师评分注意事项", "grading_notes"],
    ] as const;
    for (const [label, name] of fields) {
      expect(screen.getByLabelText(label)).toHaveAttribute("name", name);
      expect(screen.getByLabelText(label)).toHaveAttribute("autocomplete", "off");
    }
  });

  it("associates rubric counts and entry errors with the rubric input", async () => {
    emptyDashboard();
    const { user } = await openCreateDialog();
    const rubricInput = screen.getByLabelText("新增评分要点");
    const counter = screen.getByText("当前项 0 / 500 · 已添加 0 / 20");

    expect(counter).toHaveAttribute("id", "assignment-rubric-count");
    expect(rubricInput).toHaveAttribute("aria-describedby", "assignment-rubric-count");
    expect(rubricInput).toHaveAttribute("aria-invalid", "false");

    fireEvent.change(rubricInput, { target: { value: "🧠".repeat(500) } });
    expect(counter).toHaveTextContent("当前项 500 / 500 · 已添加 0 / 20");
    await user.click(screen.getByRole("button", { name: "添加要点" }));
    expect(counter).toHaveTextContent("当前项 0 / 500 · 已添加 1 / 20");

    fireEvent.change(rubricInput, { target: { value: "🧠".repeat(500) } });
    await user.click(screen.getByRole("button", { name: "添加要点" }));
    const duplicateError = await screen.findByText("评分要点不能重复。");
    expect(duplicateError).toHaveAttribute("id", "assignment-rubric-entry-error");
    expect(rubricInput).toHaveAttribute("aria-invalid", "true");
    expect(rubricInput).toHaveAttribute(
      "aria-describedby",
      "assignment-rubric-count assignment-rubric-entry-error",
    );

    fireEvent.change(rubricInput, { target: { value: "🧠".repeat(501) } });
    await user.click(screen.getByRole("button", { name: "添加要点" }));
    expect(counter).toHaveTextContent("当前项 501 / 500 · 已添加 1 / 20");
    expect(await screen.findByText("每项评分要点不能超过 500 个字符。")).toHaveAttribute(
      "id",
      "assignment-rubric-entry-error",
    );
  });

  it("counts user-authored text by Unicode code point", async () => {
    emptyDashboard();
    await openCreateDialog();

    fireEvent.change(screen.getByLabelText("作业标题"), { target: { value: "🧠" } });
    fireEvent.change(screen.getByLabelText("题目内容"), { target: { value: "🧠" } });
    fireEvent.change(screen.getByLabelText("学生说明"), { target: { value: "🧠" } });
    fireEvent.change(screen.getByLabelText("新增评分要点"), { target: { value: "🧠" } });
    fireEvent.change(screen.getByLabelText("教师评分注意事项"), { target: { value: "🧠" } });

    expect(screen.getByText("1 / 200")).toBeInTheDocument();
    expect(screen.getByText("1 / 50000")).toBeInTheDocument();
    expect(screen.getByText("1 / 10000")).toBeInTheDocument();
    expect(screen.getByText("当前项 1 / 500 · 已添加 0 / 20")).toBeInTheDocument();
    expect(screen.getByText("1 / 5000")).toBeInTheDocument();
  });

  it("accepts Unicode rubric boundaries and trims grading notes in the exact payload", async () => {
    let payload: unknown;
    emptyDashboard();
    server.use(
      http.post(`${API_BASE}/assignments`, async ({ request }) => {
        payload = await request.json();
        return HttpResponse.json(createdAssignment, { status: 201 });
      }),
    );
    const { user } = await openCreateDialog();
    const title = "🧠".repeat(128);
    const point = "✅".repeat(500);
    const gradingNotes = "📝".repeat(5_000);
    fireEvent.change(screen.getByLabelText("作业标题"), { target: { value: title } });
    fireEvent.change(screen.getByLabelText("题目内容"), { target: { value: "有效题目" } });
    fireEvent.change(screen.getByLabelText("提交截止时间"), { target: { value: "2099-08-01T12:30" } });
    fireEvent.change(screen.getByLabelText("新增评分要点"), { target: { value: point } });
    await user.click(screen.getByRole("button", { name: "添加要点" }));
    fireEvent.change(screen.getByLabelText("教师评分注意事项"), {
      target: { value: `  ${gradingNotes}  ` },
    });
    expect(screen.getByText("5000 / 5000")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    await waitFor(() => expect(payload).toBeDefined());
    expect(payload).toMatchObject({
      title,
      rubric: { required_points: [point], grading_notes: gradingNotes },
    });
  });

  it("rejects every user-text field beyond its code-point boundary", async () => {
    emptyDashboard();
    const { user } = await openCreateDialog();
    fireEvent.change(screen.getByLabelText("作业标题"), { target: { value: "a".repeat(201) } });
    fireEvent.change(screen.getByLabelText("题目内容"), { target: { value: "🧠".repeat(50_001) } });
    fireEvent.change(screen.getByLabelText("学生说明"), { target: { value: "🧠".repeat(10_001) } });
    fireEvent.change(screen.getByLabelText("提交截止时间"), { target: { value: "2099-08-01T12:30" } });
    fireEvent.change(screen.getByLabelText("新增评分要点"), { target: { value: "有效要点" } });
    await user.click(screen.getByRole("button", { name: "添加要点" }));
    fireEvent.change(screen.getByLabelText("教师评分注意事项"), { target: { value: "🧠".repeat(5_001) } });
    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    expect(screen.getAllByText("作业标题不能超过 200 个字符。")).toHaveLength(2);
    expect(screen.getAllByText("题目内容不能超过 50000 个字符。")).toHaveLength(2);
    expect(screen.getAllByText("学生说明不能超过 10000 个字符。")).toHaveLength(2);
    expect(screen.getAllByText("教师评分注意事项不能超过 5000 个字符。")).toHaveLength(2);
  });

  it("rejects title and question values beyond the evaluation UTF-8 byte limits", async () => {
    let createCalls = 0;
    emptyDashboard();
    server.use(
      http.post(`${API_BASE}/assignments`, () => {
        createCalls += 1;
        return HttpResponse.json(createdAssignment, { status: 201 });
      }),
    );
    const { user } = await openCreateDialog();
    fireEvent.change(screen.getByLabelText("作业标题"), {
      target: { value: "🧠".repeat(129) },
    });
    fireEvent.change(screen.getByLabelText("题目内容"), {
      target: { value: "题".repeat(21_846) },
    });
    fireEvent.change(screen.getByLabelText("提交截止时间"), {
      target: { value: "2099-08-01T12:30" },
    });
    fireEvent.change(screen.getByLabelText("新增评分要点"), {
      target: { value: "有效要点" },
    });
    await user.click(screen.getByRole("button", { name: "添加要点" }));

    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    expect(screen.getAllByText("作业标题不能超过 512 UTF-8 字节。")).toHaveLength(2);
    expect(screen.getAllByText("题目内容不能超过 65536 UTF-8 字节。")).toHaveLength(2);
    expect(createCalls).toBe(0);
  });

  it("rejects a nonexistent datetime-local value during a DST spring-forward gap", async () => {
    const previousTimezone = process.env.TZ;
    process.env.TZ = "America/New_York";
    let createCalls = 0;
    try {
      emptyDashboard();
      server.use(
        http.post(`${API_BASE}/assignments`, () => {
          createCalls += 1;
          return HttpResponse.json(createdAssignment, { status: 201 });
        }),
      );
      const { user } = await openCreateDialog();
      fireEvent.change(screen.getByLabelText("作业标题"), { target: { value: "DST 边界" } });
      fireEvent.change(screen.getByLabelText("题目内容"), { target: { value: "验证不存在时间" } });
      fireEvent.change(screen.getByLabelText("提交截止时间"), { target: { value: "2027-03-14T02:30" } });
      fireEvent.change(screen.getByLabelText("新增评分要点"), { target: { value: "识别时区跳变" } });
      await user.click(screen.getByRole("button", { name: "添加要点" }));

      await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

      expect(screen.getAllByText("该本地时间因夏令时切换而不存在，请选择其他时间。")).toHaveLength(2);
      expect(screen.getByLabelText("提交截止时间")).toHaveAttribute("aria-invalid", "true");
      expect(createCalls).toBe(0);
    } finally {
      if (previousTimezone === undefined) delete process.env.TZ;
      else process.env.TZ = previousTimezone;
    }
  });

  it("reports every missing requirement and focuses the first invalid field", async () => {
    emptyDashboard();
    const { user } = await openCreateDialog();

    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    const summaryAlert = await screen.findByRole("alert", { name: "请修正以下内容" });
    expect(summaryAlert).toHaveTextContent("作业标题");
    expect(within(summaryAlert).getByText("题目内容必须包含可见文字。"));
    expect(within(summaryAlert).getByText("截止时间必须晚于当前时间。"));
    expect(within(summaryAlert).getByText("至少添加 1 项评分要点。"));
    expect(screen.getByLabelText("作业标题")).toHaveAttribute("aria-invalid", "true");
    expect(document.activeElement).toBe(screen.getByLabelText("作业标题"));
  });

  it("rejects control-only text, duplicate rubric points, and field bounds", async () => {
    emptyDashboard();
    const { user } = await openCreateDialog();
    const title = screen.getByLabelText("作业标题");
    const question = screen.getByLabelText("题目内容");
    const rubricInput = screen.getByLabelText("新增评分要点");

    fireEvent.change(title, { target: { value: "\u0001 \u200B" } });
    fireEvent.change(question, { target: { value: "\u0002 \u200B" } });
    await user.type(rubricInput, "正确性{Enter}");
    await user.type(rubricInput, "  正确性  {Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("评分要点不能重复");

    fireEvent.change(title, { target: { value: "题".repeat(201) } });
    fireEvent.change(question, { target: { value: "问".repeat(50_001) } });
    fireEvent.change(screen.getByLabelText("学生说明"), { target: { value: "说".repeat(10_001) } });
    fireEvent.change(screen.getByLabelText("教师评分注意事项"), { target: { value: "注".repeat(5_001) } });
    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    expect(screen.getAllByText("作业标题不能超过 200 个字符。")).toHaveLength(2);
    expect(screen.getAllByText("题目内容不能超过 50000 个字符。")).toHaveLength(2);
    expect(screen.getAllByText("学生说明不能超过 10000 个字符。")).toHaveLength(2);
    expect(screen.getAllByText("教师评分注意事项不能超过 5000 个字符。")).toHaveLength(2);
  });

  it("sends an exact timezone-aware payload and announces the saved draft", async () => {
    let payload: unknown;
    let list = [] as unknown[];
    authenticateTeacher();
    server.use(
      http.get(`${API_BASE}/assignments`, () => HttpResponse.json(list)),
      http.post(`${API_BASE}/assignments`, async ({ request }) => {
        payload = await request.json();
        list = [assignments[0]];
        return HttpResponse.json(createdAssignment, { status: 201 });
      }),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        HttpResponse.json(summary(String(params.id))),
      ),
    );
    const { user } = await openCreateDialog();
    await fillRequiredCreationFields(user);
    await user.type(screen.getByLabelText("学生说明"), "请给出复杂度。");
    await user.type(screen.getByLabelText("教师评分注意事项"), "优先检查负权边限制。");

    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    expect(await screen.findByRole("status")).toHaveTextContent("草稿已保存，可在作业登记中确认后发布");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText("HW-0001")).toBeInTheDocument();
    expect(payload).toEqual({
      title: "图的最短路径",
      question: "说明 Dijkstra 算法的松弛过程。",
      notes: "请给出复杂度。",
      due_at: new Date("2026-08-01T12:30").toISOString(),
      rubric: {
        required_points: ["正确说明松弛条件"],
        grading_notes: "优先检查负权边限制。",
      },
    });
    const afterSave = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(afterSave)).toBe(true);
    expect(afterSave.defaultPrevented).toBe(false);
  });

  it.each([
    [401, "登录状态已失效，请重新登录。"],
    [409, "作业状态已变化，请刷新后重试。"],
    [422, "作业内容未通过服务器校验，请检查后重试。"],
    [500, "服务器暂时无法保存作业草稿，请稍后重试。"],
  ])("maps a create %s response to safe Chinese guidance", async (status, message) => {
    emptyDashboard();
    server.use(
      http.post(`${API_BASE}/assignments`, () =>
        HttpResponse.json({ detail: "secret database/provider detail" }, { status }),
      ),
    );
    const { user } = await openCreateDialog();
    await fillRequiredCreationFields(user);

    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).not.toHaveTextContent(/secret|database|provider/u);
  });

  it("maps a create network failure without losing entered content", async () => {
    emptyDashboard();
    server.use(http.post(`${API_BASE}/assignments`, () => HttpResponse.error()));
    const { user } = await openCreateDialog();
    await fillRequiredCreationFields(user);

    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("无法连接服务器，请检查网络后重试。");
    expect(screen.getByLabelText("作业标题")).toHaveValue("图的最短路径");
  });

  it("pins authentication and coalesces duplicate create submissions", async () => {
    emptyDashboard();
    let calls = 0;
    let authorization: string | null = null;
    let release!: () => void;
    server.use(
      http.post(`${API_BASE}/assignments`, ({ request }) => {
        calls += 1;
        authorization = request.headers.get("Authorization");
        return new Promise((resolve) => {
          release = () => resolve(HttpResponse.json(createdAssignment, { status: 201 }));
        });
      }),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        HttpResponse.json(summary(String(params.id))),
      ),
    );
    const { user } = await openCreateDialog();
    await fillRequiredCreationFields(user);
    const submit = screen.getByRole("button", { name: "保存作业草稿" });

    await user.dblClick(submit);
    await waitFor(() => expect(calls).toBe(1));
    expect(submit).toBeDisabled();
    expect(authorization).toBe(`Bearer ${TOKEN_A}`);
    act(() => release());
    expect(await screen.findByRole("status")).toHaveTextContent("草稿已保存");
  });

  it("keeps focus inside the dialog and restores the trigger after Escape", async () => {
    emptyDashboard();
    const { user, trigger } = await openCreateDialog();
    const close = screen.getByRole("button", { name: "关闭新作业表单" });
    const submit = screen.getByRole("button", { name: "保存作业草稿" });

    submit.focus();
    await user.tab();
    await waitFor(() => expect(document.activeElement).toBe(close));
    close.focus();
    await user.tab({ shift: true });
    await waitFor(() => expect(document.activeElement).toBe(submit));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it("prevents beforeunload while dirty and removes the guard after explicit discard", async () => {
    emptyDashboard();
    const { user } = await openCreateDialog();
    await user.type(screen.getByLabelText("作业标题"), "未保存草稿");

    const whileDirty = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(whileDirty)).toBe(false);
    expect(whileDirty.defaultPrevented).toBe(true);
    expect(screen.getByLabelText("作业标题")).toHaveValue("未保存草稿");

    await user.click(screen.getByRole("button", { name: "关闭新作业表单" }));
    await user.click(screen.getByRole("button", { name: "放弃未保存内容" }));
    const afterDiscard = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(afterDiscard)).toBe(true);
    expect(afterDiscard.defaultPrevented).toBe(false);
  });

  it("blocks internal navigation, preserves the draft on cancel, and proceeds after discard", async () => {
    emptyDashboard();
    const { user, router, dialog } = await openCreateDialog();
    const title = screen.getByLabelText("作业标题");
    await user.type(title, "路由保护草稿");

    act(() => void router.navigate(`/teacher/assignments/${ids.assignment}`));
    const prompt = await screen.findByRole("alertdialog", { name: "放弃未保存内容？" });
    expect(prompt).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/teacher");
    expect(dialog.querySelector("form")).toHaveAttribute("inert");
    expect(dialog.querySelector("form")).toHaveAttribute("aria-hidden", "true");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "继续编辑" }));

    await user.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "放弃未保存内容" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "继续编辑" }));
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/teacher");
    expect(title).toHaveValue("路由保护草稿");
    expect(document.activeElement).toBe(title);

    act(() => void router.navigate(`/teacher/assignments/${ids.assignment}`));
    await user.click(await screen.findByRole("button", { name: "放弃未保存内容" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/teacher/assignments/${ids.assignment}`));
    expect(screen.queryByRole("dialog", { name: "新建作业草稿" })).not.toBeInTheDocument();
  });

  it("blocks browser-history back navigation without losing the draft", async () => {
    emptyDashboard();
    const previousPath = `/teacher/assignments/${ids.assignment}`;
    const { user, router } = await openCreateDialog([previousPath, "/teacher"]);
    await user.type(screen.getByLabelText("作业标题"), "返回保护草稿");

    act(() => void router.navigate(-1));

    expect(await screen.findByRole("alertdialog", { name: "放弃未保存内容？" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/teacher");
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByLabelText("作业标题")).toHaveValue("返回保护草稿");
    expect(router.state.location.pathname).toBe("/teacher");
  });

  it("isolates an existing field-error summary while discard confirmation is open", async () => {
    emptyDashboard();
    const { user, dialog } = await openCreateDialog();
    await user.type(screen.getByLabelText("作业标题"), "保留错误摘要");
    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));
    const fieldErrors = await screen.findByRole("alert", { name: "请修正以下内容" });
    expect(fieldErrors).toHaveTextContent("题目内容必须包含可见文字");

    await user.click(screen.getByRole("button", { name: "关闭新作业表单" }));

    expect(screen.getByRole("alertdialog", { name: "放弃未保存内容？" })).toBeInTheDocument();
    const background = dialog.querySelector(".assignment-dialog__content");
    expect(background).toHaveAttribute("inert");
    expect(background).toHaveAttribute("aria-hidden", "true");
    expect(within(dialog).queryByRole("alert", { name: "请修正以下内容" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "关闭新作业表单" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByRole("alert", { name: "请修正以下内容" })).toBe(fieldErrors);
    expect(screen.getByLabelText("作业标题")).toHaveValue("保留错误摘要");
  });

  it("isolates and restores an existing server error around discard confirmation", async () => {
    emptyDashboard();
    server.use(
      http.post(`${API_BASE}/assignments`, () =>
        HttpResponse.json({ detail: "provider secret" }, { status: 500 }),
      ),
    );
    const { user, dialog } = await openCreateDialog();
    await fillRequiredCreationFields(user);
    await user.click(screen.getByRole("button", { name: "保存作业草稿" }));
    const serverAlert = await screen.findByRole("alert");
    expect(serverAlert).toHaveTextContent("服务器暂时无法保存作业草稿");

    await user.click(screen.getByRole("button", { name: "关闭新作业表单" }));

    expect(screen.getByRole("alertdialog", { name: "放弃未保存内容？" })).toBeInTheDocument();
    expect(dialog.querySelector(".assignment-dialog__content")).toHaveAttribute("aria-hidden", "true");
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByRole("alert")).toBe(serverAlert);
    expect(screen.getByRole("alert")).toHaveTextContent("服务器暂时无法保存作业草稿");
  });

  it("requires an explicit confirmation before discarding dirty content", async () => {
    emptyDashboard();
    const { user } = await openCreateDialog();
    await user.type(screen.getByLabelText("作业标题"), "未完成草稿");

    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const prompt = screen.getByRole("alertdialog", { name: "放弃未保存内容？" });
    expect(prompt).toHaveTextContent("有尚未保存的内容");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "继续编辑" }));
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByLabelText("作业标题")).toHaveValue("未完成草稿");
    const close = screen.getByRole("button", { name: "关闭新作业表单" });
    await user.click(close);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "继续编辑" }));
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(document.activeElement).toBe(close);
    await user.click(close);
    await user.click(screen.getByRole("button", { name: "放弃未保存内容" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("assignment lifecycle actions", () => {
  const closedId = "99999999-9999-4999-8999-999999999999";
  const archivedId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

  function lifecycleAssignment(
    id: string,
    code: string,
    status: AssignmentListItem["status"],
    dueAt = "2026-08-02T12:00:00Z",
  ): AssignmentListItem {
    return {
      id,
      code,
      title: `${code} 生命周期测试`,
      due_at: dueAt,
      status,
      created_at: "2026-07-20T08:00:00Z",
      published_at: status === "draft" ? null : "2026-07-21T08:00:00Z",
    };
  }

  function lifecycleRead(item: AssignmentListItem) {
    return {
      ...item,
      question: "验证状态流转。",
      notes: "按要求提交。",
      rubric: { required_points: ["状态正确"], grading_notes: "" },
      mattermost_channel_id: null,
      created_by: ids.user,
    };
  }

  function serveLifecycle(items: AssignmentListItem[]) {
    server.use(
      http.get(`${API_BASE}/assignments`, () => HttpResponse.json(items)),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        HttpResponse.json(summary(String(params.id))),
      ),
    );
  }

  it("publishes, closes, and reopens through the exact lifecycle endpoints", async () => {
    authenticateTeacher();
    const draft = lifecycleAssignment(ids.assignment, "HW-0101", "draft");
    const published = lifecycleAssignment(ids.submission, "HW-0102", "published");
    const closed = lifecycleAssignment(closedId, "HW-0103", "closed");
    let items = [draft, published, closed];
    const calls: string[] = [];
    server.use(
      http.get(`${API_BASE}/assignments`, () => HttpResponse.json(items)),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ params }) =>
        HttpResponse.json(summary(String(params.id))),
      ),
      http.post(`${API_BASE}/assignments/:id/:operation`, ({ params, request }) => {
        expect(request.headers.get("Authorization")).toBe(`Bearer ${TOKEN_A}`);
        const operation = String(params.operation);
        const nextStatus = operation === "close" ? "closed" : "published";
        calls.push(`${params.id}/${operation}`);
        items = items.map((item) =>
          item.id === params.id
            ? {
                ...item,
                status: nextStatus,
                published_at:
                  operation === "publish" ? "2026-07-27T01:00:00Z" : item.published_at,
              }
            : item,
        );
        const changed = items.find((item) => item.id === params.id);
        return HttpResponse.json(lifecycleRead(changed as AssignmentListItem));
      }),
    );
    const user = userEvent.setup();
    renderApp(["/teacher"]);

    await user.click(await screen.findByRole("button", { name: "发布 HW-0101" }));
    expect(await screen.findByRole("button", { name: "关闭 HW-0101" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭 HW-0102" }));
    expect(await screen.findByRole("button", { name: "重新开放 HW-0102" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新开放 HW-0103" }));
    expect(await screen.findByRole("button", { name: "关闭 HW-0103" })).toBeInTheDocument();
    expect(calls).toEqual([
      `${draft.id}/publish`,
      `${published.id}/close`,
      `${closed.id}/reopen`,
    ]);
  });

  it("coalesces a double publish and only disables the exact pending row", async () => {
    authenticateTeacher();
    const draft = lifecycleAssignment(ids.assignment, "HW-0201", "draft");
    const published = lifecycleAssignment(ids.submission, "HW-0202", "published");
    serveLifecycle([draft, published]);
    let calls = 0;
    let release!: () => void;
    server.use(
      http.post(`${API_BASE}/assignments/${draft.id}/publish`, () => {
        calls += 1;
        return new Promise((resolve) => {
          release = () => resolve(HttpResponse.json(lifecycleRead({
            ...draft,
            status: "published",
            published_at: "2026-07-27T01:00:00Z",
          })));
        });
      }),
    );
    const user = userEvent.setup();
    renderApp(["/teacher"]);

    const publish = await screen.findByRole("button", { name: "发布 HW-0201" });
    await user.dblClick(publish);
    await waitFor(() => expect(calls).toBe(1));
    expect(screen.getByRole("button", { name: "正在发布 HW-0201" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "关闭 HW-0202" })).toBeEnabled();
    act(() => release());
    expect(await screen.findByRole("status")).toHaveTextContent("HW-0201 已发布");
  });

  it("shows a safe 409 conflict and restores the action", async () => {
    authenticateTeacher();
    const draft = lifecycleAssignment(ids.assignment, "HW-0301", "draft");
    serveLifecycle([draft]);
    server.use(
      http.post(`${API_BASE}/assignments/${draft.id}/publish`, () =>
        HttpResponse.json({ detail: "internal row version=98 secret" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderApp(["/teacher"]);

    await user.click(await screen.findByRole("button", { name: "发布 HW-0301" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("作业状态已变化，请刷新列表后重试");
    expect(alert).not.toHaveTextContent("version=98");
    expect(screen.getByRole("button", { name: "发布 HW-0301" })).toBeEnabled();
  });

  it("prevents deadline-invalid publish and reopen transitions", async () => {
    authenticateTeacher();
    serveLifecycle([
      lifecycleAssignment(ids.assignment, "HW-0401", "draft", "2026-07-01T12:00:00Z"),
      lifecycleAssignment(closedId, "HW-0402", "closed", "2026-07-01T12:00:00Z"),
    ]);
    renderApp(["/teacher"]);

    expect(await screen.findByRole("button", { name: "发布 HW-0401" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新开放 HW-0402" })).toBeDisabled();
    expect(screen.getAllByText("截止时间已过，无法执行此操作")).toHaveLength(2);
  });

  it("keeps archived assignments read-only without lifecycle buttons", async () => {
    authenticateTeacher();
    serveLifecycle([lifecycleAssignment(archivedId, "HW-0501", "archived")]);
    renderApp(["/teacher"]);

    expect(await screen.findByText("只读记录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /HW-0501/u })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /HW-0501 生命周期测试/u })).toHaveAttribute(
      "href",
      `/teacher/assignments/${archivedId}`,
    );
  });
});
