import type { WorkspaceReport } from "../shared/api/schemas";

const completenessLabels = { complete: "完整", partial: "部分完整", incomplete: "不完整" } as const;
const correctnessLabels = {
  correct: "正确",
  mostly_correct: "基本正确",
  partially_correct: "部分正确",
  incorrect: "错误",
  unable_to_determine: "暂不能判断",
} as const;

function Empty({ children }: { children: string }) {
  return <p className="report-empty">{children}</p>;
}

export function ReportPanel({ report }: { report: WorkspaceReport }) {
  return (
    <div className="report-panel">
      <section aria-labelledby="report-completeness">
        <p className="report-panel__index">01 / COVERAGE</p>
        <h4 id="report-completeness">答案完整性</h4>
        <p><strong>{completenessLabels[report.completeness.level]}</strong> · {report.completeness.rationale}</p>
        {report.completeness.covered_points.length > 0 && <><h5>已覆盖</h5><ul>{report.completeness.covered_points.map((point) => <li key={point}>{point}</li>)}</ul></>}
        {report.completeness.missing_points.length > 0 && <><h5>待补充</h5><ul>{report.completeness.missing_points.map((point) => <li key={point}>{point}</li>)}</ul></>}
      </section>

      <div className="report-panel__score" aria-label="评分摘要">
        <div><span>基础评分</span><strong>{report.score} · {report.grade}</strong></div>
        <div><span>报告版本</span><strong>v{report.version}</strong></div>
      </div>

      <section aria-labelledby="report-correctness">
        <p className="report-panel__index">02 / JUDGMENT</p>
        <h4 id="report-correctness">正确性初步判断</h4>
        <p><strong>{correctnessLabels[report.correctness.judgment]}</strong> · {report.correctness.rationale}</p>
      </section>

      <section aria-labelledby="report-issues">
        <p className="report-panel__index">03 / FINDINGS</p>
        <h4 id="report-issues">主要问题</h4>
        {report.major_issues.length === 0 ? <Empty>未发现明确的主要问题。</Empty> : report.major_issues.map((issue) => (
          <article className="report-finding" key={issue.code}>
            <strong>{issue.title}</strong><span>{issue.code}</span>
            <p>证据：{issue.evidence}</p><p>影响：{issue.impact}</p>
          </article>
        ))}
      </section>

      <section aria-labelledby="report-suggestions">
        <p className="report-panel__index">04 / REVISION</p>
        <h4 id="report-suggestions">修改建议</h4>
        {report.suggestions.length === 0 ? <Empty>暂无修改建议。</Empty> : <ol>{report.suggestions.map((suggestion, index) => (
          <li key={`${suggestion.priority}:${suggestion.action}:${index}`}>
            <strong>{suggestion.priority.toUpperCase()}</strong> {suggestion.action}
            {suggestion.example !== "" && <small>示例：{suggestion.example}</small>}
          </li>
        ))}</ol>}
      </section>

      <section className="report-panel__meta" aria-label="评估边界">
        <div><span>置信度</span><strong>{Math.round(report.confidence * 100)}%</strong></div>
        <div><span>校验状态</span><strong>{report.validation_status === "valid" ? "已通过" : "已修复"}</strong></div>
        <div><span>审核状态</span><strong>{report.review_status}</strong></div>
        <div className="report-panel__limitations"><span>局限说明</span>{report.limitations.length === 0 ? <Empty>无</Empty> : <ul>{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul>}</div>
      </section>

      {report.latest_review_action !== null && (
        <aside className="latest-review-action">
          <strong>最近审核动作</strong>
          <span>{report.latest_review_action.teacher.display_name} · {report.latest_review_action.action}</span>
          {report.latest_review_action.comment !== "" && <p>{report.latest_review_action.comment}</p>}
        </aside>
      )}
    </div>
  );
}
