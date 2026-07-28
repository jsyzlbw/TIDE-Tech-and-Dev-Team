import { afterEach, describe, expect, it, vi } from "vitest";

import { ACCESS_TOKEN_STORAGE_KEY, readStoredAccessToken } from "./client";

describe("stored access-token reader", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the documented key from an explicit storage object", () => {
    const storage = {
      getItem: (key: string) => (key === ACCESS_TOKEN_STORAGE_KEY ? "stored.jwt.token" : null),
    };

    expect(readStoredAccessToken(storage)).toBe("stored.jwt.token");
  });

  it("is safe without browser storage and when storage access throws", () => {
    vi.stubGlobal("window", undefined);
    expect(readStoredAccessToken()).toBeNull();
    vi.unstubAllGlobals();
    expect(readStoredAccessToken(null)).toBeNull();
    expect(
      readStoredAccessToken({
        getItem() {
          throw new DOMException("blocked");
        },
      }),
    ).toBeNull();
  });
});
