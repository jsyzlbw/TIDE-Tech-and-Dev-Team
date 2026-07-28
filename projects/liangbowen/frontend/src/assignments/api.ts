import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../shared/api/client";
import {
  assignmentListItemSchema,
  assignmentSummarySchema,
  assignmentReadSchema,
  evaluationBatchSchema,
  evaluationJobSchema,
  type AssignmentListItem,
  type AssignmentRead,
  type AssignmentSummary,
} from "../shared/api/schemas";

const ASSIGNMENT_PAGE_SIZE = 50;
const SUMMARY_CONCURRENCY = 4;

export const assignmentKeys = {
  root: ["assignments"] as const,
  list: (userId: string) => ["assignments", "list", userId, ASSIGNMENT_PAGE_SIZE, 0] as const,
  metrics: (userId: string, assignmentIds: readonly string[]) =>
    ["assignments", "metrics", userId, assignmentIds] as const,
  detail: (userId: string, assignmentId: string) =>
    ["assignments", "detail", userId, assignmentId] as const,
  summary: (userId: string, assignmentId: string, offset: number) =>
    ["assignments", "summary", userId, assignmentId, offset, 100] as const,
};

const assignmentListSchema = assignmentListItemSchema.array();

export interface AssignmentDashboardMetrics {
  visibleAssignments: number;
  pendingEvaluation: number;
  pendingReview: number;
  failed: number;
  covered: number;
  total: number;
}

export interface AssignmentCreatePayload {
  title: string;
  question: string;
  notes: string;
  due_at: string;
  rubric: {
    required_points: string[];
    grading_notes: string;
  };
}

export function useAssignments(userId: string | undefined) {
  return useQuery({
    queryKey: assignmentKeys.list(userId ?? "anonymous"),
    enabled: userId !== undefined,
    staleTime: 30_000,
    retry: false,
    queryFn: ({ signal }) =>
      api.get("/assignments", assignmentListSchema, {
        query: { limit: ASSIGNMENT_PAGE_SIZE, offset: 0 },
        signal,
      }),
  });
}

async function loadDashboardMetrics(
  assignments: readonly AssignmentListItem[],
  signal: AbortSignal,
): Promise<AssignmentDashboardMetrics> {
  const settled: Array<
    | { status: "fulfilled"; value: AssignmentSummary }
    | { status: "rejected" }
  > = new Array(assignments.length);
  let cursor = 0;

  async function loadSummary(assignment: AssignmentListItem) {
    return api.get(`/assignments/${assignment.id}/summary`, assignmentSummarySchema, {
      query: { limit: 1, offset: 0 },
      signal,
    });
  }

  async function worker() {
    while (cursor < assignments.length) {
      if (signal.aborted) {
        throw signal.reason ?? new DOMException("The operation was aborted.", "AbortError");
      }
      const index = cursor;
      cursor += 1;
      const assignment = assignments[index];
      if (assignment === undefined) return;
      try {
        settled[index] = { status: "fulfilled", value: await loadSummary(assignment) };
      } catch (error) {
        if (signal.aborted) throw error;
        settled[index] = { status: "rejected" };
      }
    }
  }

  await Promise.all(
    Array.from(
      { length: Math.min(SUMMARY_CONCURRENCY, assignments.length) },
      () => worker(),
    ),
  );

  return settled.reduce<AssignmentDashboardMetrics>(
    (metrics, result) => {
      if (result?.status !== "fulfilled") return metrics;
      metrics.covered += 1;
      metrics.pendingEvaluation += result.value.pending_evaluation;
      metrics.pendingReview += result.value.pending_review;
      metrics.failed += result.value.failed;
      return metrics;
    },
    {
      visibleAssignments: assignments.length,
      pendingEvaluation: 0,
      pendingReview: 0,
      failed: 0,
      covered: 0,
      total: assignments.length,
    },
  );
}

export function useAssignmentDashboardMetrics(
  userId: string | undefined,
  assignments: readonly AssignmentListItem[] | undefined,
) {
  const ids = assignments?.map((assignment) => assignment.id) ?? [];
  return useQuery({
    queryKey: assignmentKeys.metrics(userId ?? "anonymous", ids),
    enabled: userId !== undefined && assignments !== undefined,
    staleTime: 15_000,
    retry: false,
    queryFn: ({ signal }) => loadDashboardMetrics(assignments ?? [], signal),
  });
}

function invalidateTeacherAssignments(queryClient: QueryClient, userId: string) {
  void queryClient.invalidateQueries({ queryKey: assignmentKeys.list(userId), exact: true });
  void queryClient.invalidateQueries({ queryKey: ["assignments", "metrics", userId] });
}

export function useCreateAssignment(userId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (payload: AssignmentCreatePayload) =>
      api.post("/assignments", payload, assignmentReadSchema),
    onSuccess: () => invalidateTeacherAssignments(queryClient, userId),
  });
}

type AssignmentOperation = "publish" | "close" | "reopen";

function useAssignmentTransition(userId: string, operation: AssignmentOperation) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (assignmentId: string) =>
      api.post(`/assignments/${assignmentId}/${operation}`, undefined, assignmentReadSchema),
    onSuccess: (assignment: AssignmentRead) => {
      queryClient.setQueryData<AssignmentListItem[]>(
        assignmentKeys.list(userId),
        (current) => current?.map((item) =>
          item.id === assignment.id
            ? {
                id: assignment.id,
                code: assignment.code,
                title: assignment.title,
                due_at: assignment.due_at,
                status: assignment.status,
                created_at: assignment.created_at,
                published_at: assignment.published_at,
              }
            : item,
        ),
      );
      invalidateTeacherAssignments(queryClient, userId);
    },
  });
}

export function usePublishAssignment(userId: string) {
  return useAssignmentTransition(userId, "publish");
}

export function useCloseAssignment(userId: string) {
  return useAssignmentTransition(userId, "close");
}

export function useReopenAssignment(userId: string) {
  return useAssignmentTransition(userId, "reopen");
}

export function useAssignment(userId: string | undefined, assignmentId: string | undefined) {
  return useQuery({
    queryKey: assignmentKeys.detail(userId ?? "anonymous", assignmentId ?? "invalid"),
    enabled: userId !== undefined && assignmentId !== undefined,
    retry: false,
    queryFn: ({ signal }) => api.get(`/assignments/${assignmentId}`, assignmentReadSchema, { signal }),
  });
}

function hasInFlightRows(summary: AssignmentSummary | undefined) {
  if (summary === undefined) return false;
  return summary.queued > 0 || summary.evaluating > 0;
}

export function useAssignmentSummary(
  userId: string | undefined,
  assignmentId: string | undefined,
  offset: number,
) {
  return useQuery({
    queryKey: assignmentKeys.summary(userId ?? "anonymous", assignmentId ?? "invalid", offset),
    enabled: userId !== undefined && assignmentId !== undefined,
    retry: false,
    queryFn: ({ signal }) => api.get(`/assignments/${assignmentId}/summary`, assignmentSummarySchema, {
      query: { limit: 100, offset },
      signal,
    }),
    refetchInterval: (query) => hasInFlightRows(query.state.data) ? 2_000 : false,
  });
}

async function refreshAssignmentWorkspace(
  queryClient: QueryClient,
  userId: string,
  assignmentId: string,
) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["assignments", "summary", userId, assignmentId] }),
    queryClient.invalidateQueries({ queryKey: ["assignments", "metrics", userId] }),
  ]);
}

export function useEvaluateAssignment(userId: string, assignmentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: () => api.post(`/assignments/${assignmentId}/evaluations`, undefined, evaluationBatchSchema),
    onSuccess: () => refreshAssignmentWorkspace(queryClient, userId, assignmentId),
  });
}

export function useEvaluateSubmission(userId: string, assignmentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (submissionId: string) =>
      api.post(`/submissions/${submissionId}/evaluations`, { reason: "provider_retry" }, evaluationJobSchema),
    onSuccess: () => refreshAssignmentWorkspace(queryClient, userId, assignmentId),
  });
}
