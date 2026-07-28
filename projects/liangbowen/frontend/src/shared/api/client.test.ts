import { delay, http, HttpResponse } from "msw";
import { z } from "zod";
import { describe, expect, it, vi } from "vitest";

import {
  assignmentSummaryFixture,
  currentUserFixture,
  ids,
  reviewReportFixture,
} from "../../test/fixtures";
import { server } from "../../test/server";
import { ACCESS_TOKEN_STORAGE_KEY, createApiClient } from "./client";
import {
  ApiAbortError,
  ApiConfigurationError,
  ApiContractError,
  ApiError,
  ApiNetworkError,
  ApiTimeoutError,
} from "./errors";
import {
  assignmentSummarySchema,
  currentUserSchema,
  reviewReportSchema,
} from "./schemas";

const API_BASE = "http://api.test/api/v1";

function client(overrides: Parameters<typeof createApiClient>[0] = {}) {
  return createApiClient({ baseUrl: API_BASE, mode: "test", timeoutMs: 100, ...overrides });
}

describe("typed API client", () => {
  it("sends JSON Accept without bearer or a forgeable request ID", async () => {
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        expect(request.headers.get("Accept")).toBe("application/json");
        expect(request.headers.has("Authorization")).toBe(false);
        expect(request.headers.has("X-Request-ID")).toBe(false);
        expect(request.headers.has("Content-Type")).toBe(false);
        return HttpResponse.json(currentUserFixture);
      }),
    );

    await expect(client({ getToken: () => null }).get("/auth/me", currentUserSchema)).resolves.toEqual(
      currentUserFixture,
    );
  });

  it("sends a validated bearer token", async () => {
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        expect(request.headers.get("Authorization")).toBe("Bearer signed.jwt.token");
        return HttpResponse.json(currentUserFixture);
      }),
    );

    await client({ getToken: () => "signed.jwt.token" }).get("auth/me", currentUserSchema);
  });

  it("reads a faithful nested assignment summary", async () => {
    server.use(
      http.get(`${API_BASE}/assignments/${ids.assignment}/summary`, () =>
        HttpResponse.json(assignmentSummaryFixture),
      ),
    );

    await expect(
      client().get(`/assignments/${ids.assignment}/summary`, assignmentSummarySchema),
    ).resolves.toEqual(assignmentSummaryFixture);
  });

  it("rejects successful response drift without exposing the payload", async () => {
    server.use(
      http.get(`${API_BASE}/auth/me`, () =>
        HttpResponse.json({ ...currentUserFixture, role: "owner", access_token: "secret" }),
      ),
    );

    const error = await client().get("/auth/me", currentUserSchema).catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiContractError);
    expect(String(error)).not.toContain("secret");
    expect(String(error)).not.toContain("owner");
  });

  it.each([
    [401, { detail: "invalid authentication" }, "HTTP_401", "invalid authentication"],
    [404, { code: "NOT_FOUND", message: "assignment not found" }, "NOT_FOUND", "assignment not found"],
    [409, { detail: "report was already reviewed", request_id: ids.request }, "HTTP_409", "report was already reviewed"],
    [422, { detail: [{ type: "missing", loc: ["body", "title"], msg: "Field required", input: "secret" }] }, "VALIDATION_ERROR", "Request validation failed"],
    [500, "Internal Server Error", "HTTP_500", "Request failed"],
  ] as const)("normalizes a %i response", async (status, body, code, message) => {
    server.use(
      http.get(`${API_BASE}/failure`, () => {
        const headers = { "X-Request-ID": ids.request };
        return typeof body === "string"
          ? HttpResponse.text(body, { status, headers })
          : HttpResponse.json(body, { status, headers });
      }),
    );

    const error = await client().get("/failure", currentUserSchema).catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status, code, message, requestId: ids.request });
    expect(String(error)).not.toContain("secret");
  });

  it("sanitizes standard error details", async () => {
    server.use(
      http.get(`${API_BASE}/failure`, () =>
        HttpResponse.json(
          {
            code: "CONFLICT",
            message: "conflict",
            request_id: ids.request,
            details: { field: "version", expected: 2, access_token: "secret", password: "hidden" },
          },
          { status: 409, headers: { "X-Request-ID": ids.request } },
        ),
      ),
    );

    const error = await client().get("/failure", currentUserSchema).catch((caught) => caught);
    expect(error).toMatchObject({ details: { field: "version", expected: 2 } });
    expect(JSON.stringify(error)).not.toContain("secret");
    expect(JSON.stringify(error)).not.toContain("hidden");
  });

  it.each([
    ["invalid JSON", "application/json", "{"],
    ["wrong content type", "text/html", "{}"],
  ])("rejects %s on success", async (_label, contentType, body) => {
    server.use(
      http.get(`${API_BASE}/contract`, () =>
        new HttpResponse(body, { status: 200, headers: { "Content-Type": contentType } }),
      ),
    );
    await expect(client().get("/contract", z.object({ ok: z.boolean() }))).rejects.toBeInstanceOf(
      ApiContractError,
    );
  });

  it("rejects a declared oversized response before parsing", async () => {
    server.use(
      http.get(`${API_BASE}/large`, () =>
        HttpResponse.json({ ok: true }, { headers: { "Content-Length": "2097153" } }),
      ),
    );
    await expect(client().get("/large", z.object({ ok: z.boolean() }))).rejects.toBeInstanceOf(
      ApiContractError,
    );
  });

  it("rejects an actually oversized response when Content-Length lies", async () => {
    server.use(
      http.get(`${API_BASE}/large`, () =>
        new HttpResponse(JSON.stringify({ value: "x".repeat(300) }), {
          headers: { "Content-Type": "application/json", "Content-Length": "10" },
        }),
      ),
    );
    await expect(
      client({ maxResponseBytes: 128 }).get("/large", z.object({ value: z.string() })),
    ).rejects.toBeInstanceOf(ApiContractError);
  });

  it("distinguishes timeout from caller abort", async () => {
    server.use(http.get(`${API_BASE}/slow`, async () => (await delay(500), HttpResponse.json({ ok: true }))));
    await expect(
      client({ timeoutMs: 5 }).get("/slow", z.object({ ok: z.boolean() })),
    ).rejects.toBeInstanceOf(ApiTimeoutError);

    const controller = new AbortController();
    const request = client({ timeoutMs: 500 }).get("/slow", z.object({ ok: z.boolean() }), {
      signal: controller.signal,
    });
    controller.abort();
    await expect(request).rejects.toBeInstanceOf(ApiAbortError);
  });

  it("normalizes a transport failure without retrying a mutation", async () => {
    let calls = 0;
    server.use(
      http.post(`${API_BASE}/mutation`, () => {
        calls += 1;
        return HttpResponse.error();
      }),
    );
    await expect(
      client().post("/mutation", { answer: "safe" }, z.object({ ok: z.boolean() })),
    ).rejects.toBeInstanceOf(ApiNetworkError);
    expect(calls).toBe(1);
  });

  it("normalizes a response-stream failure without exposing its cause", async () => {
    const requestFetch = vi.fn(async () => {
      const stream = new ReadableStream({
        start(controller) {
          controller.error(new TypeError("secret response chunk"));
        },
      });
      return new Response(stream, { headers: { "Content-Type": "application/json" } });
    }) as unknown as typeof fetch;

    const error = await client({ fetch: requestFetch })
      .get("/stream", z.object({ ok: z.boolean() }))
      .catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiNetworkError);
    expect(String(error)).not.toContain("secret response chunk");
  });

  it("keeps the timeout active while reading the response body", async () => {
    const requestFetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const stream = new ReadableStream({
        start(controller) {
          const fallback = setTimeout(() => controller.error(new TypeError("late stream")), 100);
          init?.signal?.addEventListener(
            "abort",
            () => {
              clearTimeout(fallback);
              controller.error(new DOMException("aborted", "AbortError"));
            },
            { once: true },
          );
        },
      });
      return new Response(stream, { headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;

    await expect(
      client({ fetch: requestFetch, timeoutMs: 5 }).get(
        "/stream",
        z.object({ ok: z.boolean() }),
      ),
    ).rejects.toBeInstanceOf(ApiTimeoutError);
  });

  it.each([
    "https://user:pass@api.test/api/v1",
    "https://api.test/api/v1?token=secret",
    "https://api.test/api/v1#fragment",
    "https://api.test/api/v1\u0000",
    "ftp://api.test/api/v1",
    "https://api.test/other",
    "https://api.test/api/v1/api/v1",
  ])("rejects unsafe base URL %s", (baseUrl) => {
    expect(() => createApiClient({ baseUrl, mode: "production" })).toThrow(ApiConfigurationError);
  });

  it("uses a normalized same-origin API base without resolving a browser origin", async () => {
    const requestFetch = vi.fn(async (input: RequestInfo | URL) => {
      expect(input).toBe("/api/v1/auth/me");
      return HttpResponse.json(currentUserFixture);
    }) as typeof fetch;

    await expect(
      createApiClient({
        baseUrl: "/api/v1/",
        mode: "production",
        fetch: requestFetch,
      }).get("/auth/me", currentUserSchema),
    ).resolves.toEqual(currentUserFixture);
    expect(requestFetch).toHaveBeenCalledOnce();
  });

  it("normalizes a same-origin root base to the versioned API path", async () => {
    const requestFetch = vi.fn(async (input: RequestInfo | URL) => {
      expect(input).toBe("/api/v1/auth/me");
      return HttpResponse.json(currentUserFixture);
    }) as typeof fetch;

    await expect(
      createApiClient({ baseUrl: "/", mode: "production", fetch: requestFetch }).get(
        "/auth/me",
        currentUserSchema,
      ),
    ).resolves.toEqual(currentUserFixture);
  });

  it.each([
    "",
    "api/v1",
    "//evil.test/api/v1",
    "/api/v1?redirect=https://evil.test",
    "/api/v1#fragment",
    "/api/../v1",
    "/api/%2e%2e/v1",
    "/api/%252e%252e/v1",
    "/api\\v1",
    "/api/v1\\evil",
    "/api/v1\u0000",
    "/other",
  ])("rejects unsafe or out-of-contract same-origin base %s", (baseUrl) => {
    expect(() => createApiClient({ baseUrl, mode: "production" })).toThrow(ApiConfigurationError);
  });

  it("normalizes a safe base and requires HTTPS outside development/test", async () => {
    expect(() => createApiClient({ baseUrl: "http://api.test", mode: "staging" })).toThrow(
      ApiConfigurationError,
    );
    server.use(http.get(`${API_BASE}/ping`, () => HttpResponse.json({ ok: true })));
    await expect(
      createApiClient({ baseUrl: "http://api.test/api/v1/", mode: "test" }).get(
        "/ping",
        z.object({ ok: z.boolean() }),
      ),
    ).resolves.toEqual({ ok: true });
  });

  it.each([
    "https://evil.test/steal",
    "//evil.test/steal",
    "/safe//evil",
    "/../admin",
    "/%2e%2e/admin",
    "/safe\\admin",
    "/safe%5cadmin",
    "/safe\u0000admin",
    "/safe?redirect=https://evil.test",
  ])("rejects unsafe endpoint %s before reading a token", async (endpoint) => {
    const getToken = vi.fn(() => "secret-token");
    await expect(client({ getToken }).get(endpoint, currentUserSchema)).rejects.toBeInstanceOf(
      ApiConfigurationError,
    );
    expect(getToken).not.toHaveBeenCalled();
  });

  it("serializes safe query values and JSON body", async () => {
    server.use(
      http.patch(`${API_BASE}/reports/${ids.report}`, async ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.getAll("tag")).toEqual(["one", "two"]);
        expect(url.searchParams.get("limit")).toBe("50");
        expect(url.searchParams.has("empty")).toBe(false);
        expect(request.headers.get("Content-Type")).toContain("application/json");
        expect(request.headers.get("Accept")).toBe("application/json");
        expect(await request.json()).toEqual({ score: 82 });
        return HttpResponse.json(reviewReportFixture, { headers: { "X-Request-ID": ids.request } });
      }),
    );
    await client().patch(`/reports/${ids.report}`, { score: 82 }, reviewReportSchema, {
      query: { tag: ["one", "two"], limit: 50, empty: undefined },
    });
  });

  it("handles 204 without requiring JSON or sending Content-Type for an empty body", async () => {
    server.use(
      http.delete(`${API_BASE}/resource`, ({ request }) => {
        expect(request.headers.has("Content-Type")).toBe(false);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    await expect(client().delete("/resource")).resolves.toBeUndefined();
  });

  it.each(["safe\nInjected: yes", `x${"y".repeat(4096)}`])(
    "rejects an invalid stored token without making a request",
    async (token) => {
      let calls = 0;
      server.use(http.get(`${API_BASE}/auth/me`, () => (calls += 1, HttpResponse.json(currentUserFixture))));
      await expect(client({ getToken: () => token }).get("/auth/me", currentUserSchema)).rejects.toBeInstanceOf(
        ApiConfigurationError,
      );
      expect(calls).toBe(0);
    },
  );

  it("continues safely when localStorage is unavailable", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked");
    });
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        expect(request.headers.has("Authorization")).toBe(false);
        return HttpResponse.json(currentUserFixture);
      }),
    );
    try {
      await createApiClient({ baseUrl: API_BASE, mode: "test" }).get("/auth/me", currentUserSchema);
    } finally {
      getItem.mockRestore();
    }
  });

  it("uses the exact access-token storage key", () => {
    expect(ACCESS_TOKEN_STORAGE_KEY).toBe("ai-grading.access-token");
  });

  it("accepts matching response correlation IDs and rejects conflicts", async () => {
    server.use(
      http.get(`${API_BASE}/report`, () =>
        HttpResponse.json(reviewReportFixture, { headers: { "X-Request-ID": ids.request } }),
      ),
    );
    await expect(client().get("/report", reviewReportSchema)).resolves.toEqual(reviewReportFixture);

    server.use(
      http.get(`${API_BASE}/report`, () =>
        HttpResponse.json(
          { ...reviewReportFixture, request_id: "77777777-7777-4777-8777-777777777777" },
          { headers: { "X-Request-ID": ids.request } },
        ),
      ),
    );
    await expect(client().get("/report", reviewReportSchema)).rejects.toMatchObject({
      name: "ApiContractError",
      requestId: ids.request,
    });

    server.use(
      http.get(`${API_BASE}/report`, () =>
        HttpResponse.json(
          { ...reviewReportFixture, request_id: "invalid\nrequest-id" },
          { headers: { "X-Request-ID": ids.request } },
        ),
      ),
    );
    await expect(client().get("/report", reviewReportSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
  });
});
