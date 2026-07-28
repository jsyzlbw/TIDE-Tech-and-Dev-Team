from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.types import Role
from app.integrations.mattermost.model import (
    CREATE_INTEGRATION_EVENT_GUARD_FUNCTION_SQL,
    CREATE_INTEGRATION_EVENT_GUARD_TRIGGER_SQL,
    CREATE_INTEGRATION_EVENT_TRUNCATE_TRIGGER_SQL,
    IntegrationEvent,
    IntegrationEventStatus,
    MattermostIdentity,
)
from app.users.model import User


async def _user(session: AsyncSession, name: str) -> User:
    user = User(
        username=f"{name}-{uuid.uuid4().hex}",
        display_name=name,
        role=Role.TEACHER,
        password_hash="hash",
    )
    session.add(user)
    await session.flush()
    return user


def test_model_and_migration_share_guard_ddl() -> None:
    migration = importlib.import_module("migrations.versions.0005_mattermost")
    assert migration.CREATE_INTEGRATION_EVENT_GUARD_FUNCTION_SQL == (
        CREATE_INTEGRATION_EVENT_GUARD_FUNCTION_SQL
    )
    assert migration.CREATE_INTEGRATION_EVENT_GUARD_TRIGGER_SQL == (
        CREATE_INTEGRATION_EVENT_GUARD_TRIGGER_SQL
    )
    assert migration.CREATE_INTEGRATION_EVENT_TRUNCATE_TRIGGER_SQL == (
        CREATE_INTEGRATION_EVENT_TRUNCATE_TRIGGER_SQL
    )


def test_models_expose_bounded_authoritative_contract() -> None:
    identity = MattermostIdentity.__table__
    event = IntegrationEvent.__table__
    assert identity.primary_key.columns.keys() == ["user_id"]
    assert next(iter(identity.c.user_id.foreign_keys)).ondelete == "RESTRICT"
    assert identity.c.mattermost_user_id.type.length == 128
    assert identity.c.mattermost_username.type.length == 128
    assert any(
        c.name == "uq_mattermost_identities_mattermost_user_id" for c in identity.constraints
    )
    assert event.c.request_hash.type.length == 64
    assert event.c.actor_user_id.nullable is False
    assert {item.value for item in IntegrationEventStatus} == {
        "processing",
        "completed",
        "deterministic_error",
    }


@pytest.mark.asyncio
async def test_identity_authority_and_bounds(postgres_session: AsyncSession) -> None:
    first = await _user(postgres_session, "first")
    second = await _user(postgres_session, "second")
    second_id = second.id
    postgres_session.add(
        MattermostIdentity(
            user_id=first.id,
            mattermost_user_id="mm-first",
            mattermost_username="First Display",
        )
    )
    await postgres_session.commit()

    postgres_session.add(
        MattermostIdentity(
            user_id=second_id,
            mattermost_user_id="mm-first",
            mattermost_username="Second Display",
        )
    )
    with pytest.raises(IntegrityError):
        await postgres_session.flush()
    await postgres_session.rollback()

    for bad_id in ("", "x" * 129):
        postgres_session.add(
            MattermostIdentity(
                user_id=second_id,
                mattermost_user_id=bad_id,
                mattermost_username="Display",
            )
        )
        with pytest.raises(DBAPIError):
            await postgres_session.flush()
        await postgres_session.rollback()


@pytest.mark.asyncio
async def test_identity_allows_display_refresh_but_rejects_rebind_delete_and_truncate(
    postgres_session: AsyncSession,
) -> None:
    actor = await _user(postgres_session, "identity")
    identity = MattermostIdentity(
        user_id=actor.id,
        mattermost_user_id="mm-authority",
        mattermost_username="Old Display",
    )
    postgres_session.add(identity)
    await postgres_session.commit()
    actor_id = actor.id

    await postgres_session.execute(
        update(MattermostIdentity)
        .where(MattermostIdentity.user_id == actor_id)
        .values(mattermost_username="New Display")
    )
    await postgres_session.commit()

    with pytest.raises(DBAPIError, match="mattermost identity authority is immutable"):
        await postgres_session.execute(
            delete(MattermostIdentity).where(MattermostIdentity.user_id == actor_id)
        )
    await postgres_session.rollback()

    with pytest.raises(DBAPIError, match="mattermost identity authority cannot be truncated"):
        await postgres_session.execute(text("TRUNCATE mattermost_identities CASCADE"))
    await postgres_session.rollback()


def _event(user_id: uuid.UUID, **overrides: object) -> IntegrationEvent:
    values: dict[str, object] = {
        "request_hash": "a" * 64,
        "actor_user_id": user_id,
        "status": IntegrationEventStatus.PROCESSING,
        "business_refs": {},
    }
    values.update(overrides)
    return IntegrationEvent(**values)


@pytest.mark.asyncio
async def test_event_shape_constraints(postgres_session: AsyncSession) -> None:
    actor = await _user(postgres_session, "actor")
    actor_id = actor.id
    await postgres_session.commit()
    invalid = (
        {"request_hash": "A" * 64},
        {"source": "slack"},
        {"event_type": "webhook"},
        {"response": []},
        {"business_refs": []},
        {"response": {"message": "x" * (64 * 1024)}},
        {"business_refs": {"id": "x" * (8 * 1024)}},
        {"status": IntegrationEventStatus.COMPLETED},
        {
            "status": IntegrationEventStatus.PROCESSING,
            "response": {"response_type": "ephemeral", "text": "bad"},
        },
    )
    for index, overrides in enumerate(invalid):
        values = {"request_hash": f"{index:064x}", **overrides}
        event = _event(actor_id, **values)
        postgres_session.add(event)
        with pytest.raises(IntegrityError):
            await postgres_session.flush()
        await postgres_session.rollback()


@pytest.mark.asyncio
async def test_event_allows_only_one_terminal_transition_then_is_immutable(
    postgres_session: AsyncSession,
) -> None:
    actor = await _user(postgres_session, "actor")
    event = _event(actor.id)
    postgres_session.add(event)
    await postgres_session.flush()
    event_id = event.id
    await postgres_session.commit()

    response = {"response_type": "ephemeral", "text": "done"}
    await postgres_session.execute(
        update(IntegrationEvent)
        .where(IntegrationEvent.id == event_id)
        .values(
            status=IntegrationEventStatus.COMPLETED,
            response=response,
            business_refs={"assignment_id": str(uuid.uuid4())},
            completed_at=datetime.now(UTC),
        )
    )
    await postgres_session.commit()
    await postgres_session.refresh(event)
    assert event.response == response

    mutations = (
        {"response": {"response_type": "ephemeral", "text": "rewritten"}},
        {"request_hash": "b" * 64},
        {"actor_user_id": uuid.uuid4()},
    )
    for values in mutations:
        with pytest.raises(DBAPIError, match="integration event evidence is immutable"):
            await postgres_session.execute(
                update(IntegrationEvent).where(IntegrationEvent.id == event_id).values(**values)
            )
        await postgres_session.rollback()

    with pytest.raises(DBAPIError, match="integration event evidence is immutable"):
        await postgres_session.execute(
            delete(IntegrationEvent).where(IntegrationEvent.id == event_id)
        )
    await postgres_session.rollback()

    with pytest.raises(DBAPIError, match="integration event evidence cannot be truncated"):
        await postgres_session.execute(text("TRUNCATE integration_events"))
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_processing_event_cannot_change_identity_or_arrival(
    postgres_session: AsyncSession,
) -> None:
    actor = await _user(postgres_session, "actor")
    event = _event(actor.id)
    postgres_session.add(event)
    await postgres_session.flush()
    event_id = event.id
    await postgres_session.commit()

    for values in (
        {"request_hash": "b" * 64},
        {"arrived_at": datetime.now(UTC)},
        {"business_refs": {"premature": True}},
    ):
        with pytest.raises(DBAPIError, match="invalid integration event transition"):
            await postgres_session.execute(
                update(IntegrationEvent).where(IntegrationEvent.id == event_id).values(**values)
            )
        await postgres_session.rollback()
