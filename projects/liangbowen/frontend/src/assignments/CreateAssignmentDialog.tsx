import {
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useBlocker } from "react-router";

import {
  ApiContractError,
  ApiError,
  ApiNetworkError,
  ApiTimeoutError,
} from "../shared/api/errors";
import {
  codePointLength,
  GRADING_NOTES_LIMIT,
  RUBRIC_POINT_COUNT_LIMIT,
  RUBRIC_POINT_LIMIT,
  utf8ByteLength,
} from "./assignmentLimits";
import { useCreateAssignment } from "./api";

const TITLE_LIMIT = 200;
const QUESTION_LIMIT = 50_000;
const NOTES_LIMIT = 10_000;
const TITLE_BYTE_LIMIT = 512;
const QUESTION_BYTE_LIMIT = 64 * 1024;

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not(:disabled)",
  "input:not(:disabled):not([type='hidden'])",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

type FieldName = "title" | "question" | "notes" | "dueAt" | "rubric" | "gradingNotes";
type FieldErrors = Partial<Record<FieldName, string>>;

function hasVisibleText(value: string) {
  return [...value].some(
    (character) => !/\s/u.test(character) && !/\p{C}/u.test(character),
  );
}

function normalizedPoint(value: string) {
  return value.trim().normalize("NFKC").toLocaleLowerCase("zh-CN");
}

function localDateTimeRoundTrips(value: string, parsed: Date) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/u.exec(value);
  if (match === null || Number.isNaN(parsed.getTime())) return false;
  const [, year, month, day, hour, minute] = match;
  return (
    parsed.getFullYear() === Number(year) &&
    parsed.getMonth() + 1 === Number(month) &&
    parsed.getDate() === Number(day) &&
    parsed.getHours() === Number(hour) &&
    parsed.getMinutes() === Number(minute)
  );
}

function focusableElements(dialog: HTMLElement) {
  return [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)]
    .filter(
      (element) =>
        !element.hidden &&
        element.getAttribute("aria-hidden") !== "true" &&
        element.closest("[hidden]") === null &&
        element.style.display !== "none" &&
        element.style.visibility !== "hidden",
    )
    .sort((left, right) =>
      left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
    );
}

function safeCreateMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 401) return "登录状态已失效，请重新登录。";
  if (error instanceof ApiError && error.status === 409) return "作业状态已变化，请刷新后重试。";
  if (error instanceof ApiError && error.status === 413) return "作业内容过大，请缩短后重试。";
  if (error instanceof ApiError && error.status === 422) {
    return "作业内容未通过服务器校验，请检查后重试。";
  }
  if (error instanceof ApiTimeoutError) return "保存请求超时，请检查网络后重试。";
  if (error instanceof ApiNetworkError) return "无法连接服务器，请检查网络后重试。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的作业数据，请稍后重试。";
  if (error instanceof ApiError && error.status >= 500) {
    return "服务器暂时无法保存作业草稿，请稍后重试。";
  }
  return "暂时无法保存作业草稿，请稍后重试。";
}

interface CreateAssignmentDialogProps {
  userId: string;
  returnFocusRef: RefObject<HTMLButtonElement | null>;
  onClose(): void;
  onSaved(): void;
}

export function CreateAssignmentDialog({
  userId,
  returnFocusRef,
  onClose,
  onSaved,
}: CreateAssignmentDialogProps) {
  const createAssignment = useCreateAssignment(userId);
  const submittingRef = useRef(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const submitRef = useRef<HTMLButtonElement>(null);
  const discardDialogRef = useRef<HTMLElement>(null);
  const continueEditingRef = useRef<HTMLButtonElement>(null);
  const discardRef = useRef<HTMLButtonElement>(null);
  const discardReturnFocusRef = useRef<HTMLElement | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const notesRef = useRef<HTMLTextAreaElement>(null);
  const dueAtRef = useRef<HTMLInputElement>(null);
  const rubricInputRef = useRef<HTMLInputElement>(null);
  const gradingNotesRef = useRef<HTMLTextAreaElement>(null);
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [notes, setNotes] = useState("");
  const [dueAt, setDueAt] = useState("");
  const [rubricDraft, setRubricDraft] = useState("");
  const [rubricPoints, setRubricPoints] = useState<string[]>([]);
  const [gradingNotes, setGradingNotes] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [rubricEntryError, setRubricEntryError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const dirty =
    title !== "" ||
    question !== "" ||
    notes !== "" ||
    dueAt !== "" ||
    rubricDraft !== "" ||
    rubricPoints.length > 0 ||
    gradingNotes !== "";
  const blocker = useBlocker(dirty);
  const discardPromptOpen = confirmDiscard || blocker.state === "blocked";

  useEffect(() => {
    queueMicrotask(() => titleRef.current?.focus());
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  useEffect(() => {
    if (!dirty) return;
    const preventUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventUnload);
    return () => window.removeEventListener("beforeunload", preventUnload);
  }, [dirty]);

  useEffect(() => {
    if (!discardPromptOpen) return;
    if (discardReturnFocusRef.current === null && document.activeElement instanceof HTMLElement) {
      discardReturnFocusRef.current = document.activeElement;
    }
    queueMicrotask(() => continueEditingRef.current?.focus());
  }, [discardPromptOpen]);

  function restoreTrigger() {
    queueMicrotask(() => returnFocusRef.current?.focus());
  }

  function finishClose() {
    onClose();
    restoreTrigger();
  }

  function requestClose() {
    if (createAssignment.isPending) return;
    if (dirty) {
      discardReturnFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : closeRef.current;
      setConfirmDiscard(true);
      return;
    }
    finishClose();
  }

  function continueEditing() {
    const returnFocus = discardReturnFocusRef.current;
    discardReturnFocusRef.current = null;
    setConfirmDiscard(false);
    if (blocker.state === "blocked") blocker.reset();
    queueMicrotask(() => returnFocus?.focus());
  }

  function discardChanges() {
    discardReturnFocusRef.current = null;
    setConfirmDiscard(false);
    if (blocker.state === "blocked") {
      blocker.proceed();
      return;
    }
    finishClose();
  }

  function onDialogKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      if (discardPromptOpen) {
        continueEditing();
        return;
      }
      requestClose();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = discardPromptOpen ? discardDialogRef.current : dialogRef.current;
    if (dialog === null) return;
    const focusable = focusableElements(dialog);
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    }
  }

  function addRubricPoint() {
    const value = rubricDraft.trim();
    if (!hasVisibleText(value)) {
      setRubricEntryError("评分要点必须包含可见文字。");
      return;
    }
    if (codePointLength(value) > RUBRIC_POINT_LIMIT) {
      setRubricEntryError(`每项评分要点不能超过 ${RUBRIC_POINT_LIMIT} 个字符。`);
      return;
    }
    if (rubricPoints.length >= RUBRIC_POINT_COUNT_LIMIT) {
      setRubricEntryError(`评分要点最多 ${RUBRIC_POINT_COUNT_LIMIT} 项。`);
      return;
    }
    if (rubricPoints.some((point) => normalizedPoint(point) === normalizedPoint(value))) {
      setRubricEntryError("评分要点不能重复。");
      return;
    }
    setRubricPoints((points) => [...points, value]);
    setRubricDraft("");
    setRubricEntryError(null);
    setFieldErrors((errors) => ({ ...errors, rubric: undefined }));
  }

  function validate() {
    const errors: FieldErrors = {};
    const normalizedTitle = title.trim();
    const normalizedQuestion = question.trim();
    const normalizedGradingNotes = gradingNotes.trim();
    if (!hasVisibleText(normalizedTitle)) errors.title = "作业标题必须包含可见文字。";
    else if (codePointLength(normalizedTitle) > TITLE_LIMIT) errors.title = "作业标题不能超过 200 个字符。";
    else if (utf8ByteLength(normalizedTitle) > TITLE_BYTE_LIMIT) errors.title = "作业标题不能超过 512 UTF-8 字节。";
    if (!hasVisibleText(normalizedQuestion)) errors.question = "题目内容必须包含可见文字。";
    else if (codePointLength(normalizedQuestion) > QUESTION_LIMIT) errors.question = "题目内容不能超过 50000 个字符。";
    else if (utf8ByteLength(normalizedQuestion) > QUESTION_BYTE_LIMIT) errors.question = "题目内容不能超过 65536 UTF-8 字节。";
    if (codePointLength(notes) > NOTES_LIMIT) errors.notes = "学生说明不能超过 10000 个字符。";
    const dueDate = new Date(dueAt);
    if (dueAt !== "" && !Number.isNaN(dueDate.getTime()) && !localDateTimeRoundTrips(dueAt, dueDate)) {
      errors.dueAt = "该本地时间因夏令时切换而不存在，请选择其他时间。";
    } else if (dueAt === "" || Number.isNaN(dueDate.getTime()) || dueDate.getTime() <= Date.now()) {
      errors.dueAt = "截止时间必须晚于当前时间。";
    }
    if (rubricPoints.length < 1) errors.rubric = "至少添加 1 项评分要点。";
    if (codePointLength(normalizedGradingNotes) > GRADING_NOTES_LIMIT) {
      errors.gradingNotes = "教师评分注意事项不能超过 5000 个字符。";
    }
    return { errors, normalizedTitle, normalizedQuestion, normalizedGradingNotes, dueDate };
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submittingRef.current) return;
    const result = validate();
    setFieldErrors(result.errors);
    setServerError(null);
    const firstInvalid = (Object.keys(result.errors) as FieldName[])[0];
    if (firstInvalid !== undefined) {
      const refs: Record<FieldName, RefObject<HTMLElement | null>> = {
        title: titleRef,
        question: questionRef,
        notes: notesRef,
        dueAt: dueAtRef,
        rubric: rubricInputRef,
        gradingNotes: gradingNotesRef,
      };
      queueMicrotask(() => refs[firstInvalid].current?.focus());
      return;
    }
    submittingRef.current = true;
    try {
      await createAssignment.mutateAsync({
        title: result.normalizedTitle,
        question: result.normalizedQuestion,
        notes,
        due_at: result.dueDate.toISOString(),
        rubric: {
          required_points: rubricPoints,
          grading_notes: result.normalizedGradingNotes,
        },
      });
      onSaved();
      restoreTrigger();
    } catch (error) {
      setServerError(safeCreateMessage(error));
    } finally {
      submittingRef.current = false;
    }
  }

  const errorSummary = Object.entries(fieldErrors).filter((entry) => entry[1] !== undefined);
  const rubricDescribedBy = [
    "assignment-rubric-count",
    rubricEntryError === null ? null : "assignment-rubric-entry-error",
    fieldErrors.rubric === undefined ? null : "assignment-rubric-error",
  ].filter((id): id is string => id !== null).join(" ");

  return createPortal(
    <div className="assignment-dialog-backdrop">
      <div
        ref={dialogRef}
        className="assignment-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-assignment-title"
        aria-describedby="create-assignment-description"
        onKeyDownCapture={onDialogKeyDown}
      >
        {discardPromptOpen ? (
          <section
            ref={discardDialogRef}
            className="assignment-dialog__discard"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="discard-assignment-title"
            aria-describedby="discard-assignment-description"
          >
            <strong id="discard-assignment-title">放弃未保存内容？</strong>
            <span id="discard-assignment-description">有尚未保存的内容。关闭将放弃本次填写，且无法恢复。</span>
            <div>
              <button ref={continueEditingRef} className="button button--secondary" type="button" onClick={continueEditing}>继续编辑</button>
              <button ref={discardRef} className="button" type="button" onClick={discardChanges}>放弃未保存内容</button>
            </div>
          </section>
        ) : null}

        <div
          className="assignment-dialog__content"
          inert={discardPromptOpen ? true : undefined}
          aria-hidden={discardPromptOpen ? "true" : undefined}
        >
        <header className="assignment-dialog__header">
          <div>
            <p>NEW ASSIGNMENT / DRAFT</p>
            <h2 id="create-assignment-title">新建作业草稿</h2>
            <p id="create-assignment-description">
              先保存草稿，确认内容后再从作业登记发布。所有字段均不会预填演示数据。
            </p>
          </div>
          <button ref={closeRef} type="button" className="assignment-dialog__close" aria-label="关闭新作业表单" onClick={requestClose}>×</button>
        </header>

        {errorSummary.length > 0 ? (
          <section className="assignment-dialog__errors" role="alert" aria-label="请修正以下内容">
            <strong>请修正以下内容</strong>
            <ul>{errorSummary.map(([name, message]) => <li key={name}>{message}</li>)}</ul>
          </section>
        ) : null}
        {serverError !== null ? <div className="assignment-dialog__errors" role="alert">{serverError}</div> : null}

        <form className="assignment-form" noValidate autoComplete="off" inert={discardPromptOpen ? true : undefined} aria-hidden={discardPromptOpen ? "true" : undefined} aria-busy={createAssignment.isPending} onSubmit={(event) => void onSubmit(event)}>
          <div className="assignment-form__field assignment-form__field--title">
            <label htmlFor="assignment-title">作业标题</label>
            <input ref={titleRef} className="field" id="assignment-title" name="title" autoComplete="off" value={title} aria-invalid={fieldErrors.title !== undefined} aria-describedby={fieldErrors.title === undefined ? "assignment-title-count" : "assignment-title-error assignment-title-count"} onChange={(event) => setTitle(event.target.value)} />
            {fieldErrors.title !== undefined ? <span id="assignment-title-error" className="assignment-form__error">{fieldErrors.title}</span> : null}
            <span id="assignment-title-count" className="assignment-form__count">{codePointLength(title)} / {TITLE_LIMIT}</span>
          </div>
          <div className="assignment-form__field assignment-form__field--question">
            <label htmlFor="assignment-question">题目内容</label>
            <textarea ref={questionRef} className="field" id="assignment-question" name="question" autoComplete="off" rows={7} value={question} aria-invalid={fieldErrors.question !== undefined} aria-describedby={fieldErrors.question === undefined ? "assignment-question-count" : "assignment-question-error assignment-question-count"} onChange={(event) => setQuestion(event.target.value)} />
            {fieldErrors.question !== undefined ? <span id="assignment-question-error" className="assignment-form__error">{fieldErrors.question}</span> : null}
            <span id="assignment-question-count" className="assignment-form__count">{codePointLength(question)} / {QUESTION_LIMIT}</span>
          </div>
          <div className="assignment-form__field">
            <label htmlFor="assignment-notes">学生说明</label>
            <textarea ref={notesRef} className="field" id="assignment-notes" name="notes" autoComplete="off" rows={3} value={notes} aria-invalid={fieldErrors.notes !== undefined} aria-describedby={fieldErrors.notes === undefined ? "assignment-notes-count" : "assignment-notes-error assignment-notes-count"} onChange={(event) => setNotes(event.target.value)} />
            {fieldErrors.notes !== undefined ? <span id="assignment-notes-error" className="assignment-form__error">{fieldErrors.notes}</span> : null}
            <span id="assignment-notes-count" className="assignment-form__count">{codePointLength(notes)} / {NOTES_LIMIT}</span>
          </div>
          <div className="assignment-form__field">
            <label htmlFor="assignment-due-at">提交截止时间</label>
            <input ref={dueAtRef} className="field" id="assignment-due-at" name="due_at" autoComplete="off" type="datetime-local" value={dueAt} aria-invalid={fieldErrors.dueAt !== undefined} aria-describedby={fieldErrors.dueAt === undefined ? undefined : "assignment-due-at-error"} onChange={(event) => setDueAt(event.target.value)} />
            {fieldErrors.dueAt !== undefined ? <span id="assignment-due-at-error" className="assignment-form__error">{fieldErrors.dueAt}</span> : null}
          </div>
          <fieldset className="assignment-form__rubric" aria-describedby={fieldErrors.rubric === undefined ? undefined : "assignment-rubric-error"}>
            <legend>评分要点</legend>
            <div className="assignment-form__rubric-entry">
              <label htmlFor="assignment-rubric-point">新增评分要点</label>
              <input ref={rubricInputRef} className="field" id="assignment-rubric-point" name="rubric_point" autoComplete="off" value={rubricDraft} aria-invalid={fieldErrors.rubric !== undefined || rubricEntryError !== null} aria-describedby={rubricDescribedBy} onChange={(event) => { setRubricDraft(event.target.value); setRubricEntryError(null); }} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addRubricPoint(); } }} />
              <button className="button button--secondary" type="button" onClick={addRubricPoint}>添加要点</button>
            </div>
            <span id="assignment-rubric-count" className="assignment-form__count">当前项 {codePointLength(rubricDraft)} / {RUBRIC_POINT_LIMIT} · 已添加 {rubricPoints.length} / {RUBRIC_POINT_COUNT_LIMIT}</span>
            {rubricEntryError !== null ? <span id="assignment-rubric-entry-error" className="assignment-form__error" role="alert">{rubricEntryError}</span> : null}
            {fieldErrors.rubric !== undefined ? <span id="assignment-rubric-error" className="assignment-form__error">{fieldErrors.rubric}</span> : null}
            <ul className="rubric-points">
              {rubricPoints.map((point, index) => (
                <li key={`${point}-${index}`}><span>{point}</span><button type="button" aria-label={`删除评分要点：${point}`} onClick={() => setRubricPoints((points) => points.filter((_, pointIndex) => pointIndex !== index))}>×</button></li>
              ))}
            </ul>
          </fieldset>
          <div className="assignment-form__field">
            <label htmlFor="assignment-grading-notes">教师评分注意事项</label>
            <textarea ref={gradingNotesRef} className="field" id="assignment-grading-notes" name="grading_notes" autoComplete="off" rows={3} value={gradingNotes} aria-invalid={fieldErrors.gradingNotes !== undefined} aria-describedby={fieldErrors.gradingNotes === undefined ? "assignment-grading-notes-count" : "assignment-grading-notes-error assignment-grading-notes-count"} onChange={(event) => setGradingNotes(event.target.value)} />
            {fieldErrors.gradingNotes !== undefined ? <span id="assignment-grading-notes-error" className="assignment-form__error">{fieldErrors.gradingNotes}</span> : null}
            <span id="assignment-grading-notes-count" className="assignment-form__count">{codePointLength(gradingNotes.trim())} / {GRADING_NOTES_LIMIT}</span>
          </div>
          <footer className="assignment-form__actions">
            <span>提交后保存为草稿，不会立即向学生发布。</span>
            <button ref={submitRef} className="button" type="submit" disabled={createAssignment.isPending}>{createAssignment.isPending ? "正在保存…" : "保存作业草稿"}</button>
          </footer>
        </form>
        </div>
      </div>
    </div>,
    document.body,
  );
}
