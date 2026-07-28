import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { InitialEntry } from "react-router";
import { RouterProvider } from "react-router";
import { onTestFinished } from "vitest";

import { createAppRouter } from "../app/router";

export function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function renderApp(initialEntries: InitialEntry[] = ["/"]) {
  const queryClient = createTestQueryClient();
  const router = createAppRouter(initialEntries);
  onTestFinished(() => queryClient.clear());
  const view = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...view, queryClient, router };
}
