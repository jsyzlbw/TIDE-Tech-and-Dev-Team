import { Link, Navigate } from "react-router";

import { AuthGate } from "../auth/RequireRole";
import { useAuth } from "../auth/authState";
import { roleLanding, type WorkspaceRole } from "../auth/routing";

export function LandingRedirect() {
  const { user, logout } = useAuth();
  return (
    <AuthGate>
      {user === null ? (
        <Navigate to="/login" replace />
      ) : roleLanding(user.role) === null ? (
        <section className="session-state" role="alert">
          <p className="session-state__index">ACCESS / UNASSIGNED</p>
          <h2>当前角色暂未开放工作台</h2>
          <p>此账号角色不属于教师或学生入口。</p>
          <button className="button button--secondary" type="button" onClick={logout}>退出登录</button>
        </section>
      ) : (
        <Navigate to={roleLanding(user.role)!} replace />
      )}
    </AuthGate>
  );
}

export function RoleWorkspace({ role }: { role: WorkspaceRole }) {
  const { user, logout } = useAuth();
  const teacher = role === "teacher";
  return (
    <section className="role-workspace" aria-labelledby={`${role}-workspace-title`}>
      <p className="role-workspace__folio">{teacher ? "FACULTY / 01" : "STUDENT / 01"}</p>
      <div className="role-workspace__copy">
        <p className="role-workspace__kicker">{teacher ? "REVIEW REGISTER" : "SUBMISSION REGISTER"}</p>
        <h2 id={`${role}-workspace-title`}>{teacher ? "教师评阅工作台" : "学生作业台"}</h2>
        <p>{teacher ? "作业发布与评阅流程将在下一阶段接入。" : "作业查看与提交流程将在下一阶段接入。"}</p>
      </div>
      <aside className="role-workspace__identity" aria-label="当前登录身份">
        <span>{user?.display_name}</span>
        <span>@{user?.username}</span>
        <button className="button button--secondary" type="button" onClick={logout}>退出登录</button>
      </aside>
    </section>
  );
}

export function RoutePlaceholder({ title }: { title: string }) {
  return (
    <section className="session-state">
      <p className="session-state__index">ROUTE / RESERVED</p>
      <h2>{title}</h2>
      <p>路由与访问边界已经就绪，业务内容将在后续阶段接入。</p>
    </section>
  );
}

export function NotFoundPage() {
  const { user } = useAuth();
  const entry = user === null ? "/login" : (roleLanding(user.role) ?? "/login");
  return (
    <section className="session-state" role="alert">
      <p className="session-state__index">ERROR / 404</p>
      <h2>页面未找到</h2>
      <p>这张页面不在当前评审登记簿中。</p>
      <Link className="button" to={entry}>返回入口</Link>
    </section>
  );
}
