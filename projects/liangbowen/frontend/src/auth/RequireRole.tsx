import type { ReactNode } from "react";
import { Link, Navigate, Outlet, useLocation } from "react-router";

import { useAuth } from "./authState";
import { roleLanding, type WorkspaceRole } from "./routing";

function SessionLoading() {
  return (
    <section className="session-state" role="status" aria-live="polite">
      <p className="session-state__index">SESSION / CHECK</p>
      <h2>正在核验登录状态…</h2>
      <p>请稍候，正在读取已登记的身份凭据。</p>
    </section>
  );
}

function SessionFailure() {
  const { retrySession } = useAuth();
  return (
    <section className="session-state" role="alert">
      <p className="session-state__index">SESSION / INTERRUPTED</p>
      <h2>暂时无法核验登录状态</h2>
      <p>登录凭据仍保留。请检查网络连接后重新验证。</p>
      <button className="button" type="button" onClick={() => void retrySession()}>
        重试会话验证
      </button>
    </section>
  );
}

export function RequireRole({
  role,
  children,
}: {
  role: WorkspaceRole | readonly WorkspaceRole[];
  children?: ReactNode;
}) {
  const { user, loading, sessionError, logout } = useAuth();
  const location = useLocation();

  if (loading) return <SessionLoading />;
  if (sessionError !== null) return <SessionFailure />;
  if (user === null) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}${location.hash}` }}
      />
    );
  }
  const allowedRoles: readonly WorkspaceRole[] = Array.isArray(role) ? role : [role];
  if (!allowedRoles.includes(user.role)) {
    const ownLanding = roleLanding(user.role);
    return (
      <section className="session-state" role="alert">
        <p className="session-state__index">ACCESS / DENIED</p>
        <h2>无权访问此页面</h2>
        <p>当前身份与页面要求的角色不一致，受保护内容未加载。</p>
        <div className="session-state__actions">
          {ownLanding !== null ? <Link className="button" to={ownLanding}>去自己的工作台</Link> : null}
          <button className="button button--secondary" type="button" onClick={logout}>
            退出登录
          </button>
        </div>
      </section>
    );
  }
  return children ?? <Outlet />;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const { loading, sessionError } = useAuth();
  if (loading) return <SessionLoading />;
  if (sessionError !== null) return <SessionFailure />;
  return children;
}
