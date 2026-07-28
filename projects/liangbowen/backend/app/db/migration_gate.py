from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

# Stable registry value for the versioned domain "ai-grading-migration-gate-v1".
# Never derive this with Python's process-randomized hash().
MIGRATION_GATE_LOCK_KEY_V1 = 4_478_121_963_515_536_469
MIGRATION_GATE_SHARED_SQL_TEXT = (
    f"SELECT pg_advisory_xact_lock_shared({MIGRATION_GATE_LOCK_KEY_V1})"
)
MIGRATION_GATE_EXCLUSIVE_SQL_TEXT = f"SELECT pg_advisory_xact_lock({MIGRATION_GATE_LOCK_KEY_V1})"
MIGRATION_GATE_SHARED_SQL = text(MIGRATION_GATE_SHARED_SQL_TEXT)
MIGRATION_GATE_EXCLUSIVE_SQL = text(MIGRATION_GATE_EXCLUSIVE_SQL_TEXT)
LEGACY_QUIESCENCE_CONFIRMATION_ENV = "MIGRATION_LEGACY_PROCESSES_STOPPED"
LEGACY_QUIESCENCE_CONFIRMATION_VALUE = "true"
LEGACY_QUIESCENCE_SQL = text(
    "SELECT count(*)::integer FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND pid <> pg_backend_pid() "
    "AND backend_type = 'client backend'"
)
LEGACY_QUIESCENCE_OFFLINE_COMMENT = (
    "-- PRECONDITION: MIGRATION_LEGACY_PROCESSES_STOPPED=true confirms all legacy "
    "API, worker, beat, and shell database sessions are stopped before executing this SQL."
)


class MigrationGatedSyncSession(Session):
    """Synchronous implementation behind every production AsyncSession."""


class MigrationGatedAsyncSession(AsyncSession):
    """Async ORM session whose root transactions participate in the migration gate."""

    sync_session_class = MigrationGatedSyncSession


@event.listens_for(MigrationGatedSyncSession, "after_begin")
def _acquire_migration_gate(
    _session: Session,
    transaction: SessionTransaction,
    connection: Connection,
) -> None:
    if transaction.nested:
        return
    connection.execute(MIGRATION_GATE_SHARED_SQL)


def migration_gated_sessionmaker(bind: Any, **kwargs: Any) -> async_sessionmaker:
    return async_sessionmaker(
        bind,
        class_=MigrationGatedAsyncSession,
        **kwargs,
    )


def require_legacy_process_quiescence(
    connection: Connection,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    environment = os.environ if environ is None else environ
    if environment.get(LEGACY_QUIESCENCE_CONFIRMATION_ENV) != (
        LEGACY_QUIESCENCE_CONFIRMATION_VALUE
    ):
        raise RuntimeError("legacy migration quiescence confirmation required")

    other_connection_count = connection.scalar(LEGACY_QUIESCENCE_SQL)
    if not isinstance(other_connection_count, int) or other_connection_count != 0:
        safe_count = other_connection_count if isinstance(other_connection_count, int) else 0
        raise RuntimeError(
            f"legacy migration quiescence check failed: other client connection count={safe_count}"
        )


def require_legacy_process_quiescence_confirmation(
    *, environ: Mapping[str, str] | None = None
) -> None:
    environment = os.environ if environ is None else environ
    if environment.get(LEGACY_QUIESCENCE_CONFIRMATION_ENV) != (
        LEGACY_QUIESCENCE_CONFIRMATION_VALUE
    ):
        raise RuntimeError("legacy migration quiescence confirmation required")
