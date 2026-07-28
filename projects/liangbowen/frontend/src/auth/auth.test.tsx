import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ACCESS_TOKEN_STORAGE_KEY,
  MAX_ACCESS_TOKEN_CHARACTERS,
} from "../shared/api/client";
import { currentUserFixture, ids } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import { RequireRole } from "./RequireRole";
import { AuthContext, type AuthContextValue } from "./authState";
import { roleLanding, safePostLoginDestination, type WorkspaceRole } from "./routing";

const API_BASE = "http://localhost:8000/api/v1";
const TOKEN = "signed.jwt.token";
const teacher = {
  ...currentUserFixture,
  username: "ada",
  display_name: "Ada Lovelace",
  role: "teacher" as const,
};
const admin = {
  ...teacher,
  username: "admin",
  display_name: "系统管理员",
  role: "admin" as const,
};

beforeEach(() => {
  server.use(http.get(`${API_BASE}/assignments`, () => HttpResponse.json([])));
});

function tokenResponse() {
  return HttpResponse.json({ access_token: TOKEN, token_type: "bearer" });
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((fulfill) => {
    resolve = fulfill;
  });
  return { promise, resolve };
}

describe("session restoration", () => {
  it("redirects a tokenless visitor to login without requesting /me", async () => {
    const me = vi.fn(() => HttpResponse.json(currentUserFixture));
    server.use(http.get(`${API_BASE}/auth/me`, me));

    const { router } = renderApp(["/"]);

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(me).not.toHaveBeenCalled();
  });

  it.each([
    [teacher, "/teacher", "教师评阅工作台"],
    [currentUserFixture, "/student", "我的作业"],
  ])("restores a valid $role session to its landing", async (user, path, heading) => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        expect(request.headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
        return HttpResponse.json(user);
      }),
    );

    const { router } = renderApp(["/"]);

    expect(await screen.findByRole("heading", { level: 2, name: heading })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(path);
  });

  it("clears an invalid stored token after a 401", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(
      http.get(`${API_BASE}/auth/me`, () =>
        HttpResponse.json(
          { detail: "invalid authentication" },
          { status: 401, headers: { "X-Request-ID": ids.request } },
        ),
      ),
    );

    renderApp(["/teacher"]);

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("retains a token on a transient session error and recovers on retry", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    let calls = 0;
    server.use(
      http.get(`${API_BASE}/auth/me`, () => {
        calls += 1;
        return calls === 1 ? HttpResponse.error() : HttpResponse.json(teacher);
      }),
    );
    const user = userEvent.setup();

    renderApp(["/teacher"]);

    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法核验登录状态");
    expect(screen.queryByRole("heading", { name: "登录" })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(TOKEN);
    await user.click(screen.getByRole("button", { name: "重试会话验证" }));
    expect(await screen.findByRole("heading", { level: 2, name: "教师评阅工作台" })).toBeInTheDocument();
    expect(calls).toBe(2);
  });

  it("announces a real loading state while restoring a stored session", () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(http.get(`${API_BASE}/auth/me`, () => new Promise(() => undefined)));

    renderApp(["/teacher"]);

    expect(screen.getByRole("status")).toHaveTextContent("正在核验登录状态…");
    expect(screen.queryByRole("heading", { name: "登录" })).not.toBeInTheDocument();
  });

  it.each([
    { label: "empty", token: "" },
    { label: "C0 control", token: "token\u0000value" },
    { label: "C1 control", token: "token\u0080value" },
    { label: "overlong", token: "x".repeat(MAX_ACCESS_TOKEN_CHARACTERS + 1) },
  ])("clears an invalid $label stored token without requesting /me", async ({ token }) => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
    const me = vi.fn(() => HttpResponse.json(teacher));
    server.use(http.get(`${API_BASE}/auth/me`, me));

    renderApp(["/teacher"]);

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(me).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("does not lock the login page when storage reads throw", async () => {
    const me = vi.fn(() => HttpResponse.json(teacher));
    server.use(http.get(`${API_BASE}/auth/me`, me));
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    try {
      renderApp(["/teacher"]);
      expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
      expect(me).not.toHaveBeenCalled();
    } finally {
      getItem.mockRestore();
    }
  });
});

describe("session token compare-and-swap", () => {
  const tokenA = "session-a.jwt.token";
  const tokenB = "session-b.jwt.token";
  const userB = {
    ...currentUserFixture,
    username: "grace",
    display_name: "Grace Hopper",
  };

  it("keeps and revalidates B when the pinned A session fails before its storage event", async () => {
    const responseA = deferred<Response>();
    const authorizations: Array<string | null> = [];
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, tokenA);
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        const authorization = request.headers.get("Authorization");
        authorizations.push(authorization);
        if (authorization === `Bearer ${tokenA}`) return responseA.promise;
        if (authorization === `Bearer ${tokenB}`) return HttpResponse.json(userB);
        return HttpResponse.json({ detail: "unexpected token" }, { status: 401 });
      }),
    );
    renderApp(["/"]);
    await waitFor(() => expect(authorizations).toEqual([`Bearer ${tokenA}`]));

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, tokenB);
    await act(async () => {
      responseA.resolve(HttpResponse.json({ detail: "expired A" }, { status: 401 }));
    });

    expect(await screen.findByRole("heading", { name: "我的作业" })).toBeInTheDocument();
    expect(authorizations).toEqual([`Bearer ${tokenA}`, `Bearer ${tokenB}`]);
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(tokenB);

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ACCESS_TOKEN_STORAGE_KEY,
          oldValue: tokenA,
          newValue: tokenB,
          storageArea: window.localStorage,
        }),
      );
    });
    await waitFor(() => expect(authorizations).toHaveLength(3));
    expect(authorizations[2]).toBe(`Bearer ${tokenB}`);
  });

  it("switches to B immediately when its storage event arrives while A is pending", async () => {
    const responseA = deferred<Response>();
    const authorizations: Array<string | null> = [];
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, tokenA);
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        const authorization = request.headers.get("Authorization");
        authorizations.push(authorization);
        if (authorization === `Bearer ${tokenA}`) return responseA.promise;
        if (authorization === `Bearer ${tokenB}`) return HttpResponse.json(userB);
        return HttpResponse.json({ detail: "unexpected token" }, { status: 401 });
      }),
    );
    renderApp(["/"]);
    await waitFor(() => expect(authorizations).toEqual([`Bearer ${tokenA}`]));

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, tokenB);
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ACCESS_TOKEN_STORAGE_KEY,
          oldValue: tokenA,
          newValue: tokenB,
          storageArea: window.localStorage,
        }),
      );
    });

    expect(await screen.findByRole("heading", { name: "我的作业" })).toBeInTheDocument();
    await act(async () => {
      responseA.resolve(HttpResponse.json({ detail: "expired A" }, { status: 401 }));
    });
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(tokenB);
    expect(screen.getByRole("heading", { name: "我的作业" })).toBeInTheDocument();
    expect(authorizations).toEqual([`Bearer ${tokenA}`, `Bearer ${tokenB}`]);
  });

  it("logs out safely when fatal A cleanup cannot remove the still-current token", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, tokenA);
    server.use(
      http.get(`${API_BASE}/auth/me`, () =>
        HttpResponse.json({ detail: "expired A" }, { status: 401 }),
      ),
    );
    const removeItem = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    try {
      renderApp(["/teacher"]);
      expect(await screen.findByRole("heading", { name: "登录" })).toBeInTheDocument();
      expect(removeItem).toHaveBeenCalledWith(ACCESS_TOKEN_STORAGE_KEY);
    } finally {
      removeItem.mockRestore();
    }
  });
});

describe("login transaction", () => {
  it("stores the token, verifies /me, and enters the role landing", async () => {
    server.use(
      http.post(`${API_BASE}/auth/login`, async ({ request }) => {
        expect(await request.json()).toEqual({ username: "ada", password: "analytical" });
        return tokenResponse();
      }),
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        expect(request.headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
        return HttpResponse.json(teacher);
      }),
    );
    const user = userEvent.setup();
    const { router } = renderApp(["/login"]);

    await user.type(screen.getByLabelText("用户名"), "  ada  ");
    await user.type(screen.getByLabelText("密码"), "analytical{Enter}");

    expect(await screen.findByRole("heading", { level: 2, name: "教师评阅工作台" })).toBeInTheDocument();
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(TOKEN);
    expect(router.state.location.pathname).toBe("/teacher");
  });

  it("uses a generic credential error and leaves no token after a 401", async () => {
    server.use(
      http.post(`${API_BASE}/auth/login`, () =>
        HttpResponse.json(
          { detail: "password hash mismatch: secret detail" },
          { status: 401, headers: { "X-Request-ID": ids.request } },
        ),
      ),
    );
    const user = userEvent.setup();
    renderApp(["/login"]);

    await user.type(screen.getByLabelText("用户名"), "ada");
    await user.type(screen.getByLabelText("密码"), "not-the-password");
    await user.click(screen.getByRole("button", { name: "登录并进入工作台" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("用户名或密码不正确");
    expect(alert).toHaveTextContent(ids.request);
    expect(alert).not.toHaveTextContent(/hash|secret|not-the-password/u);
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("rolls back a newly stored token when /me verification fails", async () => {
    server.use(
      http.post(`${API_BASE}/auth/login`, () => tokenResponse()),
      http.get(`${API_BASE}/auth/me`, () =>
        HttpResponse.json({ detail: "service unavailable" }, { status: 503 }),
      ),
    );
    const user = userEvent.setup();
    renderApp(["/login"]);

    await user.type(screen.getByLabelText("用户名"), "ada");
    await user.type(screen.getByLabelText("密码"), "analytical");
    await user.click(screen.getByRole("button", { name: "登录并进入工作台" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("服务器暂时无法完成登录");
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("does not call /me or report success when token persistence fails", async () => {
    const me = vi.fn(() => HttpResponse.json(teacher));
    server.use(http.post(`${API_BASE}/auth/login`, () => tokenResponse()), http.get(`${API_BASE}/auth/me`, me));
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const user = userEvent.setup();
    try {
      renderApp(["/login"]);
      await user.type(screen.getByLabelText("用户名"), "ada");
      await user.type(screen.getByLabelText("密码"), "analytical");
      fireEvent.submit(screen.getByRole("button", { name: "登录并进入工作台" }).closest("form")!);

      expect(await screen.findByRole("alert")).toHaveTextContent("浏览器无法保存登录状态");
      expect(me).not.toHaveBeenCalled();
    } finally {
      setItem.mockRestore();
    }
  });
});

describe("login token compare-and-swap", () => {
  const tokenA = "login-a.jwt.token";
  const tokenB = "login-b.jwt.token";
  const userB = {
    ...currentUserFixture,
    username: "barbara",
    display_name: "Barbara Liskov",
  };

  async function startDeferredLogin(responseA: ReturnType<typeof deferred<Response>>) {
    const authorizations: Array<string | null> = [];
    server.use(
      http.post(`${API_BASE}/auth/login`, () =>
        HttpResponse.json({ access_token: tokenA, token_type: "bearer" }),
      ),
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        const authorization = request.headers.get("Authorization");
        authorizations.push(authorization);
        if (authorization === `Bearer ${tokenA}`) return responseA.promise;
        if (authorization === `Bearer ${tokenB}`) return HttpResponse.json(userB);
        return HttpResponse.json({ detail: "unexpected token" }, { status: 401 });
      }),
    );
    const user = userEvent.setup();
    renderApp(["/login"]);
    await user.type(screen.getByLabelText("用户名"), "ada");
    await user.type(screen.getByLabelText("密码"), "analytical");
    await user.click(screen.getByRole("button", { name: "登录并进入工作台" }));
    await waitFor(() => expect(authorizations).toEqual([`Bearer ${tokenA}`]));
    return authorizations;
  }

  it("keeps B and revalidates it when pinned A verification fails", async () => {
    const responseA = deferred<Response>();
    const authorizations = await startDeferredLogin(responseA);

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, tokenB);
    await act(async () => {
      responseA.resolve(HttpResponse.json({ detail: "A unavailable" }, { status: 503 }));
    });

    expect(await screen.findByRole("heading", { name: "我的作业" })).toBeInTheDocument();
    expect(authorizations).toEqual([`Bearer ${tokenA}`, `Bearer ${tokenB}`]);
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(tokenB);
  });

  it("does not publish A user data when A succeeds after storage changed to B", async () => {
    const responseA = deferred<Response>();
    const authorizations = await startDeferredLogin(responseA);

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, tokenB);
    await act(async () => {
      responseA.resolve(HttpResponse.json(teacher));
    });

    expect(await screen.findByRole("heading", { name: "我的作业" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "教师评阅工作台" })).not.toBeInTheDocument();
    expect(authorizations).toEqual([`Bearer ${tokenA}`, `Bearer ${tokenB}`]);
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(tokenB);
  });
});

describe("authorization boundaries", () => {
  it.each([401, 403])("clears a stored token when /me returns %s", async (status) => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(
      http.get(`${API_BASE}/auth/me`, () =>
        HttpResponse.json({ detail: "not authorized" }, { status }),
      ),
    );

    const { router } = renderApp(["/teacher"]);

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("treats an invalid /me contract as a fatal session and removes the token", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json({ role: "teacher" })));

    renderApp(["/teacher"]);

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("does not render a protected child when the authenticated role is wrong", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(currentUserFixture)));

    renderApp(["/teacher/reports/secret-report"]);

    expect(await screen.findByRole("heading", { level: 2, name: "无权访问此页面" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "评阅报告页" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "去自己的工作台" })).toHaveAttribute("href", "/student");
  });

  it("rejects an admin role without loading teacher content", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(
      http.get(`${API_BASE}/auth/me`, () =>
        HttpResponse.json(admin),
      ),
    );

    renderApp(["/teacher"]);

    expect(await screen.findByRole("heading", { level: 2, name: "无权访问此页面" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "教师评阅工作台" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "去自己的工作台" })).toHaveAttribute("href", "/admin/users");
  });

  it("logs out even when browser storage removal throws", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)));
    const user = userEvent.setup();
    const { router } = renderApp(["/teacher"]);
    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();
    const removeItem = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    try {
      await user.click(screen.getByRole("button", { name: "退出登录" }));
      expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/login");
    } finally {
      removeItem.mockRestore();
    }
  });

  it("responds to a cross-tab token removal", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)));
    renderApp(["/teacher"]);
    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();

    window.localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY);
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ACCESS_TOKEN_STORAGE_KEY,
          oldValue: TOKEN,
          newValue: null,
          storageArea: window.localStorage,
        }),
      );
    });

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
  });

  it.each([
    { label: "empty", token: "" },
    { label: "control", token: "token\u0000value" },
    { label: "overlong", token: "x".repeat(MAX_ACCESS_TOKEN_CHARACTERS + 1) },
  ])("clears an invalid $label token received from another tab", async ({ token }) => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    const me = vi.fn(() => HttpResponse.json(teacher));
    server.use(http.get(`${API_BASE}/auth/me`, me));
    renderApp(["/teacher"]);
    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token);
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ACCESS_TOKEN_STORAGE_KEY,
          oldValue: TOKEN,
          newValue: token,
          storageArea: window.localStorage,
        }),
      );
    });

    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(me).toHaveBeenCalledOnce();
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("revalidates a valid token received from another tab", async () => {
    const updatedToken = "updated.jwt.token";
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    const authorizations: Array<string | null> = [];
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        authorizations.push(request.headers.get("Authorization"));
        return HttpResponse.json(teacher);
      }),
    );
    renderApp(["/teacher"]);
    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, updatedToken);
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ACCESS_TOKEN_STORAGE_KEY,
          oldValue: TOKEN,
          newValue: updatedToken,
          storageArea: window.localStorage,
        }),
      );
    });

    await waitFor(() => expect(authorizations).toHaveLength(2));
    expect(authorizations).toEqual([`Bearer ${TOKEN}`, `Bearer ${updatedToken}`]);
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(updatedToken);
    expect(screen.getByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();
  });

  it("keeps a newer valid token when a stale invalid storage event arrives", async () => {
    const updatedToken = "newer.jwt.token";
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    const authorizations: Array<string | null> = [];
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        authorizations.push(request.headers.get("Authorization"));
        return HttpResponse.json(teacher);
      }),
    );
    renderApp(["/teacher"]);
    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();

    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, updatedToken);
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ACCESS_TOKEN_STORAGE_KEY,
          oldValue: TOKEN,
          newValue: "stale\u0000token",
          storageArea: window.localStorage,
        }),
      );
    });

    await waitFor(() => expect(authorizations).toHaveLength(2));
    expect(authorizations[1]).toBe(`Bearer ${updatedToken}`);
    expect(window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)).toBe(updatedToken);
    expect(screen.getByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();
  });

  it("leaves memory logged out when invalid cross-tab token removal throws", async () => {
    const invalidToken = "invalid\u0000token";
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    const me = vi.fn(() => HttpResponse.json(teacher));
    server.use(http.get(`${API_BASE}/auth/me`, me));
    renderApp(["/teacher"]);
    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, invalidToken);
    const removeItem = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    try {
      act(() => {
        window.dispatchEvent(
          new StorageEvent("storage", {
            key: ACCESS_TOKEN_STORAGE_KEY,
            oldValue: TOKEN,
            newValue: invalidToken,
            storageArea: window.localStorage,
          }),
        );
      });

      expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
      expect(me).toHaveBeenCalledOnce();
      expect(removeItem).toHaveBeenCalledWith(ACCESS_TOKEN_STORAGE_KEY);
    } finally {
      removeItem.mockRestore();
    }
  });

  it("renders an explicit 404 route", async () => {
    renderApp(["/not-in-register"]);
    expect(await screen.findByRole("heading", { level: 2, name: "页面未找到" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回入口" })).toHaveAttribute("href", "/login");
  });
});

describe("safe post-login navigation", () => {
  it.each([
    ["admin", "/admin/users"],
    ["teacher", "/teacher"],
    ["student", "/student"],
  ] as const)("uses the %s landing", (role, expected) => {
    expect(roleLanding(role)).toBe(expected);
  });

  it.each([
    ["/teacher", "teacher", "/teacher"],
    ["/teacher/assignments/a-1?tab=reports#summary", "teacher", "/teacher/assignments/a-1?tab=reports#summary"],
    ["/teacher/users?offset=50#accounts", "teacher", "/teacher/users?offset=50#accounts"],
    ["/student/assignments/a-1", "student", "/student/assignments/a-1"],
    ["/admin/users?offset=50#accounts", "admin", "/admin/users?offset=50#accounts"],
    ["https://evil.example/teacher", "teacher", "/teacher"],
    ["//evil.example/teacher", "teacher", "/teacher"],
    ["/teacher/reports/r-1", "student", "/student"],
    ["/student/assignments/a-1", "teacher", "/teacher"],
    ["/teacher", "admin", "/admin/users"],
    ["/teacher/users", "admin", "/admin/users"],
    ["/admin/users", "teacher", "/teacher"],
    ["/admin/users", "student", "/student"],
    ["/admin/users-extra", "admin", "/admin/users"],
    ["/admin/users/extra", "admin", "/admin/users"],
    ["/teacher/users-extra", "teacher", "/teacher"],
    ["/teacher/users/extra", "teacher", "/teacher"],
  ] as const)("maps %s for %s to %s", (from, role, expected) => {
    expect(safePostLoginDestination(from, role)).toBe(expected);
  });

  it("returns an unauthenticated teacher deep link after successful login", async () => {
    server.use(
      http.post(`${API_BASE}/auth/login`, () => tokenResponse()),
      http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)),
    );
    const user = userEvent.setup();
    const { router } = renderApp(["/teacher/reports/report-1"]);
    expect(await screen.findByRole("heading", { level: 2, name: "登录" })).toBeInTheDocument();
    expect(router.state.location.state).toEqual({ from: "/teacher/reports/report-1" });

    await user.type(screen.getByLabelText("用户名"), "ada");
    await user.type(screen.getByLabelText("密码"), "analytical");
    await user.click(screen.getByRole("button", { name: "登录并进入工作台" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("报告地址无效");
    expect(router.state.location.pathname).toBe("/teacher/reports/report-1");
  });
});

function renderRoleGuard(
  user: AuthContextValue["user"],
  role: WorkspaceRole | readonly WorkspaceRole[],
) {
  const value: AuthContextValue = {
    user,
    loading: false,
    sessionError: null,
    login: vi.fn(),
    logout: vi.fn(),
    retrySession: vi.fn(),
  };
  render(
    <MemoryRouter>
      <AuthContext.Provider value={value}>
        <RequireRole role={role}>
          <h2>受保护账号登记簿</h2>
        </RequireRole>
      </AuthContext.Provider>
    </MemoryRouter>,
  );
}

describe("generalized role guard", () => {
  it.each([admin, teacher])("allows $role through a multi-role guard", (user) => {
    renderRoleGuard(user, ["admin", "teacher"]);
    expect(screen.getByRole("heading", { name: "受保护账号登记簿" })).toBeInTheDocument();
  });

  it("preserves denial UI and links an administrator to its own landing", () => {
    renderRoleGuard(admin, "teacher");
    expect(screen.getByRole("heading", { name: "无权访问此页面" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "受保护账号登记簿" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "去自己的工作台" })).toHaveAttribute("href", "/admin/users");
  });

  it("keeps the teacher-only users route unavailable to students", () => {
    renderRoleGuard(currentUserFixture, ["teacher"]);
    expect(screen.getByRole("heading", { name: "无权访问此页面" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "去自己的工作台" })).toHaveAttribute("href", "/student");
  });
});

describe("login form boundaries", () => {
  it("associates validation errors with both required fields", async () => {
    const user = userEvent.setup();
    renderApp(["/login"]);

    await user.click(screen.getByRole("button", { name: "登录并进入工作台" }));

    expect(screen.getByLabelText("用户名")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("用户名")).toHaveAccessibleDescription("用户名需为 1–64 个字符。");
    expect(screen.getByLabelText("密码")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("密码")).toHaveAccessibleDescription("请输入密码（最多 1024 个字符）。");
  });

  it("reveals and hides the password without changing its value", async () => {
    const user = userEvent.setup();
    renderApp(["/login"]);
    const password = screen.getByLabelText("密码");
    await user.type(password, "keep me secret");

    await user.click(screen.getByRole("button", { name: "显示密码" }));
    expect(password).toHaveAttribute("type", "text");
    expect(password).toHaveValue("keep me secret");
    await user.click(screen.getByRole("button", { name: "隐藏密码" }));
    expect(password).toHaveAttribute("type", "password");
  });

  it("coalesces rapid duplicate submissions into one login transaction", async () => {
    let releaseLogin!: () => void;
    const gate = new Promise<void>((resolve) => {
      releaseLogin = resolve;
    });
    const loginHandler = vi.fn(async () => {
      await gate;
      return tokenResponse();
    });
    server.use(
      http.post(`${API_BASE}/auth/login`, loginHandler),
      http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)),
    );
    const user = userEvent.setup();
    renderApp(["/login"]);
    await user.type(screen.getByLabelText("用户名"), "ada");
    await user.type(screen.getByLabelText("密码"), "analytical");
    const form = screen.getByRole("button", { name: "登录并进入工作台" }).closest("form")!;

    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(screen.getByRole("button", { name: "正在核验…" })).toBeDisabled();
    await waitFor(() => expect(loginHandler).toHaveBeenCalledOnce());
    await act(async () => releaseLogin());

    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();
  });

  it("redirects an already authenticated user away from login", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, TOKEN);
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(teacher)));
    const { router } = renderApp(["/login"]);

    expect(await screen.findByRole("heading", { name: "教师评阅工作台" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/teacher");
  });

  it("focuses the first invalid username after keyboard submission", async () => {
    const user = userEvent.setup();
    renderApp(["/login"]);
    const submit = screen.getByRole("button", { name: "登录并进入工作台" });
    submit.focus();

    await user.keyboard("{Enter}");

    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText("用户名")));
  });

  it("focuses the invalid password when username is valid", async () => {
    const user = userEvent.setup();
    renderApp(["/login"]);
    const username = screen.getByLabelText("用户名");

    await user.type(username, "ada{Enter}");

    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText("密码")));
  });
});
