// @vitest-environment node

import { execFileSync } from "node:child_process";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import { z } from "zod";

import {
  assignmentSummarySchema,
  assignmentSummaryStudentSchema,
  currentUserSchema,
  evaluationBatchSchema,
  evaluationJobSchema,
  evaluationReportSchema,
  studentAssignmentSchema,
  submissionReadSchema,
} from "./schemas";

interface OpenApiComponent {
  properties: Record<string, unknown>;
  required: string[];
}

interface OpenApiSchema {
  $ref?: string;
  items?: OpenApiSchema;
}

interface OpenApiOperation {
  requestBody?: { content?: { "application/json"?: { schema?: OpenApiSchema } } };
  responses: Record<string, { content?: { "application/json"?: { schema?: OpenApiSchema } } }>;
}

interface OpenApiSnapshot {
  components: Record<string, OpenApiComponent>;
  paths: Record<string, Record<string, OpenApiOperation>>;
}

const REPOSITORY_ROOT = fileURLToPath(new URL("../../../../", import.meta.url));

let cachedSnapshot: OpenApiSnapshot | undefined;

function liveOpenApiSnapshot(useCache = true): OpenApiSnapshot {
  if (useCache && cachedSnapshot !== undefined) return cachedSnapshot;
  const script = [
    "import json",
    "from app.main import create_app",
    "wanted = ['CurrentUser', 'AssignmentStudentRead', 'AssignmentSummary', 'AssignmentSummaryStudent', 'SubmissionCreate', 'SubmissionRead', 'EvaluationBatchRead', 'EvaluationJobRead', 'EvaluationReportRead']",
    "openapi = create_app().openapi()",
    "schemas = openapi['components']['schemas']",
    "path_names = ['/api/v1/assignments', '/api/v1/assignments/{assignment_id}', '/api/v1/assignments/{assignment_id}/summary', '/api/v1/assignments/{assignment_id}/evaluations', '/api/v1/assignments/{assignment_id}/submissions', '/api/v1/assignments/{assignment_id}/submissions/me', '/api/v1/submissions/{submission_id}/evaluations', '/api/v1/submissions/{submission_id}/reports']",
    "print(json.dumps({'components': {name: schemas[name] for name in wanted}, 'paths': {name: openapi['paths'][name] for name in path_names}}, sort_keys=True))",
  ].join("; ");
  const snapshot = JSON.parse(
    execFileSync("uv", ["run", "python", "-c", script], {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: "backend" },
    }),
  ) as OpenApiSnapshot;
  if (useCache) cachedSnapshot = snapshot;
  return snapshot;
}

function liveOpenApiComponents(useCache = true) {
  return liveOpenApiSnapshot(useCache).components;
}

function zodKeys(schema: z.ZodObject) {
  return [...schema.keyof().options].sort();
}

describe("live FastAPI DTO contract", () => {
  // Audit decisions encoded below: summary students are nested (not flat submission rows),
  // reports are student-visible and provider-free, while job diagnostics remain teacher-only.
  it("keeps frontend core DTO fields aligned with generated OpenAPI", () => {
    const components = liveOpenApiComponents();
    const pairs: Array<[z.ZodObject, string]> = [
      [currentUserSchema, "CurrentUser"],
      [studentAssignmentSchema, "AssignmentStudentRead"],
      [assignmentSummarySchema, "AssignmentSummary"],
      [assignmentSummaryStudentSchema, "AssignmentSummaryStudent"],
      [submissionReadSchema, "SubmissionRead"],
      [evaluationBatchSchema, "EvaluationBatchRead"],
      [evaluationJobSchema, "EvaluationJobRead"],
      [evaluationReportSchema, "EvaluationReportRead"],
    ];
    for (const [frontend, backendName] of pairs) {
      const backend = components[backendName];
      expect(zodKeys(frontend), backendName).toEqual(Object.keys(backend.properties).sort());
      expect(zodKeys(frontend), `${backendName} required`).toEqual([...backend.required].sort());
    }
  }, 15_000);

  it("keeps assignment detail and evaluation endpoints aligned with the live router", () => {
    const paths = liveOpenApiSnapshot().paths;

    expect(paths["/api/v1/assignments/{assignment_id}"]?.get).toBeDefined();
    expect(paths["/api/v1/assignments/{assignment_id}/summary"]?.get).toBeDefined();
    expect(paths["/api/v1/assignments/{assignment_id}/evaluations"]?.post?.responses["202"]?.content?.["application/json"]?.schema?.$ref).toBe("#/components/schemas/EvaluationBatchRead");
    expect(paths["/api/v1/submissions/{submission_id}/evaluations"]?.post?.responses["202"]?.content?.["application/json"]?.schema?.$ref).toBe("#/components/schemas/EvaluationJobRead");
  }, 15_000);

  it("keeps the student submission and report workflow aligned with live request bodies", () => {
    const snapshot = liveOpenApiSnapshot();
    const paths = snapshot.paths;
    const submissionCreate = snapshot.components.SubmissionCreate;

    expect(Object.keys(submissionCreate.properties).sort()).toEqual([
      "content_json",
      "content_text",
      "content_type",
    ]);
    expect(submissionCreate.properties).not.toHaveProperty("source");
    expect(paths["/api/v1/assignments/{assignment_id}/submissions"]?.post?.requestBody?.content?.["application/json"]?.schema?.$ref).toBe("#/components/schemas/SubmissionCreate");
    expect(paths["/api/v1/assignments/{assignment_id}/submissions/me"]?.get?.responses["200"]?.content?.["application/json"]?.schema?.items?.$ref).toBe("#/components/schemas/SubmissionRead");
    expect(paths["/api/v1/submissions/{submission_id}/reports"]?.get?.responses["200"]?.content?.["application/json"]?.schema?.items?.$ref).toBe("#/components/schemas/EvaluationReportRead");
  }, 15_000);

  it("does not depend on the Vitest process working directory", () => {
    const originalCwd = process.cwd();
    process.chdir(dirname(fileURLToPath(import.meta.url)));
    try {
      expect(Object.keys(liveOpenApiComponents(false))).toContain("CurrentUser");
    } finally {
      process.chdir(originalCwd);
    }
  }, 15_000);
});
