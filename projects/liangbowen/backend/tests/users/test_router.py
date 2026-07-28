from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.security import create_access_token
from app.core.config import get_settings
from app.db.session import get_session
from app.db.types import Role
from app.main import create_app
from app.users.creation_event import AccountCreationEvent
from app.users.model import User

ACCOUNT_CREATE_PATH = "/api/v1/users"
MAX_ACCOUNT_REQUEST_BYTES = 4 * 1024


def _user(role: Role, username: str) -> User:
    return User(
        id=uuid.uuid4(),
        username=username,
        display_name=f"{role.value.title()} {username}",
        role=role,
        password_hash="unused-by-bearer-auth",
        is_active=True,
    )


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


def _payload(
    role: Role,
    *,
    username: str | None = None,
    password: str = "Course2026!Secure",
) -> dict[str, str]:
    return {
        "username": username or f"{role.value}.{uuid.uuid4().hex[:8]}",
        "display_name": f"New {role.value.title()}",
        "role": role.value,
        "password": password,
    }


def _assert_no_secrets(value: Any) -> None:
    if isinstance(value, dict):
        assert "password" not in value
        assert "password_hash" not in value
        for child in value.values():
            _assert_no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_secrets(child)


@pytest_asyncio.fixture
async def account_api(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, dict[str, User]]]:
    monkeypatch.setenv("JWT_SECRET", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()
    users = {
        "admin": _user(Role.ADMIN, "admin.seed"),
        "teacher": _user(Role.TEACHER, "teacher.seed"),
        "student": _user(Role.STUDENT, "student.seed"),
    }
    postgres_session.add_all(users.values())
    await postgres_session.commit()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield postgres_session

    application = create_app()
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        yield client, users


@asynccontextmanager
async def _app_client() -> AsyncIterator[AsyncClient]:
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.parametrize(
    ("actor_key", "target_role"),
    [
        ("admin", Role.TEACHER),
        ("admin", Role.STUDENT),
        ("teacher", Role.STUDENT),
    ],
)
async def test_authorized_roles_can_create_accounts_without_exposing_secrets(
    account_api: tuple[AsyncClient, dict[str, User]],
    actor_key: str,
    target_role: Role,
) -> None:
    client, users = account_api
    password = "Course2026!Sensitive"
    payload = _payload(target_role, password=password)

    response = await client.post(
        ACCOUNT_CREATE_PATH,
        headers=_bearer(users[actor_key]),
        json=payload,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == payload["username"]
    assert body["display_name"] == payload["display_name"]
    assert body["role"] == target_role.value
    assert body["is_active"] is True
    assert body["created_by"] == {
        "id": str(users[actor_key].id),
        "username": users[actor_key].username,
        "display_name": users[actor_key].display_name,
    }
    _assert_no_secrets(body)
    assert password not in response.text


@pytest.mark.parametrize(
    ("actor_key", "target_role"),
    [
        ("admin", Role.ADMIN),
        ("teacher", Role.ADMIN),
        ("teacher", Role.TEACHER),
    ],
)
async def test_disallowed_target_roles_return_safe_forbidden(
    account_api: tuple[AsyncClient, dict[str, User]],
    actor_key: str,
    target_role: Role,
) -> None:
    client, users = account_api

    response = await client.post(
        ACCOUNT_CREATE_PATH,
        headers=_bearer(users[actor_key]),
        json=_payload(target_role),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role for requested account"}


async def test_student_cannot_access_account_endpoints(
    account_api: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = account_api
    headers = _bearer(users["student"])

    create_response = await client.post(
        ACCOUNT_CREATE_PATH,
        headers=headers,
        json=_payload(Role.STUDENT),
    )
    list_response = await client.get(ACCOUNT_CREATE_PATH, headers=headers)

    for response in (create_response, list_response):
        assert response.status_code == 403
        assert response.json() == {"detail": "insufficient role"}


async def test_list_maps_post_auth_actor_deactivation_to_safe_forbidden(
    postgres_session: AsyncSession,
) -> None:
    stored_actor = _user(Role.ADMIN, "deactivated.admin")
    stored_actor.is_active = False
    postgres_session.add(stored_actor)
    await postgres_session.commit()
    authenticated_actor = _user(Role.ADMIN, stored_actor.username)
    authenticated_actor.id = stored_actor.id

    async def override_current_user() -> User:
        return authenticated_actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield postgres_session

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(ACCOUNT_CREATE_PATH)

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role for requested account"}


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_account_endpoints_require_bearer_authentication(
    account_api: tuple[AsyncClient, dict[str, User]],
    method: str,
) -> None:
    client, _ = account_api

    response = await client.request(
        method,
        ACCOUNT_CREATE_PATH,
        json=_payload(Role.STUDENT) if method == "POST" else None,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authentication"}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_duplicate_username_returns_conflict_without_leaking_password(
    account_api: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = account_api
    payload = _payload(Role.STUDENT, username="duplicate.student")

    first = await client.post(
        ACCOUNT_CREATE_PATH,
        headers=_bearer(users["admin"]),
        json=payload,
    )
    duplicate = await client.post(
        ACCOUNT_CREATE_PATH,
        headers=_bearer(users["admin"]),
        json=payload,
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "username already exists"}
    assert payload["password"] not in duplicate.text


@pytest.mark.parametrize(
    "payload",
    [
        {
            "username": "UPPERCASE",
            "display_name": "Invalid",
            "role": "student",
            "password": "Course2026!Secure",
        },
        {
            "username": "valid.student",
            "display_name": "Invalid",
            "role": "student",
            "password": "TinySecret!",
        },
        {
            "username": "valid.student",
            "display_name": "Invalid",
            "role": "student",
            "password": "Course2026!Secure",
            "unexpected": "must be rejected",
        },
    ],
)
async def test_invalid_or_extra_account_fields_return_validation_error(
    account_api: tuple[AsyncClient, dict[str, User]],
    payload: dict[str, str],
) -> None:
    client, users = account_api

    response = await client.post(
        ACCOUNT_CREATE_PATH,
        headers=_bearer(users["admin"]),
        json=payload,
    )

    assert response.status_code == 422
    _assert_no_secrets(response.json())
    assert payload["password"] not in response.text


async def test_list_visibility_and_pagination_are_role_scoped_and_password_free(
    account_api: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = account_api
    for username in ("student.extra1", "student.extra2"):
        created = await client.post(
            ACCOUNT_CREATE_PATH,
            headers=_bearer(users["teacher"]),
            json=_payload(Role.STUDENT, username=username),
        )
        assert created.status_code == 201

    admin_response = await client.get(
        ACCOUNT_CREATE_PATH,
        headers=_bearer(users["admin"]),
    )
    teacher_response = await client.get(
        f"{ACCOUNT_CREATE_PATH}?limit=1&offset=1",
        headers=_bearer(users["teacher"]),
    )

    assert admin_response.status_code == 200
    admin_body = admin_response.json()
    assert admin_body["total"] == 5
    assert {item["role"] for item in admin_body["items"]} == {
        "admin",
        "teacher",
        "student",
    }
    assert admin_body["limit"] == 50
    assert admin_body["offset"] == 0
    _assert_no_secrets(admin_body)

    assert teacher_response.status_code == 200
    teacher_body = teacher_response.json()
    assert teacher_body["total"] == 3
    assert teacher_body["limit"] == 1
    assert teacher_body["offset"] == 1
    assert len(teacher_body["items"]) == 1
    assert teacher_body["items"][0]["role"] == "student"
    _assert_no_secrets(teacher_body)


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001"])
async def test_list_rejects_out_of_bounds_pagination(
    account_api: tuple[AsyncClient, dict[str, User]],
    query: str,
) -> None:
    client, users = account_api

    response = await client.get(
        f"{ACCOUNT_CREATE_PATH}?{query}",
        headers=_bearer(users["admin"]),
    )

    assert response.status_code == 422


async def test_create_records_server_request_id_in_immutable_event(
    account_api: tuple[AsyncClient, dict[str, User]],
    postgres_session: AsyncSession,
) -> None:
    client, users = account_api
    supplied_request_id = "11111111-1111-4111-8111-111111111111"
    payload = _payload(Role.STUDENT, username="audited.student")

    response = await client.post(
        ACCOUNT_CREATE_PATH,
        headers={**_bearer(users["admin"]), "X-Request-ID": supplied_request_id},
        json=payload,
    )

    assert response.status_code == 201
    server_request_id = response.headers["X-Request-ID"]
    assert str(uuid.UUID(server_request_id)) == server_request_id
    assert server_request_id != supplied_request_id
    event = await postgres_session.scalar(
        select(AccountCreationEvent).where(
            AccountCreationEvent.target_user_id == uuid.UUID(response.json()["id"])
        )
    )
    assert event is not None
    assert event.request_id == server_request_id


async def test_account_create_body_limit_ignores_content_length_and_chunks() -> None:
    secret = "SENSITIVE-ACCOUNT-BODY"
    raw_body = (
        '{"username":"oversized.student","display_name":"'
        + secret * 300
        + '","role":"student","password":"Course2026!Secure"}'
    ).encode()

    async def chunks() -> AsyncIterator[bytes]:
        midpoint = len(raw_body) // 2
        yield raw_body[:midpoint]
        yield raw_body[midpoint:]

    async with _app_client() as client:
        response = await client.post(
            ACCOUNT_CREATE_PATH,
            content=chunks(),
            headers={"content-type": "application/json", "content-length": "1"},
        )

    assert len(raw_body) > MAX_ACCOUNT_REQUEST_BYTES
    assert response.status_code == 413
    assert response.json() == {"detail": "request body is too large"}
    assert secret not in response.text


async def test_get_users_is_not_subject_to_account_create_body_limit(
    account_api: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = account_api
    raw_body = b"x" * (MAX_ACCOUNT_REQUEST_BYTES + 1)

    response: Response = await client.request(
        "GET",
        ACCOUNT_CREATE_PATH,
        headers={**_bearer(users["admin"]), "content-length": "1"},
        content=raw_body,
    )

    assert response.status_code == 200
