import asyncio
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.sql import Select, operators

from app.auth import router as auth_router_module
from app.auth.dependencies import require_roles
from app.auth.security import create_access_token, decode_access_token, hash_password
from app.core.config import get_settings
from app.db.session import get_session
from app.db.types import Role
from app.main import create_app
from app.users.model import User


class FocusedInMemorySession:
    def __init__(self, users: list[User]) -> None:
        self.users_by_id = {user.id: user for user in users}
        self.users_by_username = {user.username: user for user in users}

    async def scalar(self, statement: Select[Any]) -> User | None:
        assert statement.column_descriptions[0]["entity"] is User
        assert statement.whereclause is not None
        assert statement.whereclause.operator is operators.eq
        assert statement.whereclause.left.compare(User.__table__.c.username)
        parameters = statement.compile().params
        username = next(iter(parameters.values()))
        return self.users_by_username.get(username)

    async def get(self, model: type[User], identity: uuid.UUID) -> User | None:
        assert model is User
        return self.users_by_id.get(identity)


@pytest_asyncio.fixture
async def auth_client(
    isolate_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, dict[str, User]]]:
    monkeypatch.setenv("JWT_SECRET", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()
    users = {
        "student": User(
            id=uuid.uuid4(),
            username="grace",
            display_name="Grace Hopper",
            role=Role.STUDENT,
            password_hash=hash_password("compiler"),
            is_active=True,
        ),
        "teacher": User(
            id=uuid.uuid4(),
            username="alan",
            display_name="Alan Turing",
            role=Role.TEACHER,
            password_hash=hash_password("enigma"),
            is_active=True,
        ),
        "inactive": User(
            id=uuid.uuid4(),
            username="inactive",
            display_name="Inactive User",
            role=Role.STUDENT,
            password_hash=hash_password("unusable"),
            is_active=False,
        ),
    }
    session = FocusedInMemorySession(list(users.values()))

    async def override_session() -> AsyncIterator[FocusedInMemorySession]:
        yield session

    application = create_app()
    application.dependency_overrides[get_session] = override_session

    @application.get("/api/v1/teacher-only")
    async def teacher_only(
        current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
    ) -> dict[str, str]:
        return {"username": current_user.username}

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        yield client, users


async def login(client: AsyncClient, username: str, password: str) -> Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )


def assert_generic_bearer_unauthorized(response: Response) -> None:
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authentication"}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_successful_login_returns_real_signed_bearer_token(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = auth_client

    response = await login(client, "grace", "compiler")

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    claims = decode_access_token(response.json()["access_token"])
    assert claims["sub"] == str(users["student"].id)
    assert claims["role"] == Role.STUDENT.value


async def test_wrong_and_unknown_login_use_same_generic_response(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, _ = auth_client

    wrong_password = await login(client, "grace", "not-the-password")
    unknown_username = await login(client, "missing", "not-the-password")

    assert_generic_bearer_unauthorized(wrong_password)
    assert_generic_bearer_unauthorized(unknown_username)
    assert unknown_username.json() == wrong_password.json()


async def test_inactive_user_cannot_log_in(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, _ = auth_client

    response = await login(client, "inactive", "unusable")

    assert_generic_bearer_unauthorized(response)


async def test_auth_me_returns_the_current_user(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = auth_client
    token_response = await login(client, "grace", "compiler")

    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(users["student"].id),
        "username": "grace",
        "display_name": "Grace Hopper",
        "role": "student",
    }


@pytest.mark.parametrize("user_key", ["inactive", "unknown"])
async def test_auth_me_rejects_inactive_or_unknown_users(
    auth_client: tuple[AsyncClient, dict[str, User]],
    user_key: str,
) -> None:
    client, users = auth_client
    user_id = users["inactive"].id if user_key == "inactive" else uuid.uuid4()
    token = create_access_token(user_id, Role.STUDENT)

    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert_generic_bearer_unauthorized(response)


async def test_auth_me_rejects_missing_credentials_with_generic_response(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, _ = auth_client

    response = await client.get("/api/v1/auth/me")

    assert_generic_bearer_unauthorized(response)


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic Z3JhY2U6Y29tcGlsZXI=",
        "Bearer",
        "Bearer not-a-valid-jwt",
    ],
)
async def test_auth_me_rejects_invalid_authorization_with_bearer_challenge(
    auth_client: tuple[AsyncClient, dict[str, User]],
    authorization: str,
) -> None:
    client, _ = auth_client

    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": authorization},
    )

    assert_generic_bearer_unauthorized(response)


async def test_signed_token_missing_required_claim_is_rejected_with_bearer_challenge(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, _ = auth_client
    now = datetime.now(UTC)
    settings = get_settings()
    token = jwt.encode(
        {"iat": now, "exp": now + timedelta(minutes=5)},
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert_generic_bearer_unauthorized(response)


async def test_student_is_forbidden_from_teacher_only_route(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = auth_client
    token = create_access_token(users["student"].id, Role.STUDENT)

    response = await client.get(
        "/api/v1/teacher-only",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}


async def test_token_role_claim_cannot_elevate_database_student(
    auth_client: tuple[AsyncClient, dict[str, User]],
) -> None:
    client, users = auth_client
    now = datetime.now(UTC)
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": str(users["student"].id),
            "role": Role.TEACHER.value,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    response = await client.get(
        "/api/v1/teacher-only",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}


@pytest.mark.parametrize(
    ("actor_change", "expected_status", "expected_detail"),
    [
        ("deactivate", 401, "invalid authentication"),
        ("demote", 403, "insufficient role"),
    ],
)
async def test_preexisting_teacher_token_cannot_list_or_create_after_database_change(
    postgres_session,
    isolate_settings: None,
    monkeypatch: pytest.MonkeyPatch,
    actor_change: str,
    expected_status: int,
    expected_detail: str,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()
    teacher = User(
        id=uuid.uuid4(),
        username=f"stale-{actor_change}",
        display_name="Stale Token Teacher",
        role=Role.TEACHER,
        password_hash=hash_password("Course2026!Teacher"),
        is_active=True,
    )
    postgres_session.add(teacher)
    await postgres_session.commit()
    token = create_access_token(teacher.id, Role.TEACHER)

    if actor_change == "deactivate":
        teacher.is_active = False
    else:
        teacher.role = Role.STUDENT
    await postgres_session.commit()

    async def override_session() -> AsyncIterator[Any]:
        yield postgres_session

    application = create_app()
    application.dependency_overrides[get_session] = override_session
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "username": f"blocked-{actor_change}",
        "display_name": "Must Not Be Created",
        "role": "student",
        "password": "Course2026!Secure",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        responses = [
            await client.get("/api/v1/users", headers=headers),
            await client.post("/api/v1/users", headers=headers, json=payload),
        ]

    for response in responses:
        assert response.status_code == expected_status
        assert response.json() == {"detail": expected_detail}


async def test_malformed_stored_hash_is_logged_safely_and_rejected(
    auth_client: tuple[AsyncClient, dict[str, User]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, users = auth_client
    users["student"].password_hash = "malformed-stored-bcrypt-hash"
    caplog.set_level(logging.ERROR, logger="app.auth.router")

    response = await login(client, "grace", "compiler")

    assert_generic_bearer_unauthorized(response)
    assert "stored password hash is invalid" in caplog.text
    assert "grace" not in caplog.text
    assert users["student"].password_hash not in caplog.text


async def test_malformed_stored_hash_performs_dummy_bcrypt_work(
    auth_client: tuple[AsyncClient, dict[str, User]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, users = auth_client
    malformed_hash = "malformed-stored-bcrypt-hash"
    users["student"].password_hash = malformed_hash
    checked_hashes: list[str] = []

    def recording_verifier(password: str, password_hash: str) -> bool:
        checked_hashes.append(password_hash)
        if password_hash == malformed_hash:
            raise ValueError("invalid stored hash")
        return False

    monkeypatch.setattr(auth_router_module, "verify_password", recording_verifier)

    response = await login(client, "grace", "compiler")

    assert_generic_bearer_unauthorized(response)
    assert checked_hashes == [malformed_hash, auth_router_module.DUMMY_PASSWORD_HASH]


async def test_login_password_check_is_offloaded_while_health_remains_responsive(
    auth_client: tuple[AsyncClient, dict[str, User]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = auth_client
    main_thread_id = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    verifier_thread_ids: list[int] = []

    def blocking_verifier(password: str, password_hash: str) -> bool:
        verifier_thread_ids.append(threading.get_ident())
        entered.set()
        release.wait(timeout=1)
        return False

    monkeypatch.setattr(auth_router_module, "verify_password", blocking_verifier)
    login_task = asyncio.create_task(login(client, "grace", "compiler"))

    try:
        assert await asyncio.to_thread(entered.wait, 0.5) is True
        health_response = await client.get("/health/live")

        assert health_response.status_code == 200
        assert health_response.json() == {"status": "ok"}
        assert login_task.done() is False
        assert verifier_thread_ids
        assert all(thread_id != main_thread_id for thread_id in verifier_thread_ids)
    finally:
        release.set()
        await login_task


def test_login_password_limiter_is_explicitly_bounded() -> None:
    assert auth_router_module.password_check_limiter.total_tokens == 4


async def test_database_errors_propagate_instead_of_becoming_authentication_failures(
    isolate_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()

    class FailingSession:
        async def scalar(self, statement: Select[Any]) -> User | None:
            raise RuntimeError("database unavailable")

        async def get(self, model: type[User], identity: uuid.UUID) -> User | None:
            raise RuntimeError("database unavailable")

    async def override_session() -> AsyncIterator[FailingSession]:
        yield FailingSession()

    application = create_app()
    application.dependency_overrides[get_session] = override_session
    token = create_access_token(uuid.uuid4(), Role.STUDENT)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        with pytest.raises(RuntimeError, match="database unavailable"):
            await login(client, "grace", "compiler")
        with pytest.raises(RuntimeError, match="database unavailable"):
            await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )


def test_application_registers_auth_routes_under_api_v1() -> None:
    application: FastAPI = create_app()
    paths = application.openapi()["paths"]
    auth_router_inclusions = [
        route
        for route in application.routes
        if getattr(route, "original_router", None) is auth_router_module.router
    ]

    assert "post" in paths["/api/v1/auth/login"]
    assert "get" in paths["/api/v1/auth/me"]
    assert len(auth_router_inclusions) == 1
