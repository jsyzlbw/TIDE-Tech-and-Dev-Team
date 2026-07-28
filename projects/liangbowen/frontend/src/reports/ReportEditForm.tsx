import { useEffect, useRef, useState, type FormEvent } from "react";

import type { WorkspaceReport } from "../shared/api/schemas";
import type { ReportPatchPayload } from "./api";
import { validateReportEditDraft, type KeyedItem, type ReportEditValidationIssue } from "./reportEditValidation";

type MajorIssue = WorkspaceReport["major_issues"][number];
type Suggestion = WorkspaceReport["suggestions"][number];

function same(left: unknown, right: unknown) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function gradeForScore(score: number) {
  if (score >= 90) return "A";
  if (score >= 75) return "B";
  if (score >= 60) return "C";
  return "D";
}

function errorId(fieldName: string) {
  return `report-edit-${fieldName.replace(/[^a-zA-Z0-9_-]/gu, "-")}-error`;
}

export function ReportEditForm({
  report,
  saving,
  onCancel,
  onDirtyChange,
  onSave,
}: {
  report: WorkspaceReport;
  saving: boolean;
  onCancel(): void;
  onDirtyChange(dirty: boolean): void;
  onSave(payload: ReportPatchPayload): void;
}) {
  const [completeness, setCompleteness] = useState(report.completeness);
  const [coveredPoints, setCoveredPoints] = useState(report.completeness.covered_points.join("\n"));
  const [missingPoints, setMissingPoints] = useState(report.completeness.missing_points.join("\n"));
  const [correctness, setCorrectness] = useState(report.correctness);
  const [issues, setIssues] = useState<KeyedItem<MajorIssue>[]>(() => report.major_issues.map((value, index) => ({ key: `issue-${index + 1}`, value })));
  const [suggestions, setSuggestions] = useState<KeyedItem<Suggestion>[]>(() => report.suggestions.map((value, index) => ({ key: `suggestion-${index + 1}`, value })));
  const [score, setScore] = useState(report.score);
  const [limitations, setLimitations] = useState(report.limitations.join("\n"));
  const [comment, setComment] = useState("");
  const [validationIssue, setValidationIssue] = useState<ReportEditValidationIssue | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const nextIssueKey = useRef(report.major_issues.length + 1);
  const nextSuggestionKey = useRef(report.suggestions.length + 1);
  const issueValues = issues.map((item) => item.value);
  const suggestionValues = suggestions.map((item) => item.value);
  const dirty = completeness.level !== report.completeness.level
    || coveredPoints !== report.completeness.covered_points.join("\n")
    || missingPoints !== report.completeness.missing_points.join("\n")
    || !same(correctness, report.correctness)
    || !same(issueValues, report.major_issues)
    || !same(suggestionValues, report.suggestions)
    || score !== report.score
    || limitations !== report.limitations.join("\n")
    || comment !== "";

  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);

  function fieldProps(fieldName: string) {
    const hasError = validationIssue?.fieldName === fieldName;
    return {
      "aria-invalid": hasError || undefined,
      "aria-describedby": hasError ? errorId(fieldName) : undefined,
    };
  }

  function fieldError(fieldName: string) {
    if (validationIssue?.fieldName !== fieldName) return null;
    return <span className="report-edit__field-error" id={errorId(fieldName)}>{validationIssue.message}</span>;
  }

  function clearIssue(fieldName: string) {
    if (validationIssue?.fieldName === fieldName) setValidationIssue(null);
  }

  function focusField(fieldName: string) {
    queueMicrotask(() => {
      if (fieldName === "report_edit_form") {
        formRef.current?.focus();
        return;
      }
      const control = formRef.current?.elements.namedItem(fieldName);
      if (control instanceof HTMLElement) control.focus();
    });
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const result = validateReportEditDraft({ completeness, coveredPoints, missingPoints, correctness, issues, suggestions, score, limitations, comment }, report);
    if (!result.valid) {
      setValidationIssue(result.issue);
      focusField(result.issue.fieldName);
      return;
    }
    setValidationIssue(null);
    onSave(result.payload);
  }

  return (
    <form ref={formRef} className="report-edit" name="report_edit_form" tabIndex={-1} noValidate onSubmit={submit} aria-label="修改评估报告">
      <div className="report-edit__heading"><h4>修改评估</h4><span>新版本将保留原报告，不覆盖历史。</span></div>
      {validationIssue !== null && <p className="report-edit__error" role="alert">请检查“{validationIssue.section}”部分：{validationIssue.message}</p>}

      <fieldset><legend>答案完整性</legend>
        <label>完整程度<select className="field" name="completeness_level" autoComplete="off" {...fieldProps("completeness_level")} value={completeness.level} onChange={(event) => { clearIssue("completeness_level"); setCompleteness({ ...completeness, level: event.target.value as typeof completeness.level }); }}><option value="complete">完整</option><option value="partial">部分完整</option><option value="incomplete">不完整</option></select>{fieldError("completeness_level")}</label>
        <label>已覆盖要点（每行一项）<textarea className="field" name="covered_points" autoComplete="off" {...fieldProps("covered_points")} value={coveredPoints} onChange={(event) => { clearIssue("covered_points"); setCoveredPoints(event.target.value); }} />{fieldError("covered_points")}</label>
        <label>缺失要点（每行一项）<textarea className="field" name="missing_points" autoComplete="off" {...fieldProps("missing_points")} value={missingPoints} onChange={(event) => { clearIssue("missing_points"); setMissingPoints(event.target.value); }} />{fieldError("missing_points")}</label>
        <label>完整性说明<textarea className="field" name="completeness_rationale" autoComplete="off" required {...fieldProps("completeness_rationale")} value={completeness.rationale} onChange={(event) => { clearIssue("completeness_rationale"); setCompleteness({ ...completeness, rationale: event.target.value }); }} />{fieldError("completeness_rationale")}</label>
      </fieldset>

      <fieldset><legend>正确性初步判断</legend>
        <label>判断<select className="field" name="correctness_judgment" autoComplete="off" {...fieldProps("correctness_judgment")} value={correctness.judgment} onChange={(event) => { clearIssue("correctness_judgment"); setCorrectness({ ...correctness, judgment: event.target.value as typeof correctness.judgment }); }}><option value="correct">正确</option><option value="mostly_correct">基本正确</option><option value="partially_correct">部分正确</option><option value="incorrect">错误</option><option value="unable_to_determine">暂不能判断</option></select>{fieldError("correctness_judgment")}</label>
        <label>正确性说明<textarea className="field" name="correctness_rationale" autoComplete="off" required {...fieldProps("correctness_rationale")} value={correctness.rationale} onChange={(event) => { clearIssue("correctness_rationale"); setCorrectness({ ...correctness, rationale: event.target.value }); }} />{fieldError("correctness_rationale")}</label>
      </fieldset>

      <fieldset><legend>主要问题</legend>{issues.map((item, index) => {
        const codeName = `${item.key}-code`;
        const titleName = `${item.key}-title`;
        const evidenceName = `${item.key}-evidence`;
        const impactName = `${item.key}-impact`;
        const update = (value: MajorIssue) => setIssues((current) => current.map((candidate) => candidate.key === item.key ? { ...candidate, value } : candidate));
        return <div className="report-edit__repeat" key={item.key}>
          <label>问题 {index + 1} 编码<input className="field" name={codeName} autoComplete="off" spellCheck={false} required {...fieldProps(codeName)} value={item.value.code} onChange={(event) => { clearIssue(codeName); update({ ...item.value, code: event.target.value }); }} />{fieldError(codeName)}</label>
          <label>问题 {index + 1} 标题<input className="field" name={titleName} autoComplete="off" required {...fieldProps(titleName)} value={item.value.title} onChange={(event) => { clearIssue(titleName); update({ ...item.value, title: event.target.value }); }} />{fieldError(titleName)}</label>
          <label>问题 {index + 1} 证据<textarea className="field" name={evidenceName} autoComplete="off" required {...fieldProps(evidenceName)} value={item.value.evidence} onChange={(event) => { clearIssue(evidenceName); update({ ...item.value, evidence: event.target.value }); }} />{fieldError(evidenceName)}</label>
          <label>问题 {index + 1} 影响<textarea className="field" name={impactName} autoComplete="off" required {...fieldProps(impactName)} value={item.value.impact} onChange={(event) => { clearIssue(impactName); update({ ...item.value, impact: event.target.value }); }} />{fieldError(impactName)}</label>
          <button className="button button--secondary" type="button" onClick={() => setIssues((current) => current.filter((candidate) => candidate.key !== item.key))}>移除问题 {index + 1}</button>
        </div>;
      })}<button className="button button--secondary" type="button" onClick={() => { const key = `issue-${nextIssueKey.current++}`; setIssues((current) => [...current, { key, value: { code: `ISSUE_${current.length + 1}`, title: "", evidence: "", impact: "" } }]); }}>添加主要问题</button></fieldset>

      <fieldset><legend>修改建议</legend>{suggestions.map((item, index) => {
        const priorityName = `${item.key}-priority`;
        const actionName = `${item.key}-action`;
        const exampleName = `${item.key}-example`;
        const update = (value: Suggestion) => setSuggestions((current) => current.map((candidate) => candidate.key === item.key ? { ...candidate, value } : candidate));
        return <div className="report-edit__repeat" key={item.key}>
          <label>建议 {index + 1} 优先级<select className="field" name={priorityName} autoComplete="off" {...fieldProps(priorityName)} value={item.value.priority} onChange={(event) => { clearIssue(priorityName); update({ ...item.value, priority: event.target.value as Suggestion["priority"] }); }}><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select>{fieldError(priorityName)}</label>
          <label>建议 {index + 1} 内容<textarea className="field" name={actionName} autoComplete="off" required {...fieldProps(actionName)} value={item.value.action} onChange={(event) => { clearIssue(actionName); update({ ...item.value, action: event.target.value }); }} />{fieldError(actionName)}</label>
          <label>建议 {index + 1} 示例<textarea className="field" name={exampleName} autoComplete="off" {...fieldProps(exampleName)} value={item.value.example} onChange={(event) => { clearIssue(exampleName); update({ ...item.value, example: event.target.value }); }} />{fieldError(exampleName)}</label>
          <button className="button button--secondary" type="button" onClick={() => setSuggestions((current) => current.filter((candidate) => candidate.key !== item.key))}>移除建议 {index + 1}</button>
        </div>;
      })}<button className="button button--secondary" type="button" onClick={() => { const key = `suggestion-${nextSuggestionKey.current++}`; setSuggestions((current) => [...current, { key, value: { priority: "medium", action: "", example: "" } }]); }}>添加修改建议</button></fieldset>

      <fieldset className="report-edit__score"><legend>评分与边界</legend><label>评分<input className="field" name="score" autoComplete="off" inputMode="numeric" type="number" min="0" max="100" step="1" {...fieldProps("score")} value={score} onChange={(event) => { clearIssue("score"); setScore(event.currentTarget.valueAsNumber); }} />{fieldError("score")}</label><p>对应等级 <strong>{Number.isFinite(score) && score >= 0 && score <= 100 ? gradeForScore(score) : "—"}</strong>（A ≥90，B ≥75，C ≥60，D &lt;60）</p></fieldset>
      <label>局限说明（每行一项）<textarea className="field" name="limitations" autoComplete="off" {...fieldProps("limitations")} value={limitations} onChange={(event) => { clearIssue("limitations"); setLimitations(event.target.value); }} />{fieldError("limitations")}</label>
      <label>审核评语（大幅改分必填）<textarea className="field" name="teacher_comment" autoComplete="off" {...fieldProps("teacher_comment")} value={comment} onChange={(event) => { clearIssue("teacher_comment"); setComment(event.target.value); }} maxLength={4000} />{fieldError("teacher_comment")}</label>
      <div className="report-edit__actions"><button className="button" type="submit" disabled={saving}>{saving ? "正在保存…" : "保存修改"}</button><button className="button button--secondary" type="button" onClick={onCancel}>取消</button></div>
    </form>
  );
}
