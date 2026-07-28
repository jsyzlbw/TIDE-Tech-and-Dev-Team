import type { ReportWorkspace } from "../shared/api/schemas";
import { ids } from "./fixtures";

const EMOJI = "🧠";
const NOW = "2026-07-22T09:00:02Z";

function maxText(group: number, index: number, length: number) {
  const uniqueEmoji = String.fromCodePoint(0x1f300 + group * 100 + index);
  if (new TextEncoder().encode(uniqueEmoji).byteLength !== 4) {
    throw new Error("maximum workspace marker must use four UTF-8 bytes");
  }
  return EMOJI.repeat(length - 1) + uniqueEmoji;
}

export function maximumWorkspaceFixture(): ReportWorkspace {
  const teacher = {
    id: ids.user,
    username: "u".repeat(64),
    display_name: EMOJI.repeat(128),
  };
  const fullAction = {
    action: "modify" as const,
    teacher,
    comment: EMOJI.repeat(2_000),
    acted_at: NOW,
  };
  const compactAction = {
    action: fullAction.action,
    teacher,
    acted_at: fullAction.acted_at,
  };
  const selectedReport = {
    id: ids.report,
    submission_id: ids.submission,
    job_id: null,
    source_report_id: "77777777-7777-4777-8777-777777777777",
    origin: "teacher" as const,
    version: 1,
    schema_version: "1.0",
    completeness: {
      level: "partial" as const,
      covered_points: Array.from({ length: 30 }, (_, index) => maxText(0, index, 1_000)),
      missing_points: Array.from({ length: 30 }, (_, index) => maxText(1, index, 1_000)),
      rationale: EMOJI.repeat(4_000),
    },
    correctness: { judgment: "mostly_correct" as const, rationale: EMOJI.repeat(4_000) },
    major_issues: Array.from({ length: 20 }, (_, index) => ({
      code: `I${String(index).padStart(2, "0")}${"X".repeat(61)}`,
      title: EMOJI.repeat(200),
      evidence: EMOJI.repeat(4_000),
      impact: EMOJI.repeat(2_000),
    })),
    suggestions: Array.from({ length: 20 }, () => ({
      priority: "high" as const,
      action: EMOJI.repeat(2_000),
      example: EMOJI.repeat(4_000),
    })),
    score: 82,
    grade: "B" as const,
    confidence: 0.5,
    limitations: Array.from({ length: 20 }, () => EMOJI.repeat(4_000)),
    validation_status: "valid" as const,
    review_status: "modified" as const,
    created_at: NOW,
    author: { kind: "teacher" as const, display_name: EMOJI.repeat(128) },
    latest_review_action: fullAction,
  };
  const timeline = Array.from({ length: 100 }, (_, index) => ({
    id: `${String(index + 1).padStart(8, "0")}-0000-4000-8000-000000000000`,
    version: 100 - index,
    origin: "teacher" as const,
    review_status: index === 0 ? "modified" as const : "superseded" as const,
    score: 82,
    grade: "B" as const,
    author: { kind: "teacher" as const, display_name: EMOJI.repeat(128) },
    created_at: NOW,
    latest_review_action: compactAction,
  }));

  return {
    requested_report_id: ids.report,
    current_report_id: ids.report,
    assignment: {
      id: ids.assignment,
      code: "HW-MAX",
      title: EMOJI.repeat(200),
      question: EMOJI.repeat(50_000),
      notes: EMOJI.repeat(10_000),
      rubric: {
        required_points: Array.from({ length: 20 }, (_, index) => maxText(2, index, 500)),
        grading_notes: EMOJI.repeat(5_000),
      },
      due_at: "2026-07-30T12:00:00Z",
      status: "published",
    },
    submission: {
      id: ids.submission,
      assignment_id: ids.assignment,
      student_id: ids.user,
      version: 1,
      content_type: "text",
      content_text: EMOJI.repeat(50_000),
      content_json: null,
      status: "submitted",
      submitted_at: NOW,
      source: "web",
    },
    student: teacher,
    selected_report: selectedReport,
    timeline,
    timeline_total: 100,
    timeline_truncated: false,
    reevaluation_job: null,
    request_id: ids.request,
  };
}

export function maximumWorkspaceWireFixture() {
  const payload = maximumWorkspaceFixture();
  const body = JSON.stringify(payload);
  return { payload, body, wireBytes: new TextEncoder().encode(body).byteLength };
}
