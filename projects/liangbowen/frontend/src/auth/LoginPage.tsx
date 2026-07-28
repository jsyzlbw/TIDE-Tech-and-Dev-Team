import { type FormEvent, useRef, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import {
  ApiAbortError,
  ApiContractError,
  ApiError,
  ApiNetworkError,
  ApiTimeoutError,
} from "../shared/api/errors";
import { AuthGate } from "./RequireRole";
import { AuthStorageError, useAuth } from "./authState";
import { roleLanding, safePostLoginDestination } from "./routing";

function requestIdFor(error: unknown): string | null {
  return error instanceof ApiError && error.requestId !== undefined ? error.requestId : null;
}

function safeLoginMessage(error: unknown): string {
  if (error instanceof AuthStorageError) return "浏览器无法保存登录状态，请检查隐私设置后重试。";
  if (error instanceof ApiError && error.status === 401) return "用户名或密码不正确。";
  if (error instanceof ApiTimeoutError) return "登录请求超时，请检查网络后重试。";
  if (error instanceof ApiNetworkError) return "无法连接服务器，请检查网络后重试。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的登录结果，请稍后重试。";
  if (error instanceof ApiError && error.status >= 500) return "服务器暂时无法完成登录，请稍后重试。";
  if (error instanceof ApiAbortError) return "登录已取消。";
  return "暂时无法登录，请稍后重试。";
}

export function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const submittingRef = useRef(false);
  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<{ username?: string; password?: string }>({});
  const [failure, setFailure] = useState<{ message: string; requestId: string | null } | null>(null);

  if (user !== null && !submitting) {
    const landing = roleLanding(user.role);
    return landing === null ? <Navigate to="/" replace /> : <Navigate to={landing} replace />;
  }

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submittingRef.current) return;
    const normalizedUsername = username.trim();
    const nextErrors: { username?: string; password?: string } = {};
    if (normalizedUsername.length < 1 || normalizedUsername.length > 64) {
      nextErrors.username = "用户名需为 1–64 个字符。";
    }
    if (password.length < 1 || password.length > 1024) {
      nextErrors.password = "请输入密码（最多 1024 个字符）。";
    }
    setFieldErrors(nextErrors);
    setFailure(null);
    if (Object.keys(nextErrors).length > 0) {
      const firstInvalidField =
        nextErrors.username === undefined ? passwordRef : usernameRef;
      queueMicrotask(() => firstInvalidField.current?.focus());
      return;
    }

    submittingRef.current = true;
    setSubmitting(true);
    let navigationStarted = false;
    try {
      const authenticated = await login(normalizedUsername, password);
      const landing = roleLanding(authenticated.role);
      navigationStarted = true;
      if (landing === null) {
        navigate("/", { replace: true });
        return;
      }
      const from = (location.state as { from?: unknown } | null)?.from;
      navigate(safePostLoginDestination(from, authenticated.role), { replace: true });
    } catch (error) {
      setFailure({ message: safeLoginMessage(error), requestId: requestIdFor(error) });
    } finally {
      if (!navigationStarted) {
        submittingRef.current = false;
        setSubmitting(false);
      }
    }
  };

  return (
    <AuthGate>
      <section className="login-register" aria-labelledby="login-title">
        <div className="login-register__ledger" aria-hidden="true">
          <span>FORM 03</span>
          <span>IDENTITY REGISTER</span>
          <span>AUTHORIZED ENTRY</span>
        </div>
        <div className="login-register__intro">
          <p className="login-register__kicker">课程作业评审系统 · 安全签入</p>
          <h2 id="login-title">登录</h2>
          <p>使用课程账号登记身份。系统将在进入工作台前核验你的角色。</p>
        </div>
        <form className="login-form" onSubmit={(event) => void onSubmit(event)} aria-busy={submitting} noValidate>
          {failure !== null ? (
            <div className="login-form__alert" role="alert">
              <strong>{failure.message}</strong>
              {failure.requestId !== null ? <span>请求编号：{failure.requestId}</span> : null}
            </div>
          ) : null}
          <div className="login-form__field">
            <label htmlFor="login-username">用户名</label>
            <input
              ref={usernameRef}
              className="field"
              id="login-username"
              name="username"
              type="text"
              autoComplete="username"
              spellCheck={false}
              required
              maxLength={64}
              value={username}
              aria-invalid={fieldErrors.username !== undefined}
              aria-describedby={fieldErrors.username === undefined ? undefined : "login-username-error"}
              onChange={(event) => setUsername(event.target.value)}
            />
            {fieldErrors.username !== undefined ? <p id="login-username-error" role="alert">{fieldErrors.username}</p> : null}
          </div>
          <div className="login-form__field">
            <label htmlFor="login-password">密码</label>
            <div className="login-form__password">
              <input
                ref={passwordRef}
                className="field"
                id="login-password"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                spellCheck={false}
                required
                maxLength={1024}
                value={password}
                aria-invalid={fieldErrors.password !== undefined}
                aria-describedby={fieldErrors.password === undefined ? undefined : "login-password-error"}
                onChange={(event) => setPassword(event.target.value)}
              />
              <button
                className="login-form__reveal"
                type="button"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                aria-pressed={showPassword}
                onClick={() => setShowPassword((visible) => !visible)}
              >
                {showPassword ? "隐藏" : "显示"}
              </button>
            </div>
            {fieldErrors.password !== undefined ? <p id="login-password-error" role="alert">{fieldErrors.password}</p> : null}
          </div>
          <button className="button login-form__submit" type="submit" disabled={submitting}>
            {submitting ? "正在核验…" : "登录并进入工作台"}
          </button>
        </form>
      </section>
    </AuthGate>
  );
}
