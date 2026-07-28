import type { CurrentUser } from "../shared/api/schemas";

export type WorkspaceRole = CurrentUser["role"];

const SAFE_ADMIN_PATH = /^\/admin\/users$/u;
const SAFE_TEACHER_PATH = /^\/teacher(?:$|\/users$|\/(?:assignments|reports)\/[^/?#]+$)/u;
const SAFE_STUDENT_PATH = /^\/student(?:$|\/assignments\/[^/?#]+$)/u;

export function roleLanding(role: CurrentUser["role"]): string | null {
  if (role === "admin") return "/admin/users";
  if (role === "teacher") return "/teacher";
  if (role === "student") return "/student";
  return null;
}

export function safePostLoginDestination(from: unknown, role: CurrentUser["role"]): string {
  const landing = roleLanding(role) ?? "/login";
  if (typeof from !== "string" || !from.startsWith("/") || from.startsWith("//")) {
    return landing;
  }
  try {
    const parsed = new URL(from, "https://local.invalid");
    if (parsed.origin !== "https://local.invalid") return landing;
    const allowed = role === "admin"
      ? SAFE_ADMIN_PATH
      : role === "teacher"
        ? SAFE_TEACHER_PATH
        : SAFE_STUDENT_PATH;
    return allowed.test(parsed.pathname) ? `${parsed.pathname}${parsed.search}${parsed.hash}` : landing;
  } catch {
    return landing;
  }
}
