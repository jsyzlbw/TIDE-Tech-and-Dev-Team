import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useBeforeUnload, useBlocker, useParams, useSearchParams } from "react-router";
import { z } from "zod";

import { useAuth } from "../auth/authState";
import {
  ApiContractError,
  ApiError,
  ApiNetworkError,
  ApiTimeoutError,
} from "../shared/api/errors";
import {
  answerCompletenessSchema,
  correctnessSchema,
  majorIssueSchema,
  suggestionSchema,
  type StudentEvaluationReport,
  type SubmissionRead,
} from "../shared/api/schemas";
import { AsyncState } from "../shared/ui/AsyncState";
import { ToastRegion } from "../shared/ui/ToastRegion";
import { AnswerEditor, type AnswerMode } from "./AnswerEditor";
import {
  STUDENT_REPORT_PAGE_SIZE,
  STUDENT_SUBMISSION_PAGE_SIZE,
  useMySubmissions,
  useStudentAssignment,
  useStudentReports,
  useSubmitAnswer,
} from "./api";
import { useDeadlineState } from "./deadline";
import "./submissions.css";

const uuidSchema = z.uuid();
const INVISIBLE_CHARACTER = /[\p{C}\p{M}\p{Z}]/u;
const WHITESPACE_CHARACTER = /\s/u;

function hasVisibleCharacter(value: string) {
  return [...value].some((character) => !WHITESPACE_CHARACTER.test(character) && !INVISIBLE_CHARACTER.test(character));
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function isEditableMode(value: SubmissionRead["content_type"]): value is AnswerMode {
  return value === "text" || value === "markdown" || value === "code";
}

function submitErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 409) return "作业已关闭或截止时间已过，服务器未接收本次答案。";
  if (error instanceof ApiError && error.status === 401) return "登录状态已失效，请重新登录。";
  if (error instanceof ApiError && error.status === 422) return "答案未通过服务器校验，请检查内容后重试。";
  if (error instanceof ApiTimeoutError) return "提交请求超时，答案仍保留在编辑器中，请确认网络后重试。";
  if (error instanceof ApiNetworkError) return "暂时无法提交，答案仍保留在编辑器中，请检查网络后重试。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的提交结果，答案仍保留在编辑器中。";
  return "暂时无法提交，答案仍保留在编辑器中，请稍后重试。";
}

interface DraftConfirmationDialogProps {
  id: string;
  title: string;
  description: string;
  cancelLabel: string;
  confirmLabel: string;
  onCancel(): void;
  onConfirm(): void;
}

function DraftConfirmationDialog({
  id,
  title,
  description,
  cancelLabel,
  confirmLabel,
  onCancel,
  onConfirm,
}: DraftConfirmationDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );

  useEffect(() => {
    const background = document.querySelector<HTMLElement>(".app-frame");
    const hadInert = background?.hasAttribute("inert") ?? false;
    const previousAriaHidden = background?.getAttribute("aria-hidden");
    background?.setAttribute("inert", "");
    background?.setAttribute("aria-hidden", "true");
    queueMicrotask(() => cancelRef.current?.focus());
    return () => {
      if (background === null) return;
      if (!hadInert) background.removeAttribute("inert");
      if (previousAriaHidden == null) background.removeAttribute("aria-hidden");
      else background.setAttribute("aria-hidden", previousAriaHidden);
    };
  }, []);

  function close(action: () => void) {
    const returnFocus = returnFocusRef.current;
    action();
    queueMicrotask(() => returnFocus?.focus());
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      close(onCancel);
      return;
    }
    if (event.key !== "Tab") return;
    if (!event.shiftKey && document.activeElement === confirmRef.current) {
      event.preventDefault();
      cancelRef.current?.focus();
    } else if (event.shiftKey && document.activeElement === cancelRef.current) {
      event.preventDefault();
      confirmRef.current?.focus();
    }
  }

  return createPortal(
    <section
      className="leave-guard student-leave-guard"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-description`}
      onKeyDown={onKeyDown}
    >
      <strong id={`${id}-title`}>{title}</strong>
      <p id={`${id}-description`}>{description}</p>
      <div>
        <button ref={cancelRef} className="button" type="button" onClick={() => close(onCancel)}>{cancelLabel}</button>
        <button ref={confirmRef} className="button button--secondary" type="button" onClick={() => close(onConfirm)}>{confirmLabel}</button>
      </div>
    </section>,
    document.body,
  );
}

function SubmissionHistory({
  submissions,
  selectedId,
  onSelect,
  offset,
  onOffsetChange,
}: {
  submissions: SubmissionRead[];
  selectedId: string | undefined;
  onSelect(id: string): void;
  offset: number;
  onOffsetChange(offset: number): void;
}) {
  const hasNext = submissions.length > STUDENT_SUBMISSION_PAGE_SIZE;
  const sorted = [...submissions.slice(0, STUDENT_SUBMISSION_PAGE_SIZE)].sort((left, right) => right.version - left.version);
  const selected = sorted.find((submission) => submission.id === selectedId) ?? sorted[0];
  return (
    <section className="submission-history" aria-labelledby="submission-history-title">
      <div className="student-section-heading">
        <div><p className="student-kicker">SUBMISSION ARCHIVE</p><h3 id="submission-history-title">提交记录</h3></div>
        <span>第 {offset + 1}–{offset + sorted.length} 条 · 每页 {STUDENT_SUBMISSION_PAGE_SIZE} 条</span>
      </div>
      {sorted.length === 0 ? <p>尚未提交过答案；第一版将从当前编辑器创建。</p> : (
        <>
          <label>
            <span>查看提交版本</span>
            <select name="submission_version" autoComplete="off" value={selected?.id} onChange={(event) => onSelect(event.target.value)}>
              {sorted.map((submission, index) => (
                <option key={submission.id} value={submission.id}>
                  第 {submission.version} 版{offset === 0 && index === 0 ? " · 最新" : ""}{submission.status === "withdrawn" ? " · 已撤回" : ""}
                </option>
              ))}
            </select>
          </label>
          {selected !== undefined ? (
            <article className="submission-evidence">
              <div><strong>第 {selected.version} 版</strong><span>{selected.status === "withdrawn" ? "已撤回" : "已提交"}</span></div>
              <p>{selected.content_type.toUpperCase()} · {formatDate(selected.submitted_at)} · {selected.source === "web" ? "网页提交" : "Mattermost 提交"}</p>
              <pre>{selected.content_text}</pre>
            </article>
          ) : null}
          <nav className="student-pagination" aria-label="提交记录分页">
            <button className="button button--secondary" type="button" disabled={offset === 0} onClick={() => onOffsetChange(Math.max(0, offset - STUDENT_SUBMISSION_PAGE_SIZE))}>上一页</button>
            <span>从第 {offset + 1} 条开始</span>
            <button className="button button--secondary" type="button" disabled={!hasNext} onClick={() => onOffsetChange(offset + STUDENT_SUBMISSION_PAGE_SIZE)}>下一页</button>
          </nav>
        </>
      )}
    </section>
  );
}

function PublicReport({ report }: { report: StudentEvaluationReport }) {
  const completeness = answerCompletenessSchema.safeParse(report.completeness);
  const correctness = correctnessSchema.safeParse(report.correctness);
  const issues = z.array(majorIssueSchema).max(20).safeParse(report.major_issues);
  const suggestions = z.array(suggestionSchema).max(20).safeParse(report.suggestions);
  const limitations = z.array(z.string()).max(20).safeParse(report.limitations);
  const valid = completeness.success && correctness.success && issues.success && suggestions.success && limitations.success;

  if (!valid) return <p role="alert">这份评估报告的内容格式异常，暂时无法安全展示，请联系教师重新生成。</p>;
  return (
    <article className="student-report">
      <section>
        <h4>答案完整性</h4>
        <p>{completeness.data.rationale}</p>
        <div className="report-list-pair">
          <div><strong>已覆盖</strong>{completeness.data.covered_points.length === 0 ? <p>暂无记录</p> : <ul>{completeness.data.covered_points.map((point) => <li key={point}>{point}</li>)}</ul>}</div>
          <div><strong>待补充</strong>{completeness.data.missing_points.length === 0 ? <p>暂无记录</p> : <ul>{completeness.data.missing_points.map((point) => <li key={point}>{point}</li>)}</ul>}</div>
        </div>
      </section>
      <section><h4>正确性判断</h4><p>{correctness.data.rationale}</p></section>
      <section>
        <h4>主要问题</h4>
        {issues.data.length === 0 ? <p>报告未指出主要问题。</p> : issues.data.map((issue) => <div className="student-finding" key={`${issue.code}:${issue.title}`}><strong>{issue.title}</strong><p>{issue.evidence}</p><p>{issue.impact}</p></div>)}
      </section>
      <section>
        <h4>修改建议</h4>
        {suggestions.data.length === 0 ? <p>报告暂未提供修改建议。</p> : suggestions.data.map((suggestion, index) => <div className="student-finding" key={`${suggestion.priority}:${index}`}><strong>{suggestion.action}</strong><p>{suggestion.example}</p></div>)}
      </section>
      <section>
        <h4>评分与说明</h4>
        <div className="student-report__score"><strong>{report.score}</strong><span>{report.grade}</span></div>
        <dl className="student-report__meta">
          <div><dt>报告版本</dt><dd>第 {report.version} 版</dd></div>
          <div><dt>审核状态</dt><dd>{report.review_status}</dd></div>
          <div><dt>生成时间</dt><dd>{formatDate(report.created_at)}</dd></div>
          <div><dt>置信度</dt><dd>{Math.round(report.confidence * 100)}%</dd></div>
        </dl>
        <strong>能力边界</strong>
        {limitations.data.length === 0 ? <p>报告未声明额外限制。</p> : <ul>{limitations.data.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>}
      </section>
      <p className="student-report__disclaimer">AI 评估仅供参考，最终结果以教师审核为准。</p>
    </article>
  );
}

function ReportArchive({
  userId,
  submissionId,
  selectedReportId,
  onSelectReport,
}: {
  userId: string;
  submissionId: string | undefined;
  selectedReportId: string | undefined;
  onSelectReport(reportId: string): void;
}) {
  const [page, setPage] = useState<{ submissionId: string | undefined; offset: number }>({ submissionId, offset: 0 });
  const offset = page.submissionId === submissionId ? page.offset : 0;
  const reports = useStudentReports(userId, submissionId, offset);
  const hasNext = (reports.data?.length ?? 0) > STUDENT_REPORT_PAGE_SIZE;
  const sorted = [...(reports.data?.slice(0, STUDENT_REPORT_PAGE_SIZE) ?? [])].sort((left, right) => right.version - left.version);
  const selected = sorted.find((report) => report.id === selectedReportId) ?? sorted[0];

  return (
    <section className="report-archive" aria-labelledby="report-archive-title">
      <div className="student-section-heading"><div><p className="student-kicker">ASSESSMENT REGISTER</p><h3 id="report-archive-title">评估报告</h3></div></div>
      {submissionId === undefined ? <p>提交答案后，评估报告会显示在这里。</p> : reports.error !== null ? (
        <div role="alert"><p>暂时无法读取这份提交的评估报告。</p><button className="button button--secondary" type="button" onClick={() => void reports.refetch()}>重试读取报告</button></div>
      ) : reports.isPending ? <p role="status" aria-live="polite">正在读取评估报告…</p> : selected === undefined ? <p>这份提交暂时没有评估报告。</p> : (
        <>
          <label className="report-version-select"><span>报告版本</span><select name="student_report_version" autoComplete="off" value={selected.id} onChange={(event) => onSelectReport(event.target.value)}>{sorted.map((report, index) => <option key={report.id} value={report.id}>第 {report.version} 版{offset === 0 && index === 0 ? " · 最新" : ""}</option>)}</select></label>
          <PublicReport report={selected} />
          <nav className="student-pagination" aria-label="报告分页">
            <button className="button button--secondary" type="button" disabled={offset === 0} onClick={() => setPage({ submissionId, offset: Math.max(0, offset - STUDENT_REPORT_PAGE_SIZE) })}>上一页</button>
            <span>第 {Math.floor(offset / STUDENT_REPORT_PAGE_SIZE) + 1} 页</span>
            <button className="button button--secondary" type="button" disabled={!hasNext} onClick={() => setPage({ submissionId, offset: offset + STUDENT_REPORT_PAGE_SIZE })}>下一页</button>
          </nav>
        </>
      )}
    </section>
  );
}

interface StudentAssignmentWorkspaceProps {
  assignmentId: string;
  userId: string;
  displayName: string;
  username: string;
  logout(): void;
}

function StudentAssignmentWorkspace({ assignmentId, userId, displayName, username, logout }: StudentAssignmentWorkspaceProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const assignment = useStudentAssignment(userId, assignmentId);
  const [submissionOffset, setSubmissionOffset] = useState(0);
  const submissions = useMySubmissions(userId, assignmentId, submissionOffset);
  const submitAnswer = useSubmitAnswer(userId, assignmentId);
  const [mode, setMode] = useState<AnswerMode>("text");
  const [text, setText] = useState("");
  const [baselineMode, setBaselineMode] = useState<AnswerMode>("text");
  const [baselineText, setBaselineText] = useState("");
  const [banner, setBanner] = useState<{ message: string; error: boolean }>();
  const [discardDialogOpen, setDiscardDialogOpen] = useState(false);
  const initializedRef = useRef<string | undefined>(undefined);
  const inFlightRef = useRef(false);
  const dirty = mode !== baselineMode || text !== baselineText;
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    dirty && currentLocation.pathname !== nextLocation.pathname,
  );
  const deadline = useDeadlineState(assignment.data?.due_at ?? "9999-12-31T23:59:59Z");

  useEffect(() => {
    const scope = `${userId}:${assignmentId}`;
    if (submissionOffset !== 0 || !submissions.isSuccess || initializedRef.current === scope) return;
    const latest = [...submissions.data].sort((left, right) => right.version - left.version)[0];
    const initialMode = latest !== undefined && isEditableMode(latest.content_type) ? latest.content_type : "text";
    const initialText = latest?.content_text ?? "";
    setMode(initialMode);
    setText(initialText);
    setBaselineMode(initialMode);
    setBaselineText(initialText);
    initializedRef.current = scope;
  }, [assignmentId, submissionOffset, submissions.data, submissions.isSuccess, userId]);

  useBeforeUnload((event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  if (assignment.error !== null || submissions.error !== null) return <AsyncState headingLevel={2} className="student-state student-state--error" kind="error" title="暂时无法读取作业" description="作业详情或提交记录没有完整加载，请检查网络后重试。" action={<button className="button button--secondary" type="button" onClick={() => { void assignment.refetch(); void submissions.refetch(); }}>重试读取作业</button>} />;
  if (assignment.isPending || submissions.isPending || assignment.data === undefined) return <AsyncState headingLevel={2} className="student-state" kind="loading" title="正在读取作业…" description="正在整理题目、提交记录和评估报告。" />;

  const assignmentData = assignment.data;
  const submittable = assignmentData.status === "published" && !deadline.expired;

  function discardDraft() {
    setMode(baselineMode);
    setText(baselineText);
    setBanner(undefined);
  }

  async function submit() {
    if (inFlightRef.current) return;
    setBanner(undefined);
    if (!hasVisibleCharacter(text)) {
      setBanner({ message: "答案不能为空或只包含空白字符。", error: true });
      return;
    }
    if ([...text].length > 50_000) {
      setBanner({ message: "答案不能超过 50000 个字符。", error: true });
      return;
    }
    if (assignmentData.status !== "published" || Date.now() >= deadline.dueTimestamp) {
      deadline.recheck();
      setBanner({ message: "作业已关闭或截止时间已过，无法继续提交。", error: true });
      return;
    }
    inFlightRef.current = true;
    try {
      const saved = await submitAnswer.mutateAsync({ content_type: mode, content_text: text, content_json: null });
      setBaselineMode(mode);
      setBaselineText(text);
      setSubmissionOffset(0);
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        next.set("submission", saved.id);
        next.delete("report");
        return next;
      });
      setBanner({ message: `答案已提交为第 ${saved.version} 版。`, error: false });
    } catch (error) {
      setBanner({ message: submitErrorMessage(error), error: true });
      if (error instanceof ApiError && error.status === 409) void assignment.refetch();
    } finally {
      inFlightRef.current = false;
    }
  }

  const visibleSubmissions = submissions.data.slice(0, STUDENT_SUBMISSION_PAGE_SIZE);
  const requestedSubmissionId = searchParams.get("submission") ?? undefined;
  const activeSubmissionId = visibleSubmissions.some((item) => item.id === requestedSubmissionId)
    ? requestedSubmissionId
    : visibleSubmissions[0]?.id;
  const requestedReportId = searchParams.get("report") ?? undefined;

  return (
    <section className="student-assignment-page" aria-labelledby="student-assignment-title">
      <nav className="student-breadcrumb" aria-label="面包屑"><Link to="/student">我的作业</Link><span aria-hidden="true">/</span><span>{assignment.data.code}</span></nav>
      <header className="student-assignment-page__header">
        <div>
          <p className="student-kicker">{assignment.data.code} / ASSIGNMENT BRIEF</p>
          <h2 id="student-assignment-title">{assignment.data.title}</h2>
          <p>截止 <time dateTime={assignment.data.due_at}>{formatDate(assignment.data.due_at)}</time> · {submittable ? "可提交" : "不可提交"}</p>
        </div>
        <aside aria-label="当前学生身份"><span>{displayName}</span><span>@{username}</span><button className="button button--secondary" type="button" onClick={logout}>退出登录</button></aside>
      </header>
      <section className="assignment-brief" aria-labelledby="assignment-question-title">
        <div><p className="student-kicker">QUESTION</p><h3 id="assignment-question-title">题目要求</h3></div>
        <p>{assignment.data.question}</p>
        {assignment.data.notes !== "" ? <aside><strong>补充说明</strong><p>{assignment.data.notes}</p></aside> : null}
      </section>

      <ToastRegion className="student-banner" tone={banner?.error ? "error" : "success"} message={banner?.message ?? null} />
      <div className="student-workspace-grid">
        <AnswerEditor mode={mode} text={text} dirty={dirty} submittable={submittable} submitting={submitAnswer.isPending} onModeChange={setMode} onTextChange={setText} onDiscard={() => setDiscardDialogOpen(true)} onSubmit={() => void submit()} />
        <SubmissionHistory
          submissions={submissions.data}
          selectedId={activeSubmissionId}
          onSelect={(submissionId) => setSearchParams((current) => {
            const next = new URLSearchParams(current);
            next.set("submission", submissionId);
            next.delete("report");
            return next;
          })}
          offset={submissionOffset}
          onOffsetChange={(nextOffset) => {
            setSubmissionOffset(nextOffset);
            setSearchParams((current) => {
              const next = new URLSearchParams(current);
              next.delete("submission");
              next.delete("report");
              return next;
            });
          }}
        />
      </div>
      <ReportArchive
        userId={userId}
        submissionId={activeSubmissionId}
        selectedReportId={requestedReportId}
        onSelectReport={(reportId) => setSearchParams((current) => {
          const next = new URLSearchParams(current);
          if (activeSubmissionId !== undefined) next.set("submission", activeSubmissionId);
          next.set("report", reportId);
          return next;
        })}
      />
      {discardDialogOpen ? (
        <DraftConfirmationDialog
          id="student-clear-draft"
          title="确认清除当前草稿"
          description="清除后，编辑器会恢复到最近一次提交的内容，此操作无法撤销。"
          cancelLabel="继续编辑"
          confirmLabel="确认清除"
          onCancel={() => setDiscardDialogOpen(false)}
          onConfirm={() => {
            setDiscardDialogOpen(false);
            discardDraft();
          }}
        />
      ) : blocker.state === "blocked" ? (
        <DraftConfirmationDialog
          id="student-leave-draft"
          title="尚有未提交的答案"
          description="离开后，本次草稿修改将丢失。"
          cancelLabel="留在此页"
          confirmLabel="放弃答案并离开"
          onCancel={() => blocker.reset()}
          onConfirm={() => blocker.proceed()}
        />
      ) : null}
    </section>
  );
}

export function StudentAssignmentPage() {
  const { id: rawAssignmentId } = useParams();
  const parsedAssignmentId = uuidSchema.safeParse(rawAssignmentId);
  const { user, logout } = useAuth();
  if (!parsedAssignmentId.success) {
    return <AsyncState headingLevel={2} className="student-state student-state--error" kind="error" title="作业地址无效" description="请返回作业列表重新打开。" action={<Link className="button button--secondary" to="/student">返回我的作业</Link>} />;
  }
  if (user === null) return <AsyncState headingLevel={2} className="student-state" kind="loading" title="正在确认学生身份…" description="核验完成后会自动进入作业。" />;
  const assignmentId = parsedAssignmentId.data;
  return (
    <StudentAssignmentWorkspace
      key={`${user.id}:${assignmentId}`}
      assignmentId={assignmentId}
      userId={user.id}
      displayName={user.display_name}
      username={user.username}
      logout={logout}
    />
  );
}
