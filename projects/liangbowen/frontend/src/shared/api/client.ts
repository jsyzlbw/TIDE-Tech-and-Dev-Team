import { z } from "zod";

import {
  ApiAbortError,
  ApiConfigurationError,
  ApiContractError,
  ApiError,
  ApiNetworkError,
  ApiTimeoutError,
} from "./errors";
import { standardErrorSchema } from "./schemas";

export const ACCESS_TOKEN_STORAGE_KEY = "ai-grading.access-token";
export const DEFAULT_API_BASE_URL = "http://localhost:8000/api/v1";
export const DEFAULT_TIMEOUT_MS = 15_000;
export const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
export const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
export const MAX_ACCESS_TOKEN_CHARACTERS = 4_096;
export const MAX_API_LOCATION_CHARACTERS = 2_048;

const ABSOLUTE_SCHEME = /^[a-z][a-z\d+.-]*:/iu;
const COMPLETE_PERCENT_ESCAPE = /%[\da-f]{2}/iu;
const MALFORMED_PERCENT_ESCAPE = /%(?![\da-f]{2})/iu;
const MALFORMED_PERCENT_ESCAPE_GLOBAL = /%(?![\da-f]{2})/giu;
const MAX_DECODE_ROUNDS = 8;
const MAX_DETAIL_DEPTH = 4;
const MAX_DETAIL_KEY_CHARACTERS = 100;
const SENSITIVE_DETAIL_KEY =
  /(?:authorization|credential|password|secret|token|request_body|response_body|content_json|content_text|raw_model_output|input)/iu;
const PROTOTYPE_KEY = /^(?:__proto__|constructor|prototype)$/iu;

type QueryPrimitive = string | number | boolean | null | undefined;
export type QueryValue = QueryPrimitive | readonly QueryPrimitive[];
export type Query = Readonly<Record<string, QueryValue>>;

export interface ApiRequestOptions {
  maxResponseBytes?: number;
  query?: Query;
  signal?: AbortSignal;
}

export interface ApiClientOptions {
  baseUrl?: string;
  mode?: string;
  timeoutMs?: number;
  maxResponseBytes?: number;
  getToken?: () => string | null | undefined;
  fetch?: typeof globalThis.fetch;
}

export interface ApiClient {
  get<Schema extends z.ZodType>(
    endpoint: string,
    schema: Schema,
    options?: ApiRequestOptions,
  ): Promise<z.output<Schema>>;
  post<Schema extends z.ZodType>(
    endpoint: string,
    body: unknown,
    schema: Schema,
    options?: ApiRequestOptions,
  ): Promise<z.output<Schema>>;
  patch<Schema extends z.ZodType>(
    endpoint: string,
    body: unknown,
    schema: Schema,
    options?: ApiRequestOptions,
  ): Promise<z.output<Schema>>;
  delete(endpoint: string, options?: ApiRequestOptions): Promise<void>;
  delete<Schema extends z.ZodType>(
    endpoint: string,
    schema: Schema,
    options?: ApiRequestOptions,
  ): Promise<z.output<Schema>>;
}

function invalidConfiguration(): never {
  throw new ApiConfigurationError();
}

function isControlCharacter(character: string): boolean {
  const codePoint = character.codePointAt(0) ?? 0;
  return codePoint <= 31 || (codePoint >= 127 && codePoint <= 159);
}

function containsControlCharacters(value: string): boolean {
  return [...value].some(isControlCharacter);
}

function containsUnsafeEncoding(value: string): boolean {
  if (MALFORMED_PERCENT_ESCAPE.test(value)) return true;
  let decoded = value;
  for (let pass = 0; pass <= MAX_DECODE_ROUNDS; pass += 1) {
    if (
      containsControlCharacters(decoded) ||
      decoded.includes("\\") ||
      /%2f|%5c/iu.test(decoded) ||
      decoded.split("/").some((part) => part === "." || part === "..")
    ) {
      return true;
    }
    if (!COMPLETE_PERCENT_ESCAPE.test(decoded)) return false;
    if (pass === MAX_DECODE_ROUNDS) return true;
    try {
      const decodable = decoded.replace(MALFORMED_PERCENT_ESCAPE_GLOBAL, "%25");
      const next = decodeURIComponent(decodable);
      if (next === decoded) return false;
      decoded = next;
    } catch {
      return true;
    }
  }
  return true;
}

function normalizeBaseUrl(input: string, mode: string): string {
  if (
    input.length > MAX_API_LOCATION_CHARACTERS ||
    input.trim() !== input ||
    containsControlCharacters(input) ||
    input.includes("\\") ||
    input.includes("?") ||
    input.includes("#") ||
    containsUnsafeEncoding(input)
  ) {
    return invalidConfiguration();
  }

  if (input.startsWith("/")) {
    if (input.startsWith("//") || !["/", "/api/v1", "/api/v1/"].includes(input)) {
      return invalidConfiguration();
    }
    return "/api/v1";
  }

  let url: URL;
  try {
    url = new URL(input);
  } catch {
    return invalidConfiguration();
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    (mode !== "development" && mode !== "test" && url.protocol !== "https:") ||
    url.username !== "" ||
    url.password !== "" ||
    url.search !== "" ||
    url.hash !== "" ||
    !["", "/", "/api/v1", "/api/v1/"].includes(url.pathname)
  ) {
    return invalidConfiguration();
  }
  return `${url.origin}/api/v1`;
}

function normalizeEndpoint(endpoint: string): string {
  if (
    endpoint.length > MAX_API_LOCATION_CHARACTERS ||
    endpoint.trim() !== endpoint ||
    containsControlCharacters(endpoint) ||
    endpoint.includes("?") ||
    endpoint.includes("#") ||
    endpoint.startsWith("//") ||
    endpoint.includes("//") ||
    ABSOLUTE_SCHEME.test(endpoint) ||
    containsUnsafeEncoding(endpoint)
  ) {
    return invalidConfiguration();
  }
  const relative = endpoint.startsWith("/") ? endpoint.slice(1) : endpoint;
  if (relative.startsWith("/")) return invalidConfiguration();
  return relative;
}

type TokenStorage = Pick<Storage, "getItem">;

export function readStoredAccessToken(storage?: TokenStorage | null): string | null {
  if (storage === null) return null;
  try {
    const resolvedStorage =
      storage ?? (typeof window === "undefined" ? undefined : window.localStorage);
    return resolvedStorage?.getItem(ACCESS_TOKEN_STORAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

function validateToken(token: string | null | undefined): string | null {
  if (token === null || token === undefined || token === "") return null;
  if (token.length > MAX_ACCESS_TOKEN_CHARACTERS || containsControlCharacters(token)) {
    return invalidConfiguration();
  }
  return token;
}

function appendQuery(searchParams: URLSearchParams, query: Query | undefined): void {
  if (query === undefined) return;
  for (const [key, rawValue] of Object.entries(query)) {
    if (containsControlCharacters(key)) return invalidConfiguration();
    const values = Array.isArray(rawValue) ? rawValue : [rawValue];
    for (const value of values) {
      if (value === null || value === undefined) continue;
      if (typeof value === "number" && !Number.isFinite(value)) return invalidConfiguration();
      if (!["string", "number", "boolean"].includes(typeof value)) return invalidConfiguration();
      const serialized = String(value);
      if (containsControlCharacters(serialized)) return invalidConfiguration();
      searchParams.append(key, serialized);
    }
  }
}

function buildRequestUrl(baseUrl: string, endpoint: string, query: Query | undefined): string {
  const searchParams = new URLSearchParams();
  appendQuery(searchParams, query);
  const queryString = searchParams.toString();
  return `${baseUrl}/${endpoint}${queryString === "" ? "" : `?${queryString}`}`;
}

interface CorrelationId {
  present: boolean;
  value?: string;
}

function normalizePresentRequestId(value: unknown): CorrelationId {
  if (
    typeof value !== "string" ||
    value === "" ||
    value !== value.trim() ||
    value.length > 128 ||
    containsControlCharacters(value)
  ) {
    return { present: true };
  }
  return { present: true, value };
}

function responseRequestId(response: Response): CorrelationId {
  const rawValue = response.headers.get("X-Request-ID");
  if (rawValue === null) return { present: false };
  return normalizePresentRequestId(rawValue);
}

function bodyRequestId(body: unknown): CorrelationId {
  if (typeof body !== "object" || body === null || !("request_id" in body)) {
    return { present: false };
  }
  return normalizePresentRequestId((body as { request_id?: unknown }).request_id);
}

function assertValidHeaderRequestId(response: Response): CorrelationId {
  const header = responseRequestId(response);
  if (header.present && header.value === undefined) {
    throw new ApiContractError("API response correlation ID was invalid", response.status);
  }
  return header;
}

function assertCorrelation(
  response: Response,
  header: CorrelationId,
  body: unknown,
): string | undefined {
  const embedded = bodyRequestId(body);
  if (embedded.present && embedded.value === undefined) {
    throw new ApiContractError(
      "API response correlation ID was invalid",
      response.status,
      header.value,
    );
  }
  if (
    header.value !== undefined &&
    embedded.value !== undefined &&
    header.value !== embedded.value
  ) {
    throw new ApiContractError(
      "API response correlation mismatch",
      response.status,
      header.value,
    );
  }
  return header.value ?? embedded.value;
}

function isJsonContentType(value: string | null): boolean {
  if (value === null) return false;
  const mediaType = value.split(";", 1)[0]?.trim().toLowerCase();
  return mediaType === "application/json" || mediaType?.endsWith("+json") === true;
}

function declaredResponseSize(response: Response): number | undefined {
  const value = response.headers.get("Content-Length");
  if (value === null || !/^\d+$/u.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

function safeText(value: unknown, fallback: string, maxCharacters = 500): string {
  if (typeof value !== "string") return fallback;
  const cleaned = [...value]
    .map((character) => (isControlCharacter(character) ? " " : character))
    .join("")
    .trim()
    .slice(0, maxCharacters);
  return cleaned || fallback;
}

function matchesForbiddenDetailKey(value: string): boolean {
  const cleaned = safeText(value, "", MAX_DETAIL_KEY_CHARACTERS);
  const compact = cleaned.normalize("NFKC").replace(/[^a-z\d]/giu, "");
  return (
    SENSITIVE_DETAIL_KEY.test(value) ||
    SENSITIVE_DETAIL_KEY.test(cleaned) ||
    SENSITIVE_DETAIL_KEY.test(compact) ||
    PROTOTYPE_KEY.test(value.trim()) ||
    PROTOTYPE_KEY.test(cleaned)
  );
}

function isForbiddenDetailKey(value: string): boolean {
  let candidate = value;
  for (let pass = 0; pass < MAX_DECODE_ROUNDS; pass += 1) {
    const cleaned = safeText(candidate, "", MAX_DETAIL_KEY_CHARACTERS);
    if (matchesForbiddenDetailKey(candidate)) return true;
    try {
      const decoded = decodeURIComponent(cleaned);
      if (decoded === cleaned) return false;
      if (matchesForbiddenDetailKey(decoded)) return true;
      if (pass === MAX_DECODE_ROUNDS - 1) return true;
      candidate = decoded;
    } catch {
      return true;
    }
  }
  return true;
}

function sanitizeDetails(value: unknown, depth = 0): unknown {
  if (depth > MAX_DETAIL_DEPTH) return "[truncated]";
  if (value === null || ["boolean", "number"].includes(typeof value)) return value;
  if (typeof value === "string") return safeText(value, "");
  if (Array.isArray(value)) return value.slice(0, 50).map((item) => sanitizeDetails(item, depth + 1));
  if (typeof value !== "object") return undefined;
  const result: Record<string, unknown> = Object.create(null);
  for (const [key, item] of Object.entries(value).slice(0, 50)) {
    if (isForbiddenDetailKey(key)) continue;
    const safeKey = safeText(key, "", MAX_DETAIL_KEY_CHARACTERS);
    if (
      safeKey === "" ||
      isForbiddenDetailKey(safeKey) ||
      Object.hasOwn(result, safeKey)
    ) {
      continue;
    }
    const safe = sanitizeDetails(item, depth + 1);
    if (safe !== undefined) result[safeKey] = safe;
  }
  return result;
}

function normalizedApiError(response: Response, body: unknown, requestId: string | undefined): ApiError {
  const parsed = standardErrorSchema.safeParse(body);
  if (!parsed.success) {
    return new ApiError({
      status: response.status,
      code: `HTTP_${response.status}`,
      message: "Request failed",
      requestId,
    });
  }
  const error = parsed.data;
  if (Array.isArray(error.detail)) {
    const details = error.detail.slice(0, 50).map(({ type, loc, msg }) => ({
      type: safeText(type, "validation_error", 100),
      loc: loc.slice(0, 20).map((part) =>
        typeof part === "number" && Number.isSafeInteger(part)
          ? part
          : safeText(part, "field", 100),
      ),
      msg: safeText(msg, "Invalid value", 500),
    }));
    return new ApiError({
      status: response.status,
      code: "VALIDATION_ERROR",
      message: "Request validation failed",
      requestId,
      details,
    });
  }
  return new ApiError({
    status: response.status,
    code: safeText(error.code, `HTTP_${response.status}`),
    message: safeText(error.message ?? error.detail, "Request failed"),
    requestId,
    details: sanitizeDetails(error.details),
  });
}

type AbortReason = "caller" | "timeout";

function createAbortContext(timeoutMs: number, callerSignal: AbortSignal | undefined) {
  const controller = new AbortController();
  let reason: AbortReason | undefined;
  let listeningForCaller = false;
  let timeout: ReturnType<typeof setTimeout> | undefined;

  const removeCallerListener = () => {
    if (!listeningForCaller) return;
    callerSignal?.removeEventListener("abort", onCallerAbort);
    listeningForCaller = false;
  };
  const abortWithReason = (nextReason: AbortReason) => {
    if (reason !== undefined) return;
    reason = nextReason;
    if (timeout !== undefined) {
      clearTimeout(timeout);
      timeout = undefined;
    }
    removeCallerListener();
    controller.abort();
  };
  const onCallerAbort = () => abortWithReason("caller");

  if (callerSignal?.aborted === true) {
    abortWithReason("caller");
  } else {
    if (callerSignal !== undefined) {
      callerSignal.addEventListener("abort", onCallerAbort, { once: true });
      listeningForCaller = true;
    }
    timeout = setTimeout(() => abortWithReason("timeout"), timeoutMs);
  }

  return {
    signal: controller.signal,
    reason: () => reason,
    dispose: () => {
      if (timeout !== undefined) {
        clearTimeout(timeout);
        timeout = undefined;
      }
      removeCallerListener();
    },
  };
}

function throwTransportError(abort: ReturnType<typeof createAbortContext>): never {
  if (abort.reason() === "timeout") throw new ApiTimeoutError();
  if (abort.reason() === "caller") throw new ApiAbortError();
  throw new ApiNetworkError();
}

function cancelWithoutWaiting(cancel: () => void | Promise<void>): void {
  try {
    void Promise.resolve(cancel()).catch(() => undefined);
  } catch {
    // Cancellation is best-effort; the original response contract remains authoritative.
  }
}

async function readBoundedResponseBody(
  response: Response,
  maxResponseBytes: number,
  requestId: string | undefined,
  abort: ReturnType<typeof createAbortContext>,
): Promise<string> {
  if (response.body === null) return "";
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const chunks: string[] = [];
  let receivedBytes = 0;

  try {
    while (true) {
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        result = await reader.read();
      } catch {
        return throwTransportError(abort);
      }
      if (result.done) break;
      receivedBytes += result.value.byteLength;
      if (receivedBytes > maxResponseBytes) {
        cancelWithoutWaiting(() => reader.cancel());
        throw new ApiContractError(
          "API response exceeded the size limit",
          response.status,
          requestId,
        );
      }
      try {
        chunks.push(decoder.decode(result.value, { stream: true }));
      } catch {
        cancelWithoutWaiting(() => reader.cancel());
        throw new ApiContractError(
          "API response was not valid UTF-8",
          response.status,
          requestId,
        );
      }
    }
    try {
      chunks.push(decoder.decode());
    } catch {
      cancelWithoutWaiting(() => reader.cancel());
      throw new ApiContractError(
        "API response was not valid UTF-8",
        response.status,
        requestId,
      );
    }
    return chunks.join("");
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // A failed or cancelled stream can already have released its reader lock.
    }
  }
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const mode = options.mode ?? import.meta.env.MODE ?? "development";
  const configuredBaseUrl = options.baseUrl ?? import.meta.env.VITE_API_BASE_URL;
  // Non-local builds must name their API explicitly so a production bundle cannot call localhost.
  if (configuredBaseUrl === undefined && mode !== "development" && mode !== "test") {
    throw new ApiConfigurationError(
      "VITE_API_BASE_URL is required outside development and test",
    );
  }
  const baseUrl = normalizeBaseUrl(configuredBaseUrl ?? DEFAULT_API_BASE_URL, mode);
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const maxResponseBytes = options.maxResponseBytes ?? DEFAULT_MAX_RESPONSE_BYTES;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || !Number.isSafeInteger(maxResponseBytes) || maxResponseBytes <= 0 || maxResponseBytes > MAX_RESPONSE_BYTES) {
    return invalidConfiguration();
  }
  const injectedFetch = options.fetch;
  const getToken = options.getToken ?? readStoredAccessToken;

  async function request<Schema extends z.ZodType>(
    method: "GET" | "POST" | "PATCH" | "DELETE",
    endpoint: string,
    schema: Schema | undefined,
    body: unknown,
    requestOptions: ApiRequestOptions = {},
  ): Promise<z.output<Schema> | void> {
    const relativeEndpoint = normalizeEndpoint(endpoint);
    const url = buildRequestUrl(baseUrl, relativeEndpoint, requestOptions.query);
    const requestMaxResponseBytes = requestOptions.maxResponseBytes ?? maxResponseBytes;
    if (!Number.isSafeInteger(requestMaxResponseBytes) || requestMaxResponseBytes <= 0 || requestMaxResponseBytes > MAX_RESPONSE_BYTES) {
      return invalidConfiguration();
    }
    const token = validateToken(getToken());
    const headers = new Headers({ Accept: "application/json" });
    if (token !== null) headers.set("Authorization", `Bearer ${token}`);
    let encodedBody: string | undefined;
    if (body !== undefined) {
      try {
        encodedBody = JSON.stringify(body);
      } catch {
        return invalidConfiguration();
      }
      if (encodedBody === undefined) return invalidConfiguration();
      headers.set("Content-Type", "application/json");
    }

    const abort = createAbortContext(timeoutMs, requestOptions.signal);
    let response: Response;
    try {
      const requestFetch = injectedFetch ?? globalThis.fetch.bind(globalThis);
      response = await requestFetch(url, {
        method,
        headers,
        body: encodedBody,
        signal: abort.signal,
      });
    } catch {
      abort.dispose();
      return throwTransportError(abort);
    }

    try {
      const headerRequestId = assertValidHeaderRequestId(response);
      const declaredSize = declaredResponseSize(response);
      if (declaredSize !== undefined && declaredSize > requestMaxResponseBytes) {
        cancelWithoutWaiting(() => response.body?.cancel());
        throw new ApiContractError(
          "API response exceeded the size limit",
          response.status,
          headerRequestId.value,
        );
      }
      if (response.status === 204) {
        if (schema === undefined) return undefined;
        const validated = schema.safeParse(undefined);
        if (!validated.success) {
          throw new ApiContractError(
            "API response did not match its schema",
            response.status,
            headerRequestId.value,
          );
        }
        return validated.data;
      }

      const text = await readBoundedResponseBody(
        response,
        requestMaxResponseBytes,
        headerRequestId.value,
        abort,
      );

      let payload: unknown;
      if (isJsonContentType(response.headers.get("Content-Type"))) {
        try {
          payload = text === "" ? undefined : JSON.parse(text);
        } catch {
          if (response.ok) {
            throw new ApiContractError(
              "API response was not valid JSON",
              response.status,
              headerRequestId.value,
            );
          }
        }
      } else if (response.ok) {
        throw new ApiContractError(
          "API response did not use a JSON content type",
          response.status,
          headerRequestId.value,
        );
      }

      const requestId = assertCorrelation(response, headerRequestId, payload);
      if (!response.ok) throw normalizedApiError(response, payload, requestId);
      if (schema === undefined) {
        throw new ApiContractError("API response schema was missing", response.status, requestId);
      }
      const validated = schema.safeParse(payload);
      if (!validated.success) {
        throw new ApiContractError("API response did not match its schema", response.status, requestId);
      }
      return validated.data;
    } finally {
      abort.dispose();
    }
  }

  return {
    get: (endpoint, schema, requestOptions) =>
      request("GET", endpoint, schema, undefined, requestOptions),
    post: (endpoint, body, schema, requestOptions) =>
      request("POST", endpoint, schema, body, requestOptions),
    patch: (endpoint, body, schema, requestOptions) =>
      request("PATCH", endpoint, schema, body, requestOptions),
    delete: <Schema extends z.ZodType>(
      endpoint: string,
      schemaOrOptions?: Schema | ApiRequestOptions,
      requestOptions?: ApiRequestOptions,
    ) => {
      const schema = schemaOrOptions instanceof z.ZodType ? schemaOrOptions : undefined;
      const actualOptions = schema === undefined ? (schemaOrOptions as ApiRequestOptions) : requestOptions;
      return request("DELETE", endpoint, schema, undefined, actualOptions);
    },
  } as ApiClient;
}

export const api = createApiClient();
