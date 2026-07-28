import {
  ApiContractError,
  ApiError,
  ApiNetworkError,
  ApiTimeoutError,
} from "../shared/api/errors";

export function safeAccountCreateMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 401) return "登录状态已失效，请重新登录。";
  if (error instanceof ApiError && error.status === 403) return "当前账号没有创建该角色的权限。";
  if (error instanceof ApiError && error.status === 409) return "用户名已存在，请更换后重试。";
  if (error instanceof ApiError && error.status === 413) return "账号信息过大，请缩短后重试。";
  if (error instanceof ApiError && error.status === 422) return "账号信息未通过服务器校验，请检查后重试。";
  if (error instanceof ApiTimeoutError) return "创建请求超时，请检查网络后重试。";
  if (error instanceof ApiNetworkError) return "无法连接服务器，请检查网络后重试。";
  if (error instanceof ApiContractError) return "服务器返回了无法识别的账号数据，请稍后重试。";
  if (error instanceof ApiError && error.status >= 500) return "服务器暂时无法创建账号，请稍后重试。";
  return "暂时无法创建账号，请稍后重试。";
}
