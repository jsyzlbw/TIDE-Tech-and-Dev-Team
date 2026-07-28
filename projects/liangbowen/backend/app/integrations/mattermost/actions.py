from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from app.integrations.mattermost.ids import validate_mattermost_id

MAX_ACTION_BODY_BYTES = 16 * 1024
_CONTENT_TYPE = re.compile(
    r'\Aapplication/json(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?[ \t]*\Z',
    re.IGNORECASE,
)
_SIGNATURE = re.compile(r"\A[0-9a-f]{64}\Z")
MattermostAction = Literal["confirm", "reevaluate", "openreport"]


class ActionTransportError(ValueError):
    def __init__(self, status_code: int, public_message: str) -> None:
        self.status_code = status_code
        self.public_message = public_message
        super().__init__(public_message)


class ActionAuthenticationError(ActionTransportError):
    def __init__(self) -> None:
        super().__init__(401, "Mattermost 操作认证失败。")


class ActionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: MattermostAction
    report_id: uuid.UUID
    delivery_id: uuid.UUID
    expected_user_id: Annotated[str, Field(min_length=1, max_length=128)]
    expected_channel_id: Annotated[str, Field(min_length=1, max_length=128)]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("expected_user_id", "expected_channel_id")
    @classmethod
    def validate_expected_id(cls, value: str) -> str:
        return validate_mattermost_id(value)


class MattermostActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: Annotated[str, Field(min_length=1, max_length=128)]
    post_id: Annotated[str, Field(min_length=1, max_length=128)]
    channel_id: Annotated[str, Field(min_length=1, max_length=128)]
    team_id: Annotated[str, Field(max_length=128)]
    context: ActionContext

    @field_validator("user_id", "post_id", "channel_id")
    @classmethod
    def validate_safe_id(cls, value: str) -> str:
        return validate_mattermost_id(value)

    @field_validator("team_id")
    @classmethod
    def validate_optional_team_id(cls, value: str) -> str:
        if value:
            raise ValueError("direct-message action team_id must be empty")
        return value


def _secret_bytes(secret: SecretStr | None) -> bytes:
    if secret is None or not secret.get_secret_value():
        raise ActionAuthenticationError()
    return secret.get_secret_value().encode("utf-8")


def _framed_digest(domain: bytes, values: tuple[str, ...]) -> hashlib._Hash:
    digest = hashlib.sha256()
    digest.update(domain)
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big", signed=False))
        digest.update(encoded)
    return digest


def sign_action(
    action: MattermostAction,
    report_id: uuid.UUID,
    delivery_id: uuid.UUID,
    expected_user_id: str,
    expected_channel_id: str,
    secret: SecretStr,
) -> str:
    if action not in {"confirm", "reevaluate", "openreport"}:
        raise ValueError("unsupported Mattermost action")
    if not isinstance(report_id, uuid.UUID):
        raise TypeError("report_id must be a UUID")
    if not isinstance(delivery_id, uuid.UUID):
        raise TypeError("delivery_id must be a UUID")
    expected_user_id = validate_mattermost_id(expected_user_id, name="expected_user_id")
    expected_channel_id = validate_mattermost_id(
        expected_channel_id,
        name="expected_channel_id",
    )
    payload = _framed_digest(
        b"mattermost-action-signature-v2",
        (
            action,
            str(report_id),
            str(delivery_id),
            expected_user_id,
            expected_channel_id,
        ),
    ).digest()
    return hmac.new(_secret_bytes(secret), payload, hashlib.sha256).hexdigest()


def verify_action_signature(context: ActionContext, secret: SecretStr | None) -> None:
    if not isinstance(context, ActionContext):
        raise TypeError("context must be an ActionContext")
    try:
        expected = sign_action(
            context.action,
            context.report_id,
            context.delivery_id,
            context.expected_user_id,
            context.expected_channel_id,
            secret,  # type: ignore[arg-type]
        )
        matches = hmac.compare_digest(context.signature.encode("ascii"), expected.encode("ascii"))
    except (UnicodeError, ActionAuthenticationError):
        matches = False
    if not matches:
        raise ActionAuthenticationError()


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_and_verify_action(
    body: bytes,
    content_type: str,
    secret: SecretStr | None,
) -> MattermostActionRequest:
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    if _CONTENT_TYPE.fullmatch(content_type) is None:
        raise ActionTransportError(415, "请求必须使用 UTF-8 JSON 格式。")
    if len(body) > MAX_ACTION_BODY_BYTES:
        raise ActionTransportError(413, "请求内容过大。")
    try:
        raw = body.decode("utf-8", errors="strict")
        decoded = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        request = MattermostActionRequest.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        raise ActionTransportError(400, "Mattermost 操作请求无效。") from None
    verify_action_signature(request.context, secret)
    if not hmac.compare_digest(request.user_id, request.context.expected_user_id):
        raise ActionAuthenticationError()
    if not hmac.compare_digest(request.channel_id, request.context.expected_channel_id):
        raise ActionAuthenticationError()
    return request


def action_request_hash(request: MattermostActionRequest) -> str:
    if not isinstance(request, MattermostActionRequest):
        raise TypeError("request must be a MattermostActionRequest")
    return _framed_digest(
        b"mattermost-interactive-action-v1",
        (
            request.user_id,
            request.post_id,
            request.channel_id,
            request.team_id,
            request.context.action,
            str(request.context.report_id),
            str(request.context.delivery_id),
        ),
    ).hexdigest()
