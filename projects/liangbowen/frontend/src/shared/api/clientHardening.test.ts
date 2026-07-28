import { http, HttpResponse } from "msw";
import { z } from "zod";
import { describe, expect, it, vi } from "vitest";

import * as reportApi from "../../reports/api";
import { currentUserFixture, ids, reviewReportFixture } from "../../test/fixtures";
import { maximumWorkspaceWireFixture } from "../../test/maximumWorkspaceFixture";
import { server } from "../../test/server";
import {
  ACCESS_TOKEN_STORAGE_KEY,
  createApiClient,
} from "./client";
import {
  ApiAbortError,
  ApiConfigurationError,
  ApiContractError,
  ApiError,
  ApiTimeoutError,
} from "./errors";
import {
  currentUserSchema,
  rawReportOutputSchema,
  reportWorkspaceSchema,
  reviewReportSchema,
} from "./schemas";

const API_BASE = "http://api.test/api/v1";
const JSON_HEADERS = { "Content-Type": "application/json" };

function client(overrides: Parameters<typeof createApiClient>[0] = {}) {
  return createApiClient({ baseUrl: API_BASE, mode: "test", timeoutMs: 100, ...overrides });
}

function chunkedJsonResponse(
  chunks: string[],
  options: { cancel?: () => void | Promise<void> } = {},
) {
  const encoder = new TextEncoder();
  let index = 0;
  const pull = vi.fn((controller: ReadableStreamDefaultController<Uint8Array>) => {
    const chunk = chunks[index];
    index += 1;
    if (chunk === undefined) controller.close();
    else controller.enqueue(encoder.encode(chunk));
  });
  const cancel = vi.fn(options.cancel ?? (() => undefined));
  const body = new ReadableStream<Uint8Array>({ pull, cancel }, { highWaterMark: 0 });
  return { response: new Response(body, { headers: JSON_HEADERS }), pull, cancel };
}

function nestedEncode(value: string, rounds: number) {
  let encoded = value;
  for (let round = 0; round < rounds; round += 1) encoded = encodeURIComponent(encoded);
  return encoded;
}

function nestedPercentKey(value: string, rounds: number) {
  const bytes = new TextEncoder().encode(value);
  let encoded = [...bytes]
    .map((byte) => `%${byte.toString(16).padStart(2, "0")}`)
    .join("");
  for (let round = 1; round < rounds; round += 1) encoded = encodeURIComponent(encoded);
  return encoded;
}

function byteResponse(chunks: Uint8Array[]) {
  let index = 0;
  return new Response(
    new ReadableStream<Uint8Array>({
      pull(controller) {
        const chunk = chunks[index++];
        if (chunk === undefined) controller.close();
        else controller.enqueue(chunk);
      },
    }),
    { headers: JSON_HEADERS },
  );
}

function streamedJsonWithLength(body: string) {
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
  }), {
    headers: { ...JSON_HEADERS, "Content-Length": String(bytes.byteLength) },
  });
}

function joinBytes(...chunks: Uint8Array[]) {
  const joined = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.length;
  }
  return joined;
}

describe("abort reason precedence", () => {
  it("keeps caller abort as the first cause when fetch rejects after the timeout window", async () => {
    vi.useFakeTimers();
    try {
      let rejectFetch: ((reason: unknown) => void) | undefined;
      const requestFetch = vi.fn(
        () =>
          new Promise<Response>((_resolve, reject) => {
            rejectFetch = reject;
          }),
      ) as unknown as typeof fetch;
      const controller = new AbortController();
      const request = client({ fetch: requestFetch, timeoutMs: 5 }).get("/slow", z.unknown(), {
        signal: controller.signal,
      });

      controller.abort();
      await vi.advanceTimersByTimeAsync(10);
      rejectFetch?.(new DOMException("late rejection", "AbortError"));
      await expect(request).rejects.toBeInstanceOf(ApiAbortError);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps timeout as the first cause when the caller aborts later", async () => {
    vi.useFakeTimers();
    try {
      let rejectFetch: ((reason: unknown) => void) | undefined;
      const requestFetch = vi.fn(
        () =>
          new Promise<Response>((_resolve, reject) => {
            rejectFetch = reject;
          }),
      ) as unknown as typeof fetch;
      const controller = new AbortController();
      const request = client({ fetch: requestFetch, timeoutMs: 5 }).get("/slow", z.unknown(), {
        signal: controller.signal,
      });

      await vi.advanceTimersByTimeAsync(5);
      controller.abort();
      rejectFetch?.(new DOMException("late rejection", "AbortError"));
      await expect(request).rejects.toBeInstanceOf(ApiTimeoutError);
    } finally {
      vi.useRealTimers();
    }
  });

  it("deterministically keeps the first same-tick event", async () => {
    vi.useFakeTimers();
    try {
      const controller = new AbortController();
      setTimeout(() => controller.abort(), 5);
      let rejectFetch: ((reason: unknown) => void) | undefined;
      const requestFetch = vi.fn(
        () =>
          new Promise<Response>((_resolve, reject) => {
            rejectFetch = reject;
          }),
      ) as unknown as typeof fetch;
      const request = client({ fetch: requestFetch, timeoutMs: 5 }).get("/slow", z.unknown(), {
        signal: controller.signal,
      });

      await vi.advanceTimersByTimeAsync(5);
      rejectFetch?.(new DOMException("same-tick rejection", "AbortError"));
      await expect(request).rejects.toBeInstanceOf(ApiAbortError);
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves caller-first cause while a response stream rejects later", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const requestFetch = vi.fn(async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller;
          },
        }),
        { headers: JSON_HEADERS },
      ),
    ) as unknown as typeof fetch;
    const controller = new AbortController();
    const request = client({ fetch: requestFetch, timeoutMs: 5 }).get("/stream", z.unknown(), {
      signal: controller.signal,
    });

    controller.abort();
    await new Promise((resolve) => setTimeout(resolve, 10));
    streamController?.error(new DOMException("late stream rejection", "AbortError"));
    await expect(request).rejects.toBeInstanceOf(ApiAbortError);
  });

  it("removes the caller listener and timeout after a completed request", async () => {
    vi.useFakeTimers();
    try {
      const controller = new AbortController();
      const add = vi.spyOn(controller.signal, "addEventListener");
      const remove = vi.spyOn(controller.signal, "removeEventListener");
      const requestFetch = vi.fn(async () =>
        HttpResponse.json(currentUserFixture, { headers: JSON_HEADERS }),
      ) as unknown as typeof fetch;

      await client({ fetch: requestFetch }).get("/complete", currentUserSchema, {
        signal: controller.signal,
      });
      expect(add).toHaveBeenCalledOnce();
      expect(remove).toHaveBeenCalledOnce();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("strict UTF-8 response decoding", () => {
  const encoder = new TextEncoder();

  it.each([
    ["invalid JSON string byte", [joinBytes(encoder.encode('{"value":"'), Uint8Array.of(0xc3, 0x28), encoder.encode('"}'))]],
    ["incomplete trailing sequence", [joinBytes(encoder.encode('{"value":"'), Uint8Array.of(0xe2, 0x82))]],
  ])("rejects %s as a sanitized contract error", async (_label, chunks) => {
    const requestFetch = vi.fn(async () => byteResponse(chunks)) as unknown as typeof fetch;
    const error = await client({ fetch: requestFetch })
      .get("/utf8", z.object({ value: z.string() }))
      .catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiContractError);
    expect(error).toMatchObject({ message: "API response was not valid UTF-8" });
    expect(String(error)).not.toMatch(/195|226|130|secret/u);
  });

  it("accepts a valid multi-byte character split across chunks", async () => {
    const chinese = encoder.encode("中");
    const response = byteResponse([
      joinBytes(encoder.encode('{"value":"'), chinese.slice(0, 1)),
      chinese.slice(1, 2),
      joinBytes(chinese.slice(2), encoder.encode('"}')),
    ]);
    const requestFetch = vi.fn(async () => response) as unknown as typeof fetch;
    await expect(
      client({ fetch: requestFetch }).get("/utf8", z.object({ value: z.string() })),
    ).resolves.toEqual({ value: "中" });
  });
});

describe("declared response size cancellation", () => {
  it.each([
    ["synchronous throw", () => { throw new Error("sync cancel secret"); }],
    ["asynchronous rejection", () => Promise.reject(new Error("async cancel secret"))],
    ["stalled cancellation", () => new Promise<void>(() => undefined)],
  ])("cancels an infinite declared-oversized body despite %s", async (_label, cancelBody) => {
    const response = new Response(new ReadableStream<Uint8Array>({ pull() {} }), {
      headers: { ...JSON_HEADERS, "Content-Length": "1000" },
    });
    const cancel = vi.spyOn(response.body!, "cancel").mockImplementation(cancelBody);
    const requestFetch = vi.fn(async () => response) as unknown as typeof fetch;
    const result = await Promise.race([
      client({ fetch: requestFetch, maxResponseBytes: 10 }).get("/large", z.unknown()).catch((error) => error),
      new Promise<"stalled">((resolve) => setTimeout(() => resolve("stalled"), 50)),
    ]);
    expect(result).toBeInstanceOf(ApiContractError);
    expect(String(result)).not.toContain("cancel secret");
    expect(cancel).toHaveBeenCalledOnce();
  });
});

describe("bounded response reader", () => {
  it("cancels as soon as raw chunks cross the limit without reading the tail", async () => {
    const stream = chunkedJsonResponse(["12345678", "abcdefgh", "unread-tail"]);
    const requestFetch = vi.fn(async () => stream.response) as unknown as typeof fetch;

    await expect(
      client({ fetch: requestFetch, maxResponseBytes: 15 }).get(
        "/stream",
        z.unknown(),
      ),
    ).rejects.toBeInstanceOf(ApiContractError);
    expect(stream.cancel).toHaveBeenCalledOnce();
    expect(stream.pull).toHaveBeenCalledTimes(2);
  });

  it("accepts a multi-chunk JSON body exactly at the byte boundary", async () => {
    const body = '{"ok":true}';
    const stream = chunkedJsonResponse([body.slice(0, 4), body.slice(4)]);
    const requestFetch = vi.fn(async () => stream.response) as unknown as typeof fetch;

    await expect(
      client({ fetch: requestFetch, maxResponseBytes: new TextEncoder().encode(body).byteLength }).get(
        "/stream",
        z.object({ ok: z.boolean() }),
      ),
    ).resolves.toEqual({ ok: true });
    expect(stream.cancel).not.toHaveBeenCalled();
  });

  it("preserves the size contract when reader cancellation itself fails", async () => {
    const stream = chunkedJsonResponse(["12345678", "abcdefgh"], {
      cancel: () => {
        throw new Error("secret cancel failure");
      },
    });
    const requestFetch = vi.fn(async () => stream.response) as unknown as typeof fetch;

    const error = await client({ fetch: requestFetch, maxResponseBytes: 15 })
      .get("/stream", z.unknown())
      .catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiContractError);
    expect(String(error)).not.toContain("secret cancel failure");
  });

  it("does not wait for a stalled underlying cancellation before reporting overflow", async () => {
    const stream = chunkedJsonResponse(["12345678", "abcdefgh"], {
      cancel: () => new Promise<void>(() => undefined),
    });
    const requestFetch = vi.fn(async () => stream.response) as unknown as typeof fetch;
    const request = client({ fetch: requestFetch, maxResponseBytes: 15 })
      .get("/stream", z.unknown())
      .catch((caught) => caught);

    const result = await Promise.race([
      request,
      new Promise<"stalled">((resolve) => setTimeout(() => resolve("stalled"), 50)),
    ]);
    expect(result).toBeInstanceOf(ApiContractError);
    expect(stream.cancel).toHaveBeenCalledOnce();
  });

  it("keeps caller abort active throughout response-body reading", async () => {
    const controller = new AbortController();
    const requestFetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = new ReadableStream<Uint8Array>({
        start(streamController) {
          init?.signal?.addEventListener(
            "abort",
            () => streamController.error(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        },
      });
      return new Response(body, { headers: JSON_HEADERS });
    }) as typeof fetch;

    const request = client({ fetch: requestFetch, timeoutMs: 500 }).get(
      "/stream",
      z.unknown(),
      { signal: controller.signal },
    );
    controller.abort();
    await expect(request).rejects.toBeInstanceOf(ApiAbortError);
  });
});

describe("per-request response budgets", () => {
  const rawBudget = 13 * 1024 * 1024;

  it("reads a decoded 2 MiB raw output even when JSON escaping expands the wire body", async () => {
    const raw = "\u0000".repeat(2 * 1024 * 1024);
    const serialized = JSON.stringify({
      report_id: ids.report,
      available: true,
      raw_model_output: raw,
      request_id: ids.request,
    });
    expect(new TextEncoder().encode(serialized).byteLength).toBeGreaterThan(12 * 1024 * 1024);
    const requestFetch = vi.fn(async () => new Response(serialized, {
      headers: { ...JSON_HEADERS, "Content-Length": String(new TextEncoder().encode(serialized).byteLength) },
    })) as unknown as typeof fetch;

    const result = await client({ fetch: requestFetch }).get("/raw", rawReportOutputSchema, {
      maxResponseBytes: rawBudget,
    });
    expect(new TextEncoder().encode(result.raw_model_output ?? "").byteLength).toBe(2 * 1024 * 1024);
  });

  it("still rejects raw output whose decoded value exceeds 2 MiB", async () => {
    const raw = "a".repeat(2 * 1024 * 1024 + 1);
    const serialized = JSON.stringify({ report_id: ids.report, available: true, raw_model_output: raw, request_id: ids.request });
    const requestFetch = vi.fn(async () => new Response(serialized, { headers: { ...JSON_HEADERS, "Content-Length": String(serialized.length) } })) as unknown as typeof fetch;

    await expect(client({ fetch: requestFetch }).get("/raw", rawReportOutputSchema, {
      maxResponseBytes: rawBudget,
    })).rejects.toBeInstanceOf(ApiContractError);
  });

  it("keeps the normal request budget at 2 MiB", async () => {
    const serialized = JSON.stringify({ value: "a".repeat(2 * 1024 * 1024) });
    const requestFetch = vi.fn(async () => new Response(serialized, { headers: { ...JSON_HEADERS, "Content-Length": String(serialized.length) } })) as unknown as typeof fetch;

    await expect(client({ fetch: requestFetch }).get("/normal", z.unknown())).rejects.toBeInstanceOf(ApiContractError);
  });

  it("uses a three MiB workspace override while the same streamed response fails by default", async () => {
    const workspaceBudget = (reportApi as { WORKSPACE_MAX_RESPONSE_BYTES?: number }).WORKSPACE_MAX_RESPONSE_BYTES;
    expect(workspaceBudget).toBe(3 * 1024 * 1024);
    if (workspaceBudget === undefined) throw new Error("workspace response budget is missing");
    const { body, wireBytes } = maximumWorkspaceWireFixture();
    expect(wireBytes).toBeGreaterThan(2 * 1024 * 1024);
    expect(wireBytes).toBeLessThanOrEqual(workspaceBudget);
    const requestFetch = vi.fn(async () => streamedJsonWithLength(body)) as unknown as typeof fetch;

    await expect(client({ fetch: requestFetch }).get("/workspace", reportWorkspaceSchema, {
      maxResponseBytes: workspaceBudget,
    })).resolves.toMatchObject({ requested_report_id: ids.report });
    await expect(client({ fetch: requestFetch }).get("/workspace", reportWorkspaceSchema))
      .rejects.toBeInstanceOf(ApiContractError);
    expect(requestFetch).toHaveBeenCalledTimes(2);
  });

  it("rejects a per-request override above the 16 MiB hard ceiling before transport", async () => {
    const requestFetch = vi.fn(async () => HttpResponse.json(currentUserFixture)) as unknown as typeof fetch;
    await expect(client({ fetch: requestFetch }).get("/too-large", currentUserSchema, {
      maxResponseBytes: 16 * 1024 * 1024 + 1,
    })).rejects.toBeInstanceOf(ApiConfigurationError);
    expect(requestFetch).not.toHaveBeenCalled();
  });
});

describe("response request-id matrix", () => {
  it("accepts missing, header-only, body-only, and equal IDs", async () => {
    server.use(
      http.get(`${API_BASE}/missing`, () => HttpResponse.json(currentUserFixture)),
      http.get(`${API_BASE}/header-only`, () =>
        HttpResponse.json(currentUserFixture, { headers: { "X-Request-ID": ids.request } }),
      ),
      http.get(`${API_BASE}/body-only`, () => HttpResponse.json(reviewReportFixture)),
      http.get(`${API_BASE}/equal`, () =>
        HttpResponse.json(reviewReportFixture, { headers: { "X-Request-ID": ids.request } }),
      ),
    );

    await expect(client().get("/missing", currentUserSchema)).resolves.toEqual(currentUserFixture);
    await expect(client().get("/header-only", currentUserSchema)).resolves.toEqual(
      currentUserFixture,
    );
    await expect(client().get("/body-only", reviewReportSchema)).resolves.toEqual(
      reviewReportFixture,
    );
    await expect(client().get("/equal", reviewReportSchema)).resolves.toEqual(reviewReportFixture);
  });

  it.each(["", "x".repeat(129)])("rejects a present invalid request-id header", async (value) => {
    server.use(
      http.get(`${API_BASE}/invalid-header`, () =>
        HttpResponse.json(currentUserFixture, { headers: { "X-Request-ID": value } }),
      ),
    );
    await expect(client().get("/invalid-header", currentUserSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
  });

  it("rejects a control character in an exposed request-id header", async () => {
    const response = new Response(JSON.stringify(currentUserFixture), { headers: JSON_HEADERS });
    const realHeaders = response.headers;
    Object.defineProperty(response, "headers", {
      value: {
        get(name: string) {
          return name.toLowerCase() === "x-request-id"
            ? "invalid\nrequest-id"
            : realHeaders.get(name);
        },
      },
    });
    const requestFetch = vi.fn(async () => response) as unknown as typeof fetch;
    await expect(
      client({ fetch: requestFetch }).get("/invalid-header", currentUserSchema),
    ).rejects.toBeInstanceOf(ApiContractError);
  });

  it("rejects a padded request-id header when padding is observable", async () => {
    const response = new Response(JSON.stringify(currentUserFixture), { headers: JSON_HEADERS });
    const realHeaders = response.headers;
    Object.defineProperty(response, "headers", {
      value: {
        get(name: string) {
          return name.toLowerCase() === "x-request-id"
            ? ` ${ids.request} `
            : realHeaders.get(name);
        },
      },
    });
    const requestFetch = vi.fn(async () => response) as unknown as typeof fetch;
    await expect(
      client({ fetch: requestFetch }).get("/padded-header", z.unknown()),
    ).rejects.toBeInstanceOf(ApiContractError);
  });

  it("accepts a request-id header after the browser has normalized its whitespace", async () => {
    const response = new Response(JSON.stringify(currentUserFixture), {
      headers: { ...JSON_HEADERS, "X-Request-ID": ` ${ids.request} ` },
    });
    expect(response.headers.get("X-Request-ID")).toBe(ids.request);
    const requestFetch = vi.fn(async () => response) as unknown as typeof fetch;
    await expect(
      client({ fetch: requestFetch }).get("/normalized-header", z.unknown()),
    ).resolves.toEqual(currentUserFixture);
  });

  it("rejects invalid body IDs and valid header/body conflicts", async () => {
    server.use(
      http.get(`${API_BASE}/invalid-body`, () =>
        HttpResponse.json({ ...reviewReportFixture, request_id: "invalid\nrequest-id" }),
      ),
      http.get(`${API_BASE}/conflict`, () =>
        HttpResponse.json(
          { ...reviewReportFixture, request_id: "77777777-7777-4777-8777-777777777777" },
          { headers: { "X-Request-ID": ids.request } },
        ),
      ),
    );
    await expect(client().get("/invalid-body", reviewReportSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
    await expect(client().get("/conflict", reviewReportSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
  });

  it.each([" ", ` ${ids.request}`, `${ids.request} `])(
    "rejects a body-only request ID with whitespace: %j",
    async (requestId) => {
      const requestFetch = vi.fn(async () =>
        HttpResponse.json({ ...reviewReportFixture, request_id: requestId }, { headers: JSON_HEADERS }),
      ) as unknown as typeof fetch;
      await expect(client({ fetch: requestFetch }).get("/body-id", z.unknown())).rejects.toBeInstanceOf(
        ApiContractError,
      );
    },
  );
});

describe("safe error details", () => {
  it("drops sensitive values beyond the recursion depth", async () => {
    server.use(
      http.get(`${API_BASE}/deep-error`, () =>
        HttpResponse.json(
          {
            code: "DEEP",
            message: "failed",
            details: {
              one: { two: { three: { four: { five: { access_token: "deep-secret" } } } } },
            },
          },
          { status: 409 },
        ),
      ),
    );
    const error = await client().get("/deep-error", currentUserSchema).catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(JSON.stringify(error)).not.toContain("deep-secret");
  });

  it("bounds and cleans validation type, location, and message", async () => {
    server.use(
      http.get(`${API_BASE}/validation-error`, () =>
        HttpResponse.json(
          {
            detail: [
              {
                type: `missing\n${"t".repeat(300)}`,
                loc: ["body\nsecret", ...Array.from({ length: 40 }, (_, index) => index)],
                msg: `Field\u0000 required ${"m".repeat(800)}`,
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    const error = (await client()
      .get("/validation-error", currentUserSchema)
      .catch((caught) => caught)) as ApiError;
    const issue = (
      error.details as Array<{ type: string; loc: Array<string | number>; msg: string }>
    )[0];
    expect(issue.type.length).toBeLessThanOrEqual(100);
    expect(issue.msg.length).toBeLessThanOrEqual(500);
    expect(issue.loc.length).toBeLessThanOrEqual(20);
    const plainText = [issue.type, issue.msg, ...issue.loc.map(String)].join("");
    expect(plainText).not.toContain("\n");
    expect(plainText).not.toContain("\u0000");
  });

  it("sanitizes and bounds keys without collision overwrite or encoded-key leaks", async () => {
    const longKey = "k".repeat(150);
    const details: Record<string, unknown> = Object.create(null);
    details[" line\nbreak "] = "first";
    details["line break"] = "second";
    details["%74%6f%6b%65%6e"] = "encoded-secret";
    for (const rounds of [1, 2, 3, 8, 9]) {
      details[nestedPercentKey("token", rounds)] = `layer-secret-${rounds}`;
    }
    details["%74%6f%6b%65%6e%"] = "malformed-secret";
    details["to\u0000ken"] = "control-secret";
    details[nestedPercentKey("__proto__", 3)] = "encoded-prototype-secret";
    details.ToKeN = "case-secret";
    details[" __proto__ "] = "prototype-secret";
    details[longKey] = "long-value";
    const requestFetch = vi.fn(async () =>
      HttpResponse.json({ code: "BAD", message: "failed", details }, { status: 409 }),
    ) as unknown as typeof fetch;

    const error = (await client({ fetch: requestFetch })
      .get("/details", z.unknown())
      .catch((caught) => caught)) as ApiError;
    const sanitized = error.details as Record<string, unknown>;
    expect(Object.getPrototypeOf(sanitized)).toBeNull();
    expect(sanitized["line break"]).toBe("first");
    expect(Object.keys(sanitized).every((key) => key.length <= 100 && !key.includes("\n"))).toBe(true);
    expect(JSON.stringify(sanitized)).not.toMatch(
      /encoded-secret|layer-secret|malformed-secret|control-secret|prototype-secret/u,
    );
    expect(JSON.parse(JSON.stringify(sanitized))).toBeTypeOf("object");
  });
});

describe("recursive URL input defense", () => {
  it.each([
    "/literal-percent/%25",
    "/encoded-percent/%2525",
    "/utf8/%E4%B8%AD%E6%96%87",
    "/utf8/中文",
    "/with%20space",
    "/with space",
  ])("accepts a safe encoded endpoint %s", async (endpoint) => {
    const requestFetch = vi.fn(async () =>
      HttpResponse.json(currentUserFixture, { headers: JSON_HEADERS }),
    ) as unknown as typeof fetch;

    await expect(client({ fetch: requestFetch }).get(endpoint, currentUserSchema)).resolves.toEqual(
      currentUserFixture,
    );
    expect(requestFetch).toHaveBeenCalledOnce();
  });

  it.each([
    `/${nestedEncode("../admin", 5)}`,
    `/${nestedEncode("safe\\admin", 5)}`,
    `/${nestedEncode("\u0000admin", 2)}`,
    `/${nestedEncode("\u0080admin", 2)}`,
    "/%",
    "/%2",
    "/%GG",
    "/bad%percent",
    `/${"a".repeat(2_049)}`,
  ])("rejects recursively unsafe endpoint %s", async (endpoint) => {
    const getToken = vi.fn(() => "secret-token");
    await expect(client({ getToken }).get(endpoint, currentUserSchema)).rejects.toBeInstanceOf(
      ApiConfigurationError,
    );
    expect(getToken).not.toHaveBeenCalled();
  });

  it.each([
    `https://api.test/${nestedEncode("\u0000api/v1", 2)}`,
    "https://api.test/bad%percent",
    `https://api.test/${"a".repeat(2_049)}`,
  ])("rejects recursively unsafe or oversized base %s", (baseUrl) => {
    expect(() => createApiClient({ baseUrl, mode: "production" })).toThrow(
      ApiConfigurationError,
    );
  });
});

describe("204 schema contract", () => {
  it("accepts void only when no schema or z.void explicitly permits it", async () => {
    server.use(http.all(`${API_BASE}/empty`, () => new HttpResponse(null, { status: 204 })));
    await expect(client().delete("/empty")).resolves.toBeUndefined();
    await expect(client().get("/empty", z.void())).resolves.toBeUndefined();
    await expect(client().post("/empty", undefined, z.void())).resolves.toBeUndefined();
    await expect(client().patch("/empty", undefined, z.void())).resolves.toBeUndefined();
    await expect(client().delete("/empty", z.void())).resolves.toBeUndefined();
  });

  it("rejects 204 when a provided schema does not accept undefined", async () => {
    server.use(http.all(`${API_BASE}/empty`, () => new HttpResponse(null, { status: 204 })));
    await expect(client().get("/empty", currentUserSchema)).rejects.toBeInstanceOf(ApiContractError);
    await expect(client().post("/empty", undefined, currentUserSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
    await expect(client().patch("/empty", undefined, currentUserSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
    await expect(client().delete("/empty", currentUserSchema)).rejects.toBeInstanceOf(
      ApiContractError,
    );
  });
});

describe("storage and production defaults", () => {
  it("resolves the default fetch implementation when a request starts", async () => {
    const originalFetch = globalThis.fetch;
    const lateFetch = vi.fn(async () => HttpResponse.json(currentUserFixture)) as unknown as typeof fetch;
    const apiClient = client();
    globalThis.fetch = lateFetch;

    try {
      await expect(apiClient.get("/auth/me", currentUserSchema)).resolves.toEqual(currentUserFixture);
      expect(lateFetch).toHaveBeenCalledOnce();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("uses the exact localStorage bearer without an injected token reader", async () => {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, "stored.jwt.token");
    server.use(
      http.get(`${API_BASE}/auth/me`, ({ request }) => {
        expect(request.headers.get("Authorization")).toBe("Bearer stored.jwt.token");
        return HttpResponse.json(currentUserFixture);
      }),
    );
    await client().get("/auth/me", currentUserSchema);
  });

  it("fails fast with a diagnostic when production-like mode omits the API env", () => {
    expect(() => createApiClient({ mode: "production" })).toThrow(
      "VITE_API_BASE_URL is required outside development and test",
    );
  });
});
