import { useRef, useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/authState";
import {
  AssignmentCard,
  type AssignmentLifecycleAction,
} from "./AssignmentCard";
import { CreateAssignmentDialog } from "./CreateAssignmentDialog";
import {
  useAssignmentDashboardMetrics,
  useAssignments,
  useCloseAssignment,
  usePublishAssignment,
  useReopenAssignment,
} from "./api";
import type { AssignmentListItem } from "../shared/api/schemas";
import { ApiContractError, ApiError, ApiNetworkError, ApiTimeoutError } from "../shared/api/errors";
import { AsyncState } from "../shared/ui/AsyncState";
import { ToastRegion } from "../shared/ui/ToastRegion";
import "./assignments.css";

interface DashboardBanner {
  message: string;
  tone: "success" | "error";
}

function safeLifecycleMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 401) return "登录状态已失效，请重新登录。";
  if (error instanceof ApiError && error.status === 409) return "作业状态已变化，请刷新列表后重试。";
  if (error instanceof ApiTimeoutError) return "状态更新请求超时，请检查网络后重试。";
  if (error instanceof ApiNetworkError) return "无法连接服务器，请检查网络后重试。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的作业数据，请刷新后重试。";
  return "暂时无法更新作业状态，请稍后重试。";
}

export function TeacherDashboard() {
  const { user, logout } = useAuth();
  const createTriggerRef = useRef<HTMLButtonElement>(null);
  const inFlightActions = useRef(new Set<string>());
  const [createOpen, setCreateOpen] = useState(false);
  const [banner, setBanner] = useState<DashboardBanner | null>(null);
  const [pendingActions, setPendingActions] = useState<ReadonlySet<string>>(new Set());
  const assignments = useAssignments(user?.id);
  const metrics = useAssignmentDashboardMetrics(user?.id, assignments.data);
  const publishAssignment = usePublishAssignment(user?.id ?? "anonymous");
  const closeAssignment = useCloseAssignment(user?.id ?? "anonymous");
  const reopenAssignment = useReopenAssignment(user?.id ?? "anonymous");
  const metricValues = metrics.data ?? {
    visibleAssignments: assignments.data?.length ?? 0,
    pendingEvaluation: 0,
    pendingReview: 0,
    failed: 0,
    covered: 0,
    total: assignments.data?.length ?? 0,
  };

  async function runLifecycleAction(
    action: AssignmentLifecycleAction,
    assignment: AssignmentListItem,
  ) {
    const key = `${action}:${assignment.id}`;
    if (inFlightActions.current.has(key)) return;
    inFlightActions.current.add(key);
    setPendingActions((current) => new Set(current).add(key));
    setBanner(null);
    const mutation =
      action === "publish"
        ? publishAssignment
        : action === "close"
          ? closeAssignment
          : reopenAssignment;
    try {
      await mutation.mutateAsync(assignment.id);
      const successVerb = action === "publish" ? "已发布" : action === "close" ? "已关闭" : "已重新开放";
      setBanner({ message: `${assignment.code} ${successVerb}。`, tone: "success" });
    } catch (error) {
      setBanner({ message: safeLifecycleMessage(error), tone: "error" });
    } finally {
      inFlightActions.current.delete(key);
      setPendingActions((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
    }
  }

  return (
    <section className="teacher-dashboard" aria-labelledby="teacher-dashboard-title">
      <header className="teacher-dashboard__header">
        <div className="teacher-dashboard__heading">
          <p className="teacher-dashboard__kicker">FACULTY / ASSIGNMENT REGISTER</p>
          <h2 id="teacher-dashboard-title">教师评阅工作台</h2>
          <h3>作业发布与评阅</h3>
          <p>登记课程作业、控制发布状态，并掌握首批作业的评估与审核积压。</p>
        </div>
        <aside className="teacher-dashboard__identity" aria-label="当前教师身份">
          <span>{user?.display_name}</span>
          <span>@{user?.username}</span>
          <button className="button button--secondary" type="button" onClick={logout}>退出登录</button>
        </aside>
        <div className="teacher-dashboard__tools">
          <Link className="button button--secondary teacher-dashboard__account-link" to="/teacher/users">管理学生账号</Link>
          <button ref={createTriggerRef} className="button teacher-dashboard__create" type="button" onClick={() => { setBanner(null); setCreateOpen(true); }}>发布新作业</button>
        </div>
      </header>

      <ToastRegion className="dashboard-banner" tone={banner?.tone ?? "info"} message={banner?.message ?? null} />

      <section className="metric-register" aria-label="作业统计">
        <div><span>首批作业</span><strong data-metric="visible-assignments">{metricValues.visibleAssignments}</strong></div>
        <div><span>待评估</span><strong data-metric="pending-evaluation">{metricValues.pendingEvaluation}</strong></div>
        <div><span>待审核</span><strong data-metric="pending-review">{metricValues.pendingReview}</strong></div>
        <div><span>失败任务</span><strong data-metric="failed">{metricValues.failed}</strong></div>
        <p data-testid="metric-coverage">
          统计覆盖 {metricValues.covered} / {metricValues.total} · 首批最多 50 份作业
          {metrics.isFetching ? " · 更新中" : ""}
        </p>
      </section>

      {assignments.isPending ? (
        <AsyncState headingLevel={3} className="dashboard-state" kind="loading" title="正在读取作业登记…" description="正在调取当前教师的首批 50 份作业。" />
      ) : assignments.error !== null ? (
        <AsyncState headingLevel={3} className="dashboard-state dashboard-state--error" kind="error" title="暂时无法读取作业列表" description="作业数据没有加载，请检查连接后重试。" action={<button className="button button--secondary" type="button" onClick={() => void assignments.refetch()}>重试读取作业</button>} />
      ) : assignments.data?.length === 0 ? (
        <AsyncState headingLevel={3} className="dashboard-state" kind="empty" title="还没有作业记录" description="使用页面上方的“发布新作业”先保存一份作业草稿。" />
      ) : (
        <section className="assignment-register" aria-labelledby="assignment-register-title">
          <div className="assignment-register__heading">
            <h3 id="assignment-register-title">作业登记</h3>
            <span>{assignments.data?.length ?? 0} RECORDS</span>
          </div>
          {metrics.data !== undefined && metrics.data.covered < metrics.data.total ? (
            <AsyncState headingLevel={4} className="metric-warning" kind="partial" title="部分统计暂不可用" description="作业列表仍可继续使用。" action={<button className="button button--secondary" type="button" onClick={() => void metrics.refetch()}>重试统计</button>} />
          ) : null}
          <div className="assignment-register__rows">
            {assignments.data?.map((assignment) => (
              <AssignmentCard
                assignment={assignment}
                key={assignment.id}
                pendingAction={(["publish", "close", "reopen"] as const).find((action) =>
                  pendingActions.has(`${action}:${assignment.id}`),
                )}
                onLifecycleAction={(action, item) => void runLifecycleAction(action, item)}
              />
            ))}
          </div>
        </section>
      )}
      {createOpen && user !== null ? (
        <CreateAssignmentDialog
          userId={user.id}
          returnFocusRef={createTriggerRef}
          onClose={() => setCreateOpen(false)}
          onSaved={() => {
            setCreateOpen(false);
            setBanner({ message: "草稿已保存，可在作业登记中确认后发布。", tone: "success" });
          }}
        />
      ) : null}
    </section>
  );
}
