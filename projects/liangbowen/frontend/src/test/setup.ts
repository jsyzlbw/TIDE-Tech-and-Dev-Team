import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  try {
    window.localStorage.clear();
  } catch {
    // Storage can be unavailable in privacy-restricted or non-browser test environments.
  }
});
afterAll(() => server.close());
