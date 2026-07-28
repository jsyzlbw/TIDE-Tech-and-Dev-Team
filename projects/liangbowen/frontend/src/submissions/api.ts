import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { api } from "../shared/api/client";
import {
  assignmentListItemSchema,
  studentAssignmentSchema,
  studentEvaluationReportSchema,
  submissionReadSchema,
  type SubmissionRead,
} from "../shared/api/schemas";

export const STUDENT_ASSIGNMENT_PAGE_SIZE = 20;
export const STUDENT_SUBMISSION_PAGE_SIZE = 20;
export const STUDENT_REPORT_PAGE_SIZE = 5;
export const STUDENT_SUBMISSION_RESPONSE_BUDGET = 12 * 1024 * 1024;
export const STUDENT_REPORT_RESPONSE_BUDGET = 12 * 1024 * 1024;

const assignmentListSchema = z.array(assignmentListItemSchema).max(STUDENT_ASSIGNMENT_PAGE_SIZE + 1);
const submissionHistorySchema = z.array(submissionReadSchema).max(STUDENT_SUBMISSION_PAGE_SIZE + 1);
const studentReportListSchema = z.array(studentEvaluationReportSchema).max(STUDENT_REPORT_PAGE_SIZE + 1);

export const studentKeys = {
  root: (userId: string) => ["student", userId] as const,
  assignments: (userId: string, offset: number) =>
    [...studentKeys.root(userId), "assignments", offset, STUDENT_ASSIGNMENT_PAGE_SIZE] as const,
  assignment: (userId: string, assignmentId: string) =>
    [...studentKeys.root(userId), "assignment", assignmentId] as const,
  submissions: (userId: string, assignmentId: string, offset: number) =>
    [...studentKeys.root(userId), "submissions", assignmentId, offset, STUDENT_SUBMISSION_PAGE_SIZE] as const,
  reports: (userId: string, submissionId: string, offset: number) =>
    [...studentKeys.root(userId), "reports", submissionId, offset, STUDENT_REPORT_PAGE_SIZE] as const,
};

export function useStudentAssignments(userId: string | undefined, offset: number) {
  return useQuery({
    queryKey: studentKeys.assignments(userId ?? "anonymous", offset),
    enabled: userId !== undefined,
    queryFn: ({ signal }) => api.get("/assignments", assignmentListSchema, {
      query: { limit: STUDENT_ASSIGNMENT_PAGE_SIZE + 1, offset },
      signal,
    }),
  });
}

export function useStudentAssignment(userId: string | undefined, assignmentId: string | undefined) {
  return useQuery({
    queryKey: studentKeys.assignment(userId ?? "anonymous", assignmentId ?? "invalid"),
    enabled: userId !== undefined && assignmentId !== undefined,
    queryFn: ({ signal }) => api.get(`/assignments/${assignmentId ?? "invalid"}`, studentAssignmentSchema, { signal }),
  });
}

export function useMySubmissions(userId: string | undefined, assignmentId: string | undefined, offset: number) {
  return useQuery({
    queryKey: studentKeys.submissions(userId ?? "anonymous", assignmentId ?? "invalid", offset),
    enabled: userId !== undefined && assignmentId !== undefined,
    queryFn: ({ signal }) => api.get(
      `/assignments/${assignmentId ?? "invalid"}/submissions/me`,
      submissionHistorySchema,
      {
        query: { limit: STUDENT_SUBMISSION_PAGE_SIZE + 1, offset },
        maxResponseBytes: STUDENT_SUBMISSION_RESPONSE_BUDGET,
        signal,
      },
    ),
  });
}

export function useStudentReports(userId: string | undefined, submissionId: string | undefined, offset: number) {
  return useQuery({
    queryKey: studentKeys.reports(userId ?? "anonymous", submissionId ?? "none", offset),
    enabled: userId !== undefined && submissionId !== undefined,
    queryFn: ({ signal }) => api.get(
      `/submissions/${submissionId ?? "invalid"}/reports`,
      studentReportListSchema,
      { query: { limit: STUDENT_REPORT_PAGE_SIZE + 1, offset }, maxResponseBytes: STUDENT_REPORT_RESPONSE_BUDGET, signal },
    ),
  });
}

export interface SubmitAnswerInput {
  content_type: "text" | "markdown" | "code";
  content_text: string;
  content_json: null;
}

export function useSubmitAnswer(userId: string, assignmentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (answer: SubmitAnswerInput) =>
      api.post(`/assignments/${assignmentId}/submissions`, answer, submissionReadSchema),
    onSuccess: (submission) => {
      queryClient.setQueryData<SubmissionRead[]>(
        studentKeys.submissions(userId, assignmentId, 0),
        (current) => [submission, ...(current ?? []).filter((item) => item.id !== submission.id)]
          .slice(0, STUDENT_SUBMISSION_PAGE_SIZE + 1),
      );
      void queryClient.invalidateQueries({
        queryKey: studentKeys.assignments(userId, 0).slice(0, 3),
      });
    },
  });
}
