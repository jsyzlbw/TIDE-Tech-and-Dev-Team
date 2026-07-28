import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { SubmissionTable } from "../assignments/SubmissionTable";
import { ReportEditForm } from "../reports/ReportEditForm";
import type { AssignmentSummaryStudent, WorkspaceReport } from "../shared/api/schemas";
import { AsyncState } from "../shared/ui/AsyncState";
import { StatusBadge } from "../shared/ui/StatusBadge";
import { ToastRegion } from "../shared/ui/ToastRegion";
import { ACCESS_TOKEN_STORAGE_KEY } from "../shared/api/client";
import {
  accountPageFixture,
  adminUserFixture,
  evaluationReportFixture,
  teacherUserFixture,
} from "../test/fixtures";
import { renderApp } from "../test/render";
import { API_BASE, serveAccountWorkspace, server } from "../test/server";

const globalStyles = readFileSync(resolve("src/styles/global.css"), "utf8");
const responsiveStyles = readFileSync(resolve("src/styles/responsive.css"), "utf8");
const assignmentStyles = readFileSync(resolve("src/assignments/assignments.css"), "utf8");
const userStyles = readFileSync(resolve("src/users/users.css"), "utf8");

function selectorsInMedia(source: string, query: string) {
  const style = document.createElement("style");
  style.textContent = source;
  document.head.append(style);
  try {
    const sheet = style.sheet;
    if (sheet === null) throw new Error("CSS style sheet was not parsed");
    const mediaRule = [...sheet.cssRules].find((rule) =>
      "media" in rule && (rule as CSSMediaRule).media.mediaText === query,
    ) as CSSMediaRule | undefined;
    if (mediaRule === undefined) throw new Error(`Missing media rule: ${query}`);
    return [...mediaRule.cssRules]
      .filter((rule): rule is CSSStyleRule => "selectorText" in rule)
      .map((rule) => ({ selector: rule.selectorText, style: rule.style }));
  } finally {
    style.remove();
  }
}

function declarationsForSelector(source: string, selector: string) {
  const style = document.createElement("style");
  style.textContent = source;
  document.head.append(style);
  try {
    const sheet = style.sheet;
    if (sheet === null) throw new Error("CSS style sheet was not parsed");
    const rule = [...sheet.cssRules].find((candidate) =>
      "selectorText" in candidate && (candidate as CSSStyleRule).selectorText === selector,
    ) as CSSStyleRule | undefined;
    if (rule === undefined) throw new Error(`Missing style rule: ${selector}`);
    return {
      color: rule.style.getPropertyValue("color"),
      textDecorationThickness: rule.style.getPropertyValue("text-decoration-thickness"),
    };
  } finally {
    style.remove();
  }
}

const editableReport: WorkspaceReport = {
  ...evaluationReportFixture,
  completeness: {
    ...evaluationReportFixture.completeness,
    covered_points: [...evaluationReportFixture.completeness.covered_points],
    missing_points: [...evaluationReportFixture.completeness.missing_points],
  },
  correctness: { ...evaluationReportFixture.correctness },
  major_issues: [{ code: "EDGE_CASE", title: "边界条件", evidence: "未讨论空图。", impact: "答案不完整。" }],
  suggestions: [{ priority: "high", action: "补充复杂度。", example: "O((V+E)logV)" }],
  limitations: ["未运行代码。"],
  author: { kind: "agent", display_name: "AI Agent" },
  latest_review_action: null,
};

function submissionRow(): AssignmentSummaryStudent {
  return {
    student_id: "10000000-0000-4000-8000-000000000001",
    username: "student-with-a-very-long-identifier-token",
    display_name: "学生甲",
    latest_submission: null,
    latest_version: null,
    submitted_at: null,
    evaluation_status: null,
    evaluation_error_code: null,
    report_status: null,
    latest_report_id: null,
    score: null,
    grade: null,
    evaluation_error: null,
  };
}

describe("application accessibility contracts", () => {
  it("activates the skip link from a real routed page and focuses the unique main landmark", async () => {
    const user = userEvent.setup();
    renderApp(["/login"]);

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("main")).toHaveAttribute("id", "workspace");
    expect(screen.getByRole("main")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("heading", { level: 1, name: "作业评审台" })).toBeInTheDocument();

    await user.tab();
    expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("labels authentication controls and supplies browser input metadata", async () => {
    renderApp(["/login"]);

    const username = await screen.findByRole("textbox", { name: "用户名" });
    const password = screen.getByLabelText("密码");
    expect(username).toHaveAttribute("name", "username");
    expect(username).toHaveAttribute("autocomplete", "username");
    expect(username).toHaveAttribute("spellcheck", "false");
    expect(password).toHaveAttribute("name", "password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(password).toHaveAttribute("spellcheck", "false");
    expect(screen.getByRole("button", { name: "显示密码" })).toHaveAttribute("aria-pressed", "false");
  });

  it.each([
    [adminUserFixture, "/admin/users", "账号管理台", "创建账号", false],
    [teacherUserFixture, "/teacher/users", "学生账号管理台", "创建学生账号", true],
  ] as const)(
    "gives the $role account route named regions, semantic headers, and role-correct navigation",
    async (actor, path, heading, createLabel, hasTeacherBackLink) => {
      window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, "accessibility.account.token");
      serveAccountWorkspace(actor);
      renderApp([path]);

      expect(await screen.findByRole("heading", { level: 2, name: heading })).toBeInTheDocument();
      expect(screen.getByRole("complementary", { name: "当前登录身份" })).toHaveTextContent(actor.display_name);
      expect(screen.getByRole("region", { name: "账号统计" })).toBeInTheDocument();
      const table = await screen.findByRole("table", { name: "账号登记" });
      expect(within(table).getAllByRole("columnheader")).toHaveLength(5);
      for (const header of within(table).getAllByRole("columnheader")) {
        expect(header).toHaveAttribute("scope", "col");
      }
      expect(screen.getByRole("navigation", { name: "账号分页" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: createLabel })).toBeInTheDocument();
      if (hasTeacherBackLink) {
        expect(screen.getByRole("link", { name: "返回作业台" })).toHaveAttribute("href", "/teacher");
      } else {
        expect(screen.queryByRole("link", { name: "返回作业台" })).not.toBeInTheDocument();
      }
    },
  );

  it("labels account controls, moves and restores keyboard focus, and announces success", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, "accessibility.account.token");
    serveAccountWorkspace(teacherUserFixture);
    server.use(
      http.post(`${API_BASE}/users`, () => HttpResponse.json({
        ...accountPageFixture.items[1],
        id: "77777777-7777-4777-8777-777777777777",
        username: "student.new",
        display_name: "新学生",
      }, { status: 201 })),
    );
    renderApp(["/teacher/users"]);
    const trigger = await screen.findByRole("button", { name: "创建学生账号" });

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "创建学生账号" });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveTextContent("计入所有已有作业的学生总数与未提交人数");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "用户名" })).toHaveFocus());
    expect(screen.getByRole("textbox", { name: "用户名" })).toHaveAccessibleDescription();
    expect(screen.getByRole("textbox", { name: "显示姓名" })).toHaveAttribute("name", "display_name");
    expect(screen.getByLabelText("初始密码")).toHaveAttribute("autocomplete", "new-password");
    expect(screen.getByLabelText("确认初始密码")).toHaveAttribute("autocomplete", "new-password");

    await user.type(screen.getByRole("textbox", { name: "用户名" }), "student.new");
    await user.type(screen.getByRole("textbox", { name: "显示姓名" }), "新学生");
    await user.type(screen.getByLabelText("初始密码"), "Course2026!Secure");
    await user.type(screen.getByLabelText("确认初始密码"), "Course2026!Secure");
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" }));

    const success = await screen.findByRole("status");
    expect(success).toHaveAttribute("aria-live", "polite");
    expect(success).toHaveTextContent("student.new 已创建。");
    expect(trigger).toHaveFocus();
  });

  it("announces safe account creation errors and keeps focus inside the named dialog", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, "accessibility.account.token");
    serveAccountWorkspace(adminUserFixture);
    server.use(
      http.post(`${API_BASE}/users`, () =>
        HttpResponse.json({ detail: "private conflict trace" }, { status: 409 })),
    );
    renderApp(["/admin/users"]);
    await user.click(await screen.findByRole("button", { name: "创建账号" }));
    const dialog = screen.getByRole("dialog", { name: "创建新账号" });
    expect(dialog).toHaveTextContent("计入所有已有作业的学生总数与未提交人数");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "账号角色" }), "teacher");
    expect(dialog).not.toHaveTextContent("计入所有已有作业的学生总数与未提交人数");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "账号角色" }), "student");
    await user.type(within(dialog).getByRole("textbox", { name: "用户名" }), "student.new");
    await user.type(within(dialog).getByRole("textbox", { name: "显示姓名" }), "新学生");
    await user.type(within(dialog).getByLabelText("初始密码"), "Course2026!Secure");
    await user.type(within(dialog).getByLabelText("确认初始密码"), "Course2026!Secure");
    await user.click(within(dialog).getByRole("button", { name: "创建账号" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("用户名已存在，请更换后重试。");
    expect(within(dialog).queryByText("private conflict trace")).not.toBeInTheDocument();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  });

  it("announces loading, processing, errors, and partial failure without hiding the next step", () => {
    const { rerender } = render(<AsyncState headingLevel={2} kind="loading" title="正在读取作业…" description="正在核对数据。" />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("heading", { level: 2, name: "正在读取作业…" })).toBeInTheDocument();

    rerender(<AsyncState headingLevel={3} kind="processing" title="正在生成评估…" description="可以稍后返回查看。" />);
    expect(screen.getByRole("status")).toHaveTextContent("正在生成评估…");
    expect(screen.getByRole("heading", { level: 3, name: "正在生成评估…" })).toBeInTheDocument();

    rerender(<AsyncState headingLevel={4} kind="error" title="暂时无法读取作业" description="请检查网络后重试。" action={<button type="button">重新读取</button>} />);
    expect(screen.getByRole("alert")).toHaveTextContent("请检查网络后重试。");
    expect(screen.getByRole("heading", { level: 4, name: "暂时无法读取作业" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新读取" })).toBeInTheDocument();

    rerender(<AsyncState headingLevel={4} kind="partial" title="部分统计未加载" description="作业列表仍可使用。" action={<button type="button">重试统计</button>} />);
    expect(screen.getByRole("alert")).toHaveTextContent("作业列表仍可使用。");
    expect(screen.getByRole("button", { name: "重试统计" })).toBeInTheDocument();
  });

  it("renders explicit empty and permission states", () => {
    const { rerender } = render(<AsyncState headingLevel={2} kind="empty" title="还没有作业" description="发布第一份作业后会显示在这里。" />);
    expect(screen.getByText("还没有作业")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    rerender(<AsyncState headingLevel={2} kind="permission" title="无权访问此页面" description="请返回自己的工作台。" />);
    expect(screen.getByRole("alert")).toHaveTextContent("请返回自己的工作台。");
  });

  it("makes status understandable as text and keeps each toast to one live region", () => {
    render(<><StatusBadge tone="failed">评估失败</StatusBadge><ToastRegion tone="success" message="作业已发布" /></>);

    expect(screen.getByText("评估失败")).toHaveAccessibleName("状态：评估失败");
    const toast = screen.getByRole("status");
    expect(toast).toHaveAttribute("aria-live", "polite");
    expect(toast).toHaveTextContent("作业已发布");
    expect(within(toast).queryByRole("status")).not.toBeInTheDocument();
    expect(within(toast).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps a semantic table and gives it a compact-card representation", () => {
    render(<SubmissionTable rows={[submissionRow()]} filter="all" retryingIds={new Set()} onRetry={() => undefined} />);

    const table = screen.getByRole("table", { name: "当前页学生提交与评估状态" });
    expect(table).toHaveClass("submission-table--compact-cards");
    for (const header of within(table).getAllByRole("columnheader")) expect(header).toHaveAttribute("scope", "col");
    expect(within(table).getByRole("rowheader", { name: /学生甲/u })).toHaveAttribute("scope", "row");
    expect(screen.getByText("学生甲")).toBeInTheDocument();
    for (const cell of document.querySelectorAll(".submission-table tbody th, .submission-table tbody td")) {
      expect(cell, cell.outerHTML).toHaveAttribute("data-label");
    }
  });

  it.each([
    ["评分", "score", "评分必须是 0–100 的整数。", "101"],
    ["已覆盖要点（每行一项）", "covered_points", "完整性字段超过允许的数量或长度。", Array.from({ length: 31 }, (_, index) => `要点 ${index}`).join("\n")],
    ["完整性说明", "completeness_rationale", "完整性说明不能为空。", ""],
    ["正确性说明", "correctness_rationale", "正确性说明不能为空。", ""],
    ["问题 1 标题", "issue-1-title", "主要问题标题不能为空。", ""],
    ["建议 1 内容", "suggestion-1-action", "修改建议内容不能为空。", ""],
    ["局限说明（每行一项）", "limitations", "局限说明不能超过 20 项，每项不能超过 4000 个字符。", "界".repeat(4_001)],
  ])("maps the first %s validation error to one described, focused field", async (label, fieldName, message, value) => {
    const onSave = vi.fn();
    render(<ReportEditForm report={editableReport} saving={false} onCancel={() => undefined} onDirtyChange={() => undefined} onSave={onSave} />);
    const control = screen.getByRole(label === "评分" ? "spinbutton" : "textbox", { name: label });
    fireEvent.change(control, { target: { value } });
    const form = screen.getByRole("form", { name: "修改评估报告" });
    expect(form).toHaveAttribute("novalidate");
    fireEvent.submit(form);

    await waitFor(() => expect(control).toHaveFocus());
    expect(control).toHaveAttribute("name", fieldName);
    expect(control).toHaveAttribute("aria-invalid", "true");
    const descriptionId = control.getAttribute("aria-describedby");
    expect(descriptionId).not.toBeNull();
    expect(document.getElementById(descriptionId ?? "")).toHaveTextContent(message);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("keeps dynamic issue and suggestion field names stable after earlier items are removed", async () => {
    const user = userEvent.setup();
    const report: WorkspaceReport = {
      ...editableReport,
      major_issues: [
        editableReport.major_issues[0]!,
        { code: "SECOND", title: "第二个问题", evidence: "证据", impact: "影响" },
      ],
      suggestions: [
        editableReport.suggestions[0]!,
        { priority: "medium", action: "第二条建议", example: "示例" },
      ],
    };
    render(<ReportEditForm report={report} saving={false} onCancel={() => undefined} onDirtyChange={() => undefined} onSave={() => undefined} />);
    const secondIssueName = screen.getByRole("textbox", { name: "问题 2 标题" }).getAttribute("name");
    const secondSuggestionName = screen.getByRole("textbox", { name: "建议 2 内容" }).getAttribute("name");

    await user.click(screen.getByRole("button", { name: "移除问题 1" }));
    await user.click(screen.getByRole("button", { name: "移除建议 1" }));
    expect(screen.getByRole("textbox", { name: "问题 1 标题" })).toHaveAttribute("name", secondIssueName);
    expect(screen.getByRole("textbox", { name: "建议 1 内容" })).toHaveAttribute("name", secondSuggestionName);
  });
});

describe("responsive and motion CSS contracts", () => {
  it("resets desktop account-column sizing for mobile cards without collapsing account content", () => {
    const mobileRules = selectorsInMedia(userStyles, "(max-width: 640px)");
    const cellRule = mobileRules.find(({ selector }) =>
      selector === ".account-table tbody th, .account-table tbody td",
    );
    const sizingReset = mobileRules.find(({ selector }) =>
      selector === ".account-table tbody th:first-child, .account-table tbody td",
    );

    expect(cellRule?.style.getPropertyValue("grid-template-columns")).toBe(
      "minmax(6rem, 35%) minmax(0, 1fr)",
    );
    expect(sizingReset?.style.getPropertyValue("width")).toBe("100%");
    expect(sizingReset?.style.getPropertyValue("min-width")).toMatch(/^0(?:px)?$/u);
  });

  it("defines visible focus, safe areas, native controls, touch behavior, and modal containment", () => {
    expect(globalStyles).toContain(":focus-visible");
    expect(responsiveStyles).toContain("env(safe-area-inset-left)");
    expect(responsiveStyles).toContain("touch-action: manipulation");
    expect(responsiveStyles).toMatch(/select[^{]*\{[^}]*color:[^;}]+;[^}]*background(?:-color)?:/su);
    expect(responsiveStyles).toContain("overscroll-behavior: contain");
    expect(`${globalStyles}\n${responsiveStyles}`).not.toMatch(/transition:\s*all/u);
    expect(declarationsForSelector(globalStyles, "a:hover")).toEqual({
      color: "var(--color-action-hover)",
      textDecorationThickness: "0.14em",
    });
  });

  it("uses two report columns below 900px and one column below 640px", () => {
    expect(responsiveStyles).toContain("@media (max-width: 900px)");
    expect(responsiveStyles).toMatch(/@media \(max-width: 900px\)[\s\S]*\.report-review__columns\s*\{[^}]*grid-template-columns:\s*repeat\(2,/u);
    expect(responsiveStyles).toContain("@media (max-width: 640px)");
    expect(responsiveStyles).toMatch(/@media \(max-width: 640px\)[\s\S]*\.report-review__columns\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/u);
  });

  it("switches tables to cards, wraps long content, and fully disables optional motion", () => {
    const mobileRules = selectorsInMedia(responsiveStyles, "(max-width: 640px)");
    const rowRule = mobileRules.find(({ selector }) => selector === ".submission-table--compact-cards tbody tr");
    const labelRule = mobileRules.find(({ selector }) => selector.includes("tbody th::before") && selector.includes("tbody td::before"));
    const labelDeclarations = responsiveStyles.match(/\.submission-table--compact-cards\s+tbody\s+th::before,\s*\.submission-table--compact-cards\s+tbody\s+td::before\s*\{([^}]*)\}/u)?.[1];
    expect(rowRule?.style.getPropertyValue("display")).toBe("grid");
    expect(labelRule).toBeDefined();
    expect(labelDeclarations).toMatch(/content:\s*attr\(data-label\);/u);
    expect(selectorsInMedia(assignmentStyles, "(max-width: 560px)").some(({ selector }) => selector.includes("submission-table"))).toBe(false);
    expect(responsiveStyles).toContain("overflow-wrap: anywhere");
    expect(responsiveStyles).toContain("min-width: 0");
    expect(responsiveStyles).toContain("@media (prefers-reduced-motion: reduce)");
    expect(responsiveStyles).toContain("animation-duration: 0.01ms !important");
    expect(responsiveStyles).toContain("transition-duration: 0.01ms !important");
  });
});
