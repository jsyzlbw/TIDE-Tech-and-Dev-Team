import type { AssignmentSummary } from "../shared/api/schemas";

interface EvaluationProgressProps {
  summary: AssignmentSummary;
}

export function EvaluationProgress({ summary }: EvaluationProgressProps) {
  const metrics = [
    ["待评估", summary.pending_evaluation],
    ["评估中", summary.evaluating],
    ["待审核", summary.pending_review],
    ["已审核", summary.reviewed],
    ["失败", summary.failed],
  ] as const;

  return (
    <section className="evaluation-progress" aria-label="评估进度">
      <p className="sr-only" role="status" aria-label="评估进度播报" aria-live="polite">
        评估进度更新：待评估 {summary.pending_evaluation}，评估中 {summary.evaluating}，待审核 {summary.pending_review}，已审核 {summary.reviewed}，失败 {summary.failed}。
      </p>
      {metrics.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </section>
  );
}
