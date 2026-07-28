from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Settings
from app.db.session import get_session
from app.integrations.mattermost.actions import sign_action
from app.main import SubmissionBodyLimitMiddleware, create_app

ACTION_PATH = "/api/v1/integrations/mattermost/actions"
REPORT_ID = uuid.UUID("00000000-0000-4000-8000-000000000123")
DELIVERY_ID = uuid.UUID("00000000-0000-4000-8000-000000000456")
USER_ID = "user0000000000000000000001"
CHANNEL_ID = "chan0000000000000000000001"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        mattermost_action_secret=SecretStr("action-secret"),
        web_console_url="http://console.test",
    )


def _body(signature: str) -> bytes:
    return json.dumps(
        {
            "user_id": USER_ID,
            "post_id": "post0000000000000000000001",
            "channel_id": CHANNEL_ID,
            "team_id": "",
            "context": {
                "action": "confirm",
                "report_id": str(REPORT_ID),
                "delivery_id": str(DELIVERY_ID),
                "expected_user_id": USER_ID,
                "expected_channel_id": CHANNEL_ID,
                "signature": signature,
            },
        }
    ).encode()


def test_action_route_is_always_registered_and_stream_bounded() -> None:
    assert ACTION_PATH in create_app(_settings()).openapi()["paths"]
    assert (
        SubmissionBodyLimitMiddleware._request_limit(
            {"type": "http", "method": "POST", "path": ACTION_PATH}
        )
        == 16 * 1024
    )


@pytest.mark.asyncio
async def test_invalid_action_signature_never_requests_database() -> None:
    application = create_app(_settings())
    session_requested = False

    async def fail_session():
        nonlocal session_requested
        session_requested = True
        raise AssertionError("database dependency must not run before signature verification")
        yield  # pragma: no cover

    application.dependency_overrides[get_session] = fail_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            ACTION_PATH,
            content=_body("0" * 64),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Mattermost 操作认证失败。"
    assert response.json()["ephemeral_text"]
    assert session_requested is False


@pytest.mark.asyncio
async def test_oversized_action_is_safe_json_and_stops_streaming() -> None:
    application = create_app(_settings())
    yielded = 0

    async def oversized_stream():
        nonlocal yielded
        for _ in range(8):
            yielded += 1
            yield b"x" * 4096

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            ACTION_PATH,
            content=oversized_stream(),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {
        "error": {"message": "请求内容过大。"},
        "ephemeral_text": "请求内容过大。",
    }
    assert yielded == 5


def test_valid_signature_helper_matches_callback_payload() -> None:
    body = json.loads(
        _body(
            sign_action(
                "confirm",
                REPORT_ID,
                DELIVERY_ID,
                USER_ID,
                CHANNEL_ID,
                SecretStr("action-secret"),
            )
        )
    )
    assert body["context"]["signature"] != "action-secret"
