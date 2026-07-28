export const ids = {
  user: "11111111-1111-4111-8111-111111111111",
  assignment: "22222222-2222-4222-8222-222222222222",
  submission: "33333333-3333-4333-8333-333333333333",
  job: "44444444-4444-4444-8444-444444444444",
  report: "55555555-5555-4555-8555-555555555555",
  request: "66666666-6666-4666-8666-666666666666",
} as const;

export const currentUserFixture = {
  id: ids.user,
  username: "grace",
  display_name: "Grace Hopper",
  role: "student",
} as const;

export const adminUserFixture = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  username: "admin",
  display_name: "系统管理员",
  role: "admin",
} as const;

export const teacherUserFixture = {
  id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  username: "teacher1",
  display_name: "教师甲",
  role: "teacher",
} as const;

export const accountPageFixture = {
  items: [
    {
      ...adminUserFixture,
      is_active: true,
      created_at: "2026-07-20T08:00:00Z",
      created_by: null,
    },
    {
      id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      username: "student1",
      display_name: "学生甲",
      role: "student",
      is_active: true,
      created_at: "2026-07-22T08:00:00Z",
      created_by: {
        id: adminUserFixture.id,
        username: adminUserFixture.username,
        display_name: adminUserFixture.display_name,
      },
    },
    {
      ...teacherUserFixture,
      is_active: true,
      created_at: "2026-07-21T08:00:00Z",
      created_by: {
        id: adminUserFixture.id,
        username: adminUserFixture.username,
        display_name: adminUserFixture.display_name,
      },
    },
  ],
  total: 3,
  limit: 50,
  offset: 0,
} as const;

export const studentAssignmentFixture = {
  id: ids.assignment,
  code: "HW-0001",
  title: "图的最短路径",
  due_at: "2026-07-30T12:00:00Z",
  status: "published",
  created_at: "2026-07-20T08:00:00Z",
  published_at: "2026-07-21T08:00:00Z",
  question: "说明 Dijkstra 算法、复杂度与适用条件。",
  notes: "请写出关键松弛步骤。",
} as const;

export const submissionReadFixture = {
  id: ids.submission,
  assignment_id: ids.assignment,
  student_id: ids.user,
  version: 2,
  content_type: "markdown",
  content_text: "使用优先队列进行松弛。",
  content_json: null,
  status: "submitted",
  submitted_at: "2026-07-22T08:30:00Z",
  source: "web",
} as const;

export const submissionRowFixture = {
  student_id: ids.user,
  username: "grace",
  display_name: "Grace Hopper",
  latest_submission: submissionReadFixture,
  latest_version: 2,
  submitted_at: "2026-07-22T08:30:00Z",
  evaluation_status: "succeeded",
  evaluation_error_code: null,
  report_status: "proposed",
  latest_report_id: ids.report,
  score: 82,
  grade: "B",
  evaluation_error: null,
} as const;

export const assignmentSummaryFixture = {
  assignment_id: ids.assignment,
  total_students: 2,
  submitted_students: 1,
  missing_students: 1,
  latest_submission_at: "2026-07-22T08:30:00Z",
  latest_submission_versions: { [ids.user]: 2 },
  pending_evaluation: 0,
  queued: 0,
  evaluating: 0,
  pending_review: 1,
  reviewed: 0,
  failed: 0,
  limit: 50,
  offset: 0,
  students: [submissionRowFixture],
} as const;

export const evaluationJobFixture = {
  id: ids.job,
  submission_id: ids.submission,
  requested_by: ids.user,
  reason: "initial",
  status: "succeeded",
  attempt_count: 1,
  provider: "mock",
  model: "deterministic-v1",
  error_code: null,
  error_message: null,
  queued_at: "2026-07-22T08:31:00Z",
  started_at: "2026-07-22T08:31:01Z",
  finished_at: "2026-07-22T08:31:02Z",
} as const;

export const evaluationReportFixture = {
  id: ids.report,
  submission_id: ids.submission,
  job_id: ids.job,
  source_report_id: null,
  origin: "agent",
  version: 1,
  schema_version: "1.0",
  completeness: {
    level: "partial",
    covered_points: ["松弛"],
    missing_points: ["复杂度"],
    rationale: "覆盖核心步骤但未分析复杂度。",
  },
  correctness: { judgment: "mostly_correct", rationale: "算法方向正确。" },
  major_issues: [],
  suggestions: [{ priority: "high", action: "补充复杂度。", example: "O((V+E)logV)" }],
  score: 82,
  grade: "B",
  confidence: 0.86,
  limitations: ["未运行代码。"],
  validation_status: "valid",
  review_status: "proposed",
  created_at: "2026-07-22T08:31:03Z",
} as const;

export const reviewReportFixture = {
  ...evaluationReportFixture,
  request_id: ids.request,
} as const;
