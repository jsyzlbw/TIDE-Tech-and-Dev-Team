import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router";

import { createAppRouter } from "./app/router";
import "./styles/global.css";
import "./styles/responsive.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, gcTime: 5 * 60 * 1000 },
    mutations: { retry: false },
  },
});
const router = createAppRouter();

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("application root is missing");
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
