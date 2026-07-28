from __future__ import annotations

import uuid

import pytest
from sqlalchemy import CheckConstraint, Enum, String, delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import operators

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import Role
from app.users.creation_event import AccountCreationEvent
from app.users.model import User


def _constraint_sql(constraint: CheckConstraint) -> str:
    return " ".join(str(constraint.sqltext).split())


def _index_expression_signature(expression: object) -> tuple[str, str]:
    element = getattr(expression, "element", expression)
    direction = "desc" if getattr(expression, "modifier", None) is operators.desc_op else "asc"
    return element.name, direction


def test_account_creation_event_metadata_contract() -> None:
    table = Base.metadata.tables["account_creation_events"]

    assert table is AccountCreationEvent.__table__
    assert issubclass(AccountCreationEvent, UUIDPrimaryKeyMixin)
    assert issubclass(AccountCreationEvent, CreatedAtMixin)
    assert set(table.c) == {
        table.c.id,
        table.c.created_at,
        table.c.actor_user_id,
        table.c.target_user_id,
        table.c.target_role,
        table.c.request_id,
    }
    assert all(not column.nullable for column in table.c)

    assert isinstance(table.c.request_id.type, String)
    assert table.c.request_id.type.length == 128
    assert isinstance(table.c.target_role.type, Enum)
    assert table.c.target_role.type.name == "role"
    assert table.c.target_role.type.enums == ["teacher", "student", "admin"]

    foreign_keys = {
        foreign_key.parent.name: (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in table.foreign_keys
    }
    assert foreign_keys == {
        "actor_user_id": ("users.id", "RESTRICT"),
        "target_user_id": ("users.id", "RESTRICT"),
    }
    assert {
        constraint.name
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    } == {"uq_account_creation_events_target_user_id"}

    checks = {
        constraint.name: _constraint_sql(constraint)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_account_creation_events_target_role": "target_role IN ('teacher', 'student')",
        "ck_account_creation_events_request_id_safe": (
            "octet_length(request_id) BETWEEN 1 AND 128 "
            "AND request_id = btrim(request_id) "
            "AND request_id !~ '[[:cntrl:]]'"
        ),
    }

    indexes = {
        index.name: tuple(
            _index_expression_signature(expression) for expression in index.expressions
        )
        for index in table.indexes
    }
    assert indexes == {
        "ix_account_creation_events_actor_created_id": (
            ("actor_user_id", "asc"),
            ("created_at", "desc"),
            ("id", "desc"),
        ),
        "ix_account_creation_events_created_id": (
            ("created_at", "desc"),
            ("id", "desc"),
        ),
    }


async def _add_user(session: AsyncSession, role: Role) -> User:
    user = User(
        id=uuid.uuid4(),
        username=f"{role.value}-{uuid.uuid4().hex}",
        display_name=role.value.title(),
        role=role,
        password_hash="hash",
    )
    session.add(user)
    await session.flush([user])
    return user


@pytest.mark.asyncio
async def test_account_creation_event_database_checks_role_and_request_id_boundaries(
    postgres_session: AsyncSession,
) -> None:
    actor = await _add_user(postgres_session, Role.ADMIN)

    for target_role, request_id in (
        (Role.TEACHER, "t"),
        (Role.STUDENT, "x" * 128),
        (Role.STUDENT, "😀" * 32),
    ):
        target = await _add_user(postgres_session, target_role)
        postgres_session.add(
            AccountCreationEvent(
                actor_user_id=actor.id,
                target_user_id=target.id,
                target_role=target_role,
                request_id=request_id,
            )
        )
        await postgres_session.flush()

    invalid_values = (
        (Role.ADMIN, "admin-target", "23514"),
        (Role.STUDENT, "", "23514"),
        (Role.STUDENT, "x" * 129, "22001"),
        (Role.STUDENT, "😀" * 33, "23514"),
        (Role.STUDENT, " leading", "23514"),
        (Role.STUDENT, "trailing ", "23514"),
        (Role.STUDENT, "line\nbreak", "23514"),
    )
    for target_role, request_id, sqlstate in invalid_values:
        target = await _add_user(postgres_session, target_role)
        with pytest.raises(DBAPIError) as raised:
            async with postgres_session.begin_nested():
                postgres_session.add(
                    AccountCreationEvent(
                        actor_user_id=actor.id,
                        target_user_id=target.id,
                        target_role=target_role,
                        request_id=request_id,
                    )
                )
                await postgres_session.flush()
        assert raised.value.orig.sqlstate == sqlstate


@pytest.mark.asyncio
async def test_account_creation_event_target_user_is_unique(
    postgres_session: AsyncSession,
) -> None:
    actor = await _add_user(postgres_session, Role.ADMIN)
    target = await _add_user(postgres_session, Role.STUDENT)
    postgres_session.add(
        AccountCreationEvent(
            actor_user_id=actor.id,
            target_user_id=target.id,
            target_role=Role.STUDENT,
            request_id="first-request",
        )
    )
    await postgres_session.flush()

    with pytest.raises(IntegrityError):
        async with postgres_session.begin_nested():
            postgres_session.add(
                AccountCreationEvent(
                    actor_user_id=actor.id,
                    target_user_id=target.id,
                    target_role=Role.STUDENT,
                    request_id="duplicate-request",
                )
            )
            await postgres_session.flush()


@pytest.mark.asyncio
async def test_account_creation_event_rejects_update_delete_and_truncate(
    postgres_session: AsyncSession,
) -> None:
    actor = await _add_user(postgres_session, Role.ADMIN)
    target = await _add_user(postgres_session, Role.STUDENT)
    event = AccountCreationEvent(
        actor_user_id=actor.id,
        target_user_id=target.id,
        target_role=Role.STUDENT,
        request_id="request-123",
    )
    postgres_session.add(event)
    await postgres_session.flush()

    statements = (
        update(AccountCreationEvent)
        .where(AccountCreationEvent.id == event.id)
        .values(request_id="tampered"),
        delete(AccountCreationEvent).where(AccountCreationEvent.id == event.id),
        text("TRUNCATE TABLE account_creation_events"),
    )
    for statement in statements:
        with pytest.raises(DBAPIError) as raised:
            async with postgres_session.begin_nested():
                await postgres_session.execute(statement)
        assert raised.value.orig.sqlstate == "55000"

    count = await postgres_session.scalar(text("SELECT count(*) FROM account_creation_events"))
    assert count == 1
    trigger_definitions = dict(
        (
            await postgres_session.execute(
                text(
                    "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                    "WHERE tgrelid = 'account_creation_events'::regclass AND NOT tgisinternal"
                )
            )
        ).all()
    )
    assert set(trigger_definitions) == {
        "trg_account_creation_events_no_update",
        "trg_account_creation_events_no_delete",
        "trg_account_creation_events_no_truncate",
    }
    assert " BEFORE UPDATE ON " in trigger_definitions["trg_account_creation_events_no_update"]
    assert " FOR EACH ROW " in trigger_definitions["trg_account_creation_events_no_update"]
    assert " BEFORE DELETE ON " in trigger_definitions["trg_account_creation_events_no_delete"]
    assert " FOR EACH ROW " in trigger_definitions["trg_account_creation_events_no_delete"]
    assert " BEFORE TRUNCATE ON " in trigger_definitions["trg_account_creation_events_no_truncate"]
    assert " FOR EACH STATEMENT " in trigger_definitions["trg_account_creation_events_no_truncate"]
