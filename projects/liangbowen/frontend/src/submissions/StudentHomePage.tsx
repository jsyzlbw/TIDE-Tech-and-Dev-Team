import { useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/authState";
import type { AssignmentListItem } from "../shared/api/schemas";
import { AsyncState } from "../shared/ui/AsyncState";
import { StatusBadge } from "../shared/ui/StatusBadge";
import {
  STUDENT_ASSIGNMENT_PAGE_SIZE,
  useStudentAssignments,
} from "./api";
import { useDeadlineState } from "./deadline";
import "./submissions.css";

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

export function StudentAssignmentCard({ assignment }: { assignment: AssignmentListItem }) {
  const deadline = useDeadlineState(assignment.due_at);
  const closed = assignment.status !== "published";
  const submittable = !closed && !deadline.expired;
  const state = closed ? "已关闭" : deadline.expired ? "已逾期" : "进行中";

  function finalDeadlineCheck(event: React.MouseEvent<HTMLAnchorElement>) {
    if (closed || Date.now() < deadline.dueTimestamp) return;
    event.preventDefault();
    deadline.recheck();
  }

  return (
    <article className="student-assignment-record">
      <div className="student-assignment-record__code">
        <span>{assignment.code}</span>
        <StatusBadge className={`status-mark status-mark--${submittable ? "open" : "closed"}`} tone={submittable ? "success" : "failed"}>{state}</StatusBadge>
      </div>
      <div className="student-assignment-record__body">
        <h3>{assignment.title}</h3>
        <dl>
          <div><dt>截止时间</dt><dd><time dateTime={assignment.due_at}>{formatDate(assignment.due_at)}</time></dd></div>
          <div><dt>提交状态</dt><dd>{submittable ? "可提交" : "不可提交"}</dd></div>
        </dl>
      </div>
      <Link
        className={`button ${submittable ? "" : "button--secondary"}`}
        to={`/student/assignments/${assignment.id}`}
        aria-label={`${submittable ? "打开" : "查看"} ${assignment.code}`}
        onClick={finalDeadlineCheck}
      >
        {submittable ? "进入作业" : "查看记录"}
      </Link>
    </article>
  );
}

export function StudentHomePage() {
  const { user, logout } = useAuth();
  const [offset, setOffset] = useState(0);
  const assignments = useStudentAssignments(user?.id, offset);
  const rows = assignments.data?.slice(0, STUDENT_ASSIGNMENT_PAGE_SIZE) ?? [];
  const hasNext = (assignments.data?.length ?? 0) > STUDENT_ASSIGNMENT_PAGE_SIZE;
  const page = offset / STUDENT_ASSIGNMENT_PAGE_SIZE + 1;

  return (
    <section className="student-home" aria-labelledby="student-home-title">
      <header className="student-home__header">
        <div className="student-home__heading">
          <p className="student-kicker">STUDENT / ASSIGNMENT REGISTER</p>
          <h2 id="student-home-title">我的作业</h2>
          <p>按截止时间查阅课程作业，在同一案头完成答案提交与评估回看。</p>
        </div>
        <aside className="student-home__identity" aria-label="当前学生身份">
          <span>{user?.display_name}</span>
          <span>@{user?.username}</span>
          <button className="button button--secondary" type="button" onClick={logout}>退出登录</button>
        </aside>
      </header>

      {assignments.error !== null ? (
        <AsyncState headingLevel={3} className="student-state student-state--error" kind="error" title="暂时无法读取作业" description="作业列表没有加载，请检查连接后重试。" action={<button className="button button--secondary" type="button" onClick={() => void assignments.refetch()}>重试读取作业</button>} />
      ) : assignments.isPending ? (
        <AsyncState headingLevel={3} className="student-state" kind="loading" title="正在读取作业…" description="正在整理当前页的课程作业。" />
      ) : rows.length === 0 ? (
        <AsyncState headingLevel={3} className="student-state" kind="empty" title="还没有已发布的作业" description={offset === 0 ? "教师发布作业后会显示在这里。" : "这一页没有更多作业。"} />
      ) : (
        <section className="student-assignment-register" aria-labelledby="student-register-title">
          <div className="student-assignment-register__heading">
            <h3 id="student-register-title">作业登记</h3>
            <span>{rows.length} RECORDS</span>
          </div>
          {rows.map((assignment) => <StudentAssignmentCard key={assignment.id} assignment={assignment} />)}
        </section>
      )}

      <nav className="student-pagination" aria-label="作业分页">
        <button className="button button--secondary" type="button" disabled={offset === 0 || assignments.isFetching} onClick={() => setOffset((value) => Math.max(0, value - STUDENT_ASSIGNMENT_PAGE_SIZE))}>上一页</button>
        <span aria-live="polite">第 {page} 页{assignments.isFetching && !assignments.isPending ? " · 更新中" : ""}</span>
        <button className="button button--secondary" type="button" disabled={!hasNext || assignments.isFetching} onClick={() => setOffset((value) => value + STUDENT_ASSIGNMENT_PAGE_SIZE)}>下一页</button>
      </nav>
    </section>
  );
}
