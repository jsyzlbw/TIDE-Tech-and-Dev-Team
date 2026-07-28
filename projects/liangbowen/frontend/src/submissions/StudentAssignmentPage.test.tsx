import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ACCESS_TOKEN_STORAGE_KEY } from "../shared/api/client";
import {
  currentUserFixture,
  evaluationReportFixture,
  ids,
  studentAssignmentFixture,
  submissionReadFixture,
} from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import {
  STUDENT_REPORT_RESPONSE_BUDGET,
  STUDENT_SUBMISSION_RESPONSE_BUDGET,
} from "./api";

const API_BASE = "http://localhost:8000/api/v1";
const TOKEN = "student.jwt.token";
const OLD_SUBMISSION_ID = "77777777-7777-4777-8777-777777777777";
const NEW_SUBMISSION_ID = "88888888-8888-4888-8888-888888888888";
const SECOND_USER_ID = "99999999-9999-4999-8999-999999999999";
let submissionQueries: string[];
let reportQueries: string[];

const oldSubmission = {
  ...submissionReadFixture,
  id: OLD_SUBMISSION_ID,
  version: 1,
  content_type: "text" as const,
  content_text: "第一版答案 <script>alert(1)</script>",
  status: "withdrawn" as const,
  submitted_at: "2026-07-21T08:30:00Z",
};

function installAssignmentApi() {
  server.use(
    http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(currentUserFixture)),
    http.get(`${API_BASE}/assignments/${ids.assignment}`, () => HttpResponse.json(studentAssignmentFixture)),
    http.get(`${API_BASE}/assignments/${ids.assignment}/submissions/me`, ({ request }) => {
      submissionQueries.push(new URL(request.url).search);
      return HttpResponse.json([submissionReadFixture, oldSubmission]);
    }),
    http.get(`${API_BASE}/submissions/${ids.submission}/reports`, ({ request }) => {
      reportQueries.push(new URL(request.url).search);
      return HttpResponse.json([evaluationReportFixture]);
    }),
    http.get(`${API_BASE}/submissions/${OLD_SUBMISSION_ID}/reports`, () => HttpResponse.json([])),
  );
}

function streamedJsonResponse(body: string) {
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

describe("student assignment workspace", () => {
  beforeEach(() => {
    submissionQueries = [];
    reportQueries = [];
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    installAssignmentApi();
  });

  it("renders safe assignment evidence, version history, and the public report sections", async () => {
    renderApp([`/student/assignments/${ids.assignment}`]);

    expect(await screen.findByRole("heading", { name: "图的最短路径" })).toBeInTheDocument();
    expect(screen.getByText(studentAssignmentFixture.question)).toBeInTheDocument();
    expect(screen.getByText(studentAssignmentFixture.notes)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/rubric|created_by|mattermost|provider|raw_model_output|job_id/iu);
    expect(screen.getByText("第 2 版 · 最新")).toBeInTheDocument();
    expect(screen.getByText(/第 1 版.*已撤回/u)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "作业答案" })).toHaveValue(submissionReadFixture.content_text);
    expect(await screen.findByRole("heading", { name: "答案完整性" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "正确性判断" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "主要问题" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "修改建议" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "评分与说明" })).toBeInTheDocument();
    expect(screen.getByText("AI 评估仅供参考，最终结果以教师审核为准。")).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    expect(submissionQueries[0]).toContain("limit=21");
    expect(submissionQueries[0]).toContain("offset=0");
    expect(reportQueries[0]).toContain("limit=6");
    expect(reportQueries[0]).toContain("offset=0");
  });

  it("switches history without overwriting a dirty draft", async () => {
    const user = userEvent.setup();
    const { router } = renderApp([`/student/assignments/${ids.assignment}`]);
    const editor = await screen.findByRole("textbox", { name: "作业答案" });

    await user.type(editor, "\n我的未提交修改");
    await user.selectOptions(screen.getByRole("combobox", { name: "查看提交版本" }), OLD_SUBMISSION_ID);

    expect(screen.getByText(oldSubmission.content_text)).toBeInTheDocument();
    expect(editor).toHaveValue(`${submissionReadFixture.content_text}\n我的未提交修改`);
    expect(screen.getByText("这份提交暂时没有评估报告。")).toBeInTheDocument();
    expect(router.state.location.search).toContain(`submission=${OLD_SUBMISSION_ID}`);
    await router.navigate(-1);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "查看提交版本" })).toHaveValue(ids.submission));
    expect(editor).toHaveValue(`${submissionReadFixture.content_text}\n我的未提交修改`);
  });

  it("preserves mode and text on retry, submits an explicit null content_json, and advances the latest version", async () => {
    const bodies: unknown[] = [];
    let attempt = 0;
    server.use(http.post(`${API_BASE}/assignments/${ids.assignment}/submissions`, async ({ request }) => {
      bodies.push(await request.json());
      attempt += 1;
      if (attempt === 1) return HttpResponse.json({ detail: "offline" }, { status: 503 });
      return HttpResponse.json({
        ...submissionReadFixture,
        id: NEW_SUBMISSION_ID,
        version: 3,
        content_type: "code",
        content_text: "print('ok')",
        submitted_at: "2026-07-27T08:30:00Z",
      });
    }));
    server.use(http.get(`${API_BASE}/submissions/${NEW_SUBMISSION_ID}/reports`, () => HttpResponse.json([])));
    const user = userEvent.setup();
    renderApp([`/student/assignments/${ids.assignment}`]);
    const editor = await screen.findByRole("textbox", { name: "作业答案" });

    await user.selectOptions(screen.getByRole("combobox", { name: "答案格式" }), "code");
    await user.clear(editor);
    await user.type(editor, "print('ok')");
    await user.click(screen.getByRole("button", { name: "提交答案" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法提交");
    expect(editor).toHaveValue("print('ok')");
    expect(screen.getByRole("combobox", { name: "答案格式" })).toHaveValue("code");

    await user.click(screen.getByRole("button", { name: "提交答案" }));
    expect(await screen.findByText("第 3 版 · 最新")).toBeInTheDocument();
    expect(screen.getByText("答案已提交为第 3 版。")).toBeInTheDocument();
    expect(bodies).toEqual([
      { content_type: "code", content_text: "print('ok')", content_json: null },
      { content_type: "code", content_text: "print('ok')", content_json: null },
    ]);
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);
  });

  it("validates visible text by Unicode code points and guards dirty router navigation", async () => {
    const post = vi.fn();
    server.use(http.post(`${API_BASE}/assignments/${ids.assignment}/submissions`, post));
    const user = userEvent.setup();
    const { router } = renderApp([`/student/assignments/${ids.assignment}`]);
    const editor = await screen.findByRole("textbox", { name: "作业答案" });

    fireEvent.change(editor, { target: { value: "😀".repeat(50_001) } });
    await user.click(screen.getByRole("button", { name: "提交答案" }));
    expect(screen.getByRole("alert")).toHaveTextContent("不能超过 50000 个字符");
    expect(post).not.toHaveBeenCalled();

    fireEvent.change(editor, { target: { value: "\u200b\u0301" } });
    await user.click(screen.getByRole("button", { name: "提交答案" }));
    expect(screen.getByRole("alert")).toHaveTextContent("答案不能为空");
    expect(post).not.toHaveBeenCalled();

    fireEvent.change(editor, { target: { value: "未提交答案" } });
    editor.focus();
    act(() => { void router.navigate("/student"); });
    expect(await screen.findByRole("alertdialog", { name: "尚有未提交的答案" })).toBeInTheDocument();
    expect(document.querySelector(".app-frame")).toHaveAttribute("inert");
    const stay = screen.getByRole("button", { name: "留在此页" });
    const leave = screen.getByRole("button", { name: "放弃答案并离开" });
    await waitFor(() => expect(stay).toHaveFocus());
    await user.tab({ shift: true });
    expect(leave).toHaveFocus();
    await user.tab();
    expect(stay).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/student/assignments/${ids.assignment}`);
    expect(editor).toHaveFocus();

    act(() => { void router.navigate("/student"); });
    await user.click(await screen.findByRole("button", { name: "放弃答案并离开" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/student"));
  });

  it("confirms local draft clearing with a trapped, focus-restoring dialog", async () => {
    const user = userEvent.setup();
    renderApp([`/student/assignments/${ids.assignment}`]);
    const editor = await screen.findByRole("textbox", { name: "作业答案" });
    await user.type(editor, "\n不应被单击清除");
    const discard = screen.getByRole("button", { name: "放弃草稿" });

    await user.click(discard);
    expect(screen.getByRole("alertdialog", { name: "确认清除当前草稿" })).toBeInTheDocument();
    expect(document.querySelector(".app-frame")).toHaveAttribute("inert");
    const keep = screen.getByRole("button", { name: "继续编辑" });
    const confirm = screen.getByRole("button", { name: "确认清除" });
    await waitFor(() => expect(keep).toHaveFocus());
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
    await user.tab();
    expect(keep).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(discard).toHaveFocus();
    expect(editor).toHaveValue(`${submissionReadFixture.content_text}\n不应被单击清除`);

    await user.click(discard);
    await user.click(await screen.findByRole("button", { name: "确认清除" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(editor).toHaveValue(submissionReadFixture.content_text);
  });

  it("treats a server 409 as authoritative and keeps the rejected draft", async () => {
    server.use(http.post(`${API_BASE}/assignments/${ids.assignment}/submissions`, () =>
      HttpResponse.json({ detail: "assignment closed" }, { status: 409 })));
    const user = userEvent.setup();
    renderApp([`/student/assignments/${ids.assignment}`]);
    const editor = await screen.findByRole("textbox", { name: "作业答案" });

    await user.clear(editor);
    await user.type(editor, "截止瞬间的答案");
    await user.click(screen.getByRole("button", { name: "提交答案" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("作业已关闭或截止时间已过");
    expect(editor).toHaveValue("截止瞬间的答案");
  });

  it("deduplicates rapid submit clicks while the request is in flight", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    let requests = 0;
    server.use(http.post(`${API_BASE}/assignments/${ids.assignment}/submissions`, () => {
      requests += 1;
      return new Promise<Response>((resolve) => { resolveRequest = resolve; });
    }));
    renderApp([`/student/assignments/${ids.assignment}`]);
    const editor = await screen.findByRole("textbox", { name: "作业答案" });
    fireEvent.change(editor, { target: { value: "只提交一次" } });
    const submit = screen.getByRole("button", { name: "提交答案" });

    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(requests).toBe(1));
    resolveRequest?.(HttpResponse.json({ ...submissionReadFixture, version: 3, content_text: "只提交一次" }));
    expect(await screen.findByText("答案已提交为第 3 版。")).toBeInTheDocument();
    expect(editor).toHaveValue("只提交一次");
  });

  it("resets draft and selected evidence when the authenticated student changes", async () => {
    const secondToken = "second.student.token";
    const secondSubmission = {
      ...submissionReadFixture,
      id: NEW_SUBMISSION_ID,
      student_id: SECOND_USER_ID,
      version: 1,
      content_text: "第二位学生自己的答案",
    };
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        const second = request.headers.get("Authorization") === `Bearer ${secondToken}`;
        return HttpResponse.json(second
          ? { ...currentUserFixture, id: SECOND_USER_ID, username: "ada", display_name: "Ada Lovelace" }
          : currentUserFixture);
      }),
      http.get(`${API_BASE}/assignments/${ids.assignment}/submissions/me`, ({ request }) => {
        const second = request.headers.get("Authorization") === `Bearer ${secondToken}`;
        return HttpResponse.json(second ? [secondSubmission] : [submissionReadFixture]);
      }),
      http.get(`${API_BASE}/submissions/${NEW_SUBMISSION_ID}/reports`, () => HttpResponse.json([])),
    );
    const user = userEvent.setup();
    renderApp([`/student/assignments/${ids.assignment}`]);
    const editor = await screen.findByRole("textbox", { name: "作业答案" });
    await user.type(editor, "\n第一位学生的私有草稿");

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, secondToken);
    window.dispatchEvent(new StorageEvent("storage", {
      key: ACCESS_TOKEN_STORAGE_KEY,
      oldValue: TOKEN,
      newValue: secondToken,
    }));

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "作业答案" })).toHaveValue(secondSubmission.content_text));
    expect(screen.queryByText(/第一位学生的私有草稿/u)).not.toBeInTheDocument();
  });

  it("pages submission history without pretending to know a total", async () => {
    const requests: string[] = [];
    const submissions = Array.from({ length: 21 }, (_, index) => ({
      ...submissionReadFixture,
      id: `${String(index + 10).padStart(8, "0")}-3333-4333-8333-333333333333`,
      version: 30 - index,
      content_text: `答案版本 ${30 - index}`,
    }));
    const lastPageSubmission = {
      ...submissionReadFixture,
      id: "66666666-3333-4333-8333-333333333333",
      version: 10,
      content_text: "最后一页答案",
    };
    server.use(
      http.get(`${API_BASE}/assignments/${ids.assignment}/submissions/me`, ({ request }) => {
        const url = new URL(request.url);
        requests.push(url.search);
        return HttpResponse.json(url.searchParams.get("offset") === "20" ? [lastPageSubmission] : submissions);
      }),
      http.get(`${API_BASE}/submissions/:submissionId/reports`, () => HttpResponse.json([])),
    );
    const user = userEvent.setup();
    renderApp([`/student/assignments/${ids.assignment}`]);

    expect(await screen.findByText("答案版本 30")).toBeInTheDocument();
    const pagination = screen.getByRole("navigation", { name: "提交记录分页" });
    await user.click(within(pagination).getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("最后一页答案")).toBeInTheDocument();
    expect(requests.at(-1)).toContain("limit=21");
    expect(requests.at(-1)).toContain("offset=20");
    expect(within(screen.getByRole("navigation", { name: "提交记录分页" })).getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(screen.getByRole("option", { name: "第 10 版" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /第 10 版.*最新/u })).not.toBeInTheDocument();
  });

  it("loads a maximum submission page containing four-byte and JSON-escaped characters", async () => {
    const maximalText = `😀${"\u0001".repeat(49_999)}`;
    expect([...maximalText]).toHaveLength(50_000);
    const maximalJsonString = "\u0001".repeat(17_065);
    expect(new TextEncoder().encode(JSON.stringify({ v: maximalJsonString })).byteLength).toBe(102_398);
    const maximalPage = Array.from({ length: 21 }, (_, index) => ({
      ...submissionReadFixture,
      id: `${String(index + 40).padStart(8, "0")}-3333-4333-8333-333333333333`,
      version: 50 - index,
      content_type: "structured",
      content_text: maximalText,
      content_json: { v: maximalJsonString },
    }));
    const body = JSON.stringify(maximalPage);
    const wireBytes = new TextEncoder().encode(body).byteLength;
    expect(wireBytes).toBeGreaterThan(8 * 1024 * 1024);
    expect(wireBytes).toBeLessThan(STUDENT_SUBMISSION_RESPONSE_BUDGET);
    expect(STUDENT_SUBMISSION_RESPONSE_BUDGET).toBeLessThan(16 * 1024 * 1024);
    server.use(
      http.get(`${API_BASE}/assignments/${ids.assignment}/submissions/me`, () => streamedJsonResponse(body)),
      http.get(`${API_BASE}/submissions/:submissionId/reports`, () => HttpResponse.json([])),
    );
    renderApp([`/student/assignments/${ids.assignment}`]);

    const editor = await screen.findByRole("textbox", { name: "作业答案" }, { timeout: 5_000 });
    expect(editor).toHaveValue(maximalText);
  }, 10_000);

  it("loads a bounded maximum report page without crossing the global response ceiling", async () => {
    const unicode = (length: number) => "😀".repeat(length);
    const distinctUnicode = (index: number, length: number) =>
      `${String.fromCodePoint(0x1f600 + index)}${unicode(length - 1)}`;
    const largeReports = Array.from({ length: 6 }, (_, reportIndex) => ({
      ...evaluationReportFixture,
      id: `${String(reportIndex + 70).padStart(8, "0")}-5555-4555-8555-555555555555`,
      version: 6 - reportIndex,
      completeness: {
        level: "partial",
        covered_points: Array.from({ length: 30 }, (_, index) => distinctUnicode(index, 1_000)),
        missing_points: Array.from({ length: 30 }, (_, index) => distinctUnicode(index + 30, 1_000)),
        rationale: unicode(4_000),
      },
      correctness: { judgment: "mostly_correct", rationale: unicode(4_000) },
      major_issues: Array.from({ length: 20 }, (_, index) => ({
        code: `I${String(index).padStart(2, "0")}${"X".repeat(61)}`,
        title: unicode(200),
        evidence: unicode(4_000),
        impact: unicode(2_000),
      })),
      suggestions: Array.from({ length: 20 }, (_, index) => ({
        priority: index === 0 ? "high" : "medium",
        action: unicode(2_000),
        example: unicode(4_000),
      })),
      limitations: Array.from({ length: 20 }, (_, index) => distinctUnicode(index, 4_000)),
    }));
    expect(largeReports[0]?.completeness.covered_points).toHaveLength(30);
    expect([...(largeReports[0]?.completeness.covered_points[0] ?? "")]).toHaveLength(1_000);
    expect([...(largeReports[0]?.major_issues[0]?.code ?? "")]).toHaveLength(64);
    expect([...(largeReports[0]?.major_issues[0]?.title ?? "")]).toHaveLength(200);
    expect([...(largeReports[0]?.major_issues[0]?.evidence ?? "")]).toHaveLength(4_000);
    expect([...(largeReports[0]?.major_issues[0]?.impact ?? "")]).toHaveLength(2_000);
    expect([...(largeReports[0]?.suggestions[0]?.action ?? "")]).toHaveLength(2_000);
    expect([...(largeReports[0]?.suggestions[0]?.example ?? "")]).toHaveLength(4_000);
    expect([...(largeReports[0]?.limitations[0] ?? "")]).toHaveLength(4_000);
    const body = JSON.stringify(largeReports);
    const wireBytes = new TextEncoder().encode(body).byteLength;
    expect(wireBytes).toBe(9_431_947);
    expect(wireBytes).toBeGreaterThan(9_400_000);
    expect(wireBytes).toBeLessThan(9_500_000);
    expect(wireBytes).toBeLessThan(STUDENT_REPORT_RESPONSE_BUDGET);
    expect(STUDENT_REPORT_RESPONSE_BUDGET).toBeLessThan(16 * 1024 * 1024);
    server.use(http.get(`${API_BASE}/submissions/${ids.submission}/reports`, () => streamedJsonResponse(body)));
    renderApp([`/student/assignments/${ids.assignment}`]);

    expect(await screen.findByText("第 6 版 · 最新", {}, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "报告分页" })).toBeInTheDocument();
  }, 10_000);
});
