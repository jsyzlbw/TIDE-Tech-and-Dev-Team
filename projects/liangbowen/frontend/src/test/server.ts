import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

import type { CurrentUser } from "../shared/api/schemas";
import { accountPageFixture } from "./fixtures";

export const API_BASE = "http://localhost:8000/api/v1";
export const server = setupServer();

export function serveAccountWorkspace(actor: CurrentUser) {
  const visibleItems = actor.role === "teacher"
    ? accountPageFixture.items.filter((account) => account.role === "student")
    : [...accountPageFixture.items];
  server.use(
    http.get(`${API_BASE}/auth/me`, () => HttpResponse.json(actor)),
    http.get(`${API_BASE}/users`, ({ request }) => {
      const offset = Number(new URL(request.url).searchParams.get("offset") ?? 0);
      return HttpResponse.json({
        items: visibleItems,
        total: visibleItems.length,
        limit: 50,
        offset,
      });
    }),
  );
}
