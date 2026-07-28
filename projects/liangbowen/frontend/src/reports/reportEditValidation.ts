import type { WorkspaceReport } from "../shared/api/schemas";
import type { ReportPatchPayload } from "./api";

type MajorIssue = WorkspaceReport["major_issues"][number];
type Suggestion = WorkspaceReport["suggestions"][number];

export interface KeyedItem<T> {
  key: string;
  value: T;
}

export type ReportEditSection = "score" | "completeness" | "correctness" | "issues" | "suggestions" | "limitations" | "comment" | "form";

export interface ReportEditValidationIssue {
  fieldName: string;
  section: ReportEditSection;
  message: string;
}

export interface ReportEditDraft {
  completeness: WorkspaceReport["completeness"];
  coveredPoints: string;
  missingPoints: string;
  correctness: WorkspaceReport["correctness"];
  issues: KeyedItem<MajorIssue>[];
  suggestions: KeyedItem<Suggestion>[];
  score: number;
  limitations: string;
  comment: string;
}

export type ReportEditValidationResult =
  | { valid: true; payload: ReportPatchPayload }
  | { valid: false; issue: ReportEditValidationIssue };

function uniqueLines(value: string) {
  const seen = new Set<string>();
  return value.split("\n").map((item) => item.trim()).filter((item) => {
    const key = item.normalize("NFKC").toLocaleLowerCase();
    if (item === "" || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function same(left: unknown, right: unknown) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function invalidComment(value: string) {
  return [...value].length > 4_000 || new TextEncoder().encode(value).byteLength > 8_000;
}

function invalid(fieldName: string, section: ReportEditSection, message: string): ReportEditValidationResult {
  return { valid: false, issue: { fieldName, section, message } };
}

export function validateReportEditDraft(draft: ReportEditDraft, report: WorkspaceReport): ReportEditValidationResult {
  if (!Number.isInteger(draft.score) || draft.score < 0 || draft.score > 100) {
    return invalid("score", "score", "评分必须是 0–100 的整数。");
  }

  const nextCompleteness = {
    ...draft.completeness,
    covered_points: uniqueLines(draft.coveredPoints),
    missing_points: uniqueLines(draft.missingPoints),
  };
  if (nextCompleteness.covered_points.length > 30 || nextCompleteness.covered_points.some((point) => [...point].length > 1_000)) {
    return invalid("covered_points", "completeness", "完整性字段超过允许的数量或长度。");
  }
  if (nextCompleteness.missing_points.length > 30 || nextCompleteness.missing_points.some((point) => [...point].length > 1_000)) {
    return invalid("missing_points", "completeness", "完整性字段超过允许的数量或长度。");
  }
  if (nextCompleteness.rationale.trim() === "") {
    return invalid("completeness_rationale", "completeness", "完整性说明不能为空。");
  }
  if ([...nextCompleteness.rationale].length > 4_000) {
    return invalid("completeness_rationale", "completeness", "完整性说明不能超过 4000 个字符。");
  }
  const coveredKeys = new Set(nextCompleteness.covered_points.map((point) => point.normalize("NFKC").toLocaleLowerCase()));
  if (nextCompleteness.missing_points.some((point) => coveredKeys.has(point.normalize("NFKC").toLocaleLowerCase()))) {
    return invalid("missing_points", "completeness", "已覆盖要点和缺失要点不能重复。");
  }
  if (
    (nextCompleteness.level === "complete" && nextCompleteness.missing_points.length > 0)
    || (nextCompleteness.level !== "complete" && nextCompleteness.missing_points.length === 0)
  ) {
    return invalid("missing_points", "completeness", "完整程度与缺失要点不一致。");
  }

  if (draft.correctness.rationale.trim() === "") {
    return invalid("correctness_rationale", "correctness", "正确性说明不能为空。");
  }
  if ([...draft.correctness.rationale].length > 4_000) {
    return invalid("correctness_rationale", "correctness", "正确性说明不能超过 4000 个字符。");
  }

  if (draft.issues.length > 20) {
    return invalid(draft.issues[0] === undefined ? "report_edit_form" : `${draft.issues[0].key}-code`, "issues", "主要问题不能超过 20 项。");
  }
  const issueCodes = new Set<string>();
  for (const item of draft.issues) {
    const { key, value } = item;
    if (value.code.trim() === "") return invalid(`${key}-code`, "issues", "主要问题编码不能为空。");
    if (value.title.trim() === "") return invalid(`${key}-title`, "issues", "主要问题标题不能为空。");
    if (value.evidence.trim() === "") return invalid(`${key}-evidence`, "issues", "主要问题证据不能为空。");
    if (value.impact.trim() === "") return invalid(`${key}-impact`, "issues", "主要问题影响不能为空。");
    if ([...value.title].length > 200) return invalid(`${key}-title`, "issues", "主要问题标题不能超过 200 个字符。");
    if ([...value.evidence].length > 4_000) return invalid(`${key}-evidence`, "issues", "主要问题证据不能超过 4000 个字符。");
    if ([...value.impact].length > 2_000) return invalid(`${key}-impact`, "issues", "主要问题影响不能超过 2000 个字符。");
    const normalizedCode = value.code.trim().toLocaleUpperCase();
    if (issueCodes.has(normalizedCode)) return invalid(`${key}-code`, "issues", "主要问题编码不能重复。");
    issueCodes.add(normalizedCode);
  }

  if (draft.suggestions.length > 20) {
    return invalid(draft.suggestions[0] === undefined ? "report_edit_form" : `${draft.suggestions[0].key}-action`, "suggestions", "修改建议不能超过 20 项。");
  }
  for (const item of draft.suggestions) {
    const { key, value } = item;
    if (value.action.trim() === "") return invalid(`${key}-action`, "suggestions", "修改建议内容不能为空。");
    if ([...value.action].length > 2_000) return invalid(`${key}-action`, "suggestions", "修改建议内容不能超过 2000 个字符。");
    if ([...value.example].length > 4_000) return invalid(`${key}-example`, "suggestions", "修改建议示例不能超过 4000 个字符。");
  }

  const nextLimitations = uniqueLines(draft.limitations);
  if (nextLimitations.length > 20 || nextLimitations.some((item) => [...item].length > 4_000)) {
    return invalid("limitations", "limitations", "局限说明不能超过 20 项，每项不能超过 4000 个字符。");
  }
  if (invalidComment(draft.comment)) {
    return invalid("teacher_comment", "comment", "审核评语不能超过 4000 个字符或 8000 个 UTF-8 字节。");
  }
  if (Math.abs(draft.score - report.score) >= 10 && draft.comment.trim() === "") {
    return invalid("teacher_comment", "comment", "评分变化达到 10 分，请填写审核评语。");
  }

  const issueValues = draft.issues.map((item) => item.value);
  const suggestionValues = draft.suggestions.map((item) => item.value);
  const payload: ReportPatchPayload = {};
  if (!same(nextCompleteness, report.completeness)) payload.completeness = nextCompleteness;
  if (!same(draft.correctness, report.correctness)) payload.correctness = draft.correctness;
  if (!same(issueValues, report.major_issues)) payload.major_issues = issueValues;
  if (!same(suggestionValues, report.suggestions)) payload.suggestions = suggestionValues;
  if (draft.score !== report.score) payload.score = draft.score;
  if (!same(nextLimitations, report.limitations)) payload.limitations = nextLimitations;
  if (draft.comment.trim() !== "") payload.comment = draft.comment.trim();
  if (Object.keys(payload).length === 0) {
    return invalid("report_edit_form", "form", "尚未修改任何评估字段。");
  }
  return { valid: true, payload };
}
