import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useBeforeUnload, useBlocker, useNavigate, useParams } from "react-router";
import { z } from "zod";

import { useAuth } from "../auth/authState";
import { AsyncState } from "../shared/ui/AsyncState";
import { ToastRegion } from "../shared/ui/ToastRegion";
import { ApiContractError, ApiError, ApiNetworkError, ApiTimeoutError } from "../shared/api/errors";
import type { ReportWorkspace, WorkspaceReport } from "../shared/api/schemas";
import {
  downloadReportJson,
  useConfirmReport,
  useModifyReport,
  useRawReportOutput,
  useReevaluateReport,
  useReportWorkspace,
} from "./api";
import { ReportEditForm } from "./ReportEditForm";
import { ReportPanel } from "./ReportPanel";
import { ReportTimeline } from "./ReportTimeline";
import "./reports.css";

const reportIdSchema = z.uuid();

function loadMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 404) return "没有找到这份评估报告。";
  if (error instanceof ApiError && error.status === 403) return "你没有权限查看这份评估报告。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的报告数据。";
  if (error instanceof ApiTimeoutError) return "读取报告超时，请检查网络后重试。";
  if (error instanceof ApiNetworkError) return "无法连接服务器，请检查网络后重试。";
  return "暂时无法读取评估报告，请稍后重试。";
}

function actionMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 409) return "该报告已被更新，请刷新后重试";
  if (error instanceof ApiError && error.status === 403) return "你没有权限执行这项审核操作。";
  if (error instanceof ApiError && error.status === 404) return "报告已不存在，请返回作业详情。";
  if (error instanceof ApiError && error.status === 422) return "提交的审核字段不符合要求，请检查长度、必填项和格式。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的审核结果。";
  if (error instanceof ApiTimeoutError || error instanceof ApiNetworkError) return "审核请求未送达，请检查网络后重试。";
  return "审核操作暂时失败，请稍后重试。";
}

function commentError(value: string) {
  if ([...value].length > 4_000 || new TextEncoder().encode(value).byteLength > 8_000) {
    return "审核评语不能超过 4000 个字符或 8000 个 UTF-8 字节。";
  }
  return null;
}

function rubricParts(rubric: ReportWorkspace["assignment"]["rubric"]) {
  const required = rubric.required_points;
  const notes = rubric.grading_notes;
  return {
    points: Array.isArray(required) && required.length <= 100 && required.every((item) => typeof item === "string") ? required : [],
    notes: typeof notes === "string" && notes.trim() !== "" ? notes : null,
  };
}

function Answer({ workspace }: { workspace: ReportWorkspace }) {
  const submission = workspace.submission;
  return (
    <article className="review-column answer-column" aria-labelledby="answer-title">
      <header className="review-column__header"><p>STUDENT ANSWER / v{submission.version}</p><h3 id="answer-title">{workspace.student.display_name}</h3><span>@{workspace.student.username} · {submission.content_type}</span></header>
      {submission.content_type === "structured" ? (
        <pre className="answer-sheet">{JSON.stringify(submission.content_json, null, 2)}</pre>
      ) : (
        <pre className={`answer-sheet answer-sheet--${submission.content_type}`}>{submission.content_text}</pre>
      )}
      {submission.content_type === "markdown" && <p className="safe-render-note">Markdown 以安全纯文本显示，不执行内嵌 HTML。</p>}
    </article>
  );
}

function Assignment({ workspace }: { workspace: ReportWorkspace }) {
  const rubric = rubricParts(workspace.assignment.rubric);
  return (
    <article className="review-column assignment-column" aria-labelledby="report-assignment-title">
      <header className="review-column__header"><p>{workspace.assignment.code} · ASSIGNMENT</p><h3 id="report-assignment-title">{workspace.assignment.title}</h3><Link to={`/teacher/assignments/${workspace.assignment.id}`}>返回作业汇总</Link></header>
      <section><h4>题目内容</h4><div className="prompt-copy">{workspace.assignment.question}</div>{workspace.assignment.notes !== "" && <p className="assignment-note">说明：{workspace.assignment.notes}</p>}</section>
      <section><h4>评分要点</h4>{rubric.points.length === 0 ? <p>未提供结构化评分要点。</p> : <ol>{rubric.points.map((point, index) => <li key={`${index}:${point}`}>{point}</li>)}</ol>}<h5>教师评分说明</h5><p>{rubric.notes ?? "未提供教师评分说明。"}</p></section>
    </article>
  );
}

function RawOutput({ userId, report }: { userId: string; report: WorkspaceReport }) {
  const [expanded, setExpanded] = useState(false);
  const raw = useRawReportOutput(userId, report.id, expanded);
  return (
    <details className="raw-output" onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>原始模型输出</summary>
      {!expanded ? null : raw.isPending ? <p role="status" aria-live="polite">正在读取原始输出…</p> : raw.error !== null ? <p role="alert">原始输出读取失败，请稍后重新展开此区域重试。</p> : raw.data?.available ? <pre>{raw.data.raw_model_output}</pre> : <p>这份报告没有保存原始模型输出。</p>}
    </details>
  );
}

function LeaveGuard({ onContinue, onDiscard }: { onContinue(): void; onDiscard(): void }) {
  const continueRef = useRef<HTMLButtonElement>(null);
  const discardRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );

  useEffect(() => {
    const background = document.querySelector<HTMLElement>(".app-frame");
    const hadInert = background?.hasAttribute("inert") ?? false;
    const previousAriaHidden = background?.getAttribute("aria-hidden");
    background?.setAttribute("inert", "");
    background?.setAttribute("aria-hidden", "true");
    queueMicrotask(() => continueRef.current?.focus());
    return () => {
      if (background !== null) {
        if (!hadInert) background.removeAttribute("inert");
        if (previousAriaHidden == null) background.removeAttribute("aria-hidden");
        else background.setAttribute("aria-hidden", previousAriaHidden);
      }
    };
  }, []);

  function continueEditing() {
    const returnFocus = returnFocusRef.current;
    onContinue();
    queueMicrotask(() => returnFocus?.focus());
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      continueEditing();
      return;
    }
    if (event.key !== "Tab") return;
    if (!event.shiftKey && document.activeElement === discardRef.current) {
      event.preventDefault();
      continueRef.current?.focus();
    } else if (event.shiftKey && document.activeElement === continueRef.current) {
      event.preventDefault();
      discardRef.current?.focus();
    }
  }

  return createPortal(
    <section
      className="leave-guard"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="leave-guard-title"
      aria-describedby="leave-guard-description"
      onKeyDown={onKeyDown}
    >
      <strong id="leave-guard-title">尚有未保存的修改</strong>
      <p id="leave-guard-description">离开后，本次表单修改将丢失。</p>
      <div>
        <button ref={continueRef} className="button" type="button" onClick={continueEditing}>继续编辑</button>
        <button ref={discardRef} className="button button--secondary" type="button" onClick={onDiscard}>放弃修改并离开</button>
      </div>
    </section>,
    document.body,
  );
}

function ReviewWorkspace({ userId, workspace }: { userId: string; workspace: ReportWorkspace }) {
  const navigate = useNavigate();
  const report = workspace.selected_report;
  const current = report.id === workspace.current_report_id && report.review_status !== "superseded";
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [comment, setComment] = useState("");
  const [dirty, setDirty] = useState(false);
  const [banner, setBanner] = useState<{ text: string; error: boolean } | null>(null);
  const confirm = useConfirmReport(userId, report.id, workspace.assignment.id);
  const modify = useModifyReport(userId, report.id, workspace.assignment.id);
  const reevaluate = useReevaluateReport(userId, report.id, workspace.assignment.id);
  const serverPending = workspace.reevaluation_job?.status === "queued" || workspace.reevaluation_job?.status === "running";
  const unsaved = dirty || comment !== "";
  const mountedRef = useRef(true);
  const scopeRef = useRef(`${userId}:${report.id}`);
  const allowedNavigationRef = useRef<string | null>(null);
  const blocker = useBlocker(({ nextLocation }) =>
    unsaved && nextLocation.pathname !== allowedNavigationRef.current,
  );
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  useBeforeUnload((event) => {
    if (!unsaved) return;
    event.preventDefault();
    event.returnValue = "";
  });

  async function confirmReport() {
    setBanner(null);
    const validation = commentError(comment);
    if (validation !== null) {
      setBanner({ text: validation, error: true });
      return;
    }
    try {
      await confirm.mutateAsync(comment);
      setComment("");
      setBanner({ text: "评估结果已确认。", error: false });
    } catch (error) {
      setBanner({ text: actionMessage(error), error: true });
    }
  }

  async function modifyReport(payload: Parameters<typeof modify.mutateAsync>[0]) {
    const requestScope = scopeRef.current;
    setBanner(null);
    try {
      const result = await modify.mutateAsync(payload);
      if (!mountedRef.current || scopeRef.current !== requestScope) return;
      const destination = `/teacher/reports/${result.id}`;
      allowedNavigationRef.current = destination;
      navigate(destination, { replace: true });
    } catch (error) {
      if (!mountedRef.current || scopeRef.current !== requestScope) return;
      setBanner({ text: actionMessage(error), error: true });
    }
  }

  async function reevaluateReport() {
    if (serverPending || reevaluate.isPending) return;
    setBanner(null);
    const validation = commentError(comment);
    if (validation !== null) {
      setBanner({ text: validation, error: true });
      return;
    }
    try {
      const job = await reevaluate.mutateAsync(comment);
      setComment("");
      const terminal = job.status === "succeeded"
        ? "该报告已有完成的重评任务，未重复加入队列。"
        : job.status === "failed"
          ? "已有重评任务失败，未重复加入队列。"
          : job.status === "cancelled"
            ? "已有重评任务已取消，未重复加入队列。"
            : "重评任务已加入队列。";
      setBanner({ text: terminal, error: job.status === "failed" || job.status === "cancelled" });
    } catch (error) {
      setBanner({ text: actionMessage(error), error: true });
    }
  }

  return (
    <section className="report-review" aria-labelledby="report-review-title">
      <div className="report-review__toolbar">
        <div><p>REPORT REVIEW / v{report.version}</p><h2 id="report-review-title">评估报告审阅</h2><span>{workspace.student.display_name} · {workspace.assignment.code}</span></div>
      </div>
      <ToastRegion className="review-banner" tone={banner?.error ? "error" : "success"} message={banner?.text ?? null} />
      <div className="report-review__columns">
        <Assignment workspace={workspace} />
        <Answer workspace={workspace} />
        <article className="review-column report-column" aria-labelledby="evaluation-title">
          <header className="review-column__header"><p>AI EVALUATION / HUMAN REVIEW</p><h3 id="evaluation-title">评估与审核</h3>{!current && <span className="read-only-mark">历史版本 · 只读</span>}</header>
          {mode === "edit" ? <ReportEditForm report={report} saving={modify.isPending} onCancel={() => setMode("view")} onDirtyChange={setDirty} onSave={(payload) => void modifyReport(payload)} /> : <ReportPanel report={report} />}
          {current && mode === "view" && <section className="review-actions" aria-label="教师审核操作">
            <label>审核评语（可选）<textarea className="field" name="review_comment" autoComplete="off" value={comment} onChange={(event) => setComment(event.target.value)} maxLength={4000} /></label>
            <div><button className="button" type="button" disabled={confirm.isPending || report.review_status !== "proposed"} onClick={() => void confirmReport()}>确认评估</button><button className="button button--secondary" type="button" disabled={modify.isPending || report.review_status !== "proposed"} onClick={() => setMode("edit")}>修改评估</button><button className="button button--secondary" type="button" disabled={reevaluate.isPending || serverPending} onClick={() => void reevaluateReport()}>重新评估</button></div>
            {reevaluate.isPending && <p role="status" aria-live="polite">正在发起重评任务…</p>}
          </section>}
          {report.origin === "agent" && <RawOutput userId={userId} report={report} />}
        </article>
      </div>
      <ReportTimeline workspace={workspace} selected={report} onExport={() => downloadReportJson(report)} />
      {blocker.state === "blocked" && <LeaveGuard onContinue={() => blocker.reset()} onDiscard={() => blocker.proceed()} />}
    </section>
  );
}

export function ReportReviewPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const reportId = reportIdSchema.safeParse(id).success ? id : undefined;
  const workspace = useReportWorkspace(user?.id, reportId);

  if (reportId === undefined) return <AsyncState headingLevel={2} className="detail-state" kind="error" title="报告地址无效" description="请返回作业汇总重新打开报告。" action={<Link className="button button--secondary" to="/teacher">返回作业台</Link>} />;
  if (user === null) return null;
  if (workspace.error !== null) return <AsyncState headingLevel={2} className="detail-state detail-state--error" kind="error" title={loadMessage(workspace.error)} description="页面没有展示不完整或未经验证的数据，请重试读取。" action={<button className="button button--secondary" type="button" onClick={() => void workspace.refetch()}>重新读取</button>} />;
  if (workspace.isPending) return <AsyncState headingLevel={2} className="detail-state" kind="loading" title="正在读取评估报告…" description="正在核对作业、提交、报告版本与审核状态。" />;
  if (workspace.data === undefined) return <AsyncState headingLevel={2} className="detail-state" kind="empty" title="报告工作区为空" description="请返回作业汇总选择其他报告。" />;
  return <ReviewWorkspace key={`${user.id}:${reportId}`} userId={user.id} workspace={workspace.data} />;
}
