from __future__ import annotations

from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Settings
from app.db.session import get_session
from app.main import SubmissionBodyLimitMiddleware, create_app

COMMAND_PATH = "/api/v1/integrations/mattermost/commands"
DEMO_PATH = "/api/v1/integrations/mattermost/demo-bindings"


def test_asgi_body_limiter_covers_both_mattermost_ingress_paths() -> None:
    assert (
        SubmissionBodyLimitMiddleware._request_limit(
            {"type": "http", "method": "POST", "path": COMMAND_PATH}
        )
        == 64 * 1024
    )
    assert (
        SubmissionBodyLimitMiddleware._request_limit(
            {"type": "http", "method": "POST", "path": DEMO_PATH},
            mattermost_demo_enabled=True,
        )
        == 4 * 1024
    )
    assert (
        SubmissionBodyLimitMiddleware._request_limit(
            {"type": "http", "method": "POST", "path": DEMO_PATH},
            mattermost_demo_enabled=False,
        )
        is None
    )


def _settings(env: str, *, demo_key: bool = True) -> Settings:
    return Settings(
        app_env=env,
        jwt_secret=("test-only" if env in {"development", "test"} else "x" * 32),
        mattermost_command_token=SecretStr("slash-secret"),
        mattermost_demo_setup_key=SecretStr("demo-secret") if demo_key else None,
    )


def _body(*, token: str = "slash-secret", text: str = "help") -> bytes:
    return urlencode(
        {
            "token": token,
            "team_id": "team-1",
            "team_domain": "course",
            "channel_id": "channel-1",
            "channel_name": "homework",
            "user_id": "mm-unknown",
            "user_name": "unknown",
            "command": "/hw",
            "text": text,
            "trigger_id": "trigger-1",
            "response_url": "https://mattermost.invalid/hooks/response",
        }
    ).encode()


def test_route_registration_is_environment_and_key_safe() -> None:
    for env in ("development", "test"):
        paths = create_app(_settings(env)).openapi()["paths"]
        assert COMMAND_PATH in paths
        assert DEMO_PATH in paths

    for settings in (_settings("development", demo_key=False), _settings("production")):
        paths = create_app(settings).openapi()["paths"]
        assert COMMAND_PATH in paths
        assert DEMO_PATH not in paths


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "wrong"])
async def test_invalid_command_token_never_requests_a_database_session(token: str) -> None:
    application = create_app(_settings("test"))
    session_requested = False

    async def fail_session():
        nonlocal session_requested
        session_requested = True
        raise AssertionError("database dependency must not run before token verification")
        yield  # pragma: no cover

    application.dependency_overrides[get_session] = fail_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            COMMAND_PATH,
            content=_body(token=token),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 401
    assert response.json() == {
        "response_type": "ephemeral",
        "text": "Mattermost 请求认证失败。",
    }
    assert response.headers["x-request-id"]
    assert session_requested is False


@pytest.mark.asyncio
async def test_command_transport_rejects_oversize_with_safe_ephemeral_response() -> None:
    application = create_app(_settings("test"))
    yielded = 0

    async def oversized_stream():
        nonlocal yielded
        for _ in range(16):
            yielded += 1
            yield b"x" * (16 * 1024)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            COMMAND_PATH,
            content=oversized_stream(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 413
    assert response.json() == {"response_type": "ephemeral", "text": "请求内容过大。"}
    assert response.headers["x-request-id"]
    assert yielded == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{}", b"x" * (4 * 1024 + 1)])
async def test_production_demo_route_is_a_real_404_for_every_body_size(body: bytes) -> None:
    application = create_app(_settings("production"))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            DEMO_PATH,
            content=body,
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 404
