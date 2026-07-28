from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.session import get_session
from app.db.types import Role
from app.integrations.mattermost.model import MattermostIdentity
from app.integrations.mattermost.schemas import DemoBindingRequest
from app.integrations.mattermost.service import _is_unique_violation
from app.main import create_app
from app.users.model import User
from app.users.schemas import UserCreate
from app.users.service import create_user

PATH = "/api/v1/integrations/mattermost/demo-bindings"


@pytest.mark.parametrize("mattermost_user_id", ["bad/id", "_bad-leading"])
def test_demo_binding_request_uses_shared_identifier_contract(
    mattermost_user_id: str,
) -> None:
    with pytest.raises(ValidationError):
        DemoBindingRequest(
            local_username="teacher",
            mattermost_user_id=mattermost_user_id,
            mattermost_username="teacher",
        )


@pytest.mark.parametrize(("sqlstate", "expected"), [("23505", True), ("23514", False)])
def test_demo_binding_distinguishes_unique_race_from_constraint_failure(
    sqlstate: str,
    expected: bool,
) -> None:
    class DatabaseCause(Exception):
        pass

    cause = DatabaseCause("redacted")
    cause.sqlstate = sqlstate  # type: ignore[attr-defined]
    error = IntegrityError("insert", {}, cause)
    assert _is_unique_violation(error) is expected


@pytest.mark.parametrize("mattermost_user_id", ["bad/id", "_bad-leading"])
@pytest.mark.asyncio
async def test_demo_binding_invalid_external_id_is_422_before_database(
    mattermost_user_id: str,
) -> None:
    application = create_app(_settings())
    session_requested = False

    async def fail_session():
        nonlocal session_requested
        session_requested = True
        raise AssertionError("database must not run for an invalid identity")
        yield  # pragma: no cover

    application.dependency_overrides[get_session] = fail_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            PATH,
            json={
                "local_username": "teacher",
                "mattermost_user_id": mattermost_user_id,
                "mattermost_username": "teacher",
            },
            headers={"x-demo-setup-key": "demo-secret"},
        )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid demo binding request"}
    assert session_requested is False


def _settings() -> Settings:
    return Settings(
        app_env="test",
        jwt_secret="test-only",
        mattermost_command_token=SecretStr("slash-secret"),
        mattermost_demo_setup_key=SecretStr("demo-secret"),
    )


@pytest.mark.asyncio
async def test_demo_binding_is_idempotent_refreshable_and_non_reassignable(
    postgres_session,
) -> None:
    teacher = User(
        username=f"bind-teacher-{uuid.uuid4().hex}",
        display_name="Teacher",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=True,
    )
    student = User(
        username=f"bind-student-{uuid.uuid4().hex}",
        display_name="Student",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add_all([teacher, student])
    await postgres_session.commit()
    teacher_id = teacher.id
    teacher_username = teacher.username
    student_username = student.username

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    payload = {
        "local_username": teacher_username,
        "mattermost_user_id": "mm-teacher",
        "mattermost_username": "teacher-old",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        missing = await client.post(PATH, json=payload)
        wrong = await client.post(PATH, json=payload, headers={"x-demo-setup-key": "wrong"})
        created = await client.post(
            PATH,
            json=payload,
            headers={"x-demo-setup-key": "demo-secret"},
        )
        refreshed = await client.post(
            PATH,
            json={**payload, "mattermost_username": "teacher-new"},
            headers={"x-demo-setup-key": "demo-secret"},
        )
        reassigned = await client.post(
            PATH,
            json={**payload, "local_username": student_username},
            headers={"x-demo-setup-key": "demo-secret"},
        )

    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json() == {"detail": "invalid demo setup authentication"}
    assert created.status_code == 201
    assert refreshed.status_code == 200
    assert refreshed.json()["mattermost_username"] == "teacher-new"
    assert reassigned.status_code == 409
    identities = (await postgres_session.execute(select(MattermostIdentity))).scalars().all()
    assert len(identities) == 1
    assert identities[0].user_id == teacher_id
    assert identities[0].mattermost_username == "teacher-new"


@pytest.mark.asyncio
async def test_demo_binding_rejects_unknown_inactive_and_admin_accounts(postgres_session) -> None:
    users = [
        User(
            username=f"inactive-{uuid.uuid4().hex}",
            display_name="Inactive",
            role=Role.STUDENT,
            password_hash="hash",
            is_active=False,
        ),
        User(
            username=f"admin-{uuid.uuid4().hex}",
            display_name="Admin",
            role=Role.ADMIN,
            password_hash="hash",
            is_active=True,
        ),
    ]
    postgres_session.add_all(users)
    await postgres_session.commit()

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        for username in ("missing", users[0].username, users[1].username):
            response = await client.post(
                PATH,
                json={
                    "local_username": username,
                    "mattermost_user_id": f"mm-{username}",
                    "mattermost_username": username,
                },
                headers={"x-demo-setup-key": "demo-secret"},
            )
            assert response.status_code == 400

    assert await postgres_session.scalar(select(MattermostIdentity)) is None


@pytest.mark.asyncio
async def test_local_account_creation_stays_unbound_until_explicit_supported_binding(
    postgres_session,
) -> None:
    admin = User(
        username=f"account-admin-{uuid.uuid4().hex}",
        display_name="Account Admin",
        role=Role.ADMIN,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add(admin)
    await postgres_session.commit()
    created = []
    for role in (Role.TEACHER, Role.STUDENT):
        created.append(
            await create_user(
                postgres_session,
                admin.id,
                UserCreate(
                    username=f"new-{role.value}-{uuid.uuid4().hex}",
                    display_name=f"New {role.value.title()}",
                    role=role,
                    password="Course2026!Secure",
                ),
                f"mattermost-create-{role.value}",
            )
        )

    assert await postgres_session.scalar(select(MattermostIdentity)) is None
    await postgres_session.commit()

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        accepted = []
        for account in created:
            accepted.append(
                await client.post(
                    PATH,
                    json={
                        "local_username": account.username,
                        "mattermost_user_id": f"mm-{account.role.value}-new",
                        "mattermost_username": f"mm_{account.role.value}_new",
                    },
                    headers={"x-demo-setup-key": "demo-secret"},
                )
            )
        rejected_admin = await client.post(
            PATH,
            json={
                "local_username": admin.username,
                "mattermost_user_id": "mm-admin-new",
                "mattermost_username": "mm_admin_new",
            },
            headers={"x-demo-setup-key": "demo-secret"},
        )

    assert [response.status_code for response in accepted] == [201, 201]
    assert rejected_admin.status_code == 400
    assert rejected_admin.json() == {"detail": "local account is not eligible"}
    identities = (await postgres_session.scalars(select(MattermostIdentity))).all()
    assert {identity.user_id for identity in identities} == {account.id for account in created}
