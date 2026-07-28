import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../shared/api/client";
import type { AssignmentListItem } from "../shared/api/schemas";
import { useAssignmentDashboardMetrics } from "./api";

const assignments = Array.from({ length: 8 }, (_, index): AssignmentListItem => ({
  id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
  code: `HW-${String(index + 1).padStart(4, "0")}`,
  title: `并发取消 ${index + 1}`,
  due_at: "2099-08-01T12:00:00Z",
  status: "draft",
  created_at: "2026-07-20T08:00:00Z",
  published_at: null,
}));

function MetricsProbe({ onFetching }: { onFetching(value: boolean): void }) {
  const metrics = useAssignmentDashboardMetrics("teacher", assignments);
  useEffect(() => onFetching(metrics.isFetching), [metrics.isFetching, onFetching]);
  return null;
}

describe("assignment dashboard metrics cancellation", () => {
  afterEach(() => vi.restoreAllMocks());

  it("stops workers from claiming more summaries after the query is aborted", async () => {
    const requested: string[] = [];
    vi.spyOn(api, "get").mockImplementation(((endpoint: string, _schema: unknown, options?: { signal?: AbortSignal }) => {
      requested.push(endpoint);
      return new Promise((_resolve, reject) => {
        const signal = options?.signal;
        const abort = () => reject(new DOMException("aborted", "AbortError"));
        if (signal?.aborted) abort();
        else signal?.addEventListener("abort", abort, { once: true });
      });
    }) as typeof api.get);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onFetching = vi.fn();
    const view = render(
      <QueryClientProvider client={client}>
        <MetricsProbe onFetching={onFetching} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(requested).toHaveLength(4));
    view.unmount();
    await waitFor(() => expect(onFetching).toHaveBeenCalledWith(true));
    await Promise.resolve();
    await Promise.resolve();

    expect(requested).toHaveLength(4);
  });
});
