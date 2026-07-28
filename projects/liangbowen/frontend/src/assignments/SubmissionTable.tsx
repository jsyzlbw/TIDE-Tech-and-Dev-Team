import type { AssignmentSummaryStudent } from "../shared/api/schemas";
import { Link } from "react-router";
import { StatusBadge, type StatusBadgeTone } from "../shared/ui/StatusBadge";
import { classifySubmission, filterLabels, type SubmissionFilter } from "./submissionStatus";

function rowStatus(row: AssignmentSummaryStudent) {
  const category = classifySubmission(row);
  return category === "missing" ? "尚未提交" : filterLabels[category];
}

function rowTone(row: AssignmentSummaryStudent): StatusBadgeTone {
  const category = classifySubmission(row);
  if (category === "failed") return "failed";
  if (category === "review") return "warning";
  if (category === "reviewed") return "success";
  if (category === "running") return "info";
  return "neutral";
}

const SAFE_FAILURE_MESSAGES: Readonly<Record<string, string>> = {
  configuration: "评估服务配置不可用，请联系管理员。",
  timeout: "评估服务响应超时，请重新发起评估。",
  network: "评估服务网络连接失败，请重新发起评估。",
  authentication: "评估服务认证失败，请联系管理员。",
  rate_limited: "评估请求过于频繁，请稍后重新发起评估。",
  upstream: "评估服务暂时不可用，请稍后重新发起评估。",
  protocol: "评估服务返回了无效结果，请重新发起评估。",
  response_too_large: "评估结果过大，无法处理，请联系管理员。",
  fixture: "评估测试数据无效，请联系管理员。",
  validation_exhausted: "评估结果无法通过校验，请重新发起评估。",
  internal_error: "评估服务发生内部错误，请重新发起评估。",
};

function safeFailureMessage(code: string | null) {
  return code === null ? "评估未完成，请重新发起评估。" : SAFE_FAILURE_MESSAGES[code] ?? "评估未完成，请重新发起评估。";
}

function formatTime(value: string | null) {
  if (value === null) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

interface SubmissionTableProps {
  rows: AssignmentSummaryStudent[];
  filter: SubmissionFilter;
  retryingIds: ReadonlySet<string>;
  onRetry(row: AssignmentSummaryStudent): void;
}

export function SubmissionTable({ rows, filter, retryingIds, onRetry }: SubmissionTableProps) {
  const visible = filter === "all" ? rows : rows.filter((row) => classifySubmission(row) === filter);

  return (
    <div className="submission-table-wrap">
      <p className="sr-only" role="status" aria-label="提交列表更新" aria-live="polite">
        当前页提交列表已更新，共显示 {visible.length} 名学生。
      </p>
      <table className={`submission-table submission-table--compact-cards${rows.length > 50 ? " submission-table--large" : ""}`}>
        <caption className="sr-only">当前页学生提交与评估状态</caption>
        <thead>
          <tr><th scope="col">学生</th><th scope="col">最新版本</th><th scope="col">提交时间</th><th scope="col">评估状态</th><th scope="col">分数等级</th><th scope="col">操作</th></tr>
        </thead>
        <tbody>
          {visible.length === 0 ? (
            <tr><td className="submission-table__empty" colSpan={6}>当前页没有“{filterLabels[filter]}”学生</td></tr>
          ) : visible.map((row) => {
            const category = classifySubmission(row);
            const retrying = row.latest_submission !== null && retryingIds.has(row.latest_submission.id);
            return (
              <tr key={row.student_id} data-state={category}>
                <th scope="row" data-label="学生"><strong>{row.display_name || row.username}</strong><span>@{row.username}</span></th>
                <td data-label="最新版本">{row.latest_version === null ? "—" : `v${row.latest_version}`}</td>
                <td data-label="提交时间">{formatTime(row.submitted_at)}</td>
                <td data-label="评估状态"><StatusBadge className={`status-mark status-mark--${category}`} tone={rowTone(row)}>{rowStatus(row)}</StatusBadge>{category === "failed" ? <small>{safeFailureMessage(row.evaluation_error_code)}</small> : null}</td>
                <td data-label="分数等级">{row.score === null ? "—" : `${row.score} · ${row.grade ?? "—"}`}</td>
                <td data-label="操作">
                  {category === "failed" && row.latest_submission !== null ? (
                    <button className="button button--secondary" type="button" disabled={retrying} onClick={() => onRetry(row)} aria-label={`重新评估 ${row.display_name || row.username}`}>{retrying ? "正在重试…" : "重新评估"}</button>
                  ) : category === "review" && row.latest_report_id !== null ? (
                    <Link className="button button--secondary" to={`/teacher/reports/${row.latest_report_id}`}>打开报告</Link>
                  ) : <span aria-hidden="true">—</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
