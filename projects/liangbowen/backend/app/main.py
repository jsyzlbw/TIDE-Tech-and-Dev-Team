import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Message, Receive, Scope, Send

from app.assignments.router import router as assignments_router
from app.auth.router import router as auth_router
from app.core.config import Settings, get_settings
from app.db.session import engine
from app.evaluations.router import router as evaluations_router
from app.integrations.mattermost.router import build_mattermost_router, demo_bindings_enabled
from app.reviews.router import router as reviews_router
from app.submissions.router import router as submissions_router
from app.users.router import router as users_router

MAX_SUBMISSION_REQUEST_BYTES = 384 * 1024
MAX_ASSIGNMENT_REQUEST_BYTES = 256 * 1024
MAX_EVALUATION_REQUEST_BYTES = 4 * 1024
MAX_REVIEW_REQUEST_BYTES = 16 * 1024
MAX_MATTERMOST_COMMAND_REQUEST_BYTES = 64 * 1024
MAX_MATTERMOST_DEMO_REQUEST_BYTES = 4 * 1024
MAX_MATTERMOST_ACTION_REQUEST_BYTES = 16 * 1024
MAX_ACCOUNT_REQUEST_BYTES = 4 * 1024
MATTERMOST_COMMAND_PATH = "/api/v1/integrations/mattermost/commands"
MATTERMOST_DEMO_PATH = "/api/v1/integrations/mattermost/demo-bindings"
MATTERMOST_ACTION_PATH = "/api/v1/integrations/mattermost/actions"
ACCOUNT_CREATE_PATH = "/api/v1/users"
READINESS_DATABASE_TIMEOUT_SECONDS = 1.5
AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]
logger = logging.getLogger(__name__)


def _is_review_path(path: str) -> bool:
    return path.startswith("/api/v1/reports/")


def _is_mattermost_command_path(path: str) -> bool:
    return path == MATTERMOST_COMMAND_PATH


def _is_mattermost_action_path(path: str) -> bool:
    return path == MATTERMOST_ACTION_PATH


def _scope_request_id(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    request_id = state.get("request_id")
    if isinstance(request_id, str):
        return request_id
    generated = str(uuid.uuid4())
    state["request_id"] = generated
    return generated


class RequestIdMiddleware:
    def __init__(self, app: AsgiApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", []))
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _readiness_failure_category(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "connectivity"
    return "database"


async def database_is_ready() -> bool:
    try:
        async with asyncio.timeout(READINESS_DATABASE_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except (OSError, SQLAlchemyError, TimeoutError) as exc:
        logger.warning(
            "database readiness check failed category=%s error_type=%s",
            _readiness_failure_category(exc),
            type(exc).__name__,
        )
        return False
    return True


class SubmissionBodyLimitMiddleware:
    def __init__(self, app: AsgiApp, *, mattermost_demo_enabled: bool = False) -> None:
        self.app = app
        self.mattermost_demo_enabled = mattermost_demo_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_limit = self._request_limit(
            scope,
            mattermost_demo_enabled=self.mattermost_demo_enabled,
        )
        if request_limit is None:
            await self.app(scope, receive, send)
            return

        messages: list[Message] = []
        total_bytes = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                messages.append(message)
                break
            total_bytes += len(message.get("body", b""))
            if total_bytes > request_limit:
                path = str(scope.get("path", ""))
                if _is_mattermost_command_path(path):
                    content = {
                        "response_type": "ephemeral",
                        "text": "请求内容过大。",
                    }
                elif _is_mattermost_action_path(path):
                    content = {
                        "error": {"message": "请求内容过大。"},
                        "ephemeral_text": "请求内容过大。",
                    }
                else:
                    content = {"detail": "request body is too large"}
                if _is_review_path(path):
                    content["request_id"] = _scope_request_id(scope)
                response = JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content=content,
                )
                await response(scope, receive, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        message_iterator = iter(messages)

        async def replay_receive() -> Message:
            try:
                return next(message_iterator)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _request_limit(
        scope: Scope,
        *,
        mattermost_demo_enabled: bool = False,
    ) -> int | None:
        path = scope.get("path", "")
        if scope.get("type") != "http":
            return None
        method = scope.get("method")
        if method == "POST" and path == MATTERMOST_COMMAND_PATH:
            return MAX_MATTERMOST_COMMAND_REQUEST_BYTES
        if method == "POST" and path == MATTERMOST_ACTION_PATH:
            return MAX_MATTERMOST_ACTION_REQUEST_BYTES
        if mattermost_demo_enabled and method == "POST" and path == MATTERMOST_DEMO_PATH:
            return MAX_MATTERMOST_DEMO_REQUEST_BYTES
        if _is_review_path(path) and method in {"POST", "PATCH"}:
            return MAX_REVIEW_REQUEST_BYTES
        if method != "POST":
            return None
        if path == ACCOUNT_CREATE_PATH:
            return MAX_ACCOUNT_REQUEST_BYTES
        if path == "/api/v1/assignments":
            return MAX_ASSIGNMENT_REQUEST_BYTES
        if path.startswith("/api/v1/assignments/") and path.endswith("/submissions"):
            return MAX_SUBMISSION_REQUEST_BYTES
        if path.endswith("/evaluations") and path.startswith(
            ("/api/v1/assignments/", "/api/v1/submissions/")
        ):
            return MAX_EVALUATION_REQUEST_BYTES
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(title="AI Grading API", version="0.1.0")
    application.add_middleware(
        SubmissionBodyLimitMiddleware,
        mattermost_demo_enabled=demo_bindings_enabled(settings),
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(RequestIdMiddleware)
    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(assignments_router, prefix="/api/v1")
    application.include_router(submissions_router, prefix="/api/v1")
    application.include_router(evaluations_router, prefix="/api/v1")
    application.include_router(reviews_router, prefix="/api/v1")
    application.include_router(users_router, prefix="/api/v1")
    application.include_router(build_mattermost_router(settings), prefix="/api/v1")

    @application.exception_handler(StarletteHTTPException)
    async def correlated_http_error(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        content: dict[str, object] = {"detail": error.detail}
        if _is_review_path(request.url.path):
            content["request_id"] = _scope_request_id(request.scope)
        return JSONResponse(
            status_code=error.status_code,
            content=jsonable_encoder(content),
            headers=error.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        safe_errors = [
            {key: value for key, value in item.items() if key in {"type", "loc", "msg"}}
            for item in error.errors()
        ]
        content: dict[str, object] = {"detail": safe_errors}
        if _is_review_path(request.url.path):
            content["request_id"] = _scope_request_id(request.scope)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=jsonable_encoder(content),
        )

    @application.exception_handler(Exception)
    async def correlated_internal_error(
        request: Request,
        error: Exception,
    ) -> JSONResponse | PlainTextResponse:
        request_id = _scope_request_id(request.scope)
        logger.error(
            "unhandled request error request_id=%s error_type=%s",
            request_id,
            type(error).__name__,
        )
        headers = {"X-Request-ID": request_id}
        if _is_review_path(request.url.path):
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "detail": "internal server error",
                    "request_id": request_id,
                },
                headers=headers,
            )
        return PlainTextResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content="Internal Server Error",
            headers=headers,
        )

    @application.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    async def health_ready() -> JSONResponse:
        if not await database_is_ready():
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable"},
            )
        return JSONResponse(content={"status": "ok"})

    @application.get("/api/v1")
    async def api_root() -> dict[str, str]:
        return {"name": "AI Grading API", "version": "v1"}

    return application


app = create_app()
