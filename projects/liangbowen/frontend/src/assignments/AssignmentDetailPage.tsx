import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router";
import { z } from "zod";

import { useAuth } from "../auth/authState";
import { AsyncState } from "../shared/ui/AsyncState";
import { ToastRegion } from "../shared/ui/ToastRegion";
import { ApiContractError, ApiError, ApiNetworkError, ApiTimeoutError } from "../shared/api/errors";
import type { AssignmentRead, AssignmentSummaryStudent, EvaluationJob } from "../shared/api/schemas";
import {
  codePointLength,
  GRADING_NOTES_LIMIT,
  RUBRIC_POINT_COUNT_LIMIT,
  RUBRIC_POINT_LIMIT,
} from "./assignmentLimits";
import { EvaluationProgress } from "./EvaluationProgress";
import { SubmissionTable } from "./SubmissionTable";
import { classifySubmission, filterLabels, type SubmissionFilter } from "./submissionStatus";
import { useAssignment, useAssignmentSummary, useEvaluateAssignment, useEvaluateSubmission } from "./api";
import "./assignments.css";

const assignmentIdSchema = z.uuid();
const PAGE_SIZE = 100;
const MAX_PAGE = 101;
const FILTERS = Object.keys(filterLabels) as SubmissionFilter[];

function safeLoadMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 404) return "没有找到这份作业。";
  if (error instanceof ApiError && error.status === 403) return "你没有权限查看这份作业。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的作业数据。";
  if (error instanceof ApiTimeoutError) return "读取作业超时，请检查网络后重试。";
  if (error instanceof ApiNetworkError) return "无法连接服务器，请检查网络后重试。";
  return "暂时无法读取作业详情，请稍后重试。";
}

function safeActionMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 403) return "你没有权限执行这项操作。";
  if (error instanceof ApiError && error.status === 404) return "目标提交已不存在，请刷新后重试。";
  if (error instanceof ApiError && error.status === 409) return "当前任务状态已变化，请刷新后重试。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的评估结果。";
  if (error instanceof ApiTimeoutError || error instanceof ApiNetworkError) return "评估请求未送达，请检查网络后重试。";
  return "暂时无法发起评估，请稍后重试。";
}

function formatDueAt(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "long", timeStyle: "short" }).format(date);
}

function parsePage(value: string | null) {
  if (value === null || !/^[1-9]\d*$/u.test(value)) return 1;
  const page = Number(value);
  return Number.isSafeInteger(page) && page <= MAX_PAGE ? page : 1;
}

function parseFilter(value: string | null): SubmissionFilter {
  return FILTERS.includes(value as SubmissionFilter) ? value as SubmissionFilter : "all";
}

interface SafeRubric {
  points: string[];
  gradingNotes: string | null;
}

function safeRubric(rubric: AssignmentRead["rubric"]): SafeRubric {
  const rawPoints = rubric.required_points;
  const points = Array.isArray(rawPoints) && rawPoints.length <= RUBRIC_POINT_COUNT_LIMIT && rawPoints.every(
    (point) => typeof point === "string" && point.trim().length > 0
      && codePointLength(point.trim()) <= RUBRIC_POINT_LIMIT,
  ) ? rawPoints.map((point) => point.trim()) : [];
  const rawNotes = rubric.grading_notes;
  const gradingNotes = typeof rawNotes === "string" && rawNotes.trim().length > 0
    && codePointLength(rawNotes.trim()) <= GRADING_NOTES_LIMIT
    ? rawNotes.trim()
    : null;
  return { points, gradingNotes };
}

function terminalRetryMessage(job: EvaluationJob) {
  if (job.status === "failed") return "已有重试任务处于失败状态，未重新加入队列。";
  if (job.status === "cancelled") return "已有重试任务已取消，未重新加入队列。";
  return "该提交已有完成的重试评估，未重复加入队列。";
}

interface WorkspaceProps {
  assignmentId: string;
  userId: string;
  page: number;
  filter: SubmissionFilter;
  navigateState(page: number, filter: SubmissionFilter): void;
}

function AssignmentWorkspace({ assignmentId, userId, page, filter, navigateState }: WorkspaceProps) {
  const offset = (page - 1) * PAGE_SIZE;
  const [banner, setBanner] = useState<{ message: string; error: boolean } | null>(null);
  const [retryingIds, setRetryingIds] = useState<ReadonlySet<string>>(new Set());
  const bulkLock = useRef(false);
  const retryLocks = useRef(new Set<string>());
  const assignment = useAssignment(userId, assignmentId);
  const summary = useAssignmentSummary(userId, assignmentId, offset);
  const bulkEvaluation = useEvaluateAssignment(userId, assignmentId);
  const rowEvaluation = useEvaluateSubmission(userId, assignmentId);
  const loadError = assignment.error ?? summary.error;

  async function evaluateAll() {
    if (bulkLock.current) return;
    bulkLock.current = true;
    setBanner(null);
    try {
      const result = await bulkEvaluation.mutateAsync();
      const message = result.queued > 0
        ? `已加入 ${result.queued} 个评估任务${result.skipped > 0 ? `；另有 ${result.skipped} 份已跳过` : ""}。`
        : `未新增评估任务${result.skipped > 0 ? `；${result.skipped} 份已跳过` : ""}。`;
      setBanner({ message, error: false });
    } catch (error) {
      setBanner({ message: safeActionMessage(error), error: true });
    } finally {
      bulkLock.current = false;
    }
  }

  async function retryRow(row: AssignmentSummaryStudent) {
    const submissionId = row.latest_submission?.id;
    if (submissionId === undefined || retryLocks.current.has(submissionId)) return;
    retryLocks.current.add(submissionId);
    setRetryingIds((current) => new Set(current).add(submissionId));
    setBanner(null);
    try {
      const job = await rowEvaluation.mutateAsync(submissionId);
      if (job.status === "queued" || job.status === "running") {
        setBanner({ message: `${row.display_name || row.username}已重新加入评估队列。`, error: false });
      } else {
        setBanner({ message: terminalRetryMessage(job), error: true });
      }
    } catch (error) {
      setBanner({ message: safeActionMessage(error), error: true });
    } finally {
      retryLocks.current.delete(submissionId);
      setRetryingIds((current) => {
        const next = new Set(current);
        next.delete(submissionId);
        return next;
      });
    }
  }

  if (loadError !== null) {
    return <AsyncState headingLevel={2} className="detail-state detail-state--error" kind="error" title={safeLoadMessage(loadError)} description="页面没有展示不完整或未经验证的数据，请重试读取。" action={<button className="button button--secondary" type="button" onClick={() => { void assignment.refetch(); void summary.refetch(); }}>重新读取</button>} />;
  }
  if (assignment.isPending || summary.isPending) {
    return <AsyncState headingLevel={2} className="detail-state" kind="loading" title="正在读取作业汇总…" description="正在核对作业信息、学生提交与评估状态。" />;
  }
  if (assignment.data === undefined || summary.data === undefined) return null;

  const rubric = safeRubric(assignment.data.rubric);
  const counts = summary.data.students.reduce<Record<SubmissionFilter, number>>((result, row) => {
    result.all += 1;
    const category = classifySubmission(row);
    if (category !== "missing") result[category] += 1;
    return result;
  }, { all: 0, pending: 0, running: 0, review: 0, reviewed: 0, failed: 0 });
  const rowCount = summary.data.students.length;
  const rangeStart = rowCount === 0 ? 0 : summary.data.offset + 1;
  const rangeEnd = rowCount === 0 ? 0 : summary.data.offset + rowCount;
  const hasPrevious = summary.data.offset > 0;
  const hasNext = summary.data.offset + summary.data.limit < summary.data.total_students;

  return (
    <section className="assignment-detail" aria-labelledby="assignment-detail-title">
      <header className="assignment-detail__header">
        <Link className="assignment-detail__back" to="/teacher">返回作业台</Link>
        <div className="assignment-detail__title"><p>{assignment.data.code} · {assignment.data.status.toUpperCase()}</p><h2 id="assignment-detail-title">{assignment.data.title}</h2><p>截止 {formatDueAt(assignment.data.due_at)}</p></div>
        <div className="assignment-detail__submission"><strong>{summary.data.submitted_students} / {summary.data.total_students} 已提交</strong><span>{summary.data.missing_students} 人尚未提交</span></div>
        <button className="button assignment-detail__bulk" type="button" disabled={bulkEvaluation.isPending} onClick={() => void evaluateAll()}>{bulkEvaluation.isPending ? "正在加入队列…" : "评估全部最新提交"}</button>
      </header>
      <ToastRegion className="dashboard-banner" tone={banner?.error ? "error" : "success"} message={banner?.message ?? null} />
      <section className="assignment-brief" aria-label="作业内容与评分规则">
        <article><p>ASSIGNMENT PROMPT</p><h3>题目内容</h3><div className="assignment-brief__question">{assignment.data.question}</div>{assignment.data.notes.trim() !== "" ? <p className="assignment-brief__notes">说明：{assignment.data.notes}</p> : null}</article>
        <article><p>GRADING RUBRIC</p><h3>评分要点</h3>{rubric.points.length > 0 ? <ol>{rubric.points.map((point, index) => <li key={`${index}:${point}`}>{point}</li>)}</ol> : <span>未提供结构化评分要点</span>}<h4>教师评分说明</h4><span>{rubric.gradingNotes ?? "未提供教师评分说明"}</span></article>
      </section>
      <EvaluationProgress summary={summary.data} />
      <section className="submission-register" aria-labelledby="submission-register-title">
        <div className="submission-register__heading"><div><p>SUBMISSION LEDGER</p><h3 id="submission-register-title">学生提交</h3></div><p>当前显示 {rangeStart}–{rangeEnd} / {summary.data.total_students} 名学生</p></div>
        <div className="submission-filters" aria-label="筛选提交状态">
          {FILTERS.map((key) => <button type="button" key={key} className={filter === key ? "is-active" : ""} aria-pressed={filter === key} onClick={() => navigateState(page, key)}>{filterLabels[key]} {counts[key]}</button>)}
        </div>
        <p className="submission-register__scope">筛选仅作用于当前页</p>
        {rowCount === 0 ? <div className="detail-empty"><strong>{summary.data.total_students === 0 ? "还没有学生记录" : "这一页没有学生记录"}</strong><span>{summary.data.total_students === 0 ? "学生账户加入课程后会出现在这里。" : "学生总数已变化，请返回上一页查看。"}</span></div> : <SubmissionTable rows={summary.data.students} filter={filter} retryingIds={retryingIds} onRetry={(row) => void retryRow(row)} />}
        <nav className="submission-pagination" aria-label="学生分页"><button className="button button--secondary" type="button" disabled={!hasPrevious} onClick={() => navigateState(Math.max(1, page - 1), "all")}>上一页</button><span>第 {page} 页</span><button className="button button--secondary" type="button" disabled={!hasNext} onClick={() => navigateState(page + 1, "all")}>下一页</button></nav>
      </section>
    </section>
  );
}

export function AssignmentDetailPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const validId = assignmentIdSchema.safeParse(id).success ? id : undefined;
  const workspaceScope = user !== null && validId !== undefined ? `${user.id}:${validId}` : null;
  const locationState = location.state as { assignmentWorkspaceScope?: unknown } | null;
  const previousScope = typeof locationState?.assignmentWorkspaceScope === "string"
    ? locationState.assignmentWorkspaceScope
    : null;
  const scopeChanged = workspaceScope !== null && previousScope !== null && previousScope !== workspaceScope;
  const page = scopeChanged ? 1 : parsePage(searchParams.get("page"));
  const filter = scopeChanged ? "all" : parseFilter(searchParams.get("filter"));
  const canonicalSearch = `page=${page}&filter=${filter}`;

  useEffect(() => {
    if (workspaceScope === null) return;
    if (searchParams.toString() === canonicalSearch && previousScope === workspaceScope) return;
    setSearchParams(
      { page: String(page), filter },
      {
        replace: true,
        state: { ...locationState, assignmentWorkspaceScope: workspaceScope },
      },
    );
  }, [canonicalSearch, filter, locationState, page, previousScope, searchParams, setSearchParams, workspaceScope]);

  if (validId === undefined) {
    return <AsyncState headingLevel={2} className="detail-state" kind="error" title="作业地址无效" description="请返回作业台重新打开作业。" action={<Link className="button button--secondary" to="/teacher">返回作业台</Link>} />;
  }
  if (user === null) return null;

  return (
    <AssignmentWorkspace
      key={`${user.id}:${validId}`}
      assignmentId={validId}
      userId={user.id}
      page={page}
      filter={filter}
      navigateState={(nextPage, nextFilter) => setSearchParams({ page: String(nextPage), filter: nextFilter })}
    />
  );
}
