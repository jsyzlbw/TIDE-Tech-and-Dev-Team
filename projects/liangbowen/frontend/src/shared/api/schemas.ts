import { z } from "zod";

const uuidSchema = z.uuid();
const dateTimeSchema = z.iso.datetime({ offset: true });
const nullableDateTimeSchema = dateTimeSchema.nullable();

export const roleSchema = z.enum(["teacher", "student", "admin"]);
export const assignmentStatusSchema = z.enum(["draft", "published", "closed", "archived"]);
export const gradeSchema = z.enum(["A", "B", "C", "D"]);
export const submissionContentTypeSchema = z.enum(["text", "markdown", "code", "structured"]);
export const submissionStatusSchema = z.enum(["submitted", "withdrawn"]);
export const submissionSourceSchema = z.enum(["web", "mattermost"]);
export const jobReasonSchema = z.enum(["initial", "manual_retry", "provider_retry"]);
export const jobStatusSchema = z.enum(["queued", "running", "succeeded", "failed", "cancelled"]);
export const reportOriginSchema = z.enum(["agent", "teacher"]);
export const validationStatusSchema = z.enum(["valid", "repaired"]);
export const reviewStatusSchema = z.enum(["proposed", "confirmed", "modified", "superseded"]);

export const currentUserSchema = z
  .object({
    id: uuidSchema,
    username: z.string(),
    display_name: z.string(),
    role: roleSchema,
  })
  .strict();

export const accountCreatorSchema = z
  .object({
    id: uuidSchema,
    username: z.string(),
    display_name: z.string(),
  })
  .strict();

export const userAccountSchema = z
  .object({
    id: uuidSchema,
    username: z.string(),
    display_name: z.string(),
    role: roleSchema,
    is_active: z.boolean(),
    created_at: dateTimeSchema,
    created_by: accountCreatorSchema.nullable(),
  })
  .strict();

export const userAccountPageSchema = z
  .object({
    items: z.array(userAccountSchema),
    total: z.int().nonnegative(),
    limit: z.int().min(1).max(100),
    offset: z.int().min(0).max(10_000),
  })
  .strict();

export const tokenResponseSchema = z
  .object({ access_token: z.string().min(1), token_type: z.literal("bearer") })
  .strict();

export const assignmentListItemSchema = z
  .object({
    id: uuidSchema,
    code: z.string(),
    title: z.string(),
    due_at: dateTimeSchema,
    status: assignmentStatusSchema,
    created_at: dateTimeSchema,
    published_at: nullableDateTimeSchema,
  })
  .strict();

export const assignmentStudentSchema = assignmentListItemSchema
  .extend({ question: z.string(), notes: z.string() })
  .strict();

/** Student route contract: intentionally excludes rubric, ownership, and channel metadata. */
export const studentAssignmentSchema = assignmentStudentSchema;

export const assignmentReadSchema = z
  .object({
    id: uuidSchema,
    code: z.string(),
    title: z.string(),
    question: z.string(),
    notes: z.string(),
    rubric: z.record(z.string(), z.unknown()),
    due_at: dateTimeSchema,
    status: assignmentStatusSchema,
    mattermost_channel_id: z.string().nullable(),
    created_by: uuidSchema,
    created_at: dateTimeSchema,
    published_at: nullableDateTimeSchema,
  })
  .strict();

export const submissionReadSchema = z
  .object({
    id: uuidSchema,
    assignment_id: uuidSchema,
    student_id: uuidSchema,
    version: z.int().positive(),
    content_type: submissionContentTypeSchema,
    content_text: z.string(),
    content_json: z.json().nullable(),
    status: submissionStatusSchema,
    submitted_at: dateTimeSchema,
    source: submissionSourceSchema,
  })
  .strict();

export const assignmentSummaryStudentSchema = z
  .object({
    student_id: uuidSchema,
    username: z.string(),
    display_name: z.string(),
    latest_submission: submissionReadSchema.nullable(),
    latest_version: z.int().positive().nullable(),
    submitted_at: nullableDateTimeSchema,
    evaluation_status: jobStatusSchema.nullable(),
    evaluation_error_code: z.string().nullable(),
    report_status: reviewStatusSchema.nullable(),
    latest_report_id: uuidSchema.nullable(),
    score: z.number().finite().nullable(),
    grade: gradeSchema.nullable(),
    evaluation_error: z.string().nullable(),
  })
  .strict();

/** Teacher-only assignment summary row; evaluation diagnostics never enter student routes. */
export const submissionRowSchema = assignmentSummaryStudentSchema;

export const assignmentSummarySchema = z
  .object({
    assignment_id: uuidSchema,
    total_students: z.int().nonnegative(),
    submitted_students: z.int().nonnegative(),
    missing_students: z.int().nonnegative(),
    latest_submission_at: nullableDateTimeSchema,
    latest_submission_versions: z.record(uuidSchema, z.int().positive()),
    pending_evaluation: z.int().nonnegative(),
    queued: z.int().nonnegative(),
    evaluating: z.int().nonnegative(),
    pending_review: z.int().nonnegative(),
    reviewed: z.int().nonnegative(),
    failed: z.int().nonnegative(),
    limit: z.int().min(1).max(100),
    offset: z.int().min(0).max(10_000),
    students: z.array(assignmentSummaryStudentSchema),
  })
  .strict();

/** Teacher-only job diagnostics, including provider/model/error metadata. */
export const evaluationJobSchema = z
  .object({
    id: uuidSchema,
    submission_id: uuidSchema,
    requested_by: uuidSchema,
    reason: jobReasonSchema,
    status: jobStatusSchema,
    attempt_count: z.int().nonnegative(),
    provider: z.string(),
    model: z.string(),
    error_code: z.string().nullable(),
    error_message: z.string().nullable(),
    queued_at: dateTimeSchema,
    started_at: nullableDateTimeSchema,
    finished_at: nullableDateTimeSchema,
  })
  .strict();

export const evaluationBatchSchema = z
  .object({
    batch_id: uuidSchema,
    queued: z.int().nonnegative(),
    skipped: z.int().nonnegative(),
    job_ids: z.array(uuidSchema),
  })
  .strict();

export const evaluationReportSchema = z
  .object({
    id: uuidSchema,
    submission_id: uuidSchema,
    job_id: uuidSchema.nullable(),
    source_report_id: uuidSchema.nullable(),
    origin: reportOriginSchema,
    version: z.int().positive(),
    schema_version: z.string(),
    completeness: z.record(z.string(), z.unknown()),
    correctness: z.record(z.string(), z.unknown()),
    major_issues: z.array(z.unknown()),
    suggestions: z.array(z.unknown()),
    score: z.int().min(0).max(100),
    grade: gradeSchema,
    confidence: z.number().finite().min(0).max(1),
    limitations: z.array(z.unknown()),
    validation_status: validationStatusSchema,
    review_status: reviewStatusSchema,
    created_at: dateTimeSchema,
  })
  .strict();

/** Student report contract: reports are shared, jobs/provider diagnostics are not. */
export const studentEvaluationReportSchema = evaluationReportSchema;

export const reviewReportSchema = evaluationReportSchema
  .extend({ request_id: z.string().min(1).max(128) })
  .strict();

export const reviewJobSchema = evaluationJobSchema
  .extend({ source_report_id: uuidSchema, request_id: z.string().min(1).max(128) })
  .strict();

export const answerCompletenessSchema = z.object({
  level: z.enum(["complete", "partial", "incomplete"]),
  covered_points: z.array(z.string()).max(30),
  missing_points: z.array(z.string()).max(30),
  rationale: z.string(),
}).strict();

export const correctnessSchema = z.object({
  judgment: z.enum(["correct", "mostly_correct", "partially_correct", "incorrect", "unable_to_determine"]),
  rationale: z.string(),
}).strict();

export const majorIssueSchema = z.object({
  code: z.string(),
  title: z.string(),
  evidence: z.string(),
  impact: z.string(),
}).strict();

export const suggestionSchema = z.object({
  priority: z.enum(["high", "medium", "low"]),
  action: z.string(),
  example: z.string(),
}).strict();

export const workspaceStudentSchema = z.object({
  id: uuidSchema,
  username: z.string(),
  display_name: z.string(),
}).strict();

const workspaceRubricPointSchema = z.string().refine(
  (value) => value.length > 0 && value === value.trim() && [...value].length <= 500,
  "workspace rubric point must be trimmed and at most 500 Unicode code points",
);

export const workspaceRubricSchema = z.object({
  required_points: z.array(workspaceRubricPointSchema).max(20),
  grading_notes: z.string().refine(
    (value) => value === value.trim() && [...value].length <= 5_000,
    "workspace grading notes must be trimmed and at most 5000 Unicode code points",
  ),
}).strict();

export const workspaceAssignmentSchema = z.object({
  id: uuidSchema,
  code: z.string(),
  title: z.string(),
  question: z.string(),
  notes: z.string(),
  rubric: workspaceRubricSchema,
  due_at: dateTimeSchema,
  status: assignmentStatusSchema,
}).strict();

export const workspaceReportAuthorSchema = z.object({
  kind: z.enum(["agent", "teacher"]),
  display_name: z.string(),
}).strict();

export const workspaceReviewActionSchema = z.object({
  action: z.enum(["confirm", "modify", "reevaluate"]),
  teacher: workspaceStudentSchema,
  comment: z.string(),
  acted_at: dateTimeSchema,
}).strict();

export const timelineReviewActionSchema = z.object({
  action: z.enum(["confirm", "modify", "reevaluate"]),
  teacher: workspaceStudentSchema,
  acted_at: dateTimeSchema,
}).strict();

export const workspaceReportSchema = evaluationReportSchema.extend({
  completeness: answerCompletenessSchema,
  correctness: correctnessSchema,
  major_issues: z.array(majorIssueSchema).max(20),
  suggestions: z.array(suggestionSchema).max(20),
  limitations: z.array(z.string()).max(20),
  author: workspaceReportAuthorSchema,
  latest_review_action: workspaceReviewActionSchema.nullable(),
}).strict();

export const timelineReportSummarySchema = z.object({
  id: uuidSchema,
  version: z.int().positive(),
  origin: reportOriginSchema,
  review_status: reviewStatusSchema,
  score: z.int().min(0).max(100),
  grade: gradeSchema,
  author: workspaceReportAuthorSchema,
  created_at: dateTimeSchema,
  latest_review_action: timelineReviewActionSchema.nullable(),
}).strict();

export const workspaceJobSchema = z.object({
  id: uuidSchema,
  source_report_id: uuidSchema,
  reason: jobReasonSchema,
  status: jobStatusSchema,
  queued_at: dateTimeSchema,
  started_at: nullableDateTimeSchema,
  finished_at: nullableDateTimeSchema,
}).strict();

export const reportWorkspaceSchema = z.object({
  requested_report_id: uuidSchema,
  current_report_id: uuidSchema,
  assignment: workspaceAssignmentSchema,
  submission: submissionReadSchema,
  student: workspaceStudentSchema,
  selected_report: workspaceReportSchema,
  timeline: z.array(timelineReportSummarySchema).max(100),
  timeline_total: z.int().positive(),
  timeline_truncated: z.boolean(),
  reevaluation_job: workspaceJobSchema.nullable(),
  request_id: z.string().min(1).max(128),
}).strict();

export const rawReportOutputSchema = z.object({
  report_id: uuidSchema,
  available: z.boolean(),
  raw_model_output: z.string().nullable(),
  request_id: z.string().min(1).max(128),
}).strict().superRefine((value, context) => {
  if (value.available !== (value.raw_model_output !== null)) {
    context.addIssue({ code: "custom", message: "raw output availability mismatch" });
  }
  if (value.raw_model_output !== null && new TextEncoder().encode(value.raw_model_output).byteLength > 2 * 1024 * 1024) {
    context.addIssue({ code: "custom", message: "raw output exceeds byte limit" });
  }
});

export const fastApiValidationIssueSchema = z
  .object({
    type: z.string(),
    loc: z.array(z.union([z.string(), z.int()])),
    msg: z.string(),
  })
  .passthrough();

export const standardErrorSchema = z
  .object({
    code: z.string().optional(),
    message: z.string().optional(),
    detail: z.union([z.string(), z.array(fastApiValidationIssueSchema)]).optional(),
    request_id: z.string().optional(),
    details: z.unknown().optional(),
  })
  .passthrough();

export type CurrentUser = z.infer<typeof currentUserSchema>;
export type AccountCreator = z.infer<typeof accountCreatorSchema>;
export type UserAccount = z.infer<typeof userAccountSchema>;
export type UserAccountPage = z.infer<typeof userAccountPageSchema>;
export type TokenResponse = z.infer<typeof tokenResponseSchema>;
export type AssignmentListItem = z.infer<typeof assignmentListItemSchema>;
export type AssignmentStudent = z.infer<typeof assignmentStudentSchema>;
export type StudentAssignment = z.infer<typeof studentAssignmentSchema>;
export type AssignmentRead = z.infer<typeof assignmentReadSchema>;
export type SubmissionRead = z.infer<typeof submissionReadSchema>;
export type AssignmentSummaryStudent = z.infer<typeof assignmentSummaryStudentSchema>;
export type SubmissionRow = z.infer<typeof submissionRowSchema>;
export type AssignmentSummary = z.infer<typeof assignmentSummarySchema>;
export type EvaluationJob = z.infer<typeof evaluationJobSchema>;
export type EvaluationBatch = z.infer<typeof evaluationBatchSchema>;
export type EvaluationReport = z.infer<typeof evaluationReportSchema>;
export type StudentEvaluationReport = z.infer<typeof studentEvaluationReportSchema>;
export type ReviewReport = z.infer<typeof reviewReportSchema>;
export type ReviewJob = z.infer<typeof reviewJobSchema>;
export type AnswerCompleteness = z.infer<typeof answerCompletenessSchema>;
export type Correctness = z.infer<typeof correctnessSchema>;
export type MajorIssue = z.infer<typeof majorIssueSchema>;
export type Suggestion = z.infer<typeof suggestionSchema>;
export type WorkspaceReport = z.infer<typeof workspaceReportSchema>;
export type TimelineReportSummary = z.infer<typeof timelineReportSummarySchema>;
export type ReportWorkspace = z.infer<typeof reportWorkspaceSchema>;
export type RawReportOutput = z.infer<typeof rawReportOutputSchema>;
export type StandardError = z.infer<typeof standardErrorSchema>;
