import { useEffect, useState } from "react";
import { Link } from "react-router";

import type { AssignmentListItem } from "../shared/api/schemas";

const statusLabels = {
  draft: "草稿",
  published: "已发布",
  closed: "已关闭",
  archived: "已归档",
} as const;

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export type AssignmentLifecycleAction = "publish" | "close" | "reopen";

interface AssignmentCardProps {
  assignment: AssignmentListItem;
  pendingAction?: AssignmentLifecycleAction;
  onLifecycleAction(action: AssignmentLifecycleAction, assignment: AssignmentListItem): void;
}

const actionLabels = {
  publish: "发布",
  close: "关闭",
  reopen: "重新开放",
} as const;

const MAX_SAFE_TIMER_DELAY = 2_147_483_647;
const DEADLINE_RECHECK_MS = 30_000;

function useDeadlineReached(value: string) {
  const dueTime = Date.parse(value);
  const [observedAt, setObservedAt] = useState(() => Date.now());

  useEffect(() => {
    if (!Number.isFinite(dueTime)) return;

    let timeoutId: number | undefined;
    let cancelled = false;

    function schedule() {
      const remaining = dueTime - Date.now();
      const delay = remaining <= 0
        ? 0
        : Math.min(remaining + 1, DEADLINE_RECHECK_MS, MAX_SAFE_TIMER_DELAY);
      timeoutId = window.setTimeout(() => {
        if (cancelled) return;
        const now = Date.now();
        setObservedAt(now);
        if (now < dueTime) schedule();
      }, delay);
    }

    const recheck = () => setObservedAt(Date.now());
    schedule();
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", recheck);
    return () => {
      cancelled = true;
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", recheck);
    };
  }, [dueTime]);

  return {
    dueTime,
    overdue: Number.isFinite(dueTime) && observedAt >= dueTime,
    recheck: () => setObservedAt(Date.now()),
  };
}

export function AssignmentCard({ assignment, pendingAction, onLifecycleAction }: AssignmentCardProps) {
  const { dueTime, overdue, recheck } = useDeadlineReached(assignment.due_at);
  const action: AssignmentLifecycleAction | null =
    assignment.status === "draft"
      ? "publish"
      : assignment.status === "published"
        ? "close"
        : assignment.status === "closed"
          ? "reopen"
          : null;
  const deadlineInvalid = overdue && (action === "publish" || action === "reopen");
  const actionLabel = action === null ? null : actionLabels[action];
  return (
    <article className="assignment-record">
      <div className="assignment-record__index">
        <span>{assignment.code}</span>
        <span className="status-stamp" data-tone={assignment.status === "published" ? "confirmed" : undefined}>
          {statusLabels[assignment.status]}
        </span>
      </div>
      <div className="assignment-record__body">
        <h4>
          <Link to={`/teacher/assignments/${assignment.id}`}>{assignment.title}</Link>
        </h4>
        <dl>
          <div>
            <dt>截止</dt>
            <dd>{formatDateTime(assignment.due_at)}{overdue ? " · 已逾期" : ""}</dd>
          </div>
          <div>
            <dt>{assignment.published_at === null ? "创建" : "发布"}</dt>
            <dd>{formatDateTime(assignment.published_at ?? assignment.created_at)}</dd>
          </div>
        </dl>
      </div>
      <div className="assignment-record__action">
        {action !== null && actionLabel !== null ? (
          <>
            <button
              className="button button--secondary"
              type="button"
              aria-label={`${pendingAction === action ? `正在${actionLabel}` : actionLabel} ${assignment.code}`}
              disabled={pendingAction === action || deadlineInvalid}
              onClick={() => {
                if (
                  Number.isFinite(dueTime) &&
                  Date.now() >= dueTime &&
                  (action === "publish" || action === "reopen")
                ) {
                  recheck();
                  return;
                }
                onLifecycleAction(action, assignment);
              }}
            >
              {pendingAction === action ? `正在${actionLabel}…` : actionLabel}
            </button>
            {deadlineInvalid ? <span className="assignment-record__deadline-note">截止时间已过，无法执行此操作</span> : null}
          </>
        ) : (
          <span>只读记录</span>
        )}
      </div>
    </article>
  );
}
