import { describe, expect, it } from "vitest";

import { canPageForward, MAX_ACCOUNT_OFFSET } from "./accountPagination";

describe("account pagination bounds", () => {
  it("stops at the API maximum offset even when the server reports more rows", () => {
    expect(canPageForward(MAX_ACCOUNT_OFFSET - 50, 10_051)).toBe(true);
    expect(canPageForward(MAX_ACCOUNT_OFFSET, 10_051)).toBe(false);
  });
});
