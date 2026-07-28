import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  accountCreatorSchema,
  userAccountPageSchema,
  userAccountSchema,
} from "../shared/api/schemas";
import { server } from "../test/server";
import { useCreateUser, useUsers, userKeys, type UserCreatePayload } from "./api";

const API_BASE = "http://localhost:8000/api/v1";
const actorA = "10000000-0000-4000-8000-000000000001";
const actorB = "10000000-0000-4000-8000-000000000002";
const accountId = "10000000-0000-4000-8000-000000000003";
const createdAt = "2026-07-28T12:00:00Z";

const creator = {
  id: actorA,
  username: "admin",
  display_name: "系统管理员",
};

const studentAccount = {
  id: accountId,
  username: "student4",
  display_name: "张三",
  role: "student" as const,
  is_active: true,
  created_at: createdAt,
  created_by: creator,
};

function queryWrapper(queryClient: QueryClient) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Number.POSITIVE_INFINITY },
      mutations: { retry: false },
    },
  });
}

function cachedMutationState(queryClient: QueryClient) {
  return queryClient.getMutationCache().getAll().map((mutation) => mutation.state);
}

function expectNoCachedSecret(queryClient: QueryClient, secret: string) {
  expect(JSON.stringify(cachedMutationState(queryClient))).not.toContain(secret);
}

function expectNoSecret(value: unknown, secret: string) {
  expect(JSON.stringify(value) ?? "").not.toContain(secret);
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((fulfill) => {
    resolve = fulfill;
  });
  return { promise, resolve };
}

afterEach(() => vi.restoreAllMocks());

describe("account response schemas", () => {
  it("accepts the password-free creator, account, and page contracts", () => {
    expect(accountCreatorSchema.parse(creator)).toEqual(creator);
    expect(userAccountSchema.parse(studentAccount)).toEqual(studentAccount);
    expect(
      userAccountPageSchema.parse({
        items: [studentAccount],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    ).toEqual({ items: [studentAccount], total: 1, limit: 50, offset: 0 });
  });

  it.each([
    ["creator password", { ...creator, password: "Course2026!Secure" }, accountCreatorSchema],
    ["creator password hash", { ...creator, password_hash: "forbidden" }, accountCreatorSchema],
    ["account password", { ...studentAccount, password: "Course2026!Secure" }, userAccountSchema],
    ["account password hash", { ...studentAccount, password_hash: "forbidden" }, userAccountSchema],
    [
      "nested creator password hash",
      { ...studentAccount, created_by: { ...creator, password_hash: "forbidden" } },
      userAccountSchema,
    ],
  ])("rejects a %s field", (_label, value, schema) => {
    expect(() => schema.parse(value)).toThrow();
  });

  it("rejects a secret field anywhere in an account page response", () => {
    expect(() =>
      userAccountPageSchema.parse({
        items: [{ ...studentAccount, password_hash: "forbidden" }],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    ).toThrow();
  });
});

describe("account queries", () => {
  it("keys cached pages by actor and offset while sending bounded pagination", async () => {
    const requests: string[] = [];
    server.use(
      http.get(`${API_BASE}/users`, ({ request }) => {
        const url = new URL(request.url);
        requests.push(url.search);
        const offset = Number(url.searchParams.get("offset"));
        return HttpResponse.json({
          items: [],
          total: 0,
          limit: 50,
          offset,
        });
      }),
    );
    const queryClient = createQueryClient();
    const { result, rerender } = renderHook(
      ({ actorId, offset }) => useUsers(actorId, offset),
      {
        initialProps: { actorId: actorA, offset: 0 },
        wrapper: queryWrapper(queryClient),
      },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(requests).toEqual(["?limit=50&offset=0"]);
    expect(queryClient.getQueryData(userKeys.list(actorA, 0))).toEqual({
      items: [], total: 0, limit: 50, offset: 0,
    });

    rerender({ actorId: actorA, offset: 50 });
    await waitFor(() => expect(result.current.data?.offset).toBe(50));
    expect(requests).toEqual(["?limit=50&offset=0", "?limit=50&offset=50"]);
    expect(queryClient.getQueryData(userKeys.list(actorA, 50))).toEqual({
      items: [], total: 0, limit: 50, offset: 50,
    });

    rerender({ actorId: actorB, offset: 50 });
    await waitFor(() => expect(requests).toHaveLength(3));
    expect(queryClient.getQueryData(userKeys.list(actorB, 50))).toEqual({
      items: [], total: 0, limit: 50, offset: 50,
    });
    expect(queryClient.getQueryData(userKeys.list(actorA, 50))).toEqual({
      items: [], total: 0, limit: 50, offset: 50,
    });
  });

  it("keeps an undefined actor disabled and out of the network", async () => {
    const handler = vi.fn(() => HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }));
    server.use(http.get(`${API_BASE}/users`, handler));
    const queryClient = createQueryClient();
    const { result } = renderHook(() => useUsers(undefined, 0), {
      wrapper: queryWrapper(queryClient),
    });

    expect(result.current.fetchStatus).toBe("idle");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(handler).not.toHaveBeenCalled();
  });
});

describe("account creation mutation", () => {
  it("returns only the parsed password-free account and invalidates only the current actor lists", async () => {
    let requestBody: unknown;
    server.use(
      http.post(`${API_BASE}/users`, async ({ request }) => {
        requestBody = await request.json();
        return HttpResponse.json(studentAccount, { status: 201 });
      }),
    );
    const queryClient = createQueryClient();
    queryClient.setQueryData(userKeys.list(actorA, 0), {
      items: [], total: 0, limit: 50, offset: 0,
    });
    queryClient.setQueryData(userKeys.list(actorA, 50), {
      items: [], total: 0, limit: 50, offset: 50,
    });
    queryClient.setQueryData(userKeys.list(actorB, 0), {
      items: [], total: 0, limit: 50, offset: 0,
    });
    const { result } = renderHook(() => useCreateUser(actorA), {
      wrapper: queryWrapper(queryClient),
    });
    const payload: UserCreatePayload = {
      username: "student4",
      display_name: "张三",
      role: "student",
      password: "Course2026!Secure",
    };

    let created: Awaited<ReturnType<typeof result.current.mutateAsync>>;
    await act(async () => {
      created = await result.current.mutateAsync(payload);
    });

    expect(requestBody).toEqual(payload);
    expect(created!).toEqual(studentAccount);
    expect(created!).not.toHaveProperty("password");
    expect(created!).not.toHaveProperty("password_hash");
    expect(result.current.variables).not.toEqual(payload);
    expectNoSecret(result.current.variables, payload.password);
    expectNoCachedSecret(queryClient, payload.password);
    expect(queryClient.getQueryState(userKeys.list(actorA, 0))?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(userKeys.list(actorA, 50))?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(userKeys.list(actorB, 0))?.isInvalidated).toBe(false);
  });

  it("rejects a create response that contains a secret", async () => {
    server.use(
      http.post(`${API_BASE}/users`, () =>
        HttpResponse.json({ ...studentAccount, password_hash: "forbidden" }, { status: 201 }),
      ),
    );
    const queryClient = createQueryClient();
    const { result } = renderHook(() => useCreateUser(actorA), {
      wrapper: queryWrapper(queryClient),
    });

    const payload: UserCreatePayload = {
      username: "student4",
      display_name: "张三",
      role: "student",
      password: "Course2026!Secure",
    };

    await act(async () => {
      await expect(result.current.mutateAsync(payload)).rejects.toThrow();
    });
    expect(result.current.variables).not.toEqual(payload);
    expectNoSecret(result.current.variables, payload.password);
    expectNoCachedSecret(queryClient, payload.password);
  });

  it("keeps concurrent requests isolated and stores only non-secret operation tokens", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post(`${API_BASE}/users`, async ({ request }) => {
        const body = await request.json() as UserCreatePayload;
        bodies.push(body);
        if (body.username === "student.fail") {
          return HttpResponse.json({ detail: "username already exists" }, { status: 409 });
        }
        return HttpResponse.json({ ...studentAccount, username: body.username }, { status: 201 });
      }),
    );
    const queryClient = createQueryClient();
    const { result } = renderHook(() => useCreateUser(actorA), {
      wrapper: queryWrapper(queryClient),
    });
    const successful: UserCreatePayload = {
      username: "student.ok",
      display_name: "成功学生",
      role: "student",
      password: "Success2026!Secret",
    };
    const rejected: UserCreatePayload = {
      username: "student.fail",
      display_name: "失败学生",
      role: "student",
      password: "Failure2026!Secret",
    };

    let outcomes: PromiseSettledResult<unknown>[] = [];
    await act(async () => {
      outcomes = await Promise.allSettled([
        result.current.mutateAsync(successful),
        result.current.mutateAsync(rejected),
      ]);
    });

    expect(outcomes.map((outcome) => outcome.status).sort()).toEqual(["fulfilled", "rejected"]);
    expect(bodies).toEqual(expect.arrayContaining([successful, rejected]));
    const variables = cachedMutationState(queryClient).map((state) => state.variables);
    expect(variables).toHaveLength(2);
    expect(new Set(variables.map((value) => JSON.stringify(value))).size).toBe(2);
    expect(variables).toEqual(expect.arrayContaining([
      expect.objectContaining({ actorId: actorA, id: expect.any(Number) }),
      expect.objectContaining({ actorId: actorA, id: expect.any(Number) }),
    ]));
    expectNoCachedSecret(queryClient, successful.password);
    expectNoCachedSecret(queryClient, rejected.password);
  });

  it("releases the private plaintext buffer before a deferred response settles", async () => {
    const responseGate = deferred<void>();
    const requestArrived = deferred<void>();
    server.use(
      http.post(`${API_BASE}/users`, async ({ request }) => {
        await request.json();
        requestArrived.resolve();
        await responseGate.promise;
        return HttpResponse.json(studentAccount, { status: 201 });
      }),
    );
    const queryClient = createQueryClient();
    const { result } = renderHook(() => useCreateUser(actorA), {
      wrapper: queryWrapper(queryClient),
    });
    const payload: UserCreatePayload = {
      username: "student4",
      display_name: "张三",
      role: "student",
      password: "Transient2026!Secret",
    };
    const originalDelete = Map.prototype.delete;
    let releasedBufferSize: number | undefined;
    vi.spyOn(Map.prototype, "delete").mockImplementation(function (
      this: Map<unknown, unknown>,
      key: unknown,
    ) {
      const releasesPayload = this.get(key) === payload;
      const deleted = originalDelete.call(this, key);
      if (releasesPayload) releasedBufferSize = this.size;
      return deleted;
    });

    let pending!: ReturnType<typeof result.current.mutateAsync>;
    act(() => {
      pending = result.current.mutateAsync(payload);
    });
    await requestArrived.promise;

    expect(releasedBufferSize).toBe(0);
    expectNoCachedSecret(queryClient, payload.password);
    responseGate.resolve();
    await act(async () => {
      await pending;
    });
  });

  it("keeps a pending actor A mutation isolated after the hook switches to actor B", async () => {
    const responseGate = deferred<void>();
    const requestArrived = deferred<void>();
    server.use(
      http.post(`${API_BASE}/users`, async () => {
        requestArrived.resolve();
        await responseGate.promise;
        return HttpResponse.json(studentAccount, { status: 201 });
      }),
    );
    const queryClient = createQueryClient();
    queryClient.setQueryData(userKeys.list(actorA, 0), {
      items: [], total: 0, limit: 50, offset: 0,
    });
    queryClient.setQueryData(userKeys.list(actorB, 0), {
      items: [], total: 0, limit: 50, offset: 0,
    });
    const { result, rerender } = renderHook(
      ({ actorId }) => useCreateUser(actorId),
      {
        initialProps: { actorId: actorA },
        wrapper: queryWrapper(queryClient),
      },
    );
    const payload: UserCreatePayload = {
      username: "student4",
      display_name: "张三",
      role: "student",
      password: "ActorA2026!Secret",
    };
    const staleSuccess = vi.fn();

    let pending!: ReturnType<typeof result.current.mutateAsync>;
    act(() => {
      pending = result.current.mutateAsync(payload, { onSuccess: staleSuccess });
    });
    await requestArrived.promise;
    rerender({ actorId: actorB });

    expect(result.current.isIdle).toBe(true);
    expect(result.current.data).toBeUndefined();
    responseGate.resolve();
    await act(async () => {
      await pending;
    });

    expect(queryClient.getQueryState(userKeys.list(actorA, 0))?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(userKeys.list(actorB, 0))?.isInvalidated).toBe(false);
    expect(staleSuccess).not.toHaveBeenCalled();
    expect(result.current.isIdle).toBe(true);
    expect(result.current.data).toBeUndefined();
    expect(result.current.variables).toBeUndefined();
    expect(queryClient.getMutationCache().getAll()[0]?.options.mutationKey).toEqual([
      ...userKeys.actor(actorA),
      "create",
    ]);
    expectNoCachedSecret(queryClient, payload.password);
  });
});
