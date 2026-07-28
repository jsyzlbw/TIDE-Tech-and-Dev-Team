import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ACCESS_TOKEN_STORAGE_KEY } from "../shared/api/client";
import { correctnessSchema, reportWorkspaceSchema } from "../shared/api/schemas";
import type { ReportWorkspace, TimelineReportSummary } from "../shared/api/schemas";
import { evaluationReportFixture, ids } from "../test/fixtures";
import { maximumWorkspaceWireFixture } from "../test/maximumWorkspaceFixture";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const API_BASE = "http://localhost:8000/api/v1";
const TOKEN = "teacher.jwt.token";
const teacher = { id: ids.user, username: "lin", display_name: "林老师", role: "teacher" as const };

const report = {
  ...evaluationReportFixture,
  author: { kind: "agent", display_name: "AI Agent" },
  latest_review_action: null,
} as const;

function timelineSummary(value: TimelineReportSummary): TimelineReportSummary {
  return {
    id: value.id,
    version: value.version,
    origin: value.origin,
    review_status: value.review_status,
    score: value.score,
    grade: value.grade,
    author: value.author,
    created_at: value.created_at,
    latest_review_action: value.latest_review_action,
  };
}

const workspace = {
  requested_report_id: ids.report,
  current_report_id: ids.report,
  assignment: {
    id: ids.assignment,
    code: "HW-0001",
    title: "图的最短路径",
    question: "说明 Dijkstra 算法、复杂度与适用条件。",
    notes: "请写出关键松弛步骤。",
    rubric: { required_points: ["算法步骤", "复杂度"], grading_notes: "关注负权边限制" },
    due_at: "2026-07-30T12:00:00Z",
    status: "published",
  },
  submission: {
    id: ids.submission,
    assignment_id: ids.assignment,
    student_id: ids.user,
    version: 1,
    content_type: "markdown",
    content_text: "使用优先队列进行松弛。<img src=x onerror=alert(1)>",
    content_json: null,
    status: "submitted",
    submitted_at: "2026-07-22T08:30:00Z",
    source: "web",
  },
  student: { id: ids.user, username: "grace", display_name: "Grace Hopper" },
  selected_report: report,
  timeline: [timelineSummary(report)],
  timeline_total: 1,
  timeline_truncated: false,
  reevaluation_job: null,
  request_id: ids.request,
};

function installWorkspace(overrides: Partial<ReportWorkspace> = {}) {
  server.use(
    http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)),
    http.get(`${API_BASE}/reports/${ids.report}/workspace`, () => HttpResponse.json({ ...workspace, ...overrides })),
  );
}

function streamedWorkspaceResponse(body: string) {
  const bytes = new TextEncoder().encode(body);
  let offset = 0;
  return new Response(new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset >= bytes.byteLength) {
        controller.close();
        return;
      }
      const nextOffset = Math.min(offset + 64 * 1024, bytes.byteLength);
      controller.enqueue(bytes.slice(offset, nextOffset));
      offset = nextOffset;
    },
  }), { headers: { "Content-Type": "application/json", "Content-Length": String(bytes.byteLength) } });
}

describe("teacher report review workspace", () => {
  beforeEach(() => window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN));

  it("renders all evidence safely in the three-column workspace", async () => {
    installWorkspace();
    renderApp([`/teacher/reports/${ids.report}`]);

    expect(await screen.findByRole("heading", { name: "图的最短路径" })).toBeInTheDocument();
    expect(screen.getByText("答案完整性")).toBeInTheDocument();
    expect(screen.getByText("正确性初步判断")).toBeInTheDocument();
    expect(screen.getByText("主要问题")).toBeInTheDocument();
    expect(screen.getByText("修改建议")).toBeInTheDocument();
    expect(screen.getByText("82 · B")).toBeInTheDocument();
    const completenessHeading = screen.getByRole("heading", { name: "答案完整性" });
    const score = screen.getByText("82 · B");
    expect(completenessHeading.compareDocumentPosition(score) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const versionHeading = screen.getByRole("heading", { name: "版本记录" }).parentElement?.parentElement;
    expect(versionHeading).toContainElement(screen.getByRole("button", { name: "导出报告 JSON" }));
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/u)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });

  it("uses one page heading, equal column headings, and lower-level section headings", async () => {
    installWorkspace();
    renderApp([`/teacher/reports/${ids.report}`]);

    expect(await screen.findByRole("heading", { level: 2, name: "评估报告审阅" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    for (const name of [workspace.assignment.title, workspace.student.display_name, "评估与审核"]) {
      expect(screen.getByRole("heading", { level: 3, name })).toBeInTheDocument();
    }
    for (const name of ["题目内容", "评分要点", "答案完整性", "正确性初步判断", "主要问题", "修改建议"]) {
      expect(screen.getByRole("heading", { level: 4, name })).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { level: 5, name: "教师评分说明" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "版本记录" })).toBeInTheDocument();
  });

  it("loads a streamed workspace response above the default two MiB budget", async () => {
    const { payload, body, wireBytes } = maximumWorkspaceWireFixture();
    expect(wireBytes).toBeGreaterThan(2 * 1024 * 1024);
    expect(wireBytes).toBeLessThanOrEqual(3 * 1024 * 1024);
    server.use(
      http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)),
      http.get(`${API_BASE}/reports/${ids.report}/workspace`, () => streamedWorkspaceResponse(body)),
    );
    renderApp([`/teacher/reports/${ids.report}`]);

    expect(await screen.findByRole("heading", { name: payload.assignment.title })).toBeInTheDocument();
  });

  it("confirms, edits only changed fields, and surfaces the exact conflict copy", async () => {
    let patchBody: unknown;
    installWorkspace();
    server.use(
      http.post(`${API_BASE}/reports/${ids.report}/confirm`, () => HttpResponse.json({ ...evaluationReportFixture, review_status: "confirmed", request_id: ids.request })),
      http.patch(`${API_BASE}/reports/${ids.report}`, async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json({ detail: "report is not current" }, { status: 409, headers: { "X-Request-ID": ids.request } });
      }),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/reports/${ids.report}`]);

    await user.click(await screen.findByRole("button", { name: "确认评估" }));
    expect(await screen.findByText("评估结果已确认。" )).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "修改评估" }));
    const score = screen.getByRole("spinbutton", { name: "评分" });
    await user.clear(score);
    await user.type(score, "88");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(patchBody).toEqual({ score: 88 });
    expect(await screen.findByRole("alert")).toHaveTextContent("该报告已被更新，请刷新后重试");
  });

  it("requires a comment for a ten-point score change and shows pending reevaluation", async () => {
    const queuedJob = { id: ids.job, source_report_id: ids.report, reason: "manual_retry" as const, status: "queued" as const, queued_at: "2026-07-22T09:00:00Z", started_at: null, finished_at: null };
    let workspaceJob: typeof queuedJob | null = null;
    server.use(
      http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)),
      http.get(`${API_BASE}/reports/${ids.report}/workspace`, () => HttpResponse.json({ ...workspace, reevaluation_job: workspaceJob })),
      http.post(`${API_BASE}/reports/${ids.report}/reevaluate`, () => {
        workspaceJob = queuedJob;
        return HttpResponse.json({ ...queuedJob, submission_id: ids.submission, requested_by: ids.user, attempt_count: 0, provider: "mock", model: "fixture-v1", error_code: null, error_message: null, request_id: ids.request });
      }),
    );
    const user = userEvent.setup();
    renderApp([`/teacher/reports/${ids.report}`]);

    await user.click(await screen.findByRole("button", { name: "修改评估" }));
    const score = screen.getByRole("spinbutton", { name: "评分" });
    await user.clear(score);
    await user.type(score, "60");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(screen.getByText("评分变化达到 10 分，请填写审核评语。" )).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "取消" }));
    const reviewComment = screen.getByRole("textbox", { name: "审核评语（可选）" });
    await user.type(reviewComment, "请重评");
    await user.click(screen.getByRole("button", { name: "重新评估" }));
    expect(await screen.findByText("重评任务排队中")).toBeInTheDocument();
    expect(reviewComment).toHaveValue("");
    const afterReevaluation = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(afterReevaluation);
    expect(afterReevaluation.defaultPrevented).toBe(false);
  });

  it("loads raw output only after expansion and exports a safe public JSON file", async () => {
    const raw = vi.fn(() => HttpResponse.json({ report_id: ids.report, available: true, raw_model_output: "RAW <script>x</script>", request_id: ids.request }));
    installWorkspace();
    server.use(http.get(`${API_BASE}/reports/${ids.report}/raw-output`, raw));
    const createObjectURL = vi.fn((blob: Blob) => {
      void blob;
      return "blob:report";
    });
    const revokeObjectURL = vi.fn();
    class TestURL extends URL {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = revokeObjectURL;
    }
    vi.stubGlobal("URL", TestURL);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const user = userEvent.setup();
    renderApp([`/teacher/reports/${ids.report}`]);

    expect(raw).not.toHaveBeenCalled();
    await user.click(await screen.findByText("原始模型输出"));
    expect(await screen.findByText("RAW <script>x</script>")).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    await user.click(screen.getByRole("button", { name: "导出报告 JSON" }));
    expect(createObjectURL).toHaveBeenCalledOnce();
    const blob = createObjectURL.mock.calls[0]?.[0] as Blob;
    const exported = JSON.parse(await blob.text()) as Record<string, unknown>;
    expect(exported).toMatchObject({ report_id: ids.report, submission_id: ids.submission, schema_version: "1.0", score: 82, grade: "B", version: 1 });
    expect(exported).not.toHaveProperty("raw_model_output");
    expect(exported).not.toHaveProperty("provider");
    expect(click).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:report"));
    vi.unstubAllGlobals();
  });

  it("keeps multiline point editing usable and replaces the URL with the new report", async () => {
    const newReportId = "77777777-7777-4777-8777-777777777777";
    let patchBody: unknown;
    const newPublicReport = {
      ...evaluationReportFixture,
      id: newReportId,
      job_id: null,
      source_report_id: ids.report,
      origin: "teacher",
      version: 2,
      score: 88,
      grade: "B",
      review_status: "modified",
      request_id: ids.request,
    } as const;
    const newWorkspaceReport = {
      ...report,
      id: newReportId,
      job_id: null,
      source_report_id: ids.report,
      origin: "teacher",
      version: 2,
      score: 88,
      grade: "B",
      review_status: "modified",
      author: { kind: "teacher", display_name: "林老师" },
      latest_review_action: null,
    } as const;
    installWorkspace();
    server.use(
      http.patch(`${API_BASE}/reports/${ids.report}`, async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json(newPublicReport);
      }),
      http.get(`${API_BASE}/reports/${newReportId}/workspace`, () => HttpResponse.json({
        ...workspace,
        requested_report_id: newReportId,
        current_report_id: newReportId,
        selected_report: newWorkspaceReport,
        timeline: [timelineSummary(newWorkspaceReport), timelineSummary(report)],
        timeline_total: 2,
      })),
    );
    const user = userEvent.setup();
    const { router } = renderApp([`/teacher/reports/${ids.report}`]);

    await user.click(await screen.findByRole("button", { name: "修改评估" }));
    fireEvent.change(screen.getByRole("textbox", { name: "已覆盖要点（每行一项）" }), { target: { value: "松弛\n负权边限制" } });
    const score = screen.getByRole("spinbutton", { name: "评分" });
    await user.clear(score);
    await user.type(score, "88");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    expect(patchBody).toEqual({ completeness: { ...report.completeness, covered_points: ["松弛", "负权边限制"] }, score: 88 });
    expect(await screen.findByText("88 · B")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/teacher/reports/${newReportId}`);
  });

  it("does not claim a terminal idempotent reevaluation is pending", async () => {
    installWorkspace();
    server.use(http.post(`${API_BASE}/reports/${ids.report}/reevaluate`, () => HttpResponse.json({
      id: ids.job, submission_id: ids.submission, requested_by: ids.user, source_report_id: ids.report,
      reason: "manual_retry", status: "succeeded", attempt_count: 1, provider: "mock", model: "fixture-v1",
      error_code: null, error_message: null, queued_at: "2026-07-22T09:00:00Z", started_at: "2026-07-22T09:00:01Z",
      finished_at: "2026-07-22T09:00:02Z", request_id: ids.request,
    })));
    const user = userEvent.setup();
    renderApp([`/teacher/reports/${ids.report}`]);

    await user.click(await screen.findByRole("button", { name: "重新评估" }));
    expect(await screen.findByText("该报告已有完成的重评任务，未重复加入队列。" )).toBeInTheDocument();
    expect(screen.queryByText(/重评任务(?:排队|执行)中/u)).not.toBeInTheDocument();
  });

  it("shows only a transient starting state while the reevaluation request is in flight", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    installWorkspace();
    server.use(http.post(`${API_BASE}/reports/${ids.report}/reevaluate`, () =>
      new Promise<Response>((resolve) => { resolveRequest = resolve; }),
    ));
    const user = userEvent.setup();
    renderApp([`/teacher/reports/${ids.report}`]);

    const button = await screen.findByRole("button", { name: "重新评估" });
    await user.click(button);
    expect(await screen.findByText("正在发起重评任务…")).toBeInTheDocument();
    expect(button).toBeDisabled();
    expect(screen.queryByText(/重评任务(?:排队|执行)中/u)).not.toBeInTheDocument();
    resolveRequest?.(HttpResponse.json({
      id: ids.job, submission_id: ids.submission, requested_by: ids.user, source_report_id: ids.report,
      reason: "manual_retry", status: "succeeded", attempt_count: 1, provider: "mock", model: "fixture-v1",
      error_code: null, error_message: null, queued_at: "2026-07-22T09:00:00Z", started_at: "2026-07-22T09:00:01Z",
      finished_at: "2026-07-22T09:00:02Z", request_id: ids.request,
    }));
    await waitFor(() => expect(screen.queryByText("正在发起重评任务…")).not.toBeInTheDocument());
  });

  it.each(["succeeded", "failed", "cancelled"] as const)("uses refreshed workspace %s state as the only pending reevaluation source", async (terminalStatus) => {
    const queuedJob = {
      id: ids.job, source_report_id: ids.report, reason: "manual_retry" as const,
      status: "queued" as const, queued_at: "2026-07-22T09:00:00Z", started_at: null, finished_at: null,
    };
    let reevaluationJob: ReportWorkspace["reevaluation_job"] = null;
    server.use(
      http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)),
      http.get(`${API_BASE}/reports/${ids.report}/workspace`, () => HttpResponse.json({ ...workspace, reevaluation_job: reevaluationJob })),
      http.post(`${API_BASE}/reports/${ids.report}/reevaluate`, () => {
        reevaluationJob = queuedJob;
        return HttpResponse.json({
          ...queuedJob, submission_id: ids.submission, requested_by: ids.user, attempt_count: 0,
          provider: "mock", model: "fixture-v1", error_code: null, error_message: null, request_id: ids.request,
        });
      }),
    );
    const user = userEvent.setup();
    const { queryClient } = renderApp([`/teacher/reports/${ids.report}`]);

    const button = await screen.findByRole("button", { name: "重新评估" });
    await user.click(button);
    expect(await screen.findByText("重评任务排队中")).toBeInTheDocument();
    expect(button).toBeDisabled();

    reevaluationJob = { ...queuedJob, status: terminalStatus, started_at: "2026-07-22T09:00:01Z", finished_at: "2026-07-22T09:00:02Z" };
    await act(() => queryClient.invalidateQueries({ queryKey: ["reports", "workspace", ids.user, ids.report] }));

    await waitFor(() => {
      expect(screen.queryByText(/重评任务(?:排队|执行)中/u)).not.toBeInTheDocument();
      expect(button).toBeEnabled();
    });
  });

  it("treats a non-empty view comment as dirty and clears the guard after confirm", async () => {
    installWorkspace();
    server.use(http.post(`${API_BASE}/reports/${ids.report}/confirm`, () => HttpResponse.json({
      ...evaluationReportFixture, review_status: "confirmed", request_id: ids.request,
    })));
    const user = userEvent.setup();
    renderApp([`/teacher/reports/${ids.report}`]);

    const comment = await screen.findByRole("textbox", { name: "审核评语（可选）" });
    await user.type(comment, "请保留这条审核说明");
    const dirtyUnload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirtyUnload);
    expect(dirtyUnload.defaultPrevented).toBe(true);
    await user.click(screen.getByRole("link", { name: "返回作业汇总" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    await user.click(screen.getByRole("button", { name: "确认评估" }));
    expect(await screen.findByText("评估结果已确认。")).toBeInTheDocument();
    expect(comment).toHaveValue("");
    const cleanUnload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanUnload);
    expect(cleanUnload.defaultPrevented).toBe(false);
  });

  it("guards dirty navigation and enforces the UTF-8 comment byte boundary", async () => {
    const reevaluate = vi.fn(() => HttpResponse.json({}));
    installWorkspace();
    server.use(http.post(`${API_BASE}/reports/${ids.report}/reevaluate`, reevaluate));
    const user = userEvent.setup();
    const { router } = renderApp([`/teacher/reports/${ids.report}`]);

    await user.click(await screen.findByRole("button", { name: "修改评估" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "评分" }), { target: { value: "83" } });
    await user.click(screen.getByRole("link", { name: "返回作业汇总" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("尚有未保存的修改");
    expect(router.state.location.pathname).toBe(`/teacher/reports/${ids.report}`);
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    await user.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.change(screen.getByRole("textbox", { name: "审核评语（可选）" }), { target: { value: "评".repeat(2_667) } });
    await user.click(screen.getByRole("button", { name: "重新评估" }));
    expect(screen.getByRole("alert")).toHaveTextContent("8000 个 UTF-8 字节");
    expect(reevaluate).not.toHaveBeenCalled();
  });

  it("renders a modal focus-trapped leave guard and restores the navigation trigger", async () => {
    installWorkspace();
    const user = userEvent.setup();
    renderApp([`/teacher/reports/${ids.report}`]);

    await user.type(await screen.findByRole("textbox", { name: "审核评语（可选）" }), "尚未提交");
    const returnLink = screen.getByRole("link", { name: "返回作业汇总" });
    await user.click(returnLink);
    const dialog = screen.getByRole("alertdialog");
    expect(dialog.parentElement).toBe(document.body);
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-describedby", "leave-guard-description");
    const background = document.querySelector(".app-frame");
    expect(background).toHaveAttribute("inert");
    expect(background).toHaveAttribute("aria-hidden", "true");
    const continueEditing = screen.getByRole("button", { name: "继续编辑" });
    const abandon = screen.getByRole("button", { name: "放弃修改并离开" });
    expect(dialog.querySelectorAll("button")).toHaveLength(2);
    expect(continueEditing).toHaveFocus();
    await user.tab();
    expect(abandon).toHaveFocus();
    await user.tab();
    expect(continueEditing).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(background).not.toHaveAttribute("inert");
    expect(background).not.toHaveAttribute("aria-hidden");
    expect(returnLink).toHaveFocus();
  });

  it("removes inert background state when an open leave guard unmounts", async () => {
    installWorkspace();
    const user = userEvent.setup();
    const { unmount } = renderApp([`/teacher/reports/${ids.report}`]);
    await user.type(await screen.findByRole("textbox", { name: "审核评语（可选）" }), "尚未提交");
    await user.click(screen.getByRole("link", { name: "返回作业汇总" }));
    const background = document.querySelector<HTMLElement>(".app-frame");
    expect(background).toHaveAttribute("inert");
    unmount();
    expect(background).not.toHaveAttribute("inert");
    expect(background).not.toHaveAttribute("aria-hidden");
  });

  it("ignores a late PATCH result after the report scope unmounts", async () => {
    const newReportId = "77777777-7777-4777-8777-777777777777";
    let resolvePatch: ((response: Response) => void) | undefined;
    let markPatchStarted: (() => void) | undefined;
    const patchStarted = new Promise<void>((resolve) => { markPatchStarted = resolve; });
    installWorkspace();
    server.use(http.patch(`${API_BASE}/reports/${ids.report}`, () => {
      markPatchStarted?.();
      return new Promise<Response>((resolve) => { resolvePatch = resolve; });
    }));
    const user = userEvent.setup();
    const { router, unmount } = renderApp([`/teacher/reports/${ids.report}`]);
    await user.click(await screen.findByRole("button", { name: "修改评估" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "评分" }), { target: { value: "88" } });
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    await patchStarted;
    unmount();
    resolvePatch?.(HttpResponse.json({
      ...evaluationReportFixture, id: newReportId, job_id: null, source_report_id: ids.report,
      origin: "teacher", version: 2, score: 88, grade: "B", review_status: "modified", request_id: ids.request,
    }));
    await new Promise((resolve) => window.setTimeout(resolve, 50));
    expect(router.state.location.pathname).toBe(`/teacher/reports/${ids.report}`);
  });

  it("navigates a successful PATCH directly without scheduling a navigation timer", async () => {
    const newReportId = "77777777-7777-4777-8777-777777777777";
    let resolvePatch: ((response: Response) => void) | undefined;
    let markPatchStarted: (() => void) | undefined;
    const patchStarted = new Promise<void>((resolve) => { markPatchStarted = resolve; });
    installWorkspace();
    server.use(
      http.patch(`${API_BASE}/reports/${ids.report}`, () => {
        markPatchStarted?.();
        return new Promise<Response>((resolve) => { resolvePatch = resolve; });
      }),
      http.get(`${API_BASE}/reports/${newReportId}/workspace`, () => HttpResponse.json({
        ...workspace, requested_report_id: newReportId, current_report_id: newReportId,
        selected_report: { ...report, id: newReportId, job_id: null, source_report_id: ids.report, origin: "teacher", version: 2, score: 88, review_status: "modified", author: { kind: "teacher", display_name: "林老师" } },
        timeline: [], timeline_total: 2,
      })),
    );
    const user = userEvent.setup();
    const { router } = renderApp([`/teacher/reports/${ids.report}`]);
    await user.click(await screen.findByRole("button", { name: "修改评估" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "评分" }), { target: { value: "88" } });
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    await patchStarted;
    const timer = vi.spyOn(window, "setTimeout");
    resolvePatch?.(HttpResponse.json({
      ...evaluationReportFixture, id: newReportId, job_id: null, source_report_id: ids.report,
      origin: "teacher", version: 2, score: 88, grade: "B", review_status: "modified", request_id: ids.request,
    }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/teacher/reports/${newReportId}`));
    expect(timer.mock.calls.some(([handler]) => typeof handler === "function" && String(handler).includes("teacher/reports"))).toBe(false);
  });

  it("uses semantic links and windowing hints for a long report timeline", async () => {
    const base = timelineSummary(report);
    const timeline = Array.from({ length: 51 }, (_, index) => ({
      ...base,
      id: `${String(index + 1).padStart(8, "0")}-0000-4000-8000-000000000000`,
      version: 51 - index,
    }));
    installWorkspace({ timeline, timeline_total: 51 });
    renderApp([`/teacher/reports/${ids.report}`]);

    const link = await screen.findByRole("link", { name: "查看版本 51" });
    expect(link).toHaveAttribute("href", `/teacher/reports/${timeline[0]?.id}`);
    expect(link.closest("ol")).toHaveClass("is-windowed");
  });

  it("keeps the selected action comment while timeline actions stay compact", async () => {
    const fullAction = {
      action: "modify" as const,
      teacher: { id: ids.user, username: "teacher", display_name: "林老师" },
      comment: "只允许完整选中报告展示这条评语",
      acted_at: "2026-07-22T09:00:02Z",
    };
    const compactAction = {
      action: fullAction.action,
      teacher: fullAction.teacher,
      acted_at: fullAction.acted_at,
    };
    const compactWorkspace = {
      ...workspace,
      selected_report: { ...report, latest_review_action: fullAction },
      timeline: [{ ...timelineSummary(report), latest_review_action: compactAction }],
    };
    const parsedWorkspace = reportWorkspaceSchema.parse(compactWorkspace);
    expect(reportWorkspaceSchema.safeParse({
      ...compactWorkspace,
      timeline: [{ ...compactWorkspace.timeline[0], latest_review_action: fullAction }],
    }).success).toBe(false);

    installWorkspace({
      selected_report: parsedWorkspace.selected_report,
      timeline: parsedWorkspace.timeline,
    });
    renderApp([`/teacher/reports/${ids.report}`]);
    expect(await screen.findByText(fullAction.comment)).toBeInTheDocument();
    expect(screen.getByText(/林老师 · modify ·/u)).toBeInTheDocument();
  });

  it("uses the backend correctness enum and discloses a truncated timeline", async () => {
    expect(correctnessSchema.safeParse({ judgment: "unable_to_determine", rationale: "证据不足" }).success).toBe(true);
    expect(correctnessSchema.safeParse({ judgment: "uncertain", rationale: "证据不足" }).success).toBe(false);
    installWorkspace({ timeline_total: 123, timeline_truncated: true });
    renderApp([`/teacher/reports/${ids.report}`]);
    expect(await screen.findByText("共 123 个版本，仅列出最近 100 个；当前旧版本会单独保留。" )).toBeInTheDocument();
  });

  it("rejects duplicated full reports in the compact timeline contract", () => {
    expect(reportWorkspaceSchema.safeParse({ ...workspace, timeline: [report] }).success).toBe(false);
  });

  it("rejects unknown workspace rubric fields instead of accepting legacy secrets", () => {
    expect(reportWorkspaceSchema.safeParse({
      ...workspace,
      assignment: {
        ...workspace.assignment,
        rubric: { ...workspace.assignment.rubric, secret_solution: "never expose" },
      },
    }).success).toBe(false);
  });
});
