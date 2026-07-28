from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import event, text

from app.db.migration_gate import (
    LEGACY_QUIESCENCE_SQL,
    MIGRATION_GATE_LOCK_KEY_V1,
    MIGRATION_GATE_SHARED_SQL_TEXT,
    migration_gated_sessionmaker,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_root_transaction_takes_gate_as_first_sql_and_skips_savepoint(
    postgres_session,
) -> None:
    engine = postgres_session.bind
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(text("SELECT 41"))
            async with session.begin_nested():
                await session.execute(text("SELECT 42"))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)

    assert statements[0] == MIGRATION_GATE_SHARED_SQL_TEXT
    assert statements.count(MIGRATION_GATE_SHARED_SQL_TEXT) == 1
    assert any(statement.startswith("SAVEPOINT ") for statement in statements)


@pytest.mark.asyncio
async def test_gate_reacquires_for_each_root_transaction(postgres_session) -> None:
    engine = postgres_session.bind
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            await session.commit()
            await session.execute(text("SELECT 2"))
            await session.rollback()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)

    assert statements.count(MIGRATION_GATE_SHARED_SQL_TEXT) == 2


@pytest.mark.asyncio
async def test_shared_gate_is_concurrent_and_rollback_releases_it(postgres_session) -> None:
    engine = postgres_session.bind
    factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
    first = factory()
    second = factory()
    try:
        await first.execute(text("SELECT 1"))
        await asyncio.wait_for(second.execute(text("SELECT 2")), timeout=0.5)
        async with engine.begin() as observer:
            assert not await observer.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": MIGRATION_GATE_LOCK_KEY_V1},
            )

        await first.rollback()
        async with engine.begin() as observer:
            assert not await observer.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": MIGRATION_GATE_LOCK_KEY_V1},
            )

        await second.rollback()
        async with engine.begin() as observer:
            assert await observer.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": MIGRATION_GATE_LOCK_KEY_V1},
            )
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_session_close_releases_gate_before_connection_pool_reuse(postgres_session) -> None:
    engine = postgres_session.bind
    factory = migration_gated_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("SELECT 1"))

    async with engine.begin() as observer:
        assert await observer.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": MIGRATION_GATE_LOCK_KEY_V1},
        )


def test_every_production_orm_factory_uses_the_migration_gate() -> None:
    offenders: list[str] = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        if path.name == "migration_gate.py":
            continue
        if "async_sessionmaker(" in path.read_text():
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    seed_source = (BACKEND_ROOT / "scripts/seed_demo.py").read_text()
    if "async_sessionmaker(" in seed_source:
        offenders.append("scripts/seed_demo.py")
    assert offenders == []


def test_gate_key_has_one_production_literal_source() -> None:
    locations: list[str] = []
    production_paths = [
        *(BACKEND_ROOT / "app").rglob("*.py"),
        *(BACKEND_ROOT / "migrations").rglob("*.py"),
        *(BACKEND_ROOT / "scripts").rglob("*.py"),
    ]
    for path in production_paths:
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Constant) and node.value == MIGRATION_GATE_LOCK_KEY_V1
            for node in ast.walk(tree)
        ):
            locations.append(str(path.relative_to(BACKEND_ROOT)))

    assert locations == ["app/db/migration_gate.py"]


def test_legacy_quiescence_query_is_count_only_and_scoped_to_client_backends() -> None:
    query = " ".join(str(LEGACY_QUIESCENCE_SQL).split())
    assert query == (
        "SELECT count(*)::integer FROM pg_stat_activity "
        "WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend'"
    )
    assert all(
        sensitive_column not in query
        for sensitive_column in ("usename", "query", "client_addr", "application_name")
    )


def test_legacy_quiescence_precedes_exclusive_gate_and_migration_operations() -> None:
    tree = ast.parse((BACKEND_ROOT / "migrations/env.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_sync_migrations"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    preflight_line = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "require_legacy_process_quiescence"
    )
    gate_line = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "MIGRATION_GATE_EXCLUSIVE_SQL"
    )
    migration_line = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "run_migrations"
    )
    assert preflight_line < gate_line < migration_line
