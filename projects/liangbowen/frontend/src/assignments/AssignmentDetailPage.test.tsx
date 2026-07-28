import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ACCESS_TOKEN_STORAGE_KEY } from "../shared/api/client";
import type { AssignmentSummaryStudent } from "../shared/api/schemas";
import { ids } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import { classifySubmission } from "./submissionStatus";

const API_BASE = "http://localhost:8000/api/v1";
const TOKEN = "teacher.detail.jwt";
const teacher = {
  id: ids.user,
  username: "lin",
  display_name: "林老师",
  role: "teacher" as const,
};
const assignment = {
  id: ids.assignment,
  code: "DS-2026-07",
  title: "图结构综合练习",
  question: "完成图的遍历与最短路径分析。",
  notes: "说明复杂度。",
  rubric: { required_points: ["遍历", "复杂度"], grading_notes: "关注边界。" },
  due_at: "2026-07-30T12:00:00Z",
  status: "published" as const,
  mattermost_channel_id: null,
  created_by: ids.user,
  created_at: "2026-07-20T08:00:00Z",
  published_at: "2026-07-21T08:00:00Z",
};

function row(
  index: number,
  overrides: Partial<AssignmentSummaryStudent> = {},
): AssignmentSummaryStudent {
  const studentId = `10000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
  const submissionId = `20000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
  return {
    student_id: studentId,
    username: `student-${index}`,
    display_name: `学生${index === 1 ? "甲" : index === 2 ? "乙" : "丙"}`,
    latest_submission: {
      id: submissionId,
      assignment_id: ids.assignment,
      student_id: studentId,
      version: 1,
      content_type: "text",
      content_text: "答案",
      content_json: null,
      status: "submitted",
      submitted_at: "2026-07-22T08:30:00Z",
      source: "web",
    },
    latest_version: 1,
    submitted_at: "2026-07-22T08:30:00Z",
    evaluation_status: null,
    evaluation_error_code: null,
    report_status: null,
    latest_report_id: null,
    score: null,
    grade: null,
    evaluation_error: null,
    ...overrides,
  };
}

function summary(
  students: AssignmentSummaryStudent[] = [
    row(1, { evaluation_status: "succeeded", report_status: "proposed", latest_report_id: ids.report, score: 82, grade: "B" }),
    row(2, { evaluation_status: "failed", evaluation_error_code: "timeout", evaluation_error: "provider api_key=secret-value" }),
    row(3, { latest_submission: null, latest_version: null, submitted_at: null }),
  ],
  overrides: Record<string, unknown> = {},
) {
  return {
    assignment_id: ids.assignment,
    total_students: 3,
    submitted_students: 2,
    missing_students: 1,
    latest_submission_at: "2026-07-22T08:30:00Z",
    latest_submission_versions: {},
    pending_evaluation: 0,
    queued: 0,
    evaluating: 0,
    pending_review: 1,
    reviewed: 0,
    failed: 1,
    limit: 100,
    offset: 0,
    students,
    ...overrides,
  };
}

function authenticate() {
  window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
  server.use(
    http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)),
    http.get(`${API_BASE}/assignments/:id`, () => HttpResponse.json(assignment)),
  );
}

afterEach(() => vi.useRealTimers());

describe("teacher assignment detail", () => {
  it("shows assignment content, rubric, 2 / 3 progress, five counters, and honest row states", async () => {
    authenticate();
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("limit")).toBe("100");
        expect(url.searchParams.get("offset")).toBe("0");
        return HttpResponse.json(summary());
      }),
    );

    renderApp([`/teacher/assignments/${ids.assignment}`]);

    expect(await screen.findByRole("heading", { name: assignment.title }, { timeout: 3_000 })).toBeInTheDocument();
    expect(screen.getByText("2 / 3 已提交")).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`^${assignment.code}`))).toBeInTheDocument();
    expect(screen.getByText(assignment.question)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "评分要点" })).toBeInTheDocument();
    expect(screen.getByText("遍历")).toBeInTheDocument();
    expect(screen.getByText("复杂度")).toBeInTheDocument();
    expect(screen.getByText("关注边界。")).toBeInTheDocument();
    const progress = screen.getByRole("region", { name: "评估进度" });
    for (const label of ["待评估", "评估中", "待审核", "已审核", "失败"]) {
      expect(within(progress).getByText(label)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: new RegExp(`^${label}`) })).toBeInTheDocument();
    }
    expect(screen.getByText("学生甲")).toBeInTheDocument();
    expect(screen.getByText("尚未提交")).toBeInTheDocument();
    expect(screen.queryByText(/secret-value/u)).not.toBeInTheDocument();
    expect(screen.getByText("评估服务响应超时，请重新发起评估。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回作业台" })).toHaveAttribute("href", "/teacher");
  });

  it("falls back safely for an unknown rubric shape without rendering raw values", async () => {
    authenticate();
    server.use(
      http.get(`${API_BASE}/assignments/:id`, () => HttpResponse.json({
        ...assignment,
        rubric: {
          required_points: "raw-required-secret",
          grading_notes: { raw: "raw-grading-secret" },
          internal_solution: "raw-solution-secret",
        },
      })),
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())),
    );
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText("未提供结构化评分要点")).toBeInTheDocument();
    expect(screen.getByText("未提供教师评分说明")).toBeInTheDocument();
    expect(screen.queryByText(/raw-required-secret|raw-grading-secret|raw-solution-secret/u)).not.toBeInTheDocument();
  });

  it.each([
    {
      label: "500-code-point rubric item",
      rubric: { required_points: [`  ${"🧠".repeat(500)}  `], grading_notes: "说明" },
      visible: "🧠".repeat(500),
      fallback: null,
    },
    {
      label: "501-code-point rubric item",
      rubric: { required_points: ["🧠".repeat(501)], grading_notes: "说明" },
      visible: "🧠".repeat(501),
      fallback: "points",
    },
    {
      label: "5000-code-point grading notes",
      rubric: { required_points: ["要点"], grading_notes: `  ${"📝".repeat(5_000)}  ` },
      visible: "📝".repeat(5_000),
      fallback: null,
    },
    {
      label: "5001-code-point grading notes",
      rubric: { required_points: ["要点"], grading_notes: "📝".repeat(5_001) },
      visible: "📝".repeat(5_001),
      fallback: "notes",
    },
    {
      label: "20 rubric items",
      rubric: { required_points: Array.from({ length: 20 }, (_, index) => `边界要点-${index + 1}`), grading_notes: "说明" },
      visible: "边界要点-20",
      fallback: null,
    },
    {
      label: "21 rubric items",
      rubric: { required_points: Array.from({ length: 21 }, (_, index) => `超限要点-${index + 1}`), grading_notes: "说明" },
      visible: "超限要点-21",
      fallback: "points",
    },
  ])("applies the creation contract to $label", async ({ rubric, visible, fallback }) => {
    authenticate();
    server.use(
      http.get(`${API_BASE}/assignments/:id`, () => HttpResponse.json({ ...assignment, rubric })),
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())),
    );
    renderApp([`/teacher/assignments/${ids.assignment}`]);

    await screen.findByRole("heading", { name: assignment.title });
    if (fallback !== null) {
      expect(screen.getByText(fallback === "points"
        ? "未提供结构化评分要点"
        : "未提供教师评分说明")).toBeInTheDocument();
      expect(screen.queryByText(visible)).not.toBeInTheDocument();
    } else {
      expect(screen.getByText(visible)).toBeInTheDocument();
    }
  });

  it("opens a report only through the latest report id", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())));
    const user = userEvent.setup();
    const { router } = renderApp([`/teacher/assignments/${ids.assignment}`]);
    const link = await screen.findByRole("link", { name: "打开报告" });
    expect(link).toHaveAttribute("href", `/teacher/reports/${ids.report}`);
    expect(link).not.toHaveAttribute("href", `/teacher/reports/20000000-0000-4000-8000-000000000001`);
    await user.click(link);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/teacher/reports/${ids.report}`));
  });

  it("does not create a report link when the summary has no valid report id", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary([
      row(1, { evaluation_status: "succeeded", report_status: "proposed", latest_report_id: null }),
    ], { total_students: 1, submitted_students: 1, missing_students: 0, pending_review: 1, failed: 0 }))));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    await screen.findByText("学生甲");
    expect(screen.queryByRole("link", { name: "打开报告" })).not.toBeInTheDocument();
  });

  it("matches backend classification for queued, running, reports, failed, and succeeded-without-report edges", () => {
    expect(classifySubmission(row(1, { latest_submission: null }))).toBe("missing");
    expect(classifySubmission(row(1, { evaluation_status: "queued" }))).toBe("pending");
    expect(classifySubmission(row(1, { evaluation_status: "running" }))).toBe("running");
    expect(classifySubmission(row(1, { evaluation_status: "failed", report_status: "confirmed" }))).toBe("failed");
    expect(classifySubmission(row(1, { evaluation_status: "succeeded", report_status: "proposed" }))).toBe("review");
    expect(classifySubmission(row(1, { evaluation_status: "cancelled", report_status: "modified" }))).toBe("reviewed");
    expect(classifySubmission(row(1, { evaluation_status: "succeeded", report_status: null }))).toBe("pending");
    expect(classifySubmission(row(1, { evaluation_status: null, report_status: "superseded" }))).toBe("pending");
  });

  it("applies every evaluation filter to the current page", async () => {
    authenticate();
    const students = [
      row(1, { display_name: "等候者", evaluation_status: "queued" }),
      row(2, { display_name: "运行者", evaluation_status: "running" }),
      row(3, { display_name: "待审者", evaluation_status: "succeeded", report_status: "proposed" }),
      row(4, { display_name: "已审者", evaluation_status: "succeeded", report_status: "modified" }),
      row(5, { display_name: "失败者", evaluation_status: "failed" }),
    ];
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary(students, {
      total_students: 5, submitted_students: 5, missing_students: 0,
      pending_evaluation: 1, evaluating: 1, pending_review: 1, reviewed: 1, failed: 1,
    }))));
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    await screen.findByText("等候者");

    for (const [buttonName, visibleName] of [
      ["待评估 1", "等候者"], ["评估中 1", "运行者"], ["待审核 1", "待审者"],
      ["已审核 1", "已审者"], ["失败 1", "失败者"],
    ] as const) {
      await user.click(screen.getByRole("button", { name: buttonName }));
      expect(screen.getByText(visibleName)).toBeInTheDocument();
      expect(screen.queryByText(visibleName === "等候者" ? "运行者" : "等候者")).not.toBeInTheDocument();
    }
  });

  it("filters only the current page and paginates with user-scoped offsets", async () => {
    authenticate();
    const offsets: string[] = [];
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, ({ request }) => {
        const offset = new URL(request.url).searchParams.get("offset") ?? "0";
        offsets.push(offset);
        return HttpResponse.json(
          offset === "0"
            ? summary([row(1, { evaluation_status: "running" })], { total_students: 101, submitted_students: 100, missing_students: 1 })
            : summary([row(2, { evaluation_status: "succeeded", report_status: "confirmed", score: 95, grade: "A" })], { total_students: 101, submitted_students: 100, missing_students: 1, offset: 100, reviewed: 1 }),
        );
      }),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}`]);

    expect(await screen.findByText("当前显示 1–1 / 101 名学生")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "已审核 0" }));
    expect(screen.getByText("当前页没有“已审核”学生")).toBeInTheDocument();
    expect(screen.getByText("筛选仅作用于当前页")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("当前显示 101–101 / 101 名学生")).toBeInTheDocument();
    expect(offsets).toContain("100");
  });

  it("prevents duplicate bulk evaluation and reports queued and skipped counts honestly", async () => {
    authenticate();
    let calls = 0;
    let release!: () => void;
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())),
      http.post(`${API_BASE}/assignments/:id/evaluations`, () => {
        calls += 1;
        return new Promise((resolve) => {
          release = () => resolve(HttpResponse.json({
            batch_id: "30000000-0000-4000-8000-000000000001",
            queued: 2,
            skipped: 1,
            job_ids: [ids.job, "40000000-0000-4000-8000-000000000002"],
          }, { status: 202 }));
        });
      }),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    const button = await screen.findByRole("button", { name: "评估全部最新提交" });

    await user.click(button);
    await user.click(button);
    expect(button).toBeDisabled();
    expect(calls).toBe(1);
    act(() => release());
    expect(await screen.findByText("已加入 2 个评估任务；另有 1 份已跳过。")).toBeInTheDocument();
  });

  it("reports all-skipped and failed bulk requests without leaking server detail", async () => {
    authenticate();
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())),
      http.post(`${API_BASE}/assignments/:id/evaluations`, () => HttpResponse.json({
        batch_id: "30000000-0000-4000-8000-000000000001", queued: 0, skipped: 2, job_ids: [],
      }, { status: 202 })),
    );
    const user = userEvent.setup();
    const skipped = renderApp([`/teacher/assignments/${ids.assignment}`]);
    await user.click(await screen.findByRole("button", { name: "评估全部最新提交" }));
    expect(await screen.findByText("未新增评估任务；2 份已跳过。")).toBeInTheDocument();
    skipped.unmount();

    server.use(http.post(`${API_BASE}/assignments/:id/evaluations`, () => HttpResponse.json({ detail: "provider_key=do-not-show" }, { status: 503 })));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    await user.click(await screen.findByRole("button", { name: "评估全部最新提交" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法发起评估");
    expect(screen.queryByText(/do-not-show/u)).not.toBeInTheDocument();
  });

  it("waits for the refreshed summary before announcing a queued batch", async () => {
    authenticate();
    let summaryCalls = 0;
    let releaseRefresh!: () => void;
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => {
        summaryCalls += 1;
        if (summaryCalls === 1) return HttpResponse.json(summary());
        return new Promise((resolve) => {
          releaseRefresh = () => resolve(HttpResponse.json(summary([], {
            total_students: 3, submitted_students: 2, missing_students: 1,
            queued: 1, pending_evaluation: 1, pending_review: 0, failed: 0,
          })));
        });
      }),
      http.post(`${API_BASE}/assignments/:id/evaluations`, () => HttpResponse.json({
        batch_id: "30000000-0000-4000-8000-000000000001",
        queued: 1,
        skipped: 0,
        job_ids: [ids.job],
      }, { status: 202 })),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}`]);

    await user.click(await screen.findByRole("button", { name: "评估全部最新提交" }));
    await waitFor(() => expect(summaryCalls).toBe(2));
    expect(screen.queryByText("已加入 1 个评估任务。")).not.toBeInTheDocument();
    act(() => releaseRefresh());
    expect(await screen.findByText("已加入 1 个评估任务。")).toBeInTheDocument();
  });

  it("does not start polling when a bulk request skips every submission", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    authenticate();
    let summaryCalls = 0;
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => {
        summaryCalls += 1;
        return HttpResponse.json(summary());
      }),
      http.post(`${API_BASE}/assignments/:id/evaluations`, () => HttpResponse.json({
        batch_id: "30000000-0000-4000-8000-000000000001",
        queued: 0,
        skipped: 2,
        job_ids: [],
      }, { status: 202 })),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderApp([`/teacher/assignments/${ids.assignment}`]);

    await user.click(await screen.findByRole("button", { name: "评估全部最新提交" }));
    expect(await screen.findByText("未新增评估任务；2 份已跳过。")).toBeInTheDocument();
    await waitFor(() => expect(summaryCalls).toBe(2));
    await act(() => vi.advanceTimersByTimeAsync(4_100));
    expect(summaryCalls).toBe(2);
  });

  it("retries a failed row independently and never exposes backend error detail", async () => {
    authenticate();
    let calls = 0;
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())),
      http.post(`${API_BASE}/submissions/:id/evaluations`, async ({ params, request }) => {
        calls += 1;
        expect(await request.json()).toEqual({ reason: "provider_retry" });
        return HttpResponse.json({
          id: ids.job,
          submission_id: params.id,
          requested_by: ids.user,
          reason: "provider_retry",
          status: "queued",
          attempt_count: 0,
          provider: "mock",
          model: "deterministic-v1",
          error_code: null,
          error_message: null,
          queued_at: "2026-07-22T08:31:00Z",
          started_at: null,
          finished_at: null,
        }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}`]);

    await user.click(await screen.findByRole("button", { name: "重新评估 学生乙" }));
    expect(await screen.findByText("学生乙已重新加入评估队列。")).toBeInTheDocument();
    expect(calls).toBe(1);
    expect(screen.queryByText(/api_key|secret-value/u)).not.toBeInTheDocument();
  });

  it("reports an idempotently returned terminal retry without claiming it was queued", async () => {
    authenticate();
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())),
      http.post(`${API_BASE}/submissions/:id/evaluations`, ({ params }) => HttpResponse.json({
        id: ids.job, submission_id: params.id, requested_by: ids.user, reason: "provider_retry",
        status: "failed", attempt_count: 1, provider: "mock", model: "deterministic-v1",
        error_code: "network", error_message: "provider private detail",
        queued_at: "2026-07-22T08:31:00Z", started_at: "2026-07-22T08:31:01Z",
        finished_at: "2026-07-22T08:31:02Z",
      }, { status: 202 })),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    await user.click(await screen.findByRole("button", { name: "重新评估 学生乙" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("已有重试任务处于失败状态，未重新加入队列");
    expect(screen.queryByText(/provider private detail/u)).not.toBeInTheDocument();
  });

  it.each([
    ["configuration", "评估服务配置不可用，请联系管理员。"],
    ["timeout", "评估服务响应超时，请重新发起评估。"],
    ["network", "评估服务网络连接失败，请重新发起评估。"],
    ["authentication", "评估服务认证失败，请联系管理员。"],
    ["rate_limited", "评估请求过于频繁，请稍后重新发起评估。"],
    ["upstream", "评估服务暂时不可用，请稍后重新发起评估。"],
    ["protocol", "评估服务返回了无效结果，请重新发起评估。"],
    ["response_too_large", "评估结果过大，无法处理，请联系管理员。"],
    ["fixture", "评估测试数据无效，请联系管理员。"],
    ["validation_exhausted", "评估结果无法通过校验，请重新发起评估。"],
    ["internal_error", "评估服务发生内部错误，请重新发起评估。"],
  ] as const)("maps backend failure code %s to safe copy", async (code, message) => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary([
      row(1, { evaluation_status: "failed", evaluation_error_code: code }),
    ], { total_students: 1, submitted_students: 1, missing_students: 0, pending_review: 0, failed: 1 }))));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it("hides unknown and raw provider failure details", async () => {
    authenticate();
    const students = [row(1, {
      evaluation_status: "failed",
      evaluation_error_code: "new_private_code",
      evaluation_error: "private-provider-detail",
    })];
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary(students, {
      total_students: students.length,
      submitted_students: students.length,
      missing_students: 0,
      pending_review: 0,
      failed: students.length,
    }))));
    renderApp([`/teacher/assignments/${ids.assignment}`]);

    await screen.findByText("学生甲");
    expect(screen.getByText("评估未完成，请重新发起评估。")).toBeInTheDocument();
    expect(screen.queryByText(/private-provider-detail/u)).not.toBeInTheDocument();
  });

  it("isolates pending state between two row retries", async () => {
    authenticate();
    let release!: () => void;
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary([
        row(1, { evaluation_status: "failed" }), row(2, { evaluation_status: "failed" }),
      ], { total_students: 2, submitted_students: 2, missing_students: 0, pending_review: 0, failed: 2 }))),
      http.post(`${API_BASE}/submissions/:id/evaluations`, ({ params }) => new Promise((resolve) => {
        release = () => resolve(HttpResponse.json({
          id: ids.job, submission_id: params.id, requested_by: ids.user, reason: "provider_retry",
          status: "queued", attempt_count: 0, provider: "mock", model: "deterministic-v1",
          error_code: null, error_message: null, queued_at: "2026-07-22T08:31:00Z",
          started_at: null, finished_at: null,
        }, { status: 202 }));
      })),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    const first = await screen.findByRole("button", { name: "重新评估 学生甲" });
    const second = screen.getByRole("button", { name: "重新评估 学生乙" });
    await user.click(first);
    expect(first).toBeDisabled();
    expect(second).toBeEnabled();
    act(() => release());
    await waitFor(() => expect(first).toBeEnabled());
  });

  it("polls queued/running rows every two seconds and stops after a terminal summary", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    authenticate();
    let calls = 0;
    server.use(
      http.get(`${API_BASE}/assignments/:id/summary`, () => {
        calls += 1;
        return HttpResponse.json(
          calls === 1
            ? summary([row(1, { evaluation_status: "queued" })], { pending_evaluation: 1, queued: 1, pending_review: 0, failed: 0 })
            : summary([row(1, { evaluation_status: "succeeded", report_status: "proposed", score: 82, grade: "B" })], { pending_review: 1, failed: 0 }),
        );
      }),
    );
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText("学生甲")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(2_100));
    await waitFor(() => expect(calls).toBe(2));
    await act(() => vi.advanceTimersByTimeAsync(4_100));
    expect(calls).toBe(2);
  });

  it("does not poll from row state when global queued and evaluating counts are terminal", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    authenticate();
    let calls = 0;
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => {
      calls += 1;
      return HttpResponse.json(summary([row(1, { evaluation_status: "queued" })], {
        queued: 0, evaluating: 0, pending_evaluation: 1, pending_review: 0, failed: 0,
      }));
    }));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText("学生甲")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(4_100));
    expect(calls).toBe(1);
  });

  it("polls for running work reported by the global aggregate outside the current page", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    authenticate();
    let calls = 0;
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => {
      calls += 1;
      return HttpResponse.json(summary([], {
        total_students: 101, submitted_students: 100, missing_students: 1,
        queued: calls === 1 ? 1 : 0, evaluating: 0, pending_review: 0, failed: 0,
      }));
    }));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText("这一页没有学生记录")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(2_100));
    await waitFor(() => expect(calls).toBe(2));
    await act(() => vi.advanceTimersByTimeAsync(4_100));
    expect(calls).toBe(2);
  });

  it("prioritizes a known query error instead of hiding it behind the other pending query", async () => {
    authenticate();
    server.use(
      http.get(`${API_BASE}/assignments/:id`, () => new Promise(() => undefined)),
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json({ detail: "private" }, { status: 403 })),
    );
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByRole("alert")).toHaveTextContent("你没有权限查看这份作业");
    expect(screen.queryByText("正在读取作业汇总…")).not.toBeInTheDocument();
  });

  it("uses response offsets and row counts for partial and out-of-range pages", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, ({ request }) => {
      const offset = Number(new URL(request.url).searchParams.get("offset"));
      return HttpResponse.json(offset === 0
        ? summary([row(1), row(2)], { total_students: 150, submitted_students: 2, missing_students: 148, limit: 100, offset: 0 })
        : summary([], { total_students: 90, submitted_students: 2, missing_students: 88, limit: 100, offset: 100, latest_submission_at: null, pending_review: 0, failed: 0 }));
    }));
    const user = userEvent.setup();
    renderApp([`/teacher/assignments/${ids.assignment}?page=1&filter=all`]);
    expect(await screen.findByText("当前显示 1–2 / 150 名学生")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("当前显示 0–0 / 90 名学生")).toBeInTheDocument();
    expect(screen.queryByText(/101–90/u)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });

  it("canonicalizes URL state and restores page and filter through navigation", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, ({ request }) => {
      const offset = Number(new URL(request.url).searchParams.get("offset"));
      return HttpResponse.json(summary([
        row(offset === 0 ? 1 : 2, { evaluation_status: offset === 0 ? "failed" : "running" }),
      ], { total_students: 101, submitted_students: 101, missing_students: 0, offset, evaluating: offset === 0 ? 0 : 1, failed: offset === 0 ? 1 : 0, pending_review: 0 }));
    }));
    const user = userEvent.setup();
    const { router } = renderApp([`/teacher/assignments/${ids.assignment}?page=bad&filter=private`]);
    await screen.findByText("学生甲");
    await waitFor(() => expect(router.state.location.search).toBe("?page=1&filter=all"));
    await user.click(screen.getByRole("button", { name: "失败 1" }));
    expect(router.state.location.search).toBe("?page=1&filter=failed");
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("学生乙")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?page=2&filter=all");
    await act(() => router.navigate(-1));
    await waitFor(() => expect(router.state.location.search).toBe("?page=1&filter=failed"));
    expect(await screen.findByText("学生甲")).toBeInTheDocument();
  });

  it("shows safe loading, invalid route, forbidden, contract, and empty-page states", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => new Promise(() => undefined)));
    const pending = renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText("正在读取作业汇总…")).toBeInTheDocument();
    pending.unmount();

    renderApp(["/teacher/assignments/not-a-uuid"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("作业地址无效");
  });

  it("aborts an in-flight summary when the workspace unmounts", async () => {
    authenticate();
    let requestStarted!: () => void;
    const started = new Promise<void>((resolve) => { requestStarted = resolve; });
    let requestAborted!: () => void;
    const aborted = new Promise<void>((resolve) => { requestAborted = resolve; });
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, ({ request }) => {
      request.signal.addEventListener("abort", () => requestAborted(), { once: true });
      requestStarted();
      return new Promise(() => undefined);
    }));
    const view = renderApp([`/teacher/assignments/${ids.assignment}`]);
    await started;

    act(() => view.unmount());
    await expect(aborted).resolves.toBeUndefined();
  });

  it("sanitizes forbidden and contract failures and offers a retry", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json({ detail: "token=server-secret" }, { status: 403 })));
    const forbidden = renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByRole("alert")).toHaveTextContent("你没有权限查看这份作业");
    expect(screen.queryByText(/server-secret/u)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新读取" })).toBeInTheDocument();
    forbidden.unmount();

    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json({ assignment_id: ids.assignment })));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByRole("alert")).toHaveTextContent("服务器返回了无法识别的作业数据");
  });

  it("distinguishes not-found and network failures with safe copy", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id`, () => HttpResponse.json({ detail: "private database row" }, { status: 404 })));
    const missing = renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByRole("alert")).toHaveTextContent("没有找到这份作业");
    expect(screen.queryByText(/private database row/u)).not.toBeInTheDocument();
    missing.unmount();

    server.use(
      http.get(`${API_BASE}/assignments/:id`, () => HttpResponse.json(assignment)),
      http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.error()),
    );
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByRole("alert")).toHaveTextContent("无法连接服务器");
  });

  it("renders a truthful empty student population and preserves mobile table semantics", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary([], {
      total_students: 0, submitted_students: 0, missing_students: 0, latest_submission_at: null,
      pending_review: 0, failed: 0,
    }))));
    const empty = renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText("还没有学生记录")).toBeInTheDocument();
    expect(screen.getByText("当前显示 0–0 / 0 名学生")).toBeInTheDocument();
    empty.unmount();

    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary())));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    const table = await screen.findByRole("table", { name: "当前页学生提交与评估状态" });
    expect(within(table).getByRole("columnheader", { name: "学生" })).toBeInTheDocument();
    expect(within(table).getByRole("rowheader", { name: /学生甲/u })).toHaveAttribute("data-label", "学生");
    expect(within(table).getAllByRole("cell").some((cell) => cell.getAttribute("data-label") === "评估状态")).toBe(true);
    expect(screen.getByRole("status", { name: "评估进度播报" })).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("status", { name: "提交列表更新" })).toHaveAttribute("aria-live", "polite");
  });

  it("marks tables over 50 rows for lightweight rendering without changing table semantics", async () => {
    authenticate();
    const students = Array.from({ length: 51 }, (_, index) => row(index + 1));
    server.use(http.get(`${API_BASE}/assignments/:id/summary`, () => HttpResponse.json(summary(students, {
      total_students: 51, submitted_students: 51, missing_students: 0, pending_evaluation: 51, pending_review: 0, failed: 0,
    }))));
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    const table = await screen.findByRole("table", { name: "当前页学生提交与评估状态" });
    expect(table).toHaveClass("submission-table--large");
    expect(within(table).getAllByRole("rowheader")).toHaveLength(51);
  });

  it("never displays one teacher's cached summary while another teacher is loading", async () => {
    authenticate();
    const secondToken = "teacher.second.jwt";
    const secondTeacher = { ...teacher, id: "90000000-0000-4000-8000-000000000001", username: "second", display_name: "第二位老师" };
    let release!: () => void;
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => HttpResponse.json(request.headers.get("Authorization") === `Bearer ${secondToken}` ? secondTeacher : teacher)),
      http.get(`${API_BASE}/assignments/:id`, ({ request }) => request.headers.get("Authorization") === `Bearer ${secondToken}`
        ? new Promise((resolve) => { release = () => resolve(HttpResponse.json({ ...assignment, title: "第二位老师的作业" })); })
        : HttpResponse.json(assignment)),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ request }) => request.headers.get("Authorization") === `Bearer ${secondToken}`
        ? new Promise(() => undefined)
        : HttpResponse.json(summary())),
    );
    renderApp([`/teacher/assignments/${ids.assignment}`]);
    expect(await screen.findByText(assignment.title)).toBeInTheDocument();
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, secondToken);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: ACCESS_TOKEN_STORAGE_KEY, oldValue: TOKEN, newValue: secondToken, storageArea: window.localStorage })));
    await waitFor(() => expect(screen.queryByText(assignment.title)).not.toBeInTheDocument());
    expect(await screen.findByText("正在读取作业汇总…")).toBeInTheDocument();
    act(() => release());
    expect(screen.queryByText(assignment.title)).not.toBeInTheDocument();
  });

  it("resets URL and local action state synchronously when the authenticated user changes", async () => {
    authenticate();
    const secondToken = "teacher.scope.jwt";
    const secondTeacher = { ...teacher, id: "90000000-0000-4000-8000-000000000009", username: "scope-two" };
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => HttpResponse.json(
        request.headers.get("Authorization") === `Bearer ${secondToken}` ? secondTeacher : teacher,
      )),
      http.get(`${API_BASE}/assignments/:id/summary`, ({ request }) => request.headers.get("Authorization") === `Bearer ${secondToken}`
        ? new Promise(() => undefined)
        : HttpResponse.json(summary([row(2, { evaluation_status: "failed" })], {
          total_students: 101, submitted_students: 101, missing_students: 0,
          offset: 100, pending_review: 0, failed: 1,
        }))),
      http.post(`${API_BASE}/submissions/:id/evaluations`, ({ params }) => HttpResponse.json({
        id: ids.job, submission_id: params.id, requested_by: ids.user, reason: "provider_retry",
        status: "queued", attempt_count: 0, provider: "mock", model: "deterministic-v1",
        error_code: null, error_message: null, queued_at: "2026-07-22T08:31:00Z",
        started_at: null, finished_at: null,
      }, { status: 202 })),
    );
    const user = userEvent.setup();
    const { router } = renderApp([`/teacher/assignments/${ids.assignment}?page=2&filter=failed`]);
    await user.click(await screen.findByRole("button", { name: "重新评估 学生乙" }));
    expect(await screen.findByText("学生乙已重新加入评估队列。")).toBeInTheDocument();

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, secondToken);
    act(() => window.dispatchEvent(new StorageEvent("storage", {
      key: ACCESS_TOKEN_STORAGE_KEY,
      oldValue: TOKEN,
      newValue: secondToken,
      storageArea: window.localStorage,
    })));

    await waitFor(() => expect(router.state.location.search).toBe("?page=1&filter=all"));
    expect(screen.queryByText("学生乙已重新加入评估队列。")).not.toBeInTheDocument();
    expect(await screen.findByText("正在读取作业汇总…")).toBeInTheDocument();
  });
});
