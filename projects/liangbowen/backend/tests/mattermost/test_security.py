from __future__ import annotations

import hmac
from urllib.parse import urlencode

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.integrations.mattermost.schemas import MattermostRequest
from app.integrations.mattermost.security import (
    MattermostAuthenticationError,
    MattermostTransportError,
    request_hash,
    verify_and_parse_form,
)


def _fields(**overrides: str) -> dict[str, str]:
    values = {
        "token": "slash-secret",
        "team_id": "team-1",
        "team_domain": "course",
        "channel_id": "channel-1",
        "channel_name": "data-structures",
        "user_id": "mm-user-1",
        "user_name": "teacher-mm",
        "command": "/hw",
        "text": 'publish --title "图" --due "2026-07-30 18:00" --question "解释"',
        "trigger_id": "trigger-1",
        "response_url": "https://mattermost.test/hooks/response",
    }
    values.update(overrides)
    return values


def _body(**overrides: str) -> bytes:
    return urlencode(_fields(**overrides)).encode()


@pytest.mark.parametrize(
    "content_type",
    [
        "application/x-www-form-urlencoded",
        "application/x-www-form-urlencoded; charset=UTF-8",
        'Application/X-Www-Form-Urlencoded ; charset = "utf-8"',
    ],
)
def test_valid_form_returns_a_secret_free_bounded_request(content_type: str) -> None:
    request = verify_and_parse_form(
        _body(),
        content_type,
        SecretStr("slash-secret"),
    )

    assert request.user_id == "mm-user-1"
    assert request.text.startswith("publish")
    assert "token" not in request.model_dump()
    assert "slash-secret" not in repr(request)


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "text/plain",
        "application/x-www-form-urlencoded; charset=gbk",
        "application/x-www-form-urlencoded; boundary=x",
        "application/x-www-form-urlencoded; charset=utf-8; charset=utf-8",
        'application/x-www-form-urlencoded; charset="utf-8',
        'application/x-www-form-urlencoded; charset=utf-8"',
        "application/x-www-form-urlencoded; charset=utf@8",
        "application/x-www-form-urlencoded; char(set)=utf-8",
        "application/x-www-form-urlencoded; charset==utf-8",
        "application/x-www-form-urlencoded; charset=utf-8 trailing",
        "application/x-www-form-urlencoded; charset=utf-8\r\nX-Evil: 1",
        "",
    ],
)
def test_only_utf8_form_content_type_is_accepted(content_type: str) -> None:
    with pytest.raises(MattermostTransportError) as raised:
        verify_and_parse_form(_body(), content_type, SecretStr("slash-secret"))

    assert raised.value.status_code == 415


@pytest.mark.parametrize(
    "body",
    [
        b"token=slash-secret&team_id=%ZZ",
        b"token=slash-secret&team_id=%A",
        b"token=slash-secret&team_id=\xff",
        b"token=slash-secret&team_id",
    ],
)
def test_malformed_form_encoding_is_rejected(body: bytes) -> None:
    with pytest.raises(MattermostTransportError) as raised:
        verify_and_parse_form(
            body,
            "application/x-www-form-urlencoded",
            SecretStr("slash-secret"),
        )

    assert raised.value.status_code == 400
    assert len(raised.value.public_message) < 200


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            urlencode({key: value for key, value in _fields().items() if key != "token"}).encode(),
            SecretStr("slash-secret"),
        ),
        (_body(token="wrong"), SecretStr("slash-secret")),
        (_body(token=""), SecretStr("slash-secret")),
        (_body(), None),
        (
            b"token=slash-secret&token=slash-secret&team_id=team",
            SecretStr("slash-secret"),
        ),
    ],
)
def test_missing_duplicate_or_wrong_token_has_one_uniform_401(
    body: bytes,
    expected: SecretStr | None,
) -> None:
    with pytest.raises(MattermostAuthenticationError) as raised:
        verify_and_parse_form(
            body,
            "application/x-www-form-urlencoded",
            expected,
        )

    assert raised.value.status_code == 401
    assert raised.value.public_message == "Mattermost 请求认证失败。"
    assert "slash-secret" not in str(raised.value)


def test_token_uses_compare_digest_before_other_field_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = hmac.compare_digest

    def observed_compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr(
        "app.integrations.mattermost.security.hmac.compare_digest",
        observed_compare,
    )
    invalid_after_auth = urlencode(_fields(team_id="")).encode()

    with pytest.raises(MattermostTransportError, match="team_id"):
        verify_and_parse_form(
            invalid_after_auth,
            "application/x-www-form-urlencoded",
            SecretStr("slash-secret"),
        )

    assert calls == [(b"slash-secret", b"slash-secret")]


@pytest.mark.parametrize(
    "body",
    [
        urlencode([*_fields().items(), ("text", "duplicate")]).encode(),
        urlencode({**_fields(), "unknown": "value"}).encode(),
        _body(team_id="team\0id"),
        _body(trigger_id=""),
        _body(response_url="x" * 2_049),
        _body(text="x" * (60 * 1024 + 1)),
    ],
)
def test_authenticated_form_rejects_duplicate_unknown_nul_and_bounds(body: bytes) -> None:
    with pytest.raises(MattermostTransportError) as raised:
        verify_and_parse_form(
            body,
            "application/x-www-form-urlencoded",
            SecretStr("slash-secret"),
        )

    assert raised.value.status_code == 400


def test_total_form_body_is_limited_to_64_kib() -> None:
    with pytest.raises(MattermostTransportError) as raised:
        verify_and_parse_form(
            b"x" * (64 * 1024 + 1),
            "application/x-www-form-urlencoded",
            SecretStr("slash-secret"),
        )

    assert raised.value.status_code == 413


def test_request_hash_uses_secret_free_length_framing_and_trigger_identity() -> None:
    first = MattermostRequest.model_validate(
        {
            key: value
            for key, value in _fields(team_id="ab", channel_id="c").items()
            if key != "token"
        }
    )
    concatenation_collision = MattermostRequest.model_validate(
        {
            key: value
            for key, value in _fields(team_id="a", channel_id="bc").items()
            if key != "token"
        }
    )
    new_trigger = MattermostRequest.model_validate(
        {
            key: value
            for key, value in _fields(team_id="ab", channel_id="c", trigger_id="trigger-2").items()
            if key != "token"
        }
    )

    assert request_hash(first) != request_hash(concatenation_collision)
    assert request_hash(first) != request_hash(new_trigger)
    assert len(request_hash(first)) == 64
    assert request_hash(first).isalnum()


def test_mattermost_secrets_are_optional_hidden_and_fail_closed() -> None:
    missing = Settings(
        mattermost_command_token="",
        mattermost_demo_setup_key="   ",
    )
    assert missing.mattermost_command_token is None
    assert missing.mattermost_demo_setup_key is None

    configured = Settings(
        mattermost_command_token="command-secret-value",
        mattermost_demo_setup_key="demo-secret-value",
    )
    assert "command-secret-value" not in repr(configured)
    assert "demo-secret-value" not in repr(configured)

    with pytest.raises(ValidationError) as raised:
        Settings(mattermost_command_token="secret\0value")
    assert "secret\0value" not in str(raised.value)
    assert "secret\\x00value" not in str(raised.value)


def test_a8_bot_token_setting_is_optional_and_secret() -> None:
    assert Settings(_env_file=None, mattermost_bot_token="").mattermost_bot_token is None
    configured = Settings(_env_file=None, mattermost_bot_token="bot-secret-value")
    assert "bot-secret-value" not in repr(configured)
