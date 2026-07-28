import errno
import os
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Final

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.db.base import Base

SETTING_ENV_NAMES = {
    "app_env",
    "database_url",
    "redis_url",
    "jwt_secret",
    "jwt_exp_minutes",
    "cors_origins",
    "agent_provider",
    "agent_mock_fixture",
    "agent_base_url",
    "agent_model",
    "agent_api_key",
    "agent_allow_insecure_http",
    "agent_timeout_seconds",
    "celery_task_always_eager",
    "evaluation_dispatch_enabled",
    "mattermost_command_token",
    "mattermost_demo_setup_key",
    "mattermost_url",
    "mattermost_bot_token",
    "mattermost_bot_user_id",
    "mattermost_action_secret",
    "mattermost_action_url",
    "web_console_url",
}
POSTGRES_UNAVAILABLE_ERRNOS: Final = {
    errno.ECONNREFUSED,
    errno.ETIMEDOUT,
    60,
    61,
    110,
    111,
}
DEFAULT_TEST_DATABASE_URL: Final = "postgresql+asyncpg://grader:grader@localhost:5433/grader_test"


def _resolve_test_database_url() -> tuple[str, str | None]:
    explicit_database_url = os.getenv("TEST_DATABASE_URL")
    database_url = explicit_database_url or DEFAULT_TEST_DATABASE_URL
    database_name = make_url(database_url).database
    if database_name is None or not database_name.endswith("_test"):
        raise RuntimeError(
            f"TEST_DATABASE_URL database name must end with '_test'; got {database_name!r}"
        )
    return database_url, explicit_database_url


def _postgres_unavailable_reason(error: BaseException) -> BaseException | None:
    pending = [error]
    seen: set[int] = set()
    unavailable_reasons: list[BaseException] = []

    while pending:
        candidate = pending.pop(0)
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))

        related: list[BaseException] = []
        if isinstance(candidate, BaseExceptionGroup):
            related.extend(candidate.exceptions)
        for value in (
            getattr(candidate, "orig", None),
            candidate.__cause__,
            candidate.__context__,
        ):
            if isinstance(value, BaseException):
                related.append(value)

        if isinstance(candidate, (ConnectionRefusedError, TimeoutError)) or (
            isinstance(candidate, OSError) and candidate.errno in POSTGRES_UNAVAILABLE_ERRNOS
        ):
            unavailable_reasons.append(candidate)
        elif isinstance(candidate, OSError) and candidate.errno is None:
            aggregate_errnos = {
                int(value) for value in re.findall(r"\[Errno (\d+)\]", str(candidate))
            }
            if aggregate_errnos and aggregate_errnos <= POSTGRES_UNAVAILABLE_ERRNOS:
                unavailable_reasons.append(candidate)
            else:
                return None
        elif isinstance(candidate, (DBAPIError, BaseExceptionGroup)) and related:
            pass
        else:
            return None

        pending.extend(related)

    return unavailable_reasons[0] if unavailable_reasons else None


async def _probe_test_database(
    engine: AsyncEngine,
    explicit_database_url: str | None,
) -> None:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (
        OSError,
        TimeoutError,
        SQLAlchemyError,
        asyncpg.PostgresError,
        BaseExceptionGroup,
    ) as exc:
        unavailable_reason = _postgres_unavailable_reason(exc)
        if explicit_database_url is not None or unavailable_reason is None:
            raise
        pytest.skip(
            "default PostgreSQL test database unavailable: "
            f"{type(unavailable_reason).__name__}: {unavailable_reason}"
        )


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for env_var in tuple(os.environ):
        if env_var.casefold() in SETTING_ENV_NAMES:
            monkeypatch.delenv(env_var)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(isolate_settings: None) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def postgres_session() -> AsyncIterator[AsyncSession]:
    from app.assignments.model import Assignment
    from app.audit.model import AuditLog
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
    database_url, explicit_database_url = _resolve_test_database_url()

    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 1},
    )
    try:
        await _probe_test_database(engine, explicit_database_url)

        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                yield session
        finally:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
    finally:
        await engine.dispose()
