from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx
from pydantic import SecretStr

from app.integrations.mattermost.ids import validate_mattermost_id
from app.integrations.mattermost.urls import validate_http_url

MAX_RESPONSE_BYTES = 64 * 1024
MAX_PROPS_BYTES = 32 * 1024
MAX_MESSAGE_CHARACTERS = 16_383


class MattermostClientError(RuntimeError):
    retryable = False

    def __init__(
        self,
        *,
        error_type: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.error_type = error_type
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Mattermost request failed")


class MattermostRetryableError(MattermostClientError):
    retryable = True


class MattermostPermanentError(MattermostClientError):
    retryable = False


def _validate_id(value: str, *, name: str) -> str:
    return validate_mattermost_id(value, name=name)


def _base_origin(value: str, *, allow_insecure_http: bool) -> str:
    return validate_http_url(
        value,
        name="Mattermost base URL",
        allow_insecure_http=allow_insecure_http,
    )


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is None or not raw.isascii() or not raw.isdecimal():
        return None
    return max(1, min(int(raw), 300))


class MattermostClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: SecretStr,
        bot_user_id: str,
        allow_insecure_http: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(token, SecretStr) or not token.get_secret_value():
            raise ValueError("Mattermost Bot token is missing")
        self._base_url = _base_origin(
            base_url,
            allow_insecure_http=allow_insecure_http,
        )
        self._bot_user_id = _validate_id(bot_user_id, name="bot_user_id")
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
            follow_redirects=False,
            transport=transport,
        )

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_direct_post(
        self,
        *,
        recipient_user_id: str,
        message: str,
        props: dict[str, object],
    ) -> str:
        channel_id = await self.create_direct_channel(recipient_user_id)
        return await self.create_post(channel_id, message, props)

    async def create_direct_channel(self, recipient_user_id: str) -> str:
        recipient_user_id = _validate_id(recipient_user_id, name="recipient_user_id")
        channel = await self._request_object(
            "/api/v4/channels/direct",
            [self._bot_user_id, recipient_user_id],
        )
        return _response_id(channel)

    async def create_post(
        self,
        channel_id: str,
        message: str,
        props: dict[str, object],
    ) -> str:
        channel_id = _validate_id(channel_id, name="channel_id")
        if not isinstance(message, str) or not 1 <= len(message) <= MAX_MESSAGE_CHARACTERS:
            raise ValueError("message is outside Mattermost bounds")
        if not isinstance(props, dict):
            raise TypeError("props must be an object")
        props_bytes = json.dumps(
            props,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(props_bytes) > MAX_PROPS_BYTES:
            raise ValueError("props are outside Mattermost bounds")
        post = await self._request_object(
            "/api/v4/posts",
            {"channel_id": channel_id, "message": message, "props": props},
        )
        return _response_id(post)

    async def _request_object(self, path: str, payload: object) -> dict[str, Any]:
        try:
            async with self._client.stream(
                "POST",
                self._base_url + path,
                json=payload,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    _raise_status(response)
                content = await _bounded_body(response)
        except MattermostClientError:
            raise
        except httpx.HTTPError:
            raise MattermostRetryableError(error_type="network") from None
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MattermostPermanentError(error_type="protocol") from None
        if not isinstance(decoded, dict):
            raise MattermostPermanentError(error_type="protocol")
        return decoded


async def _bounded_body(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length")
    if (
        length is not None
        and length.isascii()
        and length.isdecimal()
        and int(length) > MAX_RESPONSE_BYTES
    ):
        raise MattermostPermanentError(error_type="response_too_large")
    chunks: list[bytes] = []
    size = 0
    iterator: AsyncIterator[bytes] = response.aiter_bytes()
    async for chunk in iterator:
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise MattermostPermanentError(error_type="response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_status(response: httpx.Response) -> None:
    status = response.status_code
    error_type = "rate_limited" if status == 429 else "upstream"
    if status in {408, 429} or 500 <= status <= 599:
        raise MattermostRetryableError(
            error_type=error_type,
            retry_after_seconds=_retry_after(response),
        )
    raise MattermostPermanentError(
        error_type="authentication" if status in {401, 403} else "protocol"
    )


def _response_id(payload: dict[str, Any]) -> str:
    value = payload.get("id")
    if not isinstance(value, str):
        raise MattermostPermanentError(error_type="protocol")
    try:
        validate_mattermost_id(value)
    except (TypeError, ValueError):
        raise MattermostPermanentError(error_type="protocol")
    return value
