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

import type { UserAccount } from "../shared/api/schemas";
import { useCreateUser } from "./api";
import { safeAccountCreateMessage } from "./accountErrors";
import {
  normalizeUsername,
  validateUserDraft,
  type UserDraft,
  type UserFieldErrors,
} from "./userValidation";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button",
  "input:not([type='hidden'])",
  "select",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

const EMPTY_DRAFT: UserDraft = {
  username: "",
  displayName: "",
  password: "",
  confirmPassword: "",
};

function focusableElements(dialog: HTMLElement) {
  return [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].filter(
    (element) =>
      !element.hidden &&
      element.getAttribute("aria-hidden") !== "true" &&
      element.closest("[hidden]") === null &&
      !("disabled" in element && element.disabled === true) &&
      element.style.display !== "none" &&
      element.style.visibility !== "hidden",
  );
}

interface CreateUserDialogProps {
  actorId: string;
  actorRole: "admin" | "teacher";
  returnFocusRef: RefObject<HTMLButtonElement | null>;
  onClose(): void;
  onCreated(account: UserAccount): void;
}

export function CreateUserDialog({
  actorId,
  actorRole,
  returnFocusRef,
  onClose,
  onCreated,
}: CreateUserDialogProps) {
  const createUser = useCreateUser(actorId);
  const submittingRef = useRef(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const discardDialogRef = useRef<HTMLElement>(null);
  const usernameRef = useRef<HTMLInputElement>(null);
  const displayNameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmPasswordRef = useRef<HTMLInputElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const continueEditingRef = useRef<HTMLButtonElement>(null);
  const discardReturnFocusRef = useRef<HTMLElement | null>(null);
  const [draft, setDraft] = useState<UserDraft>(EMPTY_DRAFT);
  const [role, setRole] = useState<"teacher" | "student">("student");
  const [fieldErrors, setFieldErrors] = useState<UserFieldErrors>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [submissionLocked, setSubmissionLocked] = useState(false);
  const dirty = Object.values(draft).some((value) => value !== "") || (actorRole === "admin" && role !== "student");
  const pending = submissionLocked || createUser.isPending;
  const blocker = useBlocker(dirty || pending);
  const blockerRef = useRef(blocker);
  const discardPromptOpen = confirmDiscard || blocker.state === "blocked";

  useEffect(() => {
    blockerRef.current = blocker;
  }, [blocker]);

  useEffect(() => {
    queueMicrotask(() => usernameRef.current?.focus());
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
    if (submittingRef.current || pending) return;
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
    if (submittingRef.current || pending) return;
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
      if (discardPromptOpen) continueEditing();
      else requestClose();
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

  function updateDraft(field: keyof UserDraft, value: string) {
    const nextDraft = { ...draft, [field]: value };
    const nextValidation = validateUserDraft(nextDraft);
    const dependencies: Record<keyof UserDraft, readonly (keyof UserDraft)[]> = {
      username: ["username", "password"],
      displayName: ["displayName"],
      password: ["password", "confirmPassword"],
      confirmPassword: ["confirmPassword"],
    };
    setDraft(nextDraft);
    setFieldErrors((current) => {
      const nextErrors = { ...current };
      for (const dependent of dependencies[field]) {
        nextErrors[dependent] = current[dependent] === undefined
          ? undefined
          : nextValidation[dependent];
      }
      return nextErrors;
    });
    setServerError(null);
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submittingRef.current) return;
    const errors = validateUserDraft(draft);
    setFieldErrors(errors);
    setServerError(null);
    const firstInvalid = (Object.keys(errors) as (keyof UserDraft)[])[0];
    if (firstInvalid !== undefined) {
      const refs = {
        username: usernameRef,
        displayName: displayNameRef,
        password: passwordRef,
        confirmPassword: confirmPasswordRef,
      };
      queueMicrotask(() => refs[firstInvalid].current?.focus());
      return;
    }
    submittingRef.current = true;
    setSubmissionLocked(true);
    try {
      const created = await createUser.mutateAsync({
        username: normalizeUsername(draft.username),
        display_name: draft.displayName.trim(),
        role: actorRole === "teacher" ? "student" : role,
        password: draft.password,
      });
      setDraft(EMPTY_DRAFT);
      setRole("student");
      setFieldErrors({});
      setServerError(null);
      if (blockerRef.current.state === "blocked") blockerRef.current.reset();
      onCreated(created);
      restoreTrigger();
    } catch (error) {
      if (blockerRef.current.state === "blocked") blockerRef.current.reset();
      setServerError(safeAccountCreateMessage(error));
    } finally {
      submittingRef.current = false;
      setSubmissionLocked(false);
    }
  }

  const errorSummary = Object.entries(fieldErrors).filter((entry) => entry[1] !== undefined);
  const submitLabel = actorRole === "admin" ? "创建账号" : "创建学生账号";
  const createsStudent = actorRole === "teacher" || role === "student";

  return createPortal(
    <div className="user-dialog-backdrop">
      <div
        ref={dialogRef}
        className="user-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-user-title"
        aria-describedby="create-user-description"
        onKeyDownCapture={onDialogKeyDown}
      >
        {discardPromptOpen ? (
          <section
            ref={discardDialogRef}
            className="user-dialog__discard"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="discard-user-title"
            aria-describedby="discard-user-description"
          >
            <strong id="discard-user-title">{pending ? "账号正在创建" : "放弃未保存内容？"}</strong>
            <span id="discard-user-description">{pending ? "创建请求完成前不能离开此页面，也不能放弃本次操作。" : "关闭将清除本次填写的账号资料和初始密码，且无法恢复。"}</span>
            <div>
              <button ref={continueEditingRef} className="button button--secondary" type="button" onClick={continueEditing}>继续编辑</button>
              <button className="button" type="button" disabled={pending} onClick={discardChanges}>放弃未保存内容</button>
            </div>
          </section>
        ) : null}

        <div className="user-dialog__content" inert={discardPromptOpen ? true : undefined} aria-hidden={discardPromptOpen ? "true" : undefined}>
          <header className="user-dialog__header">
            <div>
              <p>ACCOUNT / NEW ENTRY</p>
              <h2 id="create-user-title">{actorRole === "admin" ? "创建新账号" : "创建学生账号"}</h2>
              <p id="create-user-description">初始密码仅用于本次创建，不会显示在账号登记中。请通过安全渠道交给使用者。</p>
            </div>
            <button ref={closeRef} className="user-dialog__close" type="button" aria-label="关闭创建账号表单" disabled={pending} onClick={requestClose}>×</button>
          </header>

          {errorSummary.length > 0 ? (
            <section className="user-dialog__errors" role="alert" aria-label="请修正以下内容">
              <strong>请修正以下内容</strong>
              <ul>{errorSummary.map(([name, message]) => <li key={name}>{message}</li>)}</ul>
            </section>
          ) : null}
          {serverError !== null ? <div className="user-dialog__errors" role="alert">{serverError}</div> : null}

          <form className="user-form" noValidate autoComplete="off" aria-busy={createUser.isPending} onSubmit={(event) => void onSubmit(event)}>
            {actorRole === "admin" ? (
              <div className="user-form__field user-form__field--role">
                <label htmlFor="user-role">账号角色</label>
                <select id="user-role" name="role" className="field" value={role} onChange={(event) => setRole(event.target.value as "teacher" | "student")}>
                  <option value="student">学生</option>
                  <option value="teacher">教师</option>
                </select>
              </div>
            ) : (
              <p className="user-form__fixed-role"><span>创建角色</span><strong>学生账号</strong></p>
            )}
            <div className="user-form__field">
              <label htmlFor="new-username">用户名</label>
              <input ref={usernameRef} id="new-username" name="new_username" className="field" autoComplete="off" spellCheck="false" value={draft.username} aria-invalid={fieldErrors.username !== undefined} aria-describedby={fieldErrors.username === undefined ? "new-username-hint" : "new-username-hint new-username-error"} onChange={(event) => updateDraft("username", event.target.value)} />
              <span id="new-username-hint" className="user-form__hint">3–64 位，仅小写字母、数字、点、下划线或连字符。</span>
              {fieldErrors.username !== undefined ? <span id="new-username-error" className="user-form__error">{fieldErrors.username}</span> : null}
            </div>
            <div className="user-form__field">
              <label htmlFor="new-display-name">显示姓名</label>
              <input ref={displayNameRef} id="new-display-name" name="display_name" className="field" autoComplete="off" value={draft.displayName} aria-invalid={fieldErrors.displayName !== undefined} aria-describedby={fieldErrors.displayName === undefined ? undefined : "new-display-name-error"} onChange={(event) => updateDraft("displayName", event.target.value)} />
              {fieldErrors.displayName !== undefined ? <span id="new-display-name-error" className="user-form__error">{fieldErrors.displayName}</span> : null}
            </div>
            <div className="user-form__field">
              <label htmlFor="new-password">初始密码</label>
              <input ref={passwordRef} id="new-password" name="new_password" className="field" type="password" autoComplete="new-password" spellCheck="false" value={draft.password} aria-invalid={fieldErrors.password !== undefined} aria-describedby={fieldErrors.password === undefined ? "new-password-hint" : "new-password-hint new-password-error"} onChange={(event) => updateDraft("password", event.target.value)} />
              <span id="new-password-hint" className="user-form__hint">12–128 个字符，不能与用户名相同。</span>
              {fieldErrors.password !== undefined ? <span id="new-password-error" className="user-form__error">{fieldErrors.password}</span> : null}
            </div>
            <div className="user-form__field">
              <label htmlFor="confirm-new-password">确认初始密码</label>
              <input ref={confirmPasswordRef} id="confirm-new-password" name="confirm_new_password" className="field" type="password" autoComplete="new-password" spellCheck="false" value={draft.confirmPassword} aria-invalid={fieldErrors.confirmPassword !== undefined} aria-describedby={fieldErrors.confirmPassword === undefined ? undefined : "confirm-new-password-error"} onChange={(event) => updateDraft("confirmPassword", event.target.value)} />
              {fieldErrors.confirmPassword !== undefined ? <span id="confirm-new-password-error" className="user-form__error">{fieldErrors.confirmPassword}</span> : null}
            </div>
            <footer className="user-form__actions">
              <span>{createsStudent
                ? "创建后账号立即生效，并计入所有已有作业的学生总数与未提交人数；初始密码不会再次显示。"
                : "创建后账号立即生效，初始密码不会再次显示。"}</span>
              <button className="button" type="submit" disabled={createUser.isPending}>{createUser.isPending ? "正在创建…" : submitLabel}</button>
            </footer>
          </form>
        </div>
      </div>
    </div>,
    document.body,
  );
}
