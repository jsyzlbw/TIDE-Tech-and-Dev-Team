from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import verify_password
from app.db.types import Role
from app.users.creation_event import AccountCreationEvent
from app.users.model import User
from app.users.schemas import UserAccountPage, UserCreate
from app.users.service import (
    AccountRoleForbidden,
    UsernameAlreadyExists,
    create_user,
    list_users,
    password_hash_limiter,
)


def payload(
    username: str,
    role: Role,
    *,
    display_name: str | None = None,
    password: str = "correct horse battery staple",
) -> UserCreate:
    return UserCreate(
        username=username,
        display_name=display_name or username.replace(".", " ").title(),
        role=role,
        password=password,
    )


async def add_user(
    session: AsyncSession,
    username: str,
    role: Role,
    *,
    user_id: uuid.UUID | None = None,
    active: bool = True,
    created_at: datetime | None = None,
) -> User:
    user = User(
        id=user_id if user_id is not None else uuid.uuid4(),
        username=username,
        display_name=username.replace(".", " ").title(),
        role=role,
        password_hash="existing-hash",
        is_active=active,
    )
    if created_at is not None:
        user.created_at = created_at
    session.add(user)
    await session.flush([user])
    return user


@pytest.mark.parametrize(
    ("actor_role", "target_role", "allowed"),
    [
        (Role.ADMIN, Role.ADMIN, False),
        (Role.ADMIN, Role.TEACHER, True),
        (Role.ADMIN, Role.STUDENT, True),
        (Role.TEACHER, Role.ADMIN, False),
        (Role.TEACHER, Role.TEACHER, False),
        (Role.TEACHER, Role.STUDENT, True),
        (Role.STUDENT, Role.ADMIN, False),
        (Role.STUDENT, Role.TEACHER, False),
        (Role.STUDENT, Role.STUDENT, False),
    ],
)
@pytest.mark.asyncio
async def test_create_user_enforces_complete_role_matrix(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    actor_role: Role,
    target_role: Role,
    allowed: bool,
) -> None:
    from app.users import service

    monkeypatch.setattr(service, "hash_password", lambda _: "test-hash")
    actor = await add_user(postgres_session, f"actor.{actor_role.value}", actor_role)
    await postgres_session.commit()
    new_user = payload(f"new.{actor_role.value}.{target_role.value}", target_role)

    if allowed:
        result = await create_user(postgres_session, actor.id, new_user, "matrix-request")
        assert result.role is target_role
    else:
        with pytest.raises(AccountRoleForbidden):
            await create_user(postgres_session, actor.id, new_user, "matrix-request")
        assert (
            await postgres_session.scalar(
                select(func.count()).select_from(User).where(User.username == new_user.username)
            )
            == 0
        )


@pytest.mark.parametrize("actor_kind", ["missing", "inactive"])
@pytest.mark.asyncio
async def test_create_user_rejects_missing_or_inactive_actor(
    postgres_session: AsyncSession,
    actor_kind: str,
) -> None:
    actor_id = uuid.uuid4()
    if actor_kind == "inactive":
        actor = await add_user(postgres_session, "inactive.admin", Role.ADMIN, active=False)
        actor_id = actor.id
        await postgres_session.commit()

    with pytest.raises(AccountRoleForbidden):
        await create_user(
            postgres_session,
            actor_id,
            payload("blocked.student", Role.STUDENT),
            "inactive-request",
        )


@pytest.mark.asyncio
async def test_create_user_hashes_password_and_returns_password_free_audited_account(
    postgres_session: AsyncSession,
) -> None:
    actor = await add_user(postgres_session, "admin.creator", Role.ADMIN)
    await postgres_session.commit()
    raw_password = "Long secure password 😀"

    result = await create_user(
        postgres_session,
        actor.id,
        payload("new.teacher", Role.TEACHER, password=raw_password),
        "request-verified-hash",
    )

    stored = await postgres_session.scalar(select(User).where(User.id == result.id))
    event = await postgres_session.scalar(
        select(AccountCreationEvent).where(AccountCreationEvent.target_user_id == result.id)
    )
    assert stored is not None
    assert stored.is_active is True
    assert stored.password_hash != raw_password
    assert verify_password(raw_password, stored.password_hash)
    assert event is not None
    assert event.actor_user_id == actor.id
    assert event.target_role is Role.TEACHER
    assert event.request_id == "request-verified-hash"
    assert result.created_by is not None
    assert result.created_by.id == actor.id
    assert not ({"password", "password_hash"} & result.model_dump().keys())
    assert raw_password not in repr(result.model_dump())
    assert stored.password_hash not in repr(result.model_dump())


class FocusedCreateSession:
    def __init__(self, actor: User) -> None:
        self.actor = actor
        self.added: list[object] = []
        self.statements: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, statement: object) -> User:
        self.statements.append(statement)
        return self.actor

    def add(self, instance: object) -> None:
        if isinstance(instance, User):
            instance.id = instance.id or uuid.uuid4()
            instance.created_at = instance.created_at or datetime.now(UTC)
        self.added.append(instance)

    async def flush(self, objects: list[object] | None = None) -> None:
        del objects

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_create_user_offloads_hashing_from_event_loop_with_bounded_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.users import service

    actor = User(
        id=uuid.uuid4(),
        username="admin.thread",
        display_name="Admin Thread",
        role=Role.ADMIN,
        password_hash="existing-hash",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    session = FocusedCreateSession(actor)
    event_loop_thread = threading.get_ident()
    hash_thread: int | None = None
    observed_limiter: object | None = None
    original_run_sync = service.to_thread.run_sync

    def observed_hash(_: str) -> str:
        nonlocal hash_thread
        hash_thread = threading.get_ident()
        return "threaded-hash"

    async def observed_run_sync(function: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal observed_limiter
        observed_limiter = kwargs.get("limiter")
        return await original_run_sync(function, *args, **kwargs)

    monkeypatch.setattr(service, "hash_password", observed_hash)
    monkeypatch.setattr(service.to_thread, "run_sync", observed_run_sync)

    await create_user(
        session,  # type: ignore[arg-type]
        actor.id,
        payload("thread.student", Role.STUDENT),
        "thread-request",
    )

    assert hash_thread is not None
    assert hash_thread != event_loop_thread
    assert observed_limiter is password_hash_limiter
    assert password_hash_limiter.total_tokens == 4
    actor_select = session.statements[0]
    assert getattr(actor_select, "_for_update_arg", None) is not None


@pytest.mark.asyncio
async def test_create_user_rolls_back_user_when_audit_insert_fails(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.users import service

    monkeypatch.setattr(service, "hash_password", lambda _: "test-hash")
    actor = await add_user(postgres_session, "admin.audit", Role.ADMIN)
    await postgres_session.commit()

    with pytest.raises(IntegrityError):
        await create_user(
            postgres_session,
            actor.id,
            payload("rolled.back", Role.STUDENT),
            "invalid\nrequest",
        )

    assert (
        await postgres_session.scalar(
            select(func.count()).select_from(User).where(User.username == "rolled.back")
        )
        == 0
    )
    assert (
        await postgres_session.scalar(select(func.count()).select_from(AccountCreationEvent)) == 0
    )


@pytest.mark.asyncio
async def test_create_user_maps_only_username_unique_constraint(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.users import service

    monkeypatch.setattr(service, "hash_password", lambda _: "test-hash")
    actor = await add_user(postgres_session, "admin.duplicate", Role.ADMIN)
    await postgres_session.commit()
    duplicate = payload("same.student", Role.STUDENT)
    await create_user(postgres_session, actor.id, duplicate, "first-request")

    with pytest.raises(UsernameAlreadyExists):
        await create_user(postgres_session, actor.id, duplicate, "second-request")

    assert (
        await postgres_session.scalar(
            select(func.count()).select_from(User).where(User.username == duplicate.username)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(AccountCreationEvent)
            .join(User, User.id == AccountCreationEvent.target_user_id)
            .where(User.username == duplicate.username)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_create_user_does_not_disguise_non_username_integrity_error(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.users import service

    monkeypatch.setattr(service, "hash_password", lambda _: "test-hash")
    actor = await add_user(postgres_session, "admin.constraint", Role.ADMIN)
    await postgres_session.commit()

    with pytest.raises(IntegrityError) as raised:
        await create_user(
            postgres_session,
            actor.id,
            payload("constraint.student", Role.STUDENT),
            "bad\nrequest-id",
        )

    assert not isinstance(raised.value, UsernameAlreadyExists)
    assert getattr(raised.value.orig, "sqlstate", None) == "23514"


@pytest.mark.asyncio
async def test_concurrent_distinct_actors_same_username_create_one_user_and_event(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.users import service

    hash_barrier = threading.Barrier(2)

    def synchronized_hash(_: str) -> str:
        hash_barrier.wait(timeout=5)
        return "test-hash"

    monkeypatch.setattr(service, "hash_password", synchronized_hash)
    actors = (
        await add_user(postgres_session, "admin.concurrent.one", Role.ADMIN),
        await add_user(postgres_session, "admin.concurrent.two", Role.ADMIN),
    )
    assert actors[0].id != actors[1].id
    await postgres_session.commit()
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    duplicate = payload("race.student", Role.STUDENT)

    async def attempt(
        actor_id: uuid.UUID,
        request_id: str,
    ) -> UserAccountPage | Exception | object:
        async with session_factory() as concurrent_session:
            try:
                return await create_user(concurrent_session, actor_id, duplicate, request_id)
            except Exception as exc:  # noqa: BLE001 - result is asserted below
                return exc

    results = await asyncio.gather(
        attempt(actors[0].id, "race-one"),
        attempt(actors[1].id, "race-two"),
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, UsernameAlreadyExists) for result in results) == 1
    assert (
        await postgres_session.scalar(
            select(func.count()).select_from(User).where(User.username == duplicate.username)
        )
        == 1
    )
    winning_actor_id = await postgres_session.scalar(
        select(AccountCreationEvent.actor_user_id)
        .join(User, User.id == AccountCreationEvent.target_user_id)
        .where(User.username == duplicate.username)
    )
    assert winning_actor_id in {actor.id for actor in actors}
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(AccountCreationEvent)
            .join(User, User.id == AccountCreationEvent.target_user_id)
            .where(User.username == duplicate.username)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_list_users_applies_visibility_count_order_pagination_and_creator_join(
    postgres_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    tied_created_at = now - timedelta(minutes=2)
    lower_tie_id = uuid.UUID(int=100)
    higher_tie_id = uuid.UUID(int=200)
    admin = await add_user(
        postgres_session,
        "admin.list",
        Role.ADMIN,
        created_at=now - timedelta(minutes=5),
    )
    teacher = await add_user(
        postgres_session,
        "teacher.list",
        Role.TEACHER,
        created_at=now - timedelta(minutes=4),
    )
    student_without_creator = await add_user(
        postgres_session,
        "student.direct",
        Role.STUDENT,
        user_id=lower_tie_id,
        created_at=tied_created_at,
    )
    student_with_creator = await add_user(
        postgres_session,
        "student.created",
        Role.STUDENT,
        user_id=higher_tie_id,
        created_at=tied_created_at,
    )
    newest_teacher = await add_user(
        postgres_session,
        "teacher.newest",
        Role.TEACHER,
        created_at=now - timedelta(minutes=1),
    )
    postgres_session.add(
        AccountCreationEvent(
            actor_user_id=teacher.id,
            target_user_id=student_with_creator.id,
            target_role=Role.STUDENT,
            request_id="list-created-student",
        )
    )
    await postgres_session.commit()

    admin_page = await list_users(postgres_session, admin.id, limit=3, offset=1)
    assert admin_page.total == 5
    assert admin_page.limit == 3
    assert admin_page.offset == 1
    assert student_with_creator.created_at == student_without_creator.created_at
    assert student_with_creator.id > student_without_creator.id
    assert [item.id for item in admin_page.items] == [
        student_with_creator.id,
        student_without_creator.id,
        teacher.id,
    ]
    by_id = {item.id: item for item in admin_page.items}
    assert by_id[student_without_creator.id].created_by is None
    assert by_id[student_with_creator.id].created_by is not None
    assert by_id[student_with_creator.id].created_by.id == teacher.id

    teacher_page = await list_users(postgres_session, teacher.id, limit=20, offset=0)
    assert teacher_page.total == 2
    assert [item.id for item in teacher_page.items] == [
        student_with_creator.id,
        student_without_creator.id,
    ]
    assert newest_teacher.id not in {item.id for item in teacher_page.items}


@pytest.mark.parametrize("actor_kind", ["student", "inactive", "missing"])
@pytest.mark.asyncio
async def test_list_users_rejects_ineligible_actor(
    postgres_session: AsyncSession,
    actor_kind: str,
) -> None:
    actor_id = uuid.uuid4()
    if actor_kind != "missing":
        actor = await add_user(
            postgres_session,
            f"{actor_kind}.list",
            Role.STUDENT if actor_kind == "student" else Role.ADMIN,
            active=actor_kind != "inactive",
        )
        actor_id = actor.id
        await postgres_session.commit()

    with pytest.raises(AccountRoleForbidden):
        await list_users(postgres_session, actor_id, limit=20, offset=0)


@pytest.mark.asyncio
async def test_services_refresh_stale_actor_role_from_database(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.users import service

    monkeypatch.setattr(service, "hash_password", lambda _: "test-hash")
    actor = await add_user(postgres_session, "actor.stale", Role.TEACHER)
    await postgres_session.commit()
    cached_actor = await postgres_session.get(User, actor.id)
    assert cached_actor is actor
    assert cached_actor.role is Role.TEACHER

    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    async with session_factory() as external_session:
        await external_session.execute(
            update(User).where(User.id == actor.id).values(role=Role.ADMIN)
        )
        await external_session.commit()

    created = await create_user(
        postgres_session,
        actor.id,
        payload("dbtruth.teacher", Role.TEACHER),
        "dbtruth-request",
    )
    assert created.role is Role.TEACHER

    async with session_factory() as external_session:
        await external_session.execute(
            update(User).where(User.id == actor.id).values(is_active=False)
        )
        await external_session.commit()

    with pytest.raises(AccountRoleForbidden):
        await list_users(postgres_session, actor.id, limit=20, offset=0)
