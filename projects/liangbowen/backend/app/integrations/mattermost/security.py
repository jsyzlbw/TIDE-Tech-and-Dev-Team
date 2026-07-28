from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import parse_qsl

from pydantic import SecretStr, ValidationError

from app.integrations.mattermost.schemas import MattermostRequest

MAX_FORM_BYTES = 64 * 1024
MAX_FORM_FIELDS = 12
ALLOWED_FIELDS = frozenset(
    {
        "token",
        "team_id",
        "team_domain",
        "channel_id",
        "channel_name",
        "user_id",
        "user_name",
        "command",
        "text",
        "trigger_id",
        "response_url",
    }
)
PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
FORM_CONTENT_TYPE = re.compile(
    r'\Aapplication/x-www-form-urlencoded(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?[ \t]*\Z',
    re.IGNORECASE,
)


class MattermostTransportError(ValueError):
    def __init__(self, status_code: int, public_message: str) -> None:
        self.status_code = status_code
        self.public_message = public_message
        super().__init__(public_message)


class MattermostAuthenticationError(MattermostTransportError):
    def __init__(self) -> None:
        super().__init__(401, "Mattermost 请求认证失败。")


def _validate_content_type(content_type: str) -> None:
    if not isinstance(content_type, str) or FORM_CONTENT_TYPE.fullmatch(content_type) is None:
        raise MattermostTransportError(415, "请求必须使用 UTF-8 表单格式。")


def _validate_percent_escapes(raw: str) -> None:
    offset = 0
    while True:
        index = raw.find("%", offset)
        if index < 0:
            return
        if PERCENT_ESCAPE.fullmatch(raw[index : index + 3]) is None:
            raise MattermostTransportError(400, "表单编码无效。")
        offset = index + 3


def _decode_pairs(body: bytes) -> list[tuple[str, str]]:
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    if len(body) > MAX_FORM_BYTES:
        raise MattermostTransportError(413, "请求内容过大。")
    try:
        raw = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise MattermostTransportError(400, "表单编码无效。") from None
    _validate_percent_escapes(raw)
    try:
        return parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=64,
        )
    except (UnicodeDecodeError, ValueError):
        raise MattermostTransportError(400, "表单编码无效。") from None


def _verify_token(
    pairs: list[tuple[str, str]],
    expected_token: SecretStr | None,
) -> None:
    received = [value for key, value in pairs if key == "token"]
    if len(received) != 1 or expected_token is None:
        raise MattermostAuthenticationError()
    try:
        matches = hmac.compare_digest(
            received[0].encode("utf-8"),
            expected_token.get_secret_value().encode("utf-8"),
        )
    except UnicodeError:
        matches = False
    if not matches:
        raise MattermostAuthenticationError()


def _validated_fields(pairs: list[tuple[str, str]]) -> dict[str, str]:
    if len(pairs) > MAX_FORM_FIELDS:
        raise MattermostTransportError(400, "表单字段无效。")
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key not in ALLOWED_FIELDS:
            raise MattermostTransportError(400, "表单包含未知字段。")
        if key in fields:
            raise MattermostTransportError(400, f"表单字段 {key} 重复。")
        fields[key] = value
    fields.pop("token", None)
    try:
        request = MattermostRequest.model_validate(fields)
    except ValidationError as exc:
        location = exc.errors()[0].get("loc", ("field",))
        field = str(location[0]) if location else "field"
        raise MattermostTransportError(400, f"表单字段 {field} 无效。") from None
    return request.model_dump()


def verify_and_parse_form(
    body: bytes,
    content_type: str,
    expected_token: SecretStr | None,
) -> MattermostRequest:
    _validate_content_type(content_type)
    pairs = _decode_pairs(body)
    _verify_token(pairs, expected_token)
    return MattermostRequest.model_validate(_validated_fields(pairs))


def request_hash(request: MattermostRequest) -> str:
    digest = hashlib.sha256()
    digest.update(b"mattermost-command-v1")
    for value in (
        request.team_id,
        request.channel_id,
        request.user_id,
        request.command,
        request.text,
        request.trigger_id,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()
