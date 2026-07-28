import type { AssignmentSummaryStudent } from "../shared/api/schemas";

export type SubmissionCategory = "missing" | "pending" | "running" | "review" | "reviewed" | "failed";
export type SubmissionFilter = "all" | Exclude<SubmissionCategory, "missing">;

export function classifySubmission(row: AssignmentSummaryStudent): SubmissionCategory {
  if (row.latest_submission === null) return "missing";
  if (row.evaluation_status === "failed") return "failed";
  if (row.evaluation_status === "running") return "running";
  if (row.evaluation_status === "queued") return "pending";
  if (row.report_status === "proposed") return "review";
  if (row.report_status === "confirmed" || row.report_status === "modified") return "reviewed";
  return "pending";
}

export const filterLabels: Record<SubmissionFilter, string> = {
  all: "全部",
  pending: "待评估",
  running: "评估中",
  review: "待审核",
  reviewed: "已审核",
  failed: "失败",
};
