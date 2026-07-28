export const USERNAME_PATTERN = /^[a-z0-9][a-z0-9._-]{2,63}$/u;
export const INITIAL_PASSWORD_MIN = 12;
export const INITIAL_PASSWORD_MAX = 128;

export interface UserDraft {
  username: string;
  displayName: string;
  password: string;
  confirmPassword: string;
}

export type UserFieldErrors = Partial<Record<keyof UserDraft, string>>;

function hasControl(value: string) {
  return /\p{C}/u.test(value);
}

function hasVisibleText(value: string) {
  return [...value].some((character) => !/\s/u.test(character) && !/\p{C}/u.test(character));
}

export function normalizeUsername(value: string) {
  return value.trim();
}

export function validateUserDraft(draft: UserDraft): UserFieldErrors {
  const errors: UserFieldErrors = {};
  const username = normalizeUsername(draft.username);
  if (!USERNAME_PATTERN.test(username)) {
    errors.username = "用户名需为 3–64 位小写字母、数字、点、下划线或连字符。";
  }
  const displayName = draft.displayName.trim();
  if (!hasVisibleText(displayName) || [...displayName].length > 128 || hasControl(displayName)) {
    errors.displayName = "显示姓名需为 1–128 个可见字符。";
  }
  if (
    draft.password !== draft.password.trim() ||
    [...draft.password].length < INITIAL_PASSWORD_MIN ||
    [...draft.password].length > INITIAL_PASSWORD_MAX ||
    hasControl(draft.password)
  ) {
    errors.password = "初始密码需为 12–128 个字符，且不能包含首尾空格或控制字符。";
  }
  if (draft.password === username) {
    errors.password = "初始密码不能与用户名相同。";
  }
  if (draft.confirmPassword !== draft.password) {
    errors.confirmPassword = "两次输入的密码不一致。";
  }
  return errors;
}
