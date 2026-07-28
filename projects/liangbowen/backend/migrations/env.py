from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.script.revision import RangeNotAncestorError
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.assignments.model import Assignment
from app.audit.model import AuditLog
from app.core.config import get_settings
from app.db.base import Base
from app.db.migration_gate import (
    LEGACY_QUIESCENCE_OFFLINE_COMMENT,
    MIGRATION_GATE_EXCLUSIVE_SQL,
    require_legacy_process_quiescence,
    require_legacy_process_quiescence_confirmation,
)
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.integrations.mattermost.model import IntegrationEvent, MattermostIdentity
from app.reviews.model import ReviewAction
from app.submissions.model import Submission
from app.users.creation_event import AccountCreationEvent
from app.users.model import User

del (
    AccountCreationEvent,
    Assignment,
    AuditLog,
    EvaluationJob,
    EvaluationReport,
    IntegrationEvent,
    MattermostIdentity,
    ReviewAction,
    Submission,
    User,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
GATED_APP_FIRST_REVISION = "0006_mattermost_delivery"


def _database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url").strip()
    return configured_url or get_settings().database_url


def _upgrade_path_enters_gated_app_version(
    starting_revisions: str | tuple[str, ...] | None,
    destination_revision: str | tuple[str, ...] | None,
) -> bool:
    if destination_revision is None:
        return False
    try:
        revisions = tuple(
            context.script.iterate_revisions(
                destination_revision,
                starting_revisions,
            )
        )
    except RangeNotAncestorError:  # downgrade cannot enter 0006 by upgrade
        return False
    return any(revision.revision == GATED_APP_FIRST_REVISION for revision in revisions)


def run_migrations_offline() -> None:
    requires_legacy_quiescence = _upgrade_path_enters_gated_app_version(
        context.get_starting_revision_argument(),
        context.get_revision_argument(),
    )
    if requires_legacy_quiescence:
        require_legacy_process_quiescence_confirmation()
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        if requires_legacy_quiescence:
            context.get_context().impl.static_output(LEGACY_QUIESCENCE_OFFLINE_COMMENT)
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        requires_legacy_quiescence = _upgrade_path_enters_gated_app_version(
            context.get_context().get_current_heads(),
            context.get_revision_argument(),
        )
        if requires_legacy_quiescence:
            require_legacy_process_quiescence(connection)
        connection.execute(MIGRATION_GATE_EXCLUSIVE_SQL)
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_sync_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_async_migrations())
