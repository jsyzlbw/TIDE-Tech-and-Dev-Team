from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.mattermost.urls import MAX_HTTP_URL_BYTES


def test_mattermost_delivery_settings_normalize_empty_values() -> None:
    settings = Settings(
        _env_file=None,
        mattermost_url="  ",
        mattermost_bot_token="",
        mattermost_bot_user_id="",
        mattermost_action_secret="",
        mattermost_action_url="",
        web_console_url="",
    )
    assert settings.mattermost_url is None
    assert settings.mattermost_bot_token is None
    assert settings.mattermost_bot_user_id is None
    assert settings.mattermost_action_secret is None
    assert settings.mattermost_action_url is None
    assert settings.web_console_url is None


def test_production_mattermost_urls_require_https_and_no_credentials() -> None:
    common = {
        "_env_file": None,
        "app_env": "production",
        "jwt_secret": "x" * 32,
    }
    for field in ("mattermost_url", "mattermost_action_url", "web_console_url"):
        with pytest.raises(ValidationError):
            Settings(**common, **{field: "http://example.test"})
        with pytest.raises(ValidationError):
            Settings(**common, **{field: "https://user:pass@example.test"})


def test_mattermost_delivery_secrets_reject_nul_without_echo() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, mattermost_action_secret="do-not-echo\0value")
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("field", ["mattermost_url", "mattermost_action_url", "web_console_url"])
def test_mattermost_urls_reject_nul_without_echo(field: str) -> None:
    secretish = "https://example.test/do-not-echo\0value"
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{field: secretish})
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("control", ["\x01", "\x1f", "\x7f", "\x85"])
@pytest.mark.parametrize("field", ["mattermost_url", "mattermost_action_url", "web_console_url"])
def test_mattermost_urls_reject_all_controls_without_echo(field: str, control: str) -> None:
    suffix = (
        "/api/v1/integrations/mattermost/actions" if field == "mattermost_action_url" else "/path"
    )
    secretish = f"https://example.test/{control}do-not-echo{suffix}"
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{field: secretish})
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("case", ["userinfo", "query", "fragment", "length"])
@pytest.mark.parametrize("field", ["mattermost_url", "mattermost_action_url", "web_console_url"])
def test_mattermost_urls_reject_every_unsafe_component_without_echo(
    field: str,
    case: str,
) -> None:
    suffix = (
        "/api/v1/integrations/mattermost/actions" if field == "mattermost_action_url" else "/path"
    )
    values = {
        "userinfo": f"https://do-not-echo:password@example.test{suffix}",
        "query": f"https://example.test{suffix}?token=do-not-echo",
        "fragment": f"https://example.test{suffix}#do-not-echo",
        "length": f"https://example.test/do-not-echo{'x' * 2_048}{suffix}",
    }
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{field: values[case]})
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("bot_user_id", ["bad/id", "_bad-leading"])
def test_mattermost_bot_user_id_uses_shared_identifier_contract(bot_user_id: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mattermost_bot_user_id=bot_user_id)


def test_conftest_isolates_every_a8_environment_variable() -> None:
    import conftest

    assert {
        "mattermost_url",
        "mattermost_bot_token",
        "mattermost_bot_user_id",
        "mattermost_action_secret",
        "mattermost_action_url",
        "web_console_url",
    } <= conftest.SETTING_ENV_NAMES


def test_development_accepts_local_http_origins() -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        mattermost_url="http://mattermost:8065",
        mattermost_action_url=("http://api:8000/api/v1/integrations/mattermost/actions"),
        web_console_url="http://localhost:5173",
    )
    assert settings.mattermost_url == "http://mattermost:8065"


@pytest.mark.parametrize(
    ("app_env", "accepted"),
    [
        ("development", True),
        ("test", True),
        ("staging", False),
        ("production", False),
    ],
)
def test_mattermost_http_policy_is_environment_dependent(
    app_env: str,
    accepted: bool,
) -> None:
    values = {
        "_env_file": None,
        "app_env": app_env,
        "jwt_secret": "x" * 32,
        "mattermost_url": "http://mattermost.test",
    }
    if accepted:
        assert Settings(**values).mattermost_url == "http://mattermost.test"
    else:
        with pytest.raises(ValidationError):
            Settings(**values)


@pytest.mark.parametrize("field", ["mattermost_url", "mattermost_action_url", "web_console_url"])
def test_mattermost_url_limit_counts_utf8_bytes_without_echo(field: str) -> None:
    suffix = "/api/v1/integrations/mattermost/actions" if field == "mattermost_action_url" else ""
    secretish = "https://example.test/" + "🚫" * 1_980 + "do-not-echo" + suffix
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{field: secretish})
    assert "do-not-echo" not in str(caught.value)


def test_ascii_urls_at_the_shared_byte_limit_remain_valid() -> None:
    prefix = "https://example.test/"
    action_suffix = "/api/v1/integrations/mattermost/actions"
    action_url = (
        prefix + "a" * (MAX_HTTP_URL_BYTES - len(prefix) - len(action_suffix)) + action_suffix
    )
    console_url = prefix + "c" * (MAX_HTTP_URL_BYTES - len(prefix))
    settings = Settings(
        _env_file=None,
        mattermost_action_url=action_url,
        web_console_url=console_url,
    )
    assert len(settings.mattermost_action_url.encode("utf-8")) == MAX_HTTP_URL_BYTES
    assert len(settings.web_console_url.encode("utf-8")) == MAX_HTTP_URL_BYTES


def test_settings_reject_card_url_combination_that_exceeds_json_budget_without_echo() -> None:
    prefix = "https://example.test/"
    action_suffix = "/api/v1/integrations/mattermost/actions"
    action_url = (
        prefix + "\\" * (MAX_HTTP_URL_BYTES - len(prefix) - len(action_suffix)) + action_suffix
    )
    console_url = (
        prefix + "\\" * (MAX_HTTP_URL_BYTES - len(prefix) - len("do-not-echo")) + "do-not-echo"
    )
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            mattermost_action_url=action_url,
            web_console_url=console_url,
        )
    assert "do-not-echo" not in str(caught.value)
