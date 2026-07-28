from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.evaluations.engine import EvaluationEngine
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationJobFailed, EvaluationService
from app.evaluations.types import JobReason
from app.reviews.schemas import ReevaluateRequest
from app.reviews.service import ReviewService
from tests.evaluations.test_jobs import _seed_subject

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True},
    )
    return compare_metadata(context, Base.metadata)


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


@pytest.mark.asyncio
async def test_0007_upgrade_and_empty_downgrade_round_trip(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0006_mattermost_delivery")
    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            unique_names = await connection.run_sync(
                lambda sync: {
                    constraint["name"]
                    for constraint in inspect(sync).get_unique_constraints("review_actions")
                }
            )
            index_names = await connection.run_sync(
                lambda sync: {
                    index["name"] for index in inspect(sync).get_indexes("review_actions")
                }
            )
            diff = await connection.run_sync(_diff)
        assert revision == "0008_account_creation_events"
        assert "uq_review_actions_report_action" not in unique_names
        assert "uq_review_actions_report_non_reevaluate" in index_names
        assert diff == []
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "0006_mattermost_delivery")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            unique_names = await connection.run_sync(
                lambda sync: {
                    constraint["name"]
                    for constraint in inspect(sync).get_unique_constraints("review_actions")
                }
            )
        assert "uq_review_actions_report_action" in unique_names
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_0007_downgrade_refuses_append_only_duplicate_evidence(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(migration_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            teacher, _, _, submission = await _seed_subject(session)
            failing = EvaluationService(
                session,
                EvaluationEngine(MockEvaluationProvider()),
                requested_by=teacher.id,
                dispatch=None,
                mock_fixture_key="missing-retry-generation-fixture",
            )
            report = await EvaluationService(
                session,
                EvaluationEngine(MockEvaluationProvider()),
                requested_by=teacher.id,
                dispatch=None,
            ).evaluate_now(submission.id, JobReason.INITIAL)
            first = await ReviewService(session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(comment="first"),
                failing,
            )
            with pytest.raises(EvaluationJobFailed):
                await failing.execute_job(first.id)
            await ReviewService(session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(comment="second immutable evidence"),
                failing,
            )
    finally:
        await engine.dispose()

    with pytest.raises(Exception, match="retry generation evidence exists"):
        await asyncio.to_thread(command.downgrade, config, "0006_mattermost_delivery")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            count = await connection.scalar(text("SELECT count(*) FROM review_actions"))
        assert revision == "0008_account_creation_events"
        assert count == 2
    finally:
        await engine.dispose()
