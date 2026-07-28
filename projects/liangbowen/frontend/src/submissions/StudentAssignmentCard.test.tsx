import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AssignmentListItem } from "../shared/api/schemas";
import { StudentAssignmentCard } from "./StudentHomePage";
import { DEADLINE_RECHECK_MS, MAX_SAFE_TIMER_DELAY } from "./deadline";

const NOW = new Date("2026-07-27T00:00:00Z").getTime();

function assignment(dueAt: number): AssignmentListItem {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    code: "HW-LIVE",
    title: "实时截止状态",
    due_at: new Date(dueAt).toISOString(),
    status: "published",
    created_at: "2026-07-26T00:00:00Z",
    published_at: "2026-07-26T01:00:00Z",
  };
}

function renderCard(dueAt: number) {
  return render(<MemoryRouter><StudentAssignmentCard assignment={assignment(dueAt)} /></MemoryRouter>);
}

describe("student assignment deadline state", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("changes a mounted card to read-only immediately after the deadline", () => {
    renderCard(NOW + 1_000);
    expect(screen.getByRole("link", { name: "打开 HW-LIVE" })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1_001));
    expect(screen.getByText("已逾期")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看 HW-LIVE" })).toBeInTheDocument();
  });

  it("segments far deadlines and notices a wall-clock jump", () => {
    renderCard(NOW + MAX_SAFE_TIMER_DELAY + 1_000);
    expect(vi.getTimerCount()).toBe(1);
    vi.setSystemTime(NOW + MAX_SAFE_TIMER_DELAY + 2_000);
    act(() => vi.advanceTimersByTime(DEADLINE_RECHECK_MS));
    expect(screen.getByText("已逾期")).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([["focus", window], ["visibilitychange", document]] as const)("rechecks on %s", (eventName, target) => {
    renderCard(NOW + 60_000);
    vi.setSystemTime(NOW + 120_000);
    act(() => target.dispatchEvent(new Event(eventName)));
    expect(screen.getByText("已逾期")).toBeInTheDocument();
  });

  it("performs a final wall-clock check before following the link", () => {
    renderCard(NOW + 60_000);
    vi.setSystemTime(NOW + 120_000);
    const followed = fireEvent.click(screen.getByRole("link", { name: "打开 HW-LIVE" }));
    expect(followed).toBe(false);
    expect(screen.getByRole("link", { name: "查看 HW-LIVE" })).toBeInTheDocument();
  });
});
