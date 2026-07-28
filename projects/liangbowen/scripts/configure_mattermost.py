from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_MATTERMOST_PORT: Final = 8065
DEFAULT_API_PORT: Final = 8000
DEFAULT_API_ORIGIN: Final = "http://api:8000"
TEAM_NAME: Final = "data-structures"
CHANNEL_NAME: Final = "course-home"
MATTERMOST_ID = re.compile(r"^[a-z0-9]{26}$")
SAFE_ACCOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Verified against Mattermost v10.5.2 and v10.5.14 server contracts in app/user.go,
# app/team.go, app/channel.go, app/command.go, store/constants.go, and sqlstore/team_store.go.
USER_EXISTS_CODES: Final = frozenset(
    {
        "app.user.save.email_exists.app_error",
        "app.user.save.username_exists.app_error",
    }
)
TEAM_EXISTS_CODES: Final = frozenset(
    {
        "app.team.save.existing.app_error",
        "store.sql_team.save_team.existing.app_error",
    }
)
CHANNEL_EXISTS_CODES: Final = frozenset(
    {
        "store.sql_channel.save_channel.exists.app_error",
        "store.sql_channel.save_channel.existing.app_error",
    }
)
MEMBERSHIP_EXISTS_CODES: Final = frozenset(
    {
        "app.channel.save_member.exists.app_error",
        "store.sql_team.save_member.exists.app_error",
    }
)
COMMAND_EXISTS_CODES: Final = frozenset({"api.command.duplicate_trigger.app_error"})


class ConfigurationError(RuntimeError):
    """A safe configuration-contract failure suitable for a redacted CLI boundary."""


class _AlreadyExists:
    def __init__(self, error_id: str) -> None:
        self.error_id = error_id


JsonValue = dict[str, Any] | list[Any]


def desired_users() -> list[dict[str, str]]:
    return [
        {
            "email": "teacher@example.com",
            "username": "teacher",
            "password": "Teacher123!",
        },
        {
            "email": "student1@example.com",
            "username": "student1",
            "password": "Student123!",
        },
        {
            "email": "student2@example.com",
            "username": "student2",
            "password": "Student123!",
        },
        {
            "email": "student3@example.com",
            "username": "student3",
            "password": "Student123!",
        },
    ]


def desired_commands(api_origin: str) -> list[dict[str, object]]:
    origin = validate_origin(api_origin, name="API origin")
    return [
        {
            "trigger": "hw",
            "display_name": "AI 作业评审",
            "description": "发布、查询、提交、汇总和评估作业",
            "auto_complete": True,
            "auto_complete_hint": "help | publish | list | show | submit | summary | evaluate",
            "method": "P",
            "url": f"{origin}/api/v1/integrations/mattermost/commands",
        }
    ]


def validate_origin(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError(f"{name} must be a non-empty HTTP(S) origin")
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise ConfigurationError(f"{name} contains unsafe characters")
    if "\\" in value or "%" in value:
        raise ConfigurationError(f"{name} contains ambiguous URL syntax")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{name} is invalid") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ConfigurationError(f"{name} must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{name} must not contain credentials")
    if (
        parsed.hostname is None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ConfigurationError(
            f"{name} must be an origin without path, query, or fragment"
        )
    if not parsed.hostname.isascii() or not re.fullmatch(
        r"[A-Za-z0-9.:-]+", parsed.hostname
    ):
        raise ConfigurationError(f"{name} contains an invalid host")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError(f"{name} contains an invalid port")
    netloc = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _url(origin: str, path: str) -> str:
    if not path.startswith("/"):
        raise AssertionError("internal API paths must be absolute")
    return f"{origin}{path}"


def _response_json(response: httpx.Response) -> JsonValue:
    try:
        payload = response.json()
    except (ValueError, UnicodeError) as exc:
        raise ConfigurationError("remote service returned invalid JSON") from exc
    if not isinstance(payload, (dict, list)):
        raise ConfigurationError("remote service returned an invalid JSON shape")
    return payload


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    json_body: Mapping[str, object] | None = None,
    params: Mapping[str, str] | None = None,
    allow_not_found: bool = False,
    already_exists_codes: frozenset[str] = frozenset(),
    headers: Mapping[str, str] | None = None,
) -> JsonValue | _AlreadyExists | None:
    response = client.request(
        method,
        url,
        json=dict(json_body) if json_body is not None else None,
        params=params,
        headers=headers,
    )
    if response.status_code in {200, 201}:
        return _response_json(response)
    if response.status_code == 404 and allow_not_found:
        return None
    if response.status_code == 400 and already_exists_codes:
        payload = _response_json(response)
        error_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(error_id, str) and error_id in already_exists_codes:
            return _AlreadyExists(error_id)
    response.raise_for_status()
    raise AssertionError("raise_for_status returned for a failed response")


def _object(payload: object, *, resource: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{resource} response has an invalid shape")
    return payload


def _resource_id(payload: object, *, resource: str) -> str:
    value = _object(payload, resource=resource).get("id")
    if not isinstance(value, str) or not MATTERMOST_ID.fullmatch(value):
        raise ConfigurationError(f"invalid {resource} id")
    return value


def _restore_authorization(client: httpx.Client, value: str | None) -> None:
    if value is None:
        client.headers.pop("Authorization", None)
    else:
        client.headers["Authorization"] = value


def poll_until_ready(
    client: httpx.Client,
    base_url: str,
    *,
    max_wait: float = 120,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    origin = validate_origin(base_url, name="base URL")
    if (
        isinstance(max_wait, bool)
        or not isinstance(max_wait, (int, float))
        or not math.isfinite(max_wait)
        or max_wait <= 0
    ):
        raise ConfigurationError("Mattermost readiness timeout must be positive")
    deadline = clock() + max_wait
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            response = client.get(
                _url(origin, "/api/v4/system/ping"),
                timeout=min(10.0, remaining),
            )
            if response.status_code in {200, 201}:
                _response_json(response)
                return
        except httpx.TransportError:
            pass
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(1.0, remaining))
    raise ConfigurationError(
        f"Mattermost did not become ready within {max_wait:g} seconds"
    )


def _login(
    client: httpx.Client,
    base_url: str,
    *,
    username: str,
    password: str,
) -> str | None:
    response = client.post(
        _url(base_url, "/api/v4/users/login"),
        json={"login_id": username, "password": password},
    )
    if response.status_code == 401:
        return None
    if response.status_code not in {200, 201}:
        response.raise_for_status()
    _response_json(response)
    token = response.headers.get("Token")
    if (
        not token
        or "\0" in token
        or "\r" in token
        or "\n" in token
        or len(token) > 4096
    ):
        raise ConfigurationError("Mattermost login omitted a valid token header")
    return token


def _authenticate_admin(
    client: httpx.Client,
    base_url: str,
    *,
    username: str,
    email: str,
    password: str,
) -> str:
    token = _login(client, base_url, username=username, password=password)
    if token is not None:
        return token
    created = _request_json(
        client,
        "POST",
        _url(base_url, "/api/v4/users"),
        json_body={"username": username, "email": email, "password": password},
        already_exists_codes=USER_EXISTS_CODES,
    )
    if isinstance(created, _AlreadyExists):
        if created.error_id == "app.user.save.username_exists.app_error":
            raise ConfigurationError(
                "administrator username is already occupied and credentials were rejected"
            )
        raise ConfigurationError(
            "administrator email is already occupied by another account"
        )
    _resource_id(created, resource="administrator")
    token = _login(client, base_url, username=username, password=password)
    if token is None:
        raise ConfigurationError("administrator login failed after bootstrap")
    return token


def _get_or_create(
    client: httpx.Client,
    *,
    get_url: str,
    create_url: str,
    create_body: Mapping[str, object],
    resource: str,
    exists_codes: frozenset[str],
) -> dict[str, Any]:
    current = _request_json(client, "GET", get_url, allow_not_found=True)
    if current is not None:
        return _object(current, resource=resource)
    created = _request_json(
        client,
        "POST",
        create_url,
        json_body=create_body,
        already_exists_codes=exists_codes,
    )
    if isinstance(created, _AlreadyExists):
        current = _request_json(client, "GET", get_url, allow_not_found=True)
        if current is None:
            raise ConfigurationError(
                f"{resource} create raced but the resource is unavailable"
            )
        return _object(current, resource=resource)
    return _object(created, resource=resource)


def _ensure_membership(
    client: httpx.Client,
    *,
    get_url: str,
    create_url: str,
    body: Mapping[str, object],
    resource: str,
) -> None:
    current = _request_json(client, "GET", get_url, allow_not_found=True)
    if current is not None:
        _object(current, resource=resource)
        return
    result = _request_json(
        client,
        "POST",
        create_url,
        json_body=body,
        already_exists_codes=MEMBERSHIP_EXISTS_CODES,
    )
    if not isinstance(result, _AlreadyExists):
        _object(result, resource=resource)


def _find_command(
    client: httpx.Client,
    *,
    base_url: str,
    team_id: str,
    trigger: object,
) -> dict[str, Any] | None:
    payload = _request_json(
        client,
        "GET",
        _url(base_url, "/api/v4/commands"),
        params={"team_id": team_id, "custom_only": "true"},
    )
    if not isinstance(payload, list):
        raise ConfigurationError("command list response has an invalid shape")
    matches = [
        entry
        for entry in payload
        if isinstance(entry, dict) and entry.get("trigger") == trigger
    ]
    if len(matches) > 1:
        raise ConfigurationError("multiple slash commands use the desired trigger")
    return matches[0] if matches else None


def _validate_command_token(value: object) -> str:
    if not isinstance(value, str) or MATTERMOST_ID.fullmatch(value) is None:
        raise ConfigurationError(
            "MATTERMOST_COMMAND_TOKEN must be a valid 26-character Mattermost ID"
        )
    return value


def _verify_command_token(payload: object, expected_token: str) -> None:
    actual_token = _object(payload, resource="command").get("token")
    if (
        not isinstance(actual_token, str)
        or MATTERMOST_ID.fullmatch(actual_token) is None
    ):
        raise ConfigurationError("existing slash command token is invalid")
    if not hmac.compare_digest(actual_token.encode(), expected_token.encode()):
        raise ConfigurationError(
            "existing slash command token does not match MATTERMOST_COMMAND_TOKEN"
        )


def _ensure_command(
    client: httpx.Client,
    *,
    base_url: str,
    team_id: str,
    desired: Mapping[str, object],
    command_token: str,
) -> str:
    existing = _find_command(
        client,
        base_url=base_url,
        team_id=team_id,
        trigger=desired["trigger"],
    )
    body = {"team_id": team_id, **desired}
    if existing is None:
        created = _request_json(
            client,
            "POST",
            _url(base_url, "/api/v4/commands"),
            json_body={**body, "token": command_token},
            already_exists_codes=COMMAND_EXISTS_CODES,
        )
        if isinstance(created, _AlreadyExists):
            existing = _find_command(
                client,
                base_url=base_url,
                team_id=team_id,
                trigger=desired["trigger"],
            )
            if existing is None:
                raise ConfigurationError(
                    "slash command create raced but the command is unavailable"
                )
        else:
            _verify_command_token(created, command_token)
            return _resource_id(created, resource="command")

    _verify_command_token(existing, command_token)
    command_id = _resource_id(existing, resource="command")
    compared_fields = (
        "team_id",
        "trigger",
        "url",
        "display_name",
        "description",
        "auto_complete",
        "auto_complete_hint",
        "method",
    )
    if any(existing.get(field) != body.get(field) for field in compared_fields):
        updated_body = {"id": command_id, **body}
        updated = _request_json(
            client,
            "PUT",
            _url(base_url, f"/api/v4/commands/{command_id}"),
            json_body=updated_body,
        )
        updated_id = _resource_id(updated, resource="command")
        if updated_id != command_id:
            raise ConfigurationError("command update returned a different id")
        _verify_command_token(updated, command_token)
    return command_id


def configure_mattermost(
    client: httpx.Client,
    *,
    setup_client: httpx.Client,
    base_url: str,
    api_origin: str,
    setup_origin: str,
    admin_username: str,
    admin_email: str,
    admin_password: str,
    setup_key: str,
    command_token: str,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    base_url = validate_origin(base_url, name="base URL")
    api_origin = validate_origin(api_origin, name="API origin")
    setup_origin = validate_origin(setup_origin, name="setup origin")
    if not SAFE_ACCOUNT_NAME.fullmatch(admin_username):
        raise ConfigurationError("MATTERMOST_ADMIN_USERNAME is invalid")
    if (
        "@" not in admin_email
        or admin_email != admin_email.strip()
        or "\0" in admin_email
    ):
        raise ConfigurationError("MATTERMOST_ADMIN_EMAIL is invalid")
    if not admin_password or "\0" in admin_password or len(admin_password) > 1024:
        raise ConfigurationError("MATTERMOST_ADMIN_PASSWORD is invalid")
    if not setup_key or "\0" in setup_key or len(setup_key) > 4096:
        raise ConfigurationError("MATTERMOST_DEMO_SETUP_KEY is invalid")
    command_token = _validate_command_token(command_token)
    if (
        setup_client.headers.get("Authorization") is not None
        or setup_client.headers.get("Cookie") is not None
        or any(True for _cookie in setup_client.cookies.jar)
    ):
        raise ConfigurationError(
            "setup client must start clean, without credentials or cookies"
        )

    previous_authorization = client.headers.get("Authorization")
    client.headers.pop("Authorization", None)
    try:
        poll_until_ready(client, base_url, clock=clock, sleep=sleep)
        token = _authenticate_admin(
            client,
            base_url,
            username=admin_username,
            email=admin_email,
            password=admin_password,
        )
    except BaseException:
        _restore_authorization(client, previous_authorization)
        raise
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        team = _get_or_create(
            client,
            get_url=_url(base_url, f"/api/v4/teams/name/{TEAM_NAME}"),
            create_url=_url(base_url, "/api/v4/teams"),
            create_body={
                "name": TEAM_NAME,
                "display_name": "Data Structures",
                "type": "O",
            },
            resource="team",
            exists_codes=TEAM_EXISTS_CODES,
        )
        team_id = _resource_id(team, resource="team")
        channel = _get_or_create(
            client,
            get_url=_url(
                base_url, f"/api/v4/teams/{team_id}/channels/name/{CHANNEL_NAME}"
            ),
            create_url=_url(base_url, "/api/v4/channels"),
            create_body={
                "team_id": team_id,
                "name": CHANNEL_NAME,
                "display_name": "Course Home",
                "type": "O",
            },
            resource="channel",
            exists_codes=CHANNEL_EXISTS_CODES,
        )
        channel_id = _resource_id(channel, resource="channel")

        user_ids: dict[str, str] = {}
        for user in desired_users():
            username = user["username"]
            encoded_username = (
                httpx.URL("http://local").copy_with(path=f"/{username}").path[1:]
            )
            account = _get_or_create(
                client,
                get_url=_url(base_url, f"/api/v4/users/username/{encoded_username}"),
                create_url=_url(base_url, "/api/v4/users"),
                create_body=user,
                resource=f"user {username}",
                exists_codes=USER_EXISTS_CODES,
            )
            user_id = _resource_id(account, resource=f"user {username}")
            returned_username = account.get("username")
            if returned_username != username:
                raise ConfigurationError(
                    f"user lookup returned an unexpected username for {username}"
                )
            user_ids[username] = user_id
            _ensure_membership(
                client,
                get_url=_url(base_url, f"/api/v4/teams/{team_id}/members/{user_id}"),
                create_url=_url(base_url, f"/api/v4/teams/{team_id}/members"),
                body={"team_id": team_id, "user_id": user_id},
                resource="team membership",
            )
            _ensure_membership(
                client,
                get_url=_url(
                    base_url, f"/api/v4/channels/{channel_id}/members/{user_id}"
                ),
                create_url=_url(base_url, f"/api/v4/channels/{channel_id}/members"),
                body={"channel_id": channel_id, "user_id": user_id},
                resource="channel membership",
            )

        command_ids = {
            str(command["trigger"]): _ensure_command(
                client,
                base_url=base_url,
                team_id=team_id,
                desired=command,
                command_token=command_token,
            )
            for command in desired_commands(api_origin)
        }
        for username, user_id in user_ids.items():
            result = _request_json(
                setup_client,
                "POST",
                _url(setup_origin, "/api/v1/integrations/mattermost/demo-bindings"),
                json_body={
                    "local_username": username,
                    "mattermost_user_id": user_id,
                    "mattermost_username": username,
                },
                headers={"X-Demo-Setup-Key": setup_key},
            )
            _object(result, resource="demo binding")
    finally:
        _restore_authorization(client, previous_authorization)

    return {
        "team_id": team_id,
        "channel_id": channel_id,
        "user_ids": user_ids,
        "command_ids": command_ids,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotently configure the local Mattermost demo"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--api-origin", default=DEFAULT_API_ORIGIN)
    parser.add_argument("--setup-origin")
    return parser


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value:
        raise ConfigurationError(f"{name} is required in the environment")
    return value


def _local_origin_from_port(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> str:
    raw_value = environment.get(name, str(default))
    if not isinstance(raw_value, str) or re.fullmatch(r"[0-9]{1,5}", raw_value) is None:
        raise ConfigurationError(f"{name} must be an integer from 1 to 65535")
    port = int(raw_value)
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{name} must be an integer from 1 to 65535")
    return f"http://localhost:{port}"


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        admin_password = _required_environment(environment, "MATTERMOST_ADMIN_PASSWORD")
        setup_key = _required_environment(environment, "MATTERMOST_DEMO_SETUP_KEY")
        command_token = _required_environment(environment, "MATTERMOST_COMMAND_TOKEN")
        base_url = args.base_url or _local_origin_from_port(
            environment,
            "MATTERMOST_PORT",
            default=DEFAULT_MATTERMOST_PORT,
        )
        setup_origin = args.setup_origin or _local_origin_from_port(
            environment,
            "API_PORT",
            default=DEFAULT_API_PORT,
        )
        with (
            client_factory(timeout=10) as client,
            client_factory(timeout=10) as setup_client,
        ):
            result = configure_mattermost(
                client,
                setup_client=setup_client,
                base_url=base_url,
                api_origin=args.api_origin,
                setup_origin=setup_origin,
                admin_username=environment.get("MATTERMOST_ADMIN_USERNAME", "admin"),
                admin_email=environment.get(
                    "MATTERMOST_ADMIN_EMAIL", "admin@example.com"
                ),
                admin_password=admin_password,
                setup_key=setup_key,
                command_token=command_token,
                clock=clock,
                sleep=sleep,
            )
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - redact remote bodies, headers, and credentials.
        print(f"setup failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
