import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../shared/api/client";
import {
  rawReportOutputSchema,
  reportWorkspaceSchema,
  reviewJobSchema,
  reviewReportSchema,
  type AnswerCompleteness,
  type Correctness,
  type MajorIssue,
  type Suggestion,
  type WorkspaceReport,
} from "../shared/api/schemas";

// A 2 MiB decoded string made only of JSON control characters expands to 12 MiB on the wire.
export const RAW_OUTPUT_MAX_RESPONSE_BYTES = 13 * 1024 * 1024;
export const WORKSPACE_MAX_RESPONSE_BYTES = 3 * 1024 * 1024;

export const reportKeys = {
  workspace: (userId: string, reportId: string) =>
    ["reports", "workspace", userId, reportId] as const,
  raw: (userId: string, reportId: string) => ["reports", "raw", userId, reportId] as const,
};

export interface ReportPatchPayload {
  completeness?: AnswerCompleteness;
  correctness?: Correctness;
  major_issues?: MajorIssue[];
  suggestions?: Suggestion[];
  score?: number;
  limitations?: string[];
  comment?: string;
}

function isPendingJob(status: string | undefined) {
  return status === "queued" || status === "running";
}

export function useReportWorkspace(userId: string | undefined, reportId: string | undefined) {
  return useQuery({
    queryKey: reportKeys.workspace(userId ?? "anonymous", reportId ?? "invalid"),
    enabled: userId !== undefined && reportId !== undefined,
    retry: false,
    queryFn: ({ signal }) => api.get(`/reports/${reportId}/workspace`, reportWorkspaceSchema, {
      signal,
      maxResponseBytes: WORKSPACE_MAX_RESPONSE_BYTES,
    }),
    refetchInterval: (query) => isPendingJob(query.state.data?.reevaluation_job?.status) ? 2_000 : false,
  });
}

export function useRawReportOutput(
  userId: string | undefined,
  reportId: string | undefined,
  expanded: boolean,
) {
  return useQuery({
    queryKey: reportKeys.raw(userId ?? "anonymous", reportId ?? "invalid"),
    enabled: expanded && userId !== undefined && reportId !== undefined,
    retry: false,
    queryFn: ({ signal }) => api.get(`/reports/${reportId}/raw-output`, rawReportOutputSchema, {
      signal,
      maxResponseBytes: RAW_OUTPUT_MAX_RESPONSE_BYTES,
    }),
  });
}

async function invalidateReview(
  client: QueryClient,
  userId: string,
  reportId: string,
  assignmentId: string | undefined,
) {
  const invalidations: Promise<unknown>[] = [
    client.invalidateQueries({ queryKey: reportKeys.workspace(userId, reportId), exact: true }),
  ];
  if (assignmentId !== undefined) {
    invalidations.push(client.invalidateQueries({ queryKey: ["assignments", "summary", userId, assignmentId] }));
  }
  await Promise.all(invalidations);
}

export function useConfirmReport(userId: string, reportId: string, assignmentId?: string) {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (comment: string) => api.post(`/reports/${reportId}/confirm`, { comment }, reviewReportSchema),
    onSuccess: () => invalidateReview(client, userId, reportId, assignmentId),
  });
}

export function useModifyReport(userId: string, reportId: string, assignmentId?: string) {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (payload: ReportPatchPayload) => api.patch(`/reports/${reportId}`, payload, reviewReportSchema),
    onSuccess: () => invalidateReview(client, userId, reportId, assignmentId),
  });
}

export function useReevaluateReport(userId: string, reportId: string, assignmentId?: string) {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (comment: string) => api.post(`/reports/${reportId}/reevaluate`, { comment }, reviewJobSchema),
    onSuccess: () => invalidateReview(client, userId, reportId, assignmentId),
  });
}

export function publicReportExport(report: WorkspaceReport) {
  return {
    report_id: report.id,
    submission_id: report.submission_id,
    job_id: report.job_id,
    source_report_id: report.source_report_id,
    origin: report.origin,
    version: report.version,
    schema_version: report.schema_version,
    completeness: report.completeness,
    correctness: report.correctness,
    major_issues: report.major_issues,
    suggestions: report.suggestions,
    score: report.score,
    grade: report.grade,
    confidence: report.confidence,
    limitations: report.limitations,
    validation_status: report.validation_status,
    review_status: report.review_status,
    author: report.author,
    latest_review_action: report.latest_review_action,
    created_at: report.created_at,
  };
}

export function downloadReportJson(report: WorkspaceReport) {
  const json = `${JSON.stringify(publicReportExport(report), null, 2)}\n`;
  const blob = new Blob([json], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `report-${report.id}-v${report.version}.json`;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
