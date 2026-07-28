import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AssignmentListItem } from "../shared/api/schemas";
import { AssignmentCard } from "./AssignmentCard";

const NOW = new Date("2026-07-27T00:00:00Z").getTime();
const MAX_SAFE_TIMER_DELAY = 2_147_483_647;
const DEADLINE_RECHECK_MS = 30_000;

function assignment(
  status: AssignmentListItem["status"],
  dueAt: number,
): AssignmentListItem {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    code: "HW-TIMER",
    title: "截止时间定时器",
    due_at: new Date(dueAt).toISOString(),
    status,
    created_at: "2026-07-26T00:00:00Z",
    published_at: status === "draft" ? null : "2026-07-26T01:00:00Z",
  };
}

function renderCard(item: AssignmentListItem, onLifecycleAction = vi.fn()) {
  return render(
    <MemoryRouter>
      <AssignmentCard assignment={item} onLifecycleAction={onLifecycleAction} />
    </MemoryRouter>,
  );
}

describe("AssignmentCard deadline timer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    ["draft", "发布 HW-TIMER"],
    ["closed", "重新开放 HW-TIMER"],
  ] as const)("updates a mounted %s card immediately after its deadline", (status, actionName) => {
    renderCard(assignment(status, NOW + 1_000));
    const action = screen.getByRole("button", { name: actionName });

    expect(action).toBeEnabled();
    expect(screen.queryByText("已逾期", { exact: false })).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1_001));

    expect(screen.getByText("已逾期", { exact: false })).toBeInTheDocument();
    expect(action).toBeDisabled();
    expect(screen.getByText("截止时间已过，无法执行此操作")).toBeInTheDocument();
  });

  it("keeps published close enabled when its deadline passes", () => {
    renderCard(assignment("published", NOW + 1_000));
    const close = screen.getByRole("button", { name: "关闭 HW-TIMER" });

    act(() => vi.advanceTimersByTime(1_001));

    expect(screen.getByText("已逾期", { exact: false })).toBeInTheDocument();
    expect(close).toBeEnabled();
  });

  it("segments deadlines beyond the platform-safe timer delay", () => {
    renderCard(assignment("draft", NOW + MAX_SAFE_TIMER_DELAY + 1_000));

    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(DEADLINE_RECHECK_MS));
    expect(screen.getByRole("button", { name: "发布 HW-TIMER" })).toBeEnabled();
    expect(vi.getTimerCount()).toBe(1);
    vi.setSystemTime(NOW + MAX_SAFE_TIMER_DELAY + 1_001);
    act(() => vi.advanceTimersByTime(DEADLINE_RECHECK_MS));
    expect(screen.getByRole("button", { name: "发布 HW-TIMER" })).toBeDisabled();
  });

  it("rechecks a far deadline after the wall clock jumps forward", () => {
    renderCard(assignment("draft", NOW + 60 * 60_000));

    vi.setSystemTime(NOW + 2 * 60 * 60_000);
    act(() => vi.advanceTimersByTime(DEADLINE_RECHECK_MS));

    expect(screen.getByText("已逾期", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发布 HW-TIMER" })).toBeDisabled();
  });

  it.each([
    ["focus", window],
    ["visibilitychange", document],
  ] as const)("rechecks immediately on %s", (eventName, target) => {
    renderCard(assignment("draft", NOW + 60 * 60_000));
    vi.setSystemTime(NOW + 2 * 60 * 60_000);

    act(() => target.dispatchEvent(new Event(eventName)));

    expect(screen.getByRole("button", { name: "发布 HW-TIMER" })).toBeDisabled();
  });

  it("performs a final deadline check before publishing", () => {
    const onLifecycleAction = vi.fn();
    renderCard(assignment("draft", NOW + 60 * 60_000), onLifecycleAction);
    const publish = screen.getByRole("button", { name: "发布 HW-TIMER" });
    vi.setSystemTime(NOW + 2 * 60 * 60_000);

    fireEvent.click(publish);

    expect(onLifecycleAction).not.toHaveBeenCalled();
    expect(publish).toBeDisabled();
    expect(screen.getByText("已逾期", { exact: false })).toBeInTheDocument();
  });

  it("replaces the timer when due_at changes without firing the stale deadline", () => {
    const { rerender } = renderCard(assignment("draft", NOW + 1_000));

    rerender(
      <MemoryRouter>
        <AssignmentCard
          assignment={assignment("draft", NOW + 5_000)}
          onLifecycleAction={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(1_001));
    expect(screen.getByRole("button", { name: "发布 HW-TIMER" })).toBeEnabled();
    act(() => vi.advanceTimersByTime(4_000));
    expect(screen.getByRole("button", { name: "发布 HW-TIMER" })).toBeDisabled();
  });

  it("cleans up its deadline timer and event listeners on unmount", () => {
    const removeWindowListener = vi.spyOn(window, "removeEventListener");
    const removeDocumentListener = vi.spyOn(document, "removeEventListener");
    const { unmount } = renderCard(assignment("draft", NOW + 10_000));

    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    expect(removeWindowListener).toHaveBeenCalledWith("focus", expect.any(Function));
    expect(removeDocumentListener).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
  });
});
