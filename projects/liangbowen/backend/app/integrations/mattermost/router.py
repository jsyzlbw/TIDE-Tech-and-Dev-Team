from __future__ import annotations

import asyncio
import hmac
import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import get_session
from app.integrations.mattermost.actions import (
    ActionAuthenticationError,
    ActionTransportError,
    parse_and_verify_action,
)
from app.integrations.mattermost.schemas import DemoBindingRead, DemoBindingRequest
from app.integrations.mattermost.security import (
    MattermostTransportError,
    verify_and_parse_form,
)
from app.integrations.mattermost.service import (
    DemoBindingConflict,
    DemoBindingPersistenceError,
    DemoBindingRejected,
    MattermostIdentityRejected,
    bind_demo_identity,
    handle_mattermost_action,
    handle_mattermost_command,
)

COMMAND_TIMEOUT_SECONDS = 2.5
MAX_DEMO_BINDING_BYTES = 4 * 1024
SessionDependency = Callable[[], object]


def demo_bindings_enabled(settings: Settings) -> bool:
    return settings.app_env in {"development", "test"} and (
        settings.mattermost_demo_setup_key is not None
    )


def _ephemeral(text: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"response_type": "ephemeral", "text": text},
    )


@asynccontextmanager
async def _request_session(request: Request) -> AsyncIterator[AsyncSession]:
    provider: SessionDependency = request.app.dependency_overrides.get(get_session, get_session)
    resource = provider()
    if inspect.isawaitable(resource):
        resource = await resource
    if isinstance(resource, AsyncSession):
        yield resource
        return
    if not hasattr(resource, "__anext__"):
        raise TypeError("session dependency must return an AsyncSession")
    generator = resource
    session = await generator.__anext__()
    if not isinstance(session, AsyncSession):
        await generator.aclose()
        raise TypeError("session dependency must yield an AsyncSession")
    try:
        yield session
    finally:
        await generator.aclose()


def _verify_demo_key(request: Request, settings: Settings) -> bool:
    received = request.headers.getlist("x-demo-setup-key")
    expected = settings.mattermost_demo_setup_key
    if len(received) != 1 or expected is None:
        return False
    return hmac.compare_digest(
        received[0].encode("utf-8"),
        expected.get_secret_value().encode("utf-8"),
    )


def build_mattermost_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/integrations/mattermost", tags=["mattermost"])

    @router.post("/commands")
    async def commands(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            command_request = verify_and_parse_form(
                body,
                request.headers.get("content-type", ""),
                settings.mattermost_command_token,
            )
        except MattermostTransportError as exc:
            return _ephemeral(exc.public_message, exc.status_code)

        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                async with _request_session(request) as session, session.begin():
                    response = await handle_mattermost_command(session, command_request, settings)
        except MattermostIdentityRejected:
            return _ephemeral("Mattermost 身份未绑定或不可用。", status.HTTP_401_UNAUTHORIZED)
        except TimeoutError:
            return _ephemeral("服务暂时不可用，请重试。", status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception:  # noqa: BLE001 - adapter must sanitize every transient failure
            return _ephemeral("服务暂时不可用，请重试。", status.HTTP_503_SERVICE_UNAVAILABLE)
        return JSONResponse(content=response.model_dump(mode="json"))

    @router.post("/actions")
    async def actions(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            action_request = parse_and_verify_action(
                body,
                request.headers.get("content-type", ""),
                settings.mattermost_action_secret,
            )
        except (ActionAuthenticationError, ActionTransportError) as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {"message": exc.public_message},
                    "ephemeral_text": exc.public_message,
                },
            )
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                async with _request_session(request) as session, session.begin():
                    response = await handle_mattermost_action(
                        session,
                        action_request,
                        settings,
                    )
        except MattermostIdentityRejected:
            message = "Mattermost 身份未绑定或不可用。"
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": {"message": message}, "ephemeral_text": message},
            )
        except Exception:  # noqa: BLE001 - adapter must sanitize transient failures
            message = "服务暂时不可用，请重试。"
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": {"message": message}, "ephemeral_text": message},
            )
        return JSONResponse(status_code=status.HTTP_200_OK, content=response)

    if demo_bindings_enabled(settings):

        @router.post("/demo-bindings")
        async def demo_bindings(request: Request) -> JSONResponse:
            if not _verify_demo_key(request, settings):
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "invalid demo setup authentication"},
                )
            body = await request.body()
            if len(body) > MAX_DEMO_BINDING_BYTES:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"detail": "request body is too large"},
                )
            try:
                payload = DemoBindingRequest.model_validate_json(body)
            except ValidationError:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"detail": "invalid demo binding request"},
                )
            try:
                async with _request_session(request) as session, session.begin():
                    identity, created = await bind_demo_identity(session, payload)
                    response = DemoBindingRead.model_validate(identity).model_dump(mode="json")
            except DemoBindingRejected:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "local account is not eligible"},
                )
            except DemoBindingConflict:
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={"detail": "identity binding cannot be reassigned"},
                )
            except DemoBindingPersistenceError:
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"detail": "service temporarily unavailable"},
                )
            return JSONResponse(
                status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
                content=response,
            )

    return router
