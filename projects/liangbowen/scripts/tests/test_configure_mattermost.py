from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import configure_mattermost as mattermost_setup
import httpx
import pytest
from configure_mattermost import (
    CHANNEL_EXISTS_CODES,
    COMMAND_EXISTS_CODES,
    MEMBERSHIP_EXISTS_CODES,
    TEAM_EXISTS_CODES,
    USER_EXISTS_CODES,
    ConfigurationError,
    configure_mattermost,
    desired_commands,
    desired_users,
    main,
    poll_until_ready,
    validate_origin,
)

ROOT = Path(__file__).resolve().parents[2]
ID = {
    "admin": "aaaaaaaaaaaaaaaaaaaaaaaaaa",
    "team": "tttttttttttttttttttttttttt",
    "channel": "cccccccccccccccccccccccccc",
    "teacher": "11111111111111111111111111",
    "student1": "22222222222222222222222222",
    "student2": "33333333333333333333333333",
    "student3": "44444444444444444444444444",
    "command": "hhhhhhhhhhhhhhhhhhhhhhhhhh",
}
COMMAND_TOKEN = "mmmmmmmmmmmmmmmmmmmmmmmmmm"


def _response(request: httpx.Request, status: int, payload: object, **headers: str):
    return httpx.Response(status, json=payload, headers=headers, request=request)


class DemoMattermost:
    def __init__(
        self,
        *,
        existing: bool = False,
        command_drift: bool = False,
        command_token: str = COMMAND_TOKEN,
        set_cookie: bool = False,
    ) -> None:
        self.ready = True
        self.admin_created = existing
        self.team_created = existing
        self.channel_created = existing
        self.users = set(ID.keys() & {"teacher", "student1", "student2", "student3"})
        if not existing:
            self.users.clear()
        self.team_members: set[str] = set()
        self.channel_members: set[str] = set()
        self.command_created = existing
        self.command_drift = command_drift
        self.command_token = command_token
        self.set_cookie = set_cookie
        self.calls: list[tuple[str, str, object | None, dict[str, str]]] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body, dict(request.headers)))
        self.urls.append(str(request.url))

        if path == "/api/v4/system/ping":
            assert "authorization" not in request.headers
            return _response(request, 200 if self.ready else 503, {"status": "OK"})
        if path == "/api/v4/users/login":
            assert "authorization" not in request.headers
            if not self.admin_created:
                return _response(
                    request,
                    401,
                    {"id": "api.user.login.invalid_credentials_email_username"},
                )
            assert body == {"login_id": "admin", "password": "Admin-secret-123!"}
            headers = {"Token": "token-secret"}
            if self.set_cookie:
                headers["Set-Cookie"] = "MMUSERID=mattermost-cookie-secret; Path=/"
            return _response(request, 200, {"id": ID["admin"]}, **headers)
        if path == "/api/v4/users" and request.method == "POST":
            username = body["username"]
            if username == "admin":
                assert "authorization" not in request.headers
                assert body["email"] == "admin@example.com"
                self.admin_created = True
                return _response(request, 201, {"id": ID["admin"], "username": "admin"})
            if username in self.users:
                return _response(
                    request,
                    400,
                    {"id": "app.user.save.username_exists.app_error"},
                )
            self.users.add(username)
            return _response(request, 201, {"id": ID[username], "username": username})

        if path == "/api/v1/integrations/mattermost/demo-bindings":
            assert request.headers["x-demo-setup-key"] == "setup-secret"
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            assert set(body) == {
                "local_username",
                "mattermost_user_id",
                "mattermost_username",
            }
            return _response(
                request, 201, {"mattermost_user_id": body["mattermost_user_id"]}
            )

        self._assert_authenticated(request)
        if path == "/api/v4/teams/name/data-structures":
            if not self.team_created:
                return _response(
                    request, 404, {"id": "app.team.get_by_name.missing.app_error"}
                )
            return _response(
                request,
                200,
                {
                    "id": ID["team"],
                    "name": "data-structures",
                    "display_name": "Data Structures",
                },
            )
        if path == "/api/v4/teams" and request.method == "POST":
            self.team_created = True
            return _response(request, 201, {"id": ID["team"], **body})
        if path == f"/api/v4/teams/{ID['team']}/channels/name/course-home":
            if not self.channel_created:
                return _response(
                    request, 404, {"id": "app.channel.get_by_name.missing.app_error"}
                )
            return _response(
                request,
                200,
                {
                    "id": ID["channel"],
                    "name": "course-home",
                    "display_name": "Course Home",
                },
            )
        if path == "/api/v4/channels" and request.method == "POST":
            self.channel_created = True
            return _response(request, 201, {"id": ID["channel"], **body})

        if path.startswith("/api/v4/users/username/"):
            username = path.rsplit("/", 1)[1]
            if username not in self.users:
                return _response(
                    request, 404, {"id": "app.user.get_by_username.missing"}
                )
            return _response(request, 200, {"id": ID[username], "username": username})

        for username in ("teacher", "student1", "student2", "student3"):
            user_id = ID[username]
            if path == f"/api/v4/teams/{ID['team']}/members/{user_id}":
                if user_id not in self.team_members:
                    return _response(
                        request, 404, {"id": "app.team.get_member.missing"}
                    )
                return _response(
                    request, 200, {"team_id": ID["team"], "user_id": user_id}
                )
            if path == f"/api/v4/channels/{ID['channel']}/members/{user_id}":
                if user_id not in self.channel_members:
                    return _response(
                        request, 404, {"id": "app.channel.get_member.missing"}
                    )
                return _response(
                    request, 200, {"channel_id": ID["channel"], "user_id": user_id}
                )

        if path == f"/api/v4/teams/{ID['team']}/members" and request.method == "POST":
            self.team_members.add(body["user_id"])
            return _response(request, 201, body)
        if (
            path == f"/api/v4/channels/{ID['channel']}/members"
            and request.method == "POST"
        ):
            self.channel_members.add(body["user_id"])
            return _response(request, 201, body)

        if path == "/api/v4/commands" and request.method == "GET":
            if not self.command_created:
                return _response(request, 200, [])
            command = {
                "id": ID["command"],
                "token": self.command_token,
                "create_at": 123,
                "delete_at": 0,
                "creator_id": ID["admin"],
                "plugin_id": "server-owned-plugin-field",
                "unknown_server_field": "must-not-be-reflected",
                "team_id": ID["team"],
                **desired_commands("http://api:8000")[0],
            }
            if self.command_drift:
                command["url"] = "http://stale.invalid/hook"
                command["auto_complete"] = False
                command["method"] = "G"
            return _response(request, 200, [command])
        if path == "/api/v4/commands" and request.method == "POST":
            self.command_token = body["token"]
            self.command_created = True
            return _response(request, 201, {"id": ID["command"], **body})
        if path == f"/api/v4/commands/{ID['command']}" and request.method == "PUT":
            self.command_drift = False
            return _response(
                request,
                200,
                {"id": ID["command"], "token": self.command_token, **body},
            )

        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    @staticmethod
    def _assert_authenticated(request: httpx.Request) -> None:
        assert request.headers["authorization"] == "Bearer token-secret"


def _run(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> dict[str, object]:
    transport = httpx.MockTransport(handler)
    with (
        httpx.Client(transport=transport, timeout=10) as client,
        httpx.Client(transport=transport, timeout=10) as setup_client,
    ):
        arguments = {
            "command_token": COMMAND_TOKEN,
            "sleep": lambda _seconds: None,
            **kwargs,
        }
        return configure_mattermost(
            client,
            setup_client=setup_client,
            base_url="http://mattermost.test",
            api_origin="http://api:8000",
            setup_origin="http://backend.test:8000",
            admin_username="admin",
            admin_email="admin@example.com",
            admin_password="Admin-secret-123!",
            setup_key="setup-secret",
            **arguments,
        )


def test_desired_state_covers_demo_users_and_single_hw_command() -> None:
    users = desired_users()
    assert users == [
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
    commands = desired_commands("http://api:8000/")
    assert [command["trigger"] for command in commands] == ["hw"]
    assert commands[0] == {
        "trigger": "hw",
        "display_name": "AI 作业评审",
        "description": "发布、查询、提交、汇总和评估作业",
        "auto_complete": True,
        "auto_complete_hint": "help | publish | list | show | submit | summary | evaluate",
        "method": "P",
        "url": "http://api:8000/api/v1/integrations/mattermost/commands",
    }


def test_ping_timeout_uses_injected_clock_and_sleep_without_real_wait() -> None:
    times = iter([0.0, 0.0, 20.0, 37.5])
    sleeps: list[float] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        return _response(request, 503, {"status": "starting"})

    with (
        httpx.Client(transport=httpx.MockTransport(unavailable), timeout=10) as client,
        pytest.raises(
            ConfigurationError,
            match=r"did not become ready within 37\.5 seconds",
        ),
    ):
        poll_until_ready(
            client,
            "http://mattermost.test",
            max_wait=37.5,
            clock=lambda: next(times),
            sleep=sleeps.append,
        )
    assert sleeps == [1.0]


def test_ping_bounds_the_last_request_by_the_remaining_deadline() -> None:
    times = iter([0.0, 119.5, 120.0])
    read_timeouts: list[float] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        return _response(request, 503, {"status": "starting"})

    with (
        httpx.Client(transport=httpx.MockTransport(unavailable), timeout=10) as client,
        pytest.raises(ConfigurationError, match="did not become ready"),
    ):
        poll_until_ready(
            client,
            "http://mattermost.test",
            max_wait=120,
            clock=lambda: next(times),
            sleep=lambda _seconds: None,
        )
    assert read_timeouts == [0.5]


def test_first_run_creates_admin_resources_memberships_command_and_bindings() -> None:
    server = DemoMattermost()
    result = _run(server)

    assert result == {
        "team_id": ID["team"],
        "channel_id": ID["channel"],
        "user_ids": {
            name: ID[name] for name in ("teacher", "student1", "student2", "student3")
        },
        "command_ids": {"hw": ID["command"]},
    }
    login_calls = [call for call in server.calls if call[1] == "/api/v4/users/login"]
    assert len(login_calls) == 2
    binding_calls = [
        call for call in server.calls if call[1].endswith("/demo-bindings")
    ]
    assert len(binding_calls) == 4
    assert binding_calls[0][2] == {
        "local_username": "teacher",
        "mattermost_user_id": ID["teacher"],
        "mattermost_username": "teacher",
    }
    command_create = next(
        call for call in server.calls if call[:2] == ("POST", "/api/v4/commands")
    )
    assert command_create[2] == {
        "team_id": ID["team"],
        "token": COMMAND_TOKEN,
        **desired_commands("http://api:8000")[0],
    }
    assert server.command_token == COMMAND_TOKEN


def test_caller_authorization_is_restored_but_never_sent_to_binding_origin() -> None:
    server = DemoMattermost()
    transport = httpx.MockTransport(server)
    with (
        httpx.Client(
            transport=transport,
            timeout=10,
            headers={"Authorization": "Bearer caller-secret"},
        ) as client,
        httpx.Client(transport=transport, timeout=10) as setup_client,
    ):
        configure_mattermost(
            client,
            setup_client=setup_client,
            base_url="http://mattermost.test",
            api_origin="http://api:8000",
            setup_origin="http://backend.test:8000",
            admin_username="admin",
            admin_email="admin@example.com",
            admin_password="Admin-secret-123!",
            setup_key="setup-secret",
            command_token=COMMAND_TOKEN,
            sleep=lambda _seconds: None,
        )
        assert client.headers["Authorization"] == "Bearer caller-secret"


def test_mattermost_localhost_cookie_is_isolated_from_setup_api_client() -> None:
    server = DemoMattermost(set_cookie=True)
    transport = httpx.MockTransport(server)
    with (
        httpx.Client(transport=transport, timeout=10) as mattermost_client,
        httpx.Client(transport=transport, timeout=10) as setup_client,
    ):
        configure_mattermost(
            mattermost_client,
            setup_client=setup_client,
            base_url="http://localhost:8065",
            api_origin="http://api:8000",
            setup_origin="http://localhost:8000",
            admin_username="admin",
            admin_email="admin@example.com",
            admin_password="Admin-secret-123!",
            setup_key="setup-secret",
            command_token=COMMAND_TOKEN,
            sleep=lambda _seconds: None,
        )
    bindings = [call for call in server.calls if call[1].endswith("/demo-bindings")]
    assert len(bindings) == 4
    assert any(
        headers.get("cookie") == "MMUSERID=mattermost-cookie-secret"
        for method, path, _, headers in server.calls
        if method == "GET" and path == "/api/v4/teams/name/data-structures"
    )
    assert all("cookie" not in headers for _, _, _, headers in bindings)
    assert all("authorization" not in headers for _, _, _, headers in bindings)


@pytest.mark.parametrize(
    "dirty_state", ["authorization", "cookie-header", "cookie-jar"]
)
def test_setup_client_rejects_credentials_without_mutating_caller_state(
    dirty_state: str,
) -> None:
    server = DemoMattermost()
    transport = httpx.MockTransport(server)
    headers: dict[str, str] = {}
    if dirty_state == "authorization":
        headers["Authorization"] = "Bearer caller-setup-secret"
    elif dirty_state == "cookie-header":
        headers["Cookie"] = "unsafe=caller-cookie-secret"
    with (
        httpx.Client(transport=transport, timeout=10) as mattermost_client,
        httpx.Client(transport=transport, timeout=10, headers=headers) as setup_client,
    ):
        if dirty_state == "cookie-jar":
            setup_client.cookies.set("unsafe", "caller-cookie-secret")
        headers_before = list(setup_client.headers.multi_items())
        cookies_before = [
            (cookie.name, cookie.value, cookie.domain, cookie.path)
            for cookie in setup_client.cookies.jar
        ]
        with pytest.raises(ConfigurationError, match="setup client must start clean"):
            configure_mattermost(
                mattermost_client,
                setup_client=setup_client,
                base_url="http://mattermost.test",
                api_origin="http://api:8000",
                setup_origin="http://backend.test:8000",
                admin_username="admin",
                admin_email="admin@example.com",
                admin_password="Admin-secret-123!",
                setup_key="setup-secret",
                command_token=COMMAND_TOKEN,
                sleep=lambda _seconds: None,
            )
        assert list(setup_client.headers.multi_items()) == headers_before
        assert [
            (cookie.name, cookie.value, cookie.domain, cookie.path)
            for cookie in setup_client.cookies.jar
        ] == cookies_before
    assert server.calls == []


def test_second_run_does_not_duplicate_resources_and_repairs_command_drift() -> None:
    server = DemoMattermost(existing=True, command_drift=True)
    server.team_members.update(
        ID[name] for name in ("teacher", "student1", "student2", "student3")
    )
    server.channel_members.update(server.team_members)

    first = _run(server)
    second = _run(server)
    assert first == second
    creates = [call for call in server.calls if call[0] == "POST"]
    assert all(
        call[1] not in {"/api/v4/teams", "/api/v4/channels", "/api/v4/users"}
        for call in creates
    )
    updates = [
        call
        for call in server.calls
        if call[:2] == ("PUT", f"/api/v4/commands/{ID['command']}")
    ]
    assert len(updates) == 1
    assert updates[0][2] == {
        "id": ID["command"],
        "team_id": ID["team"],
        **desired_commands("http://api:8000")[0],
    }
    assert set(updates[0][2]) == {
        "id",
        "team_id",
        "trigger",
        "display_name",
        "description",
        "auto_complete",
        "auto_complete_hint",
        "method",
        "url",
    }


def test_existing_command_token_mismatch_fails_safely_with_constant_time_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_token = "ssssssssssssssssssssssssss"
    compared: list[tuple[bytes, bytes]] = []

    def compare_digest(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return False

    monkeypatch.setattr(mattermost_setup.hmac, "compare_digest", compare_digest)
    server = DemoMattermost(existing=True, command_token=stale_token)
    with pytest.raises(ConfigurationError) as error:
        _run(server)
    assert "slash command token does not match" in str(error.value)
    assert stale_token not in str(error.value)
    assert COMMAND_TOKEN not in str(error.value)
    assert compared == [(stale_token.encode(), COMMAND_TOKEN.encode())]
    assert not any(call[0] == "PUT" for call in server.calls)
    assert not any(call[1].endswith("/demo-bindings") for call in server.calls)


def test_first_run_fails_if_mattermost_does_not_preserve_configured_token() -> None:
    server = DemoMattermost()
    rewritten_token = "rrrrrrrrrrrrrrrrrrrrrrrrrr"

    def rewrite_create_response(request: httpx.Request) -> httpx.Response:
        response = server(request)
        if request.method == "POST" and request.url.path == "/api/v4/commands":
            payload = response.json()
            payload["token"] = rewritten_token
            return _response(request, 201, payload)
        return response

    with pytest.raises(ConfigurationError) as error:
        _run(rewrite_create_response)
    assert "slash command token does not match" in str(error.value)
    assert rewritten_token not in str(error.value)
    assert COMMAND_TOKEN not in str(error.value)
    assert not any(call[1].endswith("/demo-bindings") for call in server.calls)


def test_invalid_command_token_is_rejected_before_any_remote_request() -> None:
    server = DemoMattermost()
    with pytest.raises(ConfigurationError, match="MATTERMOST_COMMAND_TOKEN"):
        _run(server, command_token="not-a-mattermost-id")
    assert server.calls == []


def test_v105_already_exists_codes_match_official_server_contracts() -> None:
    assert USER_EXISTS_CODES == frozenset(
        {
            "app.user.save.email_exists.app_error",
            "app.user.save.username_exists.app_error",
        }
    )
    assert TEAM_EXISTS_CODES == frozenset(
        {
            "app.team.save.existing.app_error",
            "store.sql_team.save_team.existing.app_error",
        }
    )
    assert CHANNEL_EXISTS_CODES == frozenset(
        {
            "store.sql_channel.save_channel.exists.app_error",
            "store.sql_channel.save_channel.existing.app_error",
        }
    )
    assert COMMAND_EXISTS_CODES == frozenset(
        {"api.command.duplicate_trigger.app_error"}
    )
    assert MEMBERSHIP_EXISTS_CODES == frozenset(
        {
            "app.channel.save_member.exists.app_error",
            "store.sql_team.save_member.exists.app_error",
        }
    )


@pytest.mark.parametrize(
    ("error_id", "message"),
    [
        (
            "app.user.save.username_exists.app_error",
            "administrator username is already occupied",
        ),
        (
            "app.user.save.email_exists.app_error",
            "administrator email is already occupied",
        ),
    ],
)
def test_admin_bootstrap_distinguishes_username_and_email_conflicts(
    error_id: str,
    message: str,
) -> None:
    server = DemoMattermost()

    def conflict(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v4/users":
            body = json.loads(request.content)
            if body.get("username") == "admin":
                return _response(request, 400, {"id": error_id})
        return server(request)

    with pytest.raises(ConfigurationError, match=message):
        _run(conflict)


@pytest.mark.parametrize(
    ("resource", "path", "error_id"),
    [
        ("team", "/api/v4/teams", "app.team.save.existing.app_error"),
        (
            "channel",
            "/api/v4/channels",
            "store.sql_channel.save_channel.exists.app_error",
        ),
        ("user", "/api/v4/users", "app.user.save.username_exists.app_error"),
    ],
)
def test_resource_create_races_reget_the_created_object(
    resource: str,
    path: str,
    error_id: str,
) -> None:
    server = DemoMattermost(existing=True)
    if resource == "team":
        server.team_created = False
    elif resource == "channel":
        server.channel_created = False
    else:
        server.users.remove("teacher")
    returned = False

    def race(request: httpx.Request) -> httpx.Response:
        nonlocal returned
        if not returned and request.method == "POST" and request.url.path == path:
            returned = True
            if resource == "team":
                server.team_created = True
            elif resource == "channel":
                server.channel_created = True
            else:
                server.users.add("teacher")
            return _response(request, 400, {"id": error_id})
        return server(request)

    result = _run(race)
    assert returned is True
    assert result["team_id"] == ID["team"]


def test_command_create_race_relists_and_repairs_the_winner() -> None:
    server = DemoMattermost(existing=True)
    server.command_created = False
    returned = False

    def race(request: httpx.Request) -> httpx.Response:
        nonlocal returned
        if (
            not returned
            and request.method == "POST"
            and request.url.path == "/api/v4/commands"
        ):
            returned = True
            server.command_created = True
            server.command_drift = True
            return _response(
                request,
                400,
                {"id": "api.command.duplicate_trigger.app_error"},
            )
        return server(request)

    result = _run(race)
    assert result["command_ids"] == {"hw": ID["command"]}
    updates = [
        call
        for call in server.calls
        if call[:2] == ("PUT", f"/api/v4/commands/{ID['command']}")
    ]
    assert len(updates) == 1
    assert updates[0][2]["method"] == "P"


@pytest.mark.parametrize(
    ("membership", "error_id"),
    [
        ("team", "store.sql_team.save_member.exists.app_error"),
        ("channel", "app.channel.save_member.exists.app_error"),
    ],
)
def test_official_membership_race_codes_are_idempotent(
    membership: str,
    error_id: str,
) -> None:
    server = DemoMattermost(existing=True)
    if membership == "channel":
        server.team_members.update(
            ID[name] for name in ("teacher", "student1", "student2", "student3")
        )
    returned = False

    def race(request: httpx.Request) -> httpx.Response:
        nonlocal returned
        is_team = "/api/v4/teams/" in request.url.path
        is_channel = "/api/v4/channels/" in request.url.path
        if (
            not returned
            and request.method == "POST"
            and request.url.path.endswith("/members")
            and (
                (membership == "team" and is_team)
                or (membership == "channel" and is_channel)
            )
        ):
            returned = True
            return _response(request, 400, {"id": error_id})
        return server(request)

    _run(race)
    assert returned is True


@pytest.mark.parametrize("status", [400, 409])
def test_unknown_or_non_400_member_conflicts_raise(status: int) -> None:
    server = DemoMattermost(existing=True)
    server.team_members.clear()

    def conflict(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/members"):
            return _response(
                request,
                status,
                {
                    "id": "unknown.member.conflict",
                    "message": "setup-secret token-secret",
                },
            )
        return server(request)

    with pytest.raises(httpx.HTTPStatusError):
        _run(conflict)


def test_unknown_400_command_conflict_raises() -> None:
    server = DemoMattermost(existing=True)
    server.command_created = False

    def conflict(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v4/commands":
            return _response(request, 400, {"id": "unknown.command.conflict"})
        return server(request)

    with pytest.raises(httpx.HTTPStatusError):
        _run(conflict)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://localhost:8065",
        "http://user:pass@localhost:8065",
        "http://localhost:8065/path",
        "http://localhost:8065?query=yes",
        "http://localhost:8065/#fragment",
        "http://localhost:8065\\@evil.example",
        "http://local host:8065",
        "",
    ],
)
def test_origin_validation_rejects_credentials_and_dangerous_url_semantics(
    value: str,
) -> None:
    with pytest.raises(ConfigurationError):
        validate_origin(value, name="base URL")


@pytest.mark.parametrize("bad_id", [None, "", "../admin", "short", "A" * 26])
def test_missing_or_malicious_resource_ids_are_rejected(bad_id: object) -> None:
    server = DemoMattermost(existing=True)

    def corrupt(request: httpx.Request) -> httpx.Response:
        response = server(request)
        if request.url.path == "/api/v4/teams/name/data-structures":
            payload = response.json()
            payload["id"] = bad_id
            return _response(request, 200, payload)
        return response

    with pytest.raises(ConfigurationError, match="invalid team id"):
        _run(corrupt)


def test_main_prints_only_json_and_redacts_all_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = DemoMattermost()
    transport = httpx.MockTransport(server)
    clients: list[httpx.Client] = []

    def client_factory(**kwargs: Any) -> httpx.Client:
        assert kwargs == {"timeout": 10}
        client = httpx.Client(transport=transport, **kwargs)
        clients.append(client)
        return client

    environ = {
        "MATTERMOST_ADMIN_USERNAME": "admin",
        "MATTERMOST_ADMIN_EMAIL": "admin@example.com",
        "MATTERMOST_ADMIN_PASSWORD": "Admin-secret-123!",
        "MATTERMOST_DEMO_SETUP_KEY": "setup-secret",
        "MATTERMOST_COMMAND_TOKEN": COMMAND_TOKEN,
    }
    status = main(
        [
            "--base-url",
            "http://mattermost.test",
            "--api-origin",
            "http://api:8000",
            "--setup-origin",
            "http://backend.test:8000",
        ],
        environ=environ,
        client_factory=client_factory,
        sleep=lambda _seconds: None,
    )
    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out)["team_id"] == ID["team"]
    assert captured.err == ""
    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert all(client.is_closed for client in clients)
    for secret in (
        "Admin-secret-123!",
        "setup-secret",
        "token-secret",
        COMMAND_TOKEN,
    ):
        assert secret not in captured.out + captured.err


def test_main_sanitizes_response_errors_and_requires_environment_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "response-body-secret"

    def failed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/system/ping":
            return _response(request, 200, {"status": "OK"})
        return _response(request, 500, {"message": secret})

    def client_factory(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(failed), **kwargs)

    args = [
        "--base-url",
        "http://mattermost.test",
        "--api-origin",
        "http://api:8000",
        "--setup-origin",
        "http://backend.test:8000",
    ]
    assert main(args, environ={}, client_factory=client_factory) == 2
    missing = capsys.readouterr()
    assert "MATTERMOST_ADMIN_PASSWORD" in missing.err

    missing_token_environment = {
        "MATTERMOST_ADMIN_PASSWORD": "Admin-secret-123!",
        "MATTERMOST_DEMO_SETUP_KEY": "setup-secret",
    }
    assert (
        main(args, environ=missing_token_environment, client_factory=client_factory)
        == 2
    )
    missing_token = capsys.readouterr()
    assert "MATTERMOST_COMMAND_TOKEN" in missing_token.err

    environ = {
        "MATTERMOST_ADMIN_PASSWORD": "Admin-secret-123!",
        "MATTERMOST_DEMO_SETUP_KEY": "setup-secret",
        "MATTERMOST_COMMAND_TOKEN": COMMAND_TOKEN,
    }
    assert (
        main(args, environ=environ, client_factory=client_factory, sleep=lambda _: None)
        == 1
    )
    failed_output = capsys.readouterr()
    assert secret not in failed_output.out + failed_output.err
    assert "setup failed (HTTPStatusError)" in failed_output.err


def test_main_builds_safe_local_origins_from_environment_ports(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = DemoMattermost()
    transport = httpx.MockTransport(server)

    def client_factory(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=transport, **kwargs)

    environment = {
        "MATTERMOST_ADMIN_PASSWORD": "Admin-secret-123!",
        "MATTERMOST_DEMO_SETUP_KEY": "setup-secret",
        "MATTERMOST_COMMAND_TOKEN": COMMAND_TOKEN,
        "MATTERMOST_PORT": "18065",
        "API_PORT": "18000",
    }
    assert (
        main(
            ["--api-origin", "http://api:8000"],
            environ=environment,
            client_factory=client_factory,
            sleep=lambda _seconds: None,
        )
        == 0
    )
    capsys.readouterr()
    assert any(url.startswith("http://localhost:18065/api/v4/") for url in server.urls)
    assert any(
        url == "http://localhost:18000/api/v1/integrations/mattermost/demo-bindings"
        for url in server.urls
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MATTERMOST_PORT", "8065\nattacker"),
        ("MATTERMOST_PORT", "not-a-number"),
        ("MATTERMOST_PORT", "0"),
        ("API_PORT", "65536"),
        ("API_PORT", "+8000"),
        ("API_PORT", "８０００"),
    ],
)
def test_main_rejects_unsafe_environment_ports_before_opening_clients(
    name: str,
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def client_factory(**_kwargs: Any) -> httpx.Client:
        nonlocal called
        called = True
        raise AssertionError("client must not be opened")

    environment = {
        "MATTERMOST_ADMIN_PASSWORD": "Admin-secret-123!",
        "MATTERMOST_DEMO_SETUP_KEY": "setup-secret",
        "MATTERMOST_COMMAND_TOKEN": COMMAND_TOKEN,
        name: value,
    }
    assert main([], environ=environment, client_factory=client_factory) == 2
    captured = capsys.readouterr()
    assert name in captured.err
    assert value not in captured.out + captured.err
    assert called is False


@pytest.mark.parametrize(
    "polluted_parent_environment",
    [
        {
            "APP_ENV": "production",
            "JWT_SECRET": "short",
            "AGENT_PROVIDER": "openai-compatible",
            "AGENT_API_KEY": "",
        },
        {
            "APP_ENV": "production",
            "MATTERMOST_URL": "javascript:unsafe",
            "MATTERMOST_ACTION_URL": "http://user:password@unsafe.test/callback",
            "CORS_ORIGINS": "not-json",
        },
    ],
)
def test_command_token_matches_backend_settings_and_request_verifier(
    monkeypatch: pytest.MonkeyPatch,
    polluted_parent_environment: dict[str, str],
) -> None:
    for name, value in polluted_parent_environment.items():
        monkeypatch.setenv(name, value)
    script = """
from urllib.parse import urlencode
from app.core.config import Settings
from app.integrations.mattermost.security import verify_and_parse_form

settings = Settings(_env_file=None)
assert settings.app_env == "development"
assert settings.jwt_secret.get_secret_value() == "development-secret-change-me"
assert settings.agent_provider == "mock"
assert settings.mattermost_url is None
assert settings.mattermost_action_url is None
token = settings.mattermost_command_token
assert token is not None
payload = urlencode({
    "token": token.get_secret_value(),
    "team_id": "tttttttttttttttttttttttttt",
    "team_domain": "data-structures",
    "channel_id": "cccccccccccccccccccccccccc",
    "channel_name": "course-home",
    "user_id": "11111111111111111111111111",
    "user_name": "teacher",
    "command": "/hw",
    "text": "help",
    "trigger_id": "trigger",
    "response_url": "http://mattermost.test/hooks/commands/response",
}).encode()
request = verify_and_parse_form(
    payload,
    "application/x-www-form-urlencoded",
    token,
)
assert request.command == "/hw"
print("verified")
"""
    # Deliberately do not inherit os.environ: every Settings field not listed here
    # must use its model default, regardless of the developer/CI parent process.
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(ROOT / "backend"),
        "APP_ENV": "development",
        "JWT_SECRET": "development-secret-change-me",
        "MATTERMOST_COMMAND_TOKEN": COMMAND_TOKEN,
    }
    completed = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "verified\n"
    assert COMMAND_TOKEN not in completed.stdout + completed.stderr


def test_make_targets_are_sequential_and_seed_before_binding() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "mattermost-up:" in makefile
    assert "mattermost-configure:" in makefile
    assert "demo-full:" in makefile
    assert not "demo-full: mattermost-up mattermost-configure" in makefile

    dry_run = subprocess.run(
        ["make", "--no-print-directory", "-n", "demo-full"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_run.returncode == 0
    output = dry_run.stdout
    assert output.index('"mattermost-up"') < output.index('"seed"')
    assert output.index('"seed"') < output.index('"mattermost-configure"')
    configure_command = output[output.index("scripts/configure_mattermost.py") :]
    assert "--base-url" not in configure_command
    assert "--setup-origin" not in configure_command
    assert "localhost:8065" not in configure_command
    assert "localhost:8000" not in configure_command
    assert COMMAND_TOKEN not in output + dry_run.stderr
