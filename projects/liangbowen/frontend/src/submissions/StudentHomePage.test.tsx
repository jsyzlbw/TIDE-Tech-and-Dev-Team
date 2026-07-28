import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { ACCESS_TOKEN_STORAGE_KEY } from "../shared/api/client";
import { ids } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const API_BASE = "http://localhost:8000/api/v1";
const TOKEN = "student.jwt.token";
const student = {
  id: ids.user,
  username: "grace",
  display_name: "Grace Hopper",
  role: "student" as const,
};

function assignment(index: number, overrides: Record<string, unknown> = {}) {
  return {
    id: `${String(index).padStart(8, "0")}-1111-4111-8111-111111111111`,
    code: `HW-${String(index).padStart(2, "0")}`,
    title: `数据结构作业 ${index}`,
    due_at: "2099-07-30T12:00:00Z",
    status: "published",
    created_at: "2026-07-20T08:00:00Z",
    published_at: "2026-07-21T08:00:00Z",
    ...overrides,
  };
}

describe("student assignment desk", () => {
  beforeEach(() => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(student)));
  });

  it("shows the signed-in student, safe assignment cards, and real pagination", async () => {
    const requests: string[] = [];
    server.use(http.get(`${API_BASE}/assignments`, ({ request }) => {
      const url = new URL(request.url);
      requests.push(url.search);
      const offset = Number(url.searchParams.get("offset"));
      if (offset === 20) return HttpResponse.json([assignment(21)]);
      return HttpResponse.json([
        ...Array.from({ length: 20 }, (_, index) => assignment(index + 1)),
        assignment(99),
      ]);
    }));
    const user = userEvent.setup();
    const { router } = renderApp(["/student"]);

    expect(await screen.findByRole("heading", { name: "我的作业" })).toBeInTheDocument();
    expect(screen.getByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.getByText("@grace")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /打开 HW-01/u })).toHaveAttribute(
      "href",
      "/student/assignments/00000001-1111-4111-8111-111111111111",
    );
    expect(screen.queryByText("HW-99")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/rubric|created_by|mattermost/iu);
    expect(requests[0]).toContain("limit=21");
    expect(requests[0]).toContain("offset=0");

    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("HW-21")).toBeInTheDocument();
    expect(screen.getByText("第 2 页")).toBeInTheDocument();
    expect(requests.at(-1)).toContain("offset=20");

    await user.click(screen.getByRole("button", { name: "退出登录" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  });

  it("labels closed and expired assignments honestly and prevents stale opening", async () => {
    server.use(http.get(`${API_BASE}/assignments`, () => HttpResponse.json([
      assignment(1, { status: "closed" }),
      assignment(2, { due_at: "2020-07-30T12:00:00Z" }),
    ])));
    renderApp(["/student"]);

    expect(await screen.findByText("HW-01")).toBeInTheDocument();
    expect(screen.getAllByText("不可提交")).toHaveLength(2);
    expect(screen.getByText("已关闭")).toBeInTheDocument();
    expect(screen.getByText("已逾期")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /查看 HW-/u })).toHaveLength(2);
  });

  it("renders empty and retryable error states without hiding a known error behind loading", async () => {
    let attempt = 0;
    server.use(http.get(`${API_BASE}/assignments`, () => {
      attempt += 1;
      return attempt === 1
        ? HttpResponse.json({ detail: "offline" }, { status: 503 })
        : HttpResponse.json([]);
    }));
    const user = userEvent.setup();
    renderApp(["/student"]);

    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取作业");
    expect(screen.queryByText("正在读取作业")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试读取作业" }));
    expect(await screen.findByText("还没有已发布的作业")).toBeInTheDocument();
  });
});
