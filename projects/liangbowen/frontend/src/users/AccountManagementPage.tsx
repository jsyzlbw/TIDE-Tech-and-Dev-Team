import { useRef, useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/authState";
import { ApiContractError, ApiError, ApiNetworkError, ApiTimeoutError } from "../shared/api/errors";
import type { UserAccount } from "../shared/api/schemas";
import { AsyncState } from "../shared/ui/AsyncState";
import { ToastRegion } from "../shared/ui/ToastRegion";
import { useUsers } from "./api";
import {
  ACCOUNT_PAGE_SIZE,
  canPageForward,
  MAX_ACCOUNT_OFFSET,
} from "./accountPagination";
import { CreateUserDialog } from "./CreateUserDialog";
import "./users.css";

function roleLabel(role: UserAccount["role"]) {
  if (role === "admin") return "管理员";
  if (role === "teacher") return "教师";
  return "学生";
}

function safeListMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 401) return "登录状态已失效，请重新登录。";
  if (error instanceof ApiError && error.status === 403) return "当前账号没有查看账号登记的权限。";
  if (error instanceof ApiTimeoutError) return "读取请求超时，请检查网络后重试。";
  if (error instanceof ApiNetworkError) return "无法连接服务器，请检查网络后重试。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的账号登记，请稍后重试。";
  return "账号数据没有加载，请检查连接后重试。";
}

export function AccountManagementPage() {
  const { user, logout } = useAuth();
  const createTriggerRef = useRef<HTMLButtonElement>(null);
  const [offset, setOffset] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const accounts = useUsers(user?.id, offset);
  const items = accounts.data?.items ?? [];
  const total = accounts.data?.total ?? 0;
  const isAdmin = user?.role === "admin";
  const counts = {
    admin: items.filter((account) => account.role === "admin").length,
    teacher: items.filter((account) => account.role === "teacher").length,
    student: items.filter((account) => account.role === "student").length,
  };
  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + items.length, total);

  return (
    <section className="account-workspace" aria-labelledby="account-workspace-title">
      <header className="account-workspace__header">
        <div className="account-workspace__heading">
          <p>IDENTITY OFFICE / ACCOUNT REGISTER</p>
          <h2 id="account-workspace-title">{isAdmin ? "账号管理台" : "学生账号管理台"}</h2>
          <h3>{isAdmin ? "全体账号名册" : "学生账号名册"}</h3>
          <p>{isAdmin ? "登记管理员、教师与学生账号，并为教学团队创建新的登录身份。" : "查看学生账号，并为新加入课程的学生创建登录身份。"}</p>
        </div>
        <aside className="account-workspace__identity" aria-label="当前登录身份">
          <span>{user?.display_name}</span>
          <span>@{user?.username} · {isAdmin ? "管理员" : "教师"}</span>
          {!isAdmin ? <Link className="account-workspace__back" to="/teacher">返回作业台</Link> : null}
          <button className="button button--secondary" type="button" onClick={logout}>退出登录</button>
        </aside>
        <button ref={createTriggerRef} className="button account-workspace__create" type="button" onClick={() => { setNotice(null); setCreateOpen(true); }}>
          {isAdmin ? "创建账号" : "创建学生账号"}
        </button>
      </header>

      <ToastRegion className="account-workspace__notice" tone="success" message={notice} />

      <section className="account-metrics" aria-label="账号统计">
        <div><span>账号总数</span><strong data-metric="total-count">{total}</strong></div>
        {isAdmin ? <>
          <div><span>本页管理员</span><strong data-metric="admin-count">{counts.admin}</strong></div>
          <div><span>本页教师</span><strong data-metric="teacher-count">{counts.teacher}</strong></div>
          <div><span>本页学生</span><strong data-metric="student-count">{counts.student}</strong></div>
        </> : <div><span>本页学生</span><strong data-metric="student-count">{counts.student}</strong></div>}
        <p>每页最多 {ACCOUNT_PAGE_SIZE} 条 · 仅显示当前权限可见账号</p>
      </section>

      {accounts.isPending ? (
        <AsyncState headingLevel={3} className="account-state" kind="loading" title="正在读取账号登记…" description="正在核对当前权限下可见的账号名册。" />
      ) : accounts.error !== null ? (
        <AsyncState headingLevel={3} className="account-state" kind="error" title="暂时无法读取账号登记" description={safeListMessage(accounts.error)} action={<button className="button button--secondary" type="button" onClick={() => void accounts.refetch()}>重试读取账号</button>} />
      ) : items.length === 0 ? (
        <AsyncState headingLevel={3} className="account-state" kind="empty" title="还没有账号记录" description={`使用页面上方的“${isAdmin ? "创建账号" : "创建学生账号"}”登记第一位使用者。`} />
      ) : (
        <section className="account-register" aria-labelledby="account-register-title">
          <div className="account-register__heading">
            <h3 id="account-register-title">账号登记</h3>
            <span>{pageStart}–{pageEnd} / {total} RECORDS</span>
          </div>
          <div className="account-register__table-wrap">
            <table className="account-table" aria-label="账号登记">
              <thead><tr><th scope="col">账号</th><th scope="col">角色</th><th scope="col">创建人</th><th scope="col">状态</th><th scope="col">创建时间</th></tr></thead>
              <tbody>{items.map((account) => (
                <tr key={account.id}>
                  <th scope="row" data-label="账号"><strong>{account.display_name || account.username}</strong><span>@{account.username}</span></th>
                  <td data-label="角色"><span className="account-role" data-role={account.role}>{roleLabel(account.role)}</span></td>
                  <td data-label="创建人">{account.created_by?.display_name ?? "系统初始化"}</td>
                  <td data-label="状态"><span className="account-status" data-active={account.is_active}>{account.is_active ? "正常" : "停用"}</span></td>
                  <td data-label="创建时间"><time dateTime={account.created_at}>{new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" }).format(new Date(account.created_at))}</time></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <nav className="account-pagination" aria-label="账号分页">
            <span>第 {pageStart}–{pageEnd} 条 / 共 {total} 条</span>
            <div>
              <button className="button button--secondary" type="button" disabled={offset === 0 || accounts.isFetching} onClick={() => setOffset((current) => Math.max(0, current - ACCOUNT_PAGE_SIZE))}>上一页</button>
              <button className="button button--secondary" type="button" disabled={!canPageForward(offset, total) || accounts.isFetching} onClick={() => setOffset((current) => Math.min(MAX_ACCOUNT_OFFSET, current + ACCOUNT_PAGE_SIZE))}>下一页</button>
            </div>
          </nav>
        </section>
      )}

      {createOpen && user !== null && (user.role === "admin" || user.role === "teacher") ? (
        <CreateUserDialog
          actorId={user.id}
          actorRole={user.role}
          returnFocusRef={createTriggerRef}
          onClose={() => setCreateOpen(false)}
          onCreated={(created) => {
            setCreateOpen(false);
            setNotice(`${created.username} 已创建。`);
          }}
        />
      ) : null}
    </section>
  );
}
