import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { z } from "zod";
import { expect, it, vi } from "vitest";

import { createApiClient } from "./client";

it("accepts the frontend image API base as a same-origin request prefix", async () => {
  const dockerfile = readFileSync(resolve(process.cwd(), "Dockerfile"), "utf-8");
  const dockerDefault = dockerfile.match(/^ARG VITE_API_BASE_URL=(\S+)$/mu)?.[1];
  expect(dockerDefault).toBeDefined();

  const requestFetch = vi.fn(async (input: RequestInfo | URL) => {
    expect(input).toBe("/api/v1/health/ready?probe=compose");
    return Response.json({ status: "ok" });
  }) as typeof fetch;

  await expect(
    createApiClient({
      baseUrl: dockerDefault,
      mode: "production",
      fetch: requestFetch,
    }).get("/health/ready", z.object({ status: z.literal("ok") }), {
      query: { probe: "compose" },
    }),
  ).resolves.toEqual({ status: "ok" });
  expect(requestFetch).toHaveBeenCalledOnce();
});
