from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.mattermost.client import (
    MattermostClient,
    MattermostPermanentError,
    MattermostRetryableError,
)

BOT_ID = "bot0000000000000000000001"
TEACHER_ID = "user0000000000000000000001"
CHANNEL_ID = "chan0000000000000000000001"
POST_ID = "post0000000000000000000001"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://mattermost.example.test/\x01private",
        "https://mattermost.example.test/\x7fprivate",
        "https://mattermost.example.test/" + "x" * 2_048,
    ],
)
def test_client_rejects_control_or_oversized_urls_without_echo(base_url: str) -> None:
    with pytest.raises(ValueError) as caught:
        MattermostClient(
            base_url=base_url,
            token=SecretStr("token"),
            bot_user_id=BOT_ID,
        )
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_client_reuses_connection_for_direct_channel_and_post() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer top-secret-token"
        if request.url.path == "/api/v4/channels/direct":
            assert json.loads(request.content) == [BOT_ID, TEACHER_ID]
            return httpx.Response(201, json={"id": CHANNEL_ID})
        assert request.url.path == "/api/v4/posts"
        assert json.loads(request.content) == {
            "channel_id": CHANNEL_ID,
            "message": "report",
            "props": {"a8_outbox_id": "stable"},
        }
        return httpx.Response(201, json={"id": POST_ID})

    client = MattermostClient(
        base_url="https://mattermost.example.test",
        token=SecretStr("top-secret-token"),
        bot_user_id=BOT_ID,
        transport=httpx.MockTransport(handler),
    )
    assert (
        await client.send_direct_post(
            recipient_user_id=TEACHER_ID,
            message="report",
            props={"a8_outbox_id": "stable"},
        )
        == POST_ID
    )
    assert len(requests) == 2
    await client.aclose()
    assert client.is_closed


@pytest.mark.asyncio
async def test_client_exposes_channel_then_post_for_channel_bound_signatures() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/channels/direct"):
            return httpx.Response(201, json={"id": CHANNEL_ID})
        return httpx.Response(201, json={"id": "0123456789abcdefghijklmnop"})

    client = MattermostClient(
        base_url="https://mattermost.example.test",
        token=SecretStr("token"),
        bot_user_id=BOT_ID,
        transport=httpx.MockTransport(handler),
    )
    channel_id = await client.create_direct_channel(TEACHER_ID)
    assert channel_id == CHANNEL_ID
    post_id = await client.create_post(channel_id, "report", {"signed": channel_id})
    assert post_id == "0123456789abcdefghijklmnop"
    assert seen == ["/api/v4/channels/direct", "/api/v4/posts"]
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "headers", "retryable", "retry_after"),
    [
        (400, {}, False, None),
        (401, {}, False, None),
        (408, {}, True, None),
        (429, {"Retry-After": "9999"}, True, 300),
        (500, {}, True, None),
    ],
)
@pytest.mark.asyncio
async def test_client_classifies_status_without_leaking_response(
    status: int,
    headers: dict[str, str],
    retryable: bool,
    retry_after: int | None,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, text="private answer and token")

    client = MattermostClient(
        base_url="https://mattermost.example.test",
        token=SecretStr("top-secret-token"),
        bot_user_id=BOT_ID,
        transport=httpx.MockTransport(handler),
    )
    error_type = MattermostRetryableError if retryable else MattermostPermanentError
    with pytest.raises(error_type) as caught:
        await client.send_direct_post(
            recipient_user_id=TEACHER_ID,
            message="private report",
            props={},
        )
    assert caught.value.retry_after_seconds == retry_after
    rendered = str(caught.value)
    assert "private" not in rendered
    assert "top-secret-token" not in rendered
    assert "mattermost.example" not in rendered
    await client.aclose()


@pytest.mark.asyncio
async def test_client_rejects_malformed_or_oversized_success_response() -> None:
    for content in (b"not-json", b'{"id":"bad id"}', b"{" + b" " * (64 * 1024)):

        async def handler(request: httpx.Request, content: bytes = content) -> httpx.Response:
            return httpx.Response(201, content=content)

        client = MattermostClient(
            base_url="https://mattermost.example.test",
            token=SecretStr("token"),
            bot_user_id=BOT_ID,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(MattermostPermanentError):
            await client.send_direct_post(
                recipient_user_id=TEACHER_ID,
                message="report",
                props={},
            )
        await client.aclose()


@pytest.mark.asyncio
async def test_network_failure_is_retryable_and_redacted() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret host details", request=request)

    client = MattermostClient(
        base_url="https://mattermost.example.test",
        token=SecretStr("token"),
        bot_user_id=BOT_ID,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(MattermostRetryableError) as caught:
        await client.send_direct_post(
            recipient_user_id=TEACHER_ID,
            message="report",
            props={},
        )
    assert str(caught.value) == "Mattermost request failed"
    await client.aclose()


@pytest.mark.parametrize(
    "base_url",
    [
        "mattermost.example.test",
        "ftp://mattermost.example.test",
        "https://user:pass@mattermost.example.test",
        "https://mattermost.example.test?token=secret",
        "https://mattermost.example.test/#fragment",
    ],
)
def test_client_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError):
        MattermostClient(
            base_url=base_url,
            token=SecretStr("token"),
            bot_user_id=BOT_ID,
        )


def test_client_requires_https_unless_explicitly_allowed() -> None:
    with pytest.raises(ValueError):
        MattermostClient(
            base_url="http://mattermost.example.test",
            token=SecretStr("token"),
            bot_user_id=BOT_ID,
        )
    client = MattermostClient(
        base_url="http://mattermost.example.test",
        token=SecretStr("token"),
        bot_user_id=BOT_ID,
        allow_insecure_http=True,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    assert not client.is_closed
