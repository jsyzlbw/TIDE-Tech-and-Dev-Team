export interface ApiErrorOptions {
  status: number;
  code: string;
  message: string;
  requestId?: string;
  details?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly details?: unknown;

  constructor({ status, code, message, requestId, details }: ApiErrorOptions) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

export class ApiContractError extends ApiError {
  constructor(message = "API response contract violation", status = 0, requestId?: string) {
    super({ status, code: "API_CONTRACT_ERROR", message, requestId });
    this.name = "ApiContractError";
  }
}

export class ApiConfigurationError extends Error {
  constructor(message = "Invalid API configuration") {
    super(message);
    this.name = "ApiConfigurationError";
  }
}

export class ApiTimeoutError extends Error {
  constructor() {
    super("API request timed out");
    this.name = "ApiTimeoutError";
  }
}

export class ApiAbortError extends Error {
  constructor() {
    super("API request was cancelled");
    this.name = "ApiAbortError";
  }
}

export class ApiNetworkError extends Error {
  constructor() {
    super("API request failed at the network layer");
    this.name = "ApiNetworkError";
  }
}
