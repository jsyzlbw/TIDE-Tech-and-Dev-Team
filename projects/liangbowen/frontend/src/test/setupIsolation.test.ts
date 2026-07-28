import { expect, it, vi } from "vitest";

const STORAGE_PROBE = "w2.setup-probe";

it("writes a storage value for the isolation contract", () => {
  window.localStorage.setItem(STORAGE_PROBE, "present");
  expect(window.localStorage.getItem(STORAGE_PROBE)).toBe("present");
});

it("starts the next test with storage cleared", () => {
  expect(window.localStorage.getItem(STORAGE_PROBE)).toBeNull();
});

it("does not fail teardown when storage clearing is unavailable", () => {
  vi.spyOn(Storage.prototype, "clear").mockImplementationOnce(() => {
    throw new DOMException("blocked");
  });
});
