import { describe, expect, it } from "vitest";

import {
  evaluationReportFixture,
  studentAssignmentFixture,
  submissionReadFixture,
} from "../../test/fixtures";
import {
  studentAssignmentSchema,
  studentEvaluationReportSchema,
  submissionReadSchema,
} from "./schemas";

const forbiddenStudentFields = [
  "rubric",
  "created_by",
  "mattermost_channel_id",
  "provider",
  "model",
  "error_code",
  "error_message",
  "raw_model_output",
] as const;

describe("student-visible DTO boundary", () => {
  it("accepts the real student assignment while rejecting teacher-only fields", () => {
    expect(studentAssignmentSchema.parse(studentAssignmentFixture)).toEqual(studentAssignmentFixture);
    for (const field of forbiddenStudentFields) {
      expect(
        studentAssignmentSchema.safeParse({ ...studentAssignmentFixture, [field]: "leak" }).success,
        field,
      ).toBe(false);
    }
  });

  it("keeps submissions and reports free of assignment and provider internals", () => {
    expect(submissionReadSchema.parse(submissionReadFixture)).toEqual(submissionReadFixture);
    expect(studentEvaluationReportSchema.parse(evaluationReportFixture)).toEqual(
      evaluationReportFixture,
    );
    for (const field of forbiddenStudentFields) {
      expect(
        submissionReadSchema.safeParse({ ...submissionReadFixture, [field]: "leak" }).success,
        `submission.${field}`,
      ).toBe(false);
      expect(
        studentEvaluationReportSchema.safeParse({
          ...evaluationReportFixture,
          [field]: "leak",
        }).success,
        `report.${field}`,
      ).toBe(false);
    }
  });
});
