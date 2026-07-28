import type { ChangeEvent } from "react";
import { Link, useNavigate } from "react-router";

import type { ReportWorkspace, TimelineReportSummary, WorkspaceReport } from "../shared/api/schemas";

const statusLabels = {
  proposed: "待审核", confirmed: "已确认", modified: "教师修订", superseded: "已替代",
} as const;

function dateTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function ReportTimeline({
  workspace,
  selected,
  onExport,
}: {
  workspace: ReportWorkspace;
  selected: WorkspaceReport;
  onExport(): void;
}) {
  const navigate = useNavigate();
  const reports: TimelineReportSummary[] = workspace.timeline.some((report) => report.id === selected.id)
    ? workspace.timeline
    : [selected, ...workspace.timeline];
  function selectVersion(event: ChangeEvent<HTMLSelectElement>) {
    navigate(`/teacher/reports/${event.target.value}`);
  }
  const job = workspace.reevaluation_job;
  const pending = job?.status === "queued" || job?.status === "running";

  return (
    <section className="report-timeline" aria-labelledby="timeline-title">
      <div className="report-timeline__heading">
        <div><p>VERSION REGISTER</p><h3 id="timeline-title">版本记录</h3></div>
        <div className="report-timeline__controls">
          <label>查看版本<select className="field" name="report_version" autoComplete="off" value={selected.id} onChange={selectVersion}>{reports.map((report) => <option value={report.id} key={report.id}>v{report.version} · {statusLabels[report.review_status]}</option>)}</select></label>
          <button className="button button--secondary" type="button" onClick={onExport}>导出报告 JSON</button>
        </div>
      </div>
      {workspace.timeline_truncated && <p className="timeline-disclosure">共 {workspace.timeline_total} 个版本，仅列出最近 100 个；当前旧版本会单独保留。</p>}
      {pending && <div className="timeline-pending" role="status" aria-live="polite"><strong>{job?.status === "running" ? "重评任务执行中" : "重评任务排队中"}</strong><span>来源报告 {job?.source_report_id.slice(0, 8)}</span></div>}
      <ol className={workspace.timeline.length > 50 ? "is-windowed" : undefined}>{workspace.timeline.map((report) => <li key={report.id} className={report.id === selected.id ? "is-selected" : ""}>
        <Link to={`/teacher/reports/${report.id}`} aria-label={`查看版本 ${report.version}`}>
          <span>v{report.version} · {report.origin === "agent" ? "AI" : "教师"}</span>
          <strong>{report.score} / {report.grade}</strong>
          <span>{statusLabels[report.review_status]}</span>
          <small>{report.author.display_name} · {dateTime(report.created_at)}</small>
          {report.latest_review_action !== null && <small>最近审核动作：{report.latest_review_action.teacher.display_name} · {report.latest_review_action.action} · {dateTime(report.latest_review_action.acted_at)}</small>}
        </Link>
      </li>)}</ol>
    </section>
  );
}
