from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, String, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migration_database_url() -> str:
    import conftest as conftest_module

    database_url, explicit_database_url = conftest_module._resolve_test_database_url()
    engine = create_async_engine(database_url, connect_args={"timeout": 1})
    try:
        await conftest_module._probe_test_database(engine, explicit_database_url)
    finally:
        await engine.dispose()
    return database_url


async def _assert_rejected(connection: AsyncConnection, statement: str) -> None:
    transaction = await connection.begin_nested()
    try:
        with pytest.raises(DBAPIError) as raised:
            await connection.execute(text(statement))
        assert raised.value.orig.sqlstate == "55000"
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_0008_account_creation_events_upgrade_immutability_and_downgrade(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0007_retry_generations")
    await asyncio.to_thread(command.upgrade, config, "0008_account_creation_events")

    actor_id, target_id, event_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"]: column
                    for column in inspect(sync).get_columns("account_creation_events")
                }
            )
            foreign_keys = await connection.run_sync(
                lambda sync: {
                    constraint["name"]: (
                        tuple(constraint["constrained_columns"]),
                        constraint["referred_table"],
                        tuple(constraint["referred_columns"]),
                        constraint["options"].get("ondelete"),
                    )
                    for constraint in inspect(sync).get_foreign_keys("account_creation_events")
                }
            )
            unique_names = await connection.run_sync(
                lambda sync: {
                    constraint["name"]
                    for constraint in inspect(sync).get_unique_constraints(
                        "account_creation_events"
                    )
                }
            )
            check_names = await connection.run_sync(
                lambda sync: {
                    constraint["name"]
                    for constraint in inspect(sync).get_check_constraints("account_creation_events")
                }
            )
            index_definitions = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT indexname, indexdef FROM pg_indexes "
                            "WHERE schemaname = current_schema() "
                            "AND tablename = 'account_creation_events' "
                            "AND indexname = ANY(ARRAY["
                            "'ix_account_creation_events_actor_created_id', "
                            "'ix_account_creation_events_created_id'])"
                        )
                    )
                ).all()
            )
            trigger_definitions = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                            "WHERE tgrelid = 'account_creation_events'::regclass "
                            "AND NOT tgisinternal"
                        )
                    )
                ).all()
            )
            role_values = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT enumlabel FROM pg_enum "
                            "WHERE enumtypid = 'role'::regtype ORDER BY enumsortorder"
                        )
                    )
                )
                .scalars()
                .all()
            )

            assert revision == "0008_account_creation_events"
            assert set(columns) == {
                "id",
                "created_at",
                "actor_user_id",
                "target_user_id",
                "target_role",
                "request_id",
            }
            assert all(not column["nullable"] for column in columns.values())
            assert isinstance(columns["id"]["type"], postgresql.UUID)
            assert isinstance(columns["actor_user_id"]["type"], postgresql.UUID)
            assert isinstance(columns["target_user_id"]["type"], postgresql.UUID)
            assert isinstance(columns["created_at"]["type"], DateTime)
            assert columns["created_at"]["type"].timezone is True
            assert isinstance(columns["target_role"]["type"], postgresql.ENUM)
            assert columns["target_role"]["type"].name == "role"
            assert isinstance(columns["request_id"]["type"], String)
            assert columns["request_id"]["type"].length == 128
            assert foreign_keys == {
                "fk_account_creation_events_actor_user_id_users": (
                    ("actor_user_id",),
                    "users",
                    ("id",),
                    "RESTRICT",
                ),
                "fk_account_creation_events_target_user_id_users": (
                    ("target_user_id",),
                    "users",
                    ("id",),
                    "RESTRICT",
                ),
            }
            assert unique_names == {"uq_account_creation_events_target_user_id"}
            assert check_names == {
                "ck_account_creation_events_request_id_safe",
                "ck_account_creation_events_target_role",
            }
            assert set(index_definitions) == {
                "ix_account_creation_events_actor_created_id",
                "ix_account_creation_events_created_id",
            }
            assert index_definitions["ix_account_creation_events_actor_created_id"].endswith(
                "USING btree (actor_user_id, created_at DESC, id DESC)"
            )
            assert index_definitions["ix_account_creation_events_created_id"].endswith(
                "USING btree (created_at DESC, id DESC)"
            )
            assert set(trigger_definitions) == {
                "trg_account_creation_events_no_update",
                "trg_account_creation_events_no_delete",
                "trg_account_creation_events_no_truncate",
            }
            assert (
                " BEFORE UPDATE ON " in trigger_definitions["trg_account_creation_events_no_update"]
            )
            assert " FOR EACH ROW " in trigger_definitions["trg_account_creation_events_no_update"]
            assert (
                " BEFORE DELETE ON " in trigger_definitions["trg_account_creation_events_no_delete"]
            )
            assert " FOR EACH ROW " in trigger_definitions["trg_account_creation_events_no_delete"]
            assert (
                " BEFORE TRUNCATE ON "
                in trigger_definitions["trg_account_creation_events_no_truncate"]
            )
            assert (
                " FOR EACH STATEMENT "
                in trigger_definitions["trg_account_creation_events_no_truncate"]
            )
            assert role_values == ("teacher", "student", "admin")

            await connection.execute(
                text(
                    "INSERT INTO users (id, username, display_name, role, password_hash) VALUES "
                    "(:actor, 'creation-admin', 'Admin', 'admin', 'hash'), "
                    "(:target, 'creation-student', 'Student', 'student', 'hash')"
                ),
                {"actor": actor_id, "target": target_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO account_creation_events "
                    "(id, actor_user_id, target_user_id, target_role, request_id) VALUES "
                    "(:event, :actor, :target, 'student', 'migration-request')"
                ),
                {"event": event_id, "actor": actor_id, "target": target_id},
            )
            await _assert_rejected(
                connection,
                "UPDATE account_creation_events SET request_id = 'tampered'",
            )
            await _assert_rejected(connection, "DELETE FROM account_creation_events")
            await _assert_rejected(connection, "TRUNCATE TABLE account_creation_events")
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "0007_retry_generations")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            table_exists = await connection.scalar(
                text("SELECT to_regclass('account_creation_events')")
            )
            function_exists = await connection.scalar(
                text("SELECT to_regprocedure('prevent_account_creation_event_mutation()')")
            )
        assert revision == "0007_retry_generations"
        assert table_exists is None
        assert function_exists is None
    finally:
        await engine.dispose()
