import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse, delay } from "msw";
import { describe, expect, it } from "vitest";

import { ACCESS_TOKEN_STORAGE_KEY } from "../shared/api/client";
import type { CurrentUser, UserAccount } from "../shared/api/schemas";
import {
  ApiContractError,
  ApiError,
  ApiNetworkError,
  ApiTimeoutError,
} from "../shared/api/errors";
import { ids } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import { safeAccountCreateMessage } from "./accountErrors";

const API_BASE = "http://localhost:8000/api/v1";
const token = "account.manager.jwt.token";
const admin: CurrentUser = { id: ids.user, username: "admin", display_name: "系统管理员", role: "admin" };
const teacher: CurrentUser = { ...admin, username: "teacher1", display_name: "教师甲", role: "teacher" };
const creator = { id: ids.user, username: "admin", display_name: "系统管理员" };
const accounts: UserAccount[] = [
  { id: "10000000-0000-4000-8000-000000000001", username: "admin", display_name: "系统管理员", role: "admin", is_active: true, created_at: "2026-07-20T08:00:00Z", created_by: null },
  { id: "10000000-0000-4000-8000-000000000002", username: "teacher1", display_name: "教师甲", role: "teacher", is_active: true, created_at: "2026-07-21T08:00:00Z", created_by: creator },
  { id: "10000000-0000-4000-8000-000000000003", username: "student1", display_name: "学生甲", role: "student", is_active: true, created_at: "2026-07-22T08:00:00Z", created_by: creator },
];

function authenticate(actor: CurrentUser = admin) {
  window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
  server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(actor)));
}

function accountPage(items: readonly UserAccount[] = accounts, total: number = items.length, offset = 0) {
  return { items, total, limit: 50, offset };
}

function serveAccounts(items: readonly UserAccount[] = accounts, total: number = items.length) {
  server.use(http.get(`${API_BASE}/users`, ({ request }) => {
    const offset = Number(new URL(request.url).searchParams.get("offset") ?? 0);
    return HttpResponse.json(accountPage(items, total, offset));
  }));
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function openAndFillValidStudent(username = "student4", user = userEvent.setup()) {
  await user.click(await screen.findByRole("button", { name: /创建(?:学生)?账号/u }));
  await user.type(screen.getByRole("textbox", { name: "用户名" }), username);
  await user.type(screen.getByRole("textbox", { name: "显示姓名" }), "学生丁");
  await user.type(screen.getByLabelText("初始密码"), "Course2026!Secure");
  await user.type(screen.getByLabelText("确认初始密码"), "Course2026!Secure");
  return user;
}

describe("account management workspace", () => {
  it("lets an administrator select teacher or student and shows all role totals", async () => {
    authenticate();
    serveAccounts();
    const user = userEvent.setup();
    renderApp(["/admin/users"]);

    expect(await screen.findByRole("heading", { name: "账号管理台" })).toBeInTheDocument();
    expect(await screen.findByText("@student1")).toBeInTheDocument();
    expect(screen.getByText("1", { selector: "[data-metric='admin-count']" })).toBeInTheDocument();
    expect(screen.getByText("1", { selector: "[data-metric='teacher-count']" })).toBeInTheDocument();
    expect(screen.getByText("1", { selector: "[data-metric='student-count']" })).toBeInTheDocument();
    expect(screen.getByText("系统初始化")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "创建账号" }));
    expect(screen.getByRole("combobox", { name: "账号角色" })).toHaveAttribute("name", "role");
    expect(screen.getByRole("combobox", { name: "账号角色" })).toHaveValue("student");
    expect(screen.getByRole("option", { name: "教师" })).toBeInTheDocument();
  });

  it("fixes teacher-created accounts to student", async () => {
    authenticate(teacher);
    serveAccounts([accounts[2]], 1);
    const user = userEvent.setup();
    renderApp(["/teacher/users"]);

    await user.click(await screen.findByRole("button", { name: "创建学生账号" }));
    expect(screen.queryByRole("combobox", { name: "账号角色" })).not.toBeInTheDocument();
    expect(screen.getByText("学生账号", { selector: "strong" })).toBeInTheDocument();
  });

  it("clears secret fields, closes, announces, and refreshes after success", async () => {
    authenticate(teacher);
    let created = false;
    server.use(
      http.get(`${API_BASE}/users`, () => HttpResponse.json(accountPage(created ? [
        ...accounts,
        { ...accounts[2], id: "10000000-0000-4000-8000-000000000004", username: "student4", display_name: "学生丁" },
      ] : accounts))),
      http.post(`${API_BASE}/users`, async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        expect(new URL(request.url).search).toBe("");
        expect(body.password).toBe("Course2026!Secure");
        created = true;
        return HttpResponse.json({ ...accounts[2], id: "10000000-0000-4000-8000-000000000004", username: "student4", display_name: "学生丁" }, { status: 201 });
      }),
    );
    renderApp(["/teacher/users"]);
    const user = await openAndFillValidStudent(" student4 ");

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" }));

    expect(await screen.findByText("student4 已创建。")).toBeInTheDocument();
    expect(screen.queryByLabelText("初始密码")).not.toBeInTheDocument();
    expect(await screen.findByText("@student4")).toBeInTheDocument();
  });

  it("renders validation summary and moves focus to the first invalid field", async () => {
    authenticate(teacher);
    serveAccounts([], 0);
    const user = userEvent.setup();
    renderApp(["/teacher/users"]);
    await user.click(await screen.findByRole("button", { name: "创建学生账号" }));
    await user.type(screen.getByRole("textbox", { name: "用户名" }), "Student 4");
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" }));

    expect(screen.getByRole("alert", { name: "请修正以下内容" })).toHaveTextContent("用户名需为");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "用户名" })).toHaveFocus());
  });

  it("revalidates username, password, and confirmation dependencies after errors", async () => {
    authenticate(teacher);
    serveAccounts();
    const user = userEvent.setup();
    renderApp(["/teacher/users"]);
    await user.click(await screen.findByRole("button", { name: "创建学生账号" }));
    await user.type(screen.getByRole("textbox", { name: "用户名" }), "student1234");
    await user.type(screen.getByRole("textbox", { name: "显示姓名" }), "学生丁");
    await user.type(screen.getByLabelText("初始密码"), "student1234");
    await user.type(screen.getByLabelText("确认初始密码"), "Mismatch2026!");
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" }));
    expect(screen.getAllByText("初始密码不能与用户名相同。")).toHaveLength(2);
    expect(screen.getAllByText("两次输入的密码不一致。")).toHaveLength(2);

    await user.clear(screen.getByRole("textbox", { name: "用户名" }));
    await user.type(screen.getByRole("textbox", { name: "用户名" }), "student1235");
    expect(screen.queryByText("初始密码不能与用户名相同。")).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("初始密码"));
    await user.type(screen.getByLabelText("初始密码"), "Mismatch2026!");
    expect(screen.queryByText("两次输入的密码不一致。")).not.toBeInTheDocument();
  });

  it("maps duplicate and server validation responses without exposing server details", async () => {
    authenticate(teacher);
    serveAccounts();
    server.use(http.post(`${API_BASE}/users`, () => HttpResponse.json({ detail: "secret duplicate trace" }, { status: 409 })));
    renderApp(["/teacher/users"]);
    const user = await openAndFillValidStudent();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("用户名已存在，请更换后重试。");
    expect(screen.queryByText(/secret duplicate trace/u)).not.toBeInTheDocument();

    server.use(http.post(`${API_BASE}/users`, () => HttpResponse.json({ detail: "internal model detail" }, { status: 422 })));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("账号信息未通过服务器校验，请检查后重试。");
  });

  it("maps network, timeout, contract, and server errors to safe messages", () => {
    expect(safeAccountCreateMessage(new ApiNetworkError())).toBe("无法连接服务器，请检查网络后重试。");
    expect(safeAccountCreateMessage(new ApiTimeoutError())).toBe("创建请求超时，请检查网络后重试。");
    expect(safeAccountCreateMessage(new ApiContractError())).toBe("服务器返回了无法识别的账号数据，请稍后重试。");
    expect(safeAccountCreateMessage(new ApiError({ status: 503, code: "NOPE", message: "secret" }))).toBe("服务器暂时无法创建账号，请稍后重试。");
  });

  it("blocks double submission while a creation request is pending", async () => {
    authenticate(teacher);
    serveAccounts();
    let calls = 0;
    server.use(http.post(`${API_BASE}/users`, async () => {
      calls += 1;
      await delay("infinite");
      return HttpResponse.json({});
    }));
    renderApp(["/teacher/users"]);
    await openAndFillValidStudent();
    const submit = within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" });
    fireEvent.submit(submit.closest("form")!);
    fireEvent.submit(submit.closest("form")!);

    await waitFor(() => expect(calls).toBe(1));
    expect(screen.getByRole("button", { name: "正在创建…" })).toBeDisabled();
  });

  it("keeps pending creation and navigation deterministic until a successful settlement", async () => {
    authenticate(teacher);
    serveAccounts();
    const gate = deferred<void>();
    let calls = 0;
    server.use(http.post(`${API_BASE}/users`, async () => {
      calls += 1;
      await gate.promise;
      return HttpResponse.json({ ...accounts[2], id: "10000000-0000-4000-8000-000000000004", username: "student4", display_name: "学生丁" }, { status: 201 });
    }));
    const user = userEvent.setup();
    const { router } = renderApp(["/teacher/users"]);
    await openAndFillValidStudent("student4", user);
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" }));
    expect(await screen.findByRole("button", { name: "正在创建…" })).toBeDisabled();

    await user.click(screen.getByRole("link", { name: "返回作业台" }));
    expect(await screen.findByRole("alertdialog", { name: "账号正在创建" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "放弃未保存内容" })).toBeDisabled();
    expect(router.state.location.pathname).toBe("/teacher/users");
    expect(screen.getByRole("dialog", { name: "创建学生账号" })).toBeInTheDocument();
    expect(calls).toBe(1);

    await act(async () => gate.resolve());
    expect(await screen.findByText("student4 已创建。")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/teacher/users");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("ignores close and duplicate actions in the submit-to-pending render race, then unlocks after failure", async () => {
    authenticate(teacher);
    serveAccounts();
    const gate = deferred<void>();
    let calls = 0;
    server.use(http.post(`${API_BASE}/users`, async () => {
      calls += 1;
      await gate.promise;
      return HttpResponse.json({ detail: "unavailable" }, { status: 503 });
    }));
    const user = userEvent.setup();
    const { router } = renderApp(["/teacher/users"]);
    await openAndFillValidStudent("student4", user);
    const dialog = screen.getByRole("dialog");
    const form = within(dialog).getByRole("button", { name: "创建学生账号" }).closest("form")!;
    const close = within(dialog).getByRole("button", { name: "关闭创建账号表单" });

    act(() => {
      fireEvent.submit(form);
      fireEvent.click(close);
      fireEvent.submit(form);
    });
    expect(screen.getByRole("dialog", { name: "创建学生账号" })).toBeInTheDocument();
    await waitFor(() => expect(calls).toBe(1));
    await user.click(screen.getByRole("link", { name: "返回作业台" }));
    expect(await screen.findByRole("alertdialog", { name: "账号正在创建" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "放弃未保存内容" })).toBeDisabled();
    expect(router.state.location.pathname).toBe("/teacher/users");

    await act(async () => gate.resolve());
    expect(await screen.findByRole("alert")).toHaveTextContent("服务器暂时无法创建账号");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "返回作业台" }));
    expect(await screen.findByRole("alertdialog", { name: "放弃未保存内容？" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "放弃未保存内容" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "放弃未保存内容" }));
    expect(await screen.findByRole("heading", { name: "作业发布与评阅" })).toBeInTheDocument();
  });

  it("traps focus, restores it, locks scrolling, and confirms dirty Escape dismissal", async () => {
    authenticate(teacher);
    serveAccounts();
    const user = userEvent.setup();
    renderApp(["/teacher/users"]);
    const trigger = await screen.findByRole("button", { name: "创建学生账号" });
    await user.click(trigger);
    expect(document.body.style.overflow).toBe("hidden");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "用户名" })).toHaveFocus());
    const close = screen.getByRole("button", { name: "关闭创建账号表单" });
    close.focus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "创建学生账号" })).toHaveFocus();
    screen.getByRole("textbox", { name: "用户名" }).focus();
    await user.type(screen.getByRole("textbox", { name: "用户名" }), "student4");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("alertdialog", { name: "放弃未保存内容？" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续编辑" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "用户名" })).toHaveFocus();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "放弃未保存内容" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe("");
    expect(trigger).toHaveFocus();
  });

  it("warns before unload and blocks in-app navigation while the form is dirty", async () => {
    authenticate(teacher);
    serveAccounts();
    const user = userEvent.setup();
    renderApp(["/teacher/users"]);
    await user.click(await screen.findByRole("button", { name: "创建学生账号" }));
    await user.type(screen.getByRole("textbox", { name: "用户名" }), "student4");

    const beforeUnload = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(beforeUnload)).toBe(false);
    await user.click(screen.getByRole("link", { name: "返回作业台" }));
    expect(await screen.findByRole("alertdialog", { name: "放弃未保存内容？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "放弃未保存内容" }));
    expect(await screen.findByRole("heading", { name: "作业发布与评阅" })).toBeInTheDocument();
  });

  it("shows a dedicated loading state without a stale register", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/users`, async () => {
      await delay("infinite");
      return HttpResponse.json(accountPage());
    }));
    renderApp(["/admin/users"]);

    expect(await screen.findByText("正在读取账号登记…")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "账号登记" })).not.toBeInTheDocument();
  });

  it("shows an error with retry and then an empty register", async () => {
    authenticate();
    let requestCount = 0;
    server.use(http.get(`${API_BASE}/users`, () => {
      requestCount += 1;
      if (requestCount === 1) return HttpResponse.json({ detail: "failed" }, { status: 500 });
      return HttpResponse.json(accountPage([], 0));
    }));
    const user = userEvent.setup();
    renderApp(["/admin/users"]);
    expect(await screen.findByText("暂时无法读取账号登记")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试读取账号" }));
    expect(await screen.findByText("还没有账号记录")).toBeInTheDocument();
  });

  it("provides bounded pagination over 50-row pages", async () => {
    authenticate();
    server.use(http.get(`${API_BASE}/users`, ({ request }) => {
      const offset = Number(new URL(request.url).searchParams.get("offset"));
      return HttpResponse.json(accountPage(accounts, 51, offset));
    }));
    const user = userEvent.setup();
    renderApp(["/admin/users"]);
    const previous = await screen.findByRole("button", { name: "上一页" });
    const next = screen.getByRole("button", { name: "下一页" });
    expect(previous).toBeDisabled();
    expect(next).toBeEnabled();
    await user.click(next);
    expect(await screen.findByText("第 51–51 条 / 共 51 条")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
  });

  it("renders an accessible register with long account text intact", async () => {
    authenticate();
    const long = { ...accounts[2], username: `student.${"x".repeat(50)}`, display_name: "超长姓名".repeat(20) };
    serveAccounts([long], 1);
    renderApp(["/admin/users"]);
    const table = await screen.findByRole("table", { name: "账号登记" });
    expect(within(table).getByRole("columnheader", { name: "账号" })).toBeInTheDocument();
    expect(within(table).getByText(`@${long.username}`)).toBeInTheDocument();
  });
});
