import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from types import TracebackType
from typing import Any

import asyncpg
import conftest as conftest_module
import pytest
from sqlalchemy.exc import DBAPIError, ProgrammingError


class AsyncContext(AbstractAsyncContextManager[Any]):
    def __init__(
        self,
        value: object | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.value = value
        self.error = error

    async def __aenter__(self) -> object:
        if self.error is not None:
            raise self.error
        assert self.value is not None
        return self.value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def fixture_generator() -> AsyncIterator[object]:
    fixture_function = conftest_module.postgres_session.__wrapped__
    return fixture_function()


def chained_exception(
    error: Exception,
    related: Exception,
    attribute: str,
) -> Exception:
    setattr(error, attribute, related)
    return error


def test_test_database_url_resolver_uses_safe_default_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    database_url, explicit_database_url = conftest_module._resolve_test_database_url()

    assert database_url == ("postgresql+asyncpg://grader:grader@localhost:5433/grader_test")
    assert explicit_database_url is None


def test_test_database_url_resolver_preserves_explicit_test_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_url = "postgresql+asyncpg://tester:secret@localhost:55435/shared_test"
    monkeypatch.setenv("TEST_DATABASE_URL", explicit_url)

    database_url, explicit_database_url = conftest_module._resolve_test_database_url()

    assert database_url == explicit_url
    assert explicit_database_url == explicit_url


def test_test_database_url_resolver_rejects_non_test_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://tester:secret@localhost:5432/production",
    )

    with pytest.raises(
        RuntimeError,
        match="TEST_DATABASE_URL database name must end with '_test'; got 'production'",
    ):
        conftest_module._resolve_test_database_url()


@pytest.mark.parametrize(
    "error",
    [
        chained_exception(
            asyncpg.InvalidPasswordError("password authentication failed"),
            ConnectionRefusedError("connection refused"),
            "__cause__",
        ),
        chained_exception(
            asyncpg.InvalidCatalogNameError("database does not exist"),
            TimeoutError("connection timed out"),
            "__context__",
        ),
        chained_exception(
            asyncpg.PostgresError("server rejected the request"),
            ConnectionRefusedError("connection refused"),
            "__cause__",
        ),
        ExceptionGroup(
            "mixed failures",
            [
                ConnectionRefusedError("connection refused"),
                asyncpg.PostgresError("semantic database failure"),
            ],
        ),
    ],
)
def test_postgres_unavailable_reason_rejects_mixed_semantic_error_graphs(
    error: Exception,
) -> None:
    assert conftest_module._postgres_unavailable_reason(error) is None


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("connection refused"),
        TimeoutError("connection timed out"),
        OSError(
            "Multiple exceptions: [Errno 61] Connect call failed ('127.0.0.1', 5433), "
            "[Errno 61] Connect call failed ('::1', 5433, 0, 0)"
        ),
        ExceptionGroup(
            "all unavailable",
            [
                ConnectionRefusedError("connection refused"),
                TimeoutError("connection timed out"),
            ],
        ),
    ],
)
def test_postgres_unavailable_reason_accepts_only_pure_unavailable_graphs(
    error: Exception,
) -> None:
    assert conftest_module._postgres_unavailable_reason(error) is not None


async def test_postgres_fixture_does_not_skip_mixed_auth_and_refused_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    authentication_error = chained_exception(
        asyncpg.InvalidPasswordError("password authentication failed"),
        ConnectionRefusedError("connection refused"),
        "__cause__",
    )
    wrapped_error = DBAPIError("connect", {}, authentication_error)
    engine = UnavailableEngine(wrapped_error)
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(DBAPIError) as raised:
            await anext(fixture)

    assert raised.value is wrapped_error
    assert engine.dispose_count == 1


async def test_postgres_fixture_rejects_non_test_database_before_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_creation_calls: list[str] = []

    def record_engine_creation(database_url: str, **kwargs: object) -> None:
        del kwargs
        engine_creation_calls.append(database_url)

    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://grader:grader@localhost:5433/production",
    )
    monkeypatch.setattr(conftest_module, "create_async_engine", record_engine_creation)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(
            RuntimeError,
            match="TEST_DATABASE_URL database name must end with '_test'; got 'production'",
        ):
            await anext(fixture)

    assert engine_creation_calls == []


class ProbeConnection:
    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(self, statement: object) -> None:
        assert str(statement) == "SELECT 1"
        self.execute_count += 1


class DdlConnection:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.run_sync_calls: list[Callable[..., object]] = []

    async def run_sync(self, operation: Callable[..., object]) -> None:
        self.run_sync_calls.append(operation)
        raise self.error


class DdlFailureEngine:
    def __init__(self, ddl_error: Exception) -> None:
        self.probe_connection = ProbeConnection()
        self.ddl_connection = DdlConnection(ddl_error)
        self.dispose_count = 0

    def connect(self) -> AsyncContext:
        return AsyncContext(self.probe_connection)

    def begin(self) -> AsyncContext:
        return AsyncContext(self.ddl_connection)

    async def dispose(self) -> None:
        self.dispose_count += 1


async def test_postgres_fixture_does_not_convert_ddl_error_to_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddl_error = ProgrammingError(
        "CREATE TABLE assignments",
        {},
        RuntimeError("invalid assignment schema"),
    )
    engine = DdlFailureEngine(ddl_error)
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://grader:grader@localhost:5433/grader_test",
    )
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(ProgrammingError) as raised:
            await anext(fixture)

    assert raised.value is ddl_error
    assert engine.probe_connection.execute_count == 1
    assert len(engine.ddl_connection.run_sync_calls) == 1
    assert engine.dispose_count == 1


class UnavailableEngine:
    def __init__(self, connection_error: BaseException) -> None:
        self.connection_error = connection_error
        self.dispose_count = 0

    def connect(self) -> AsyncContext:
        return AsyncContext(error=self.connection_error)

    def begin(self) -> AsyncContext:
        return AsyncContext(error=self.connection_error)

    async def dispose(self) -> None:
        self.dispose_count += 1


@pytest.mark.parametrize(
    "connection_error",
    [
        asyncpg.InvalidPasswordError("password authentication failed"),
        asyncpg.InvalidCatalogNameError("database does not exist"),
    ],
)
async def test_postgres_fixture_never_skips_authentication_or_database_name_errors(
    monkeypatch: pytest.MonkeyPatch,
    connection_error: Exception,
) -> None:
    wrapped_error = DBAPIError("connect", {}, connection_error)
    engine = UnavailableEngine(wrapped_error)
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(DBAPIError) as raised:
            await anext(fixture)

    assert raised.value is wrapped_error
    assert engine.dispose_count == 1


async def test_postgres_fixture_skips_default_connection_refused_with_exact_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    connection_error = ConnectionRefusedError("connection refused")
    wrapped_error = DBAPIError("connect", {}, connection_error)
    engine = UnavailableEngine(wrapped_error)
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(pytest.skip.Exception) as raised:
            await anext(fixture)

    assert str(raised.value) == (
        "default PostgreSQL test database unavailable: ConnectionRefusedError: connection refused"
    )
    assert engine.dispose_count == 1


async def test_postgres_fixture_skips_asyncpg_aggregate_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    connection_error = OSError(
        "Multiple exceptions: [Errno 61] Connect call failed ('127.0.0.1', 5433), "
        "[Errno 61] Connect call failed ('::1', 5433, 0, 0)"
    )
    engine = UnavailableEngine(connection_error)
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(pytest.skip.Exception):
            await anext(fixture)

    assert engine.dispose_count == 1


@pytest.mark.parametrize(
    "connection_error",
    [
        ExceptionGroup(
            "all unavailable",
            [
                ConnectionRefusedError("connection refused"),
                TimeoutError("connection timed out"),
            ],
        ),
        ExceptionGroup(
            "nested unavailable",
            [
                ExceptionGroup(
                    "refused and timed out",
                    [
                        ConnectionRefusedError("connection refused"),
                        TimeoutError("connection timed out"),
                    ],
                ),
                ConnectionRefusedError("another connection refused"),
            ],
        ),
    ],
)
async def test_postgres_fixture_skips_grouped_default_connection_failures(
    monkeypatch: pytest.MonkeyPatch,
    connection_error: ExceptionGroup,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    engine = UnavailableEngine(connection_error)
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(pytest.skip.Exception):
            await anext(fixture)

    assert engine.dispose_count == 1


@pytest.mark.parametrize(
    "semantic_error",
    [
        asyncpg.PostgresError("semantic database failure"),
        asyncpg.InvalidPasswordError("password authentication failed"),
    ],
)
async def test_postgres_fixture_propagates_mixed_grouped_connection_failures(
    monkeypatch: pytest.MonkeyPatch,
    semantic_error: Exception,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    connection_error = ExceptionGroup(
        "mixed connection failures",
        [
            ExceptionGroup(
                "unavailable",
                [
                    ConnectionRefusedError("connection refused"),
                    TimeoutError("connection timed out"),
                ],
            ),
            semantic_error,
        ],
    )
    engine = UnavailableEngine(connection_error)
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(ExceptionGroup) as raised:
            await anext(fixture)

    assert raised.value is connection_error
    assert engine.dispose_count == 1


@pytest.mark.parametrize(
    "control_flow_error",
    [KeyboardInterrupt(), asyncio.CancelledError()],
)
async def test_postgres_fixture_never_skips_base_exception_group_leaves(
    monkeypatch: pytest.MonkeyPatch,
    control_flow_error: BaseException,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    connection_error = BaseExceptionGroup(
        "control flow must propagate",
        [ConnectionRefusedError("connection refused"), control_flow_error],
    )
    engine = UnavailableEngine(connection_error)
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(BaseExceptionGroup) as raised:
            await anext(fixture)

    assert raised.value is connection_error
    assert engine.dispose_count == 1


async def test_explicit_test_database_url_grouped_unavailable_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_error = ExceptionGroup(
        "all unavailable",
        [
            ConnectionRefusedError("connection refused"),
            TimeoutError("connection timed out"),
        ],
    )
    engine = UnavailableEngine(connection_error)
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://grader:grader@localhost:5433/grader_test",
    )
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(ExceptionGroup) as raised:
            await anext(fixture)

    assert raised.value is connection_error
    assert engine.dispose_count == 1


async def test_explicit_test_database_url_connection_refused_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_error = ConnectionRefusedError("connection refused")
    wrapped_error = DBAPIError("connect", {}, connection_error)
    engine = UnavailableEngine(wrapped_error)
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://grader:grader@localhost:5433/grader_test",
    )
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)

    async with aclosing(fixture_generator()) as fixture:
        with pytest.raises(DBAPIError) as raised:
            await anext(fixture)

    assert raised.value is wrapped_error
    assert engine.dispose_count == 1


class SuccessfulDdlConnection:
    def __init__(self) -> None:
        self.run_sync_count = 0

    async def run_sync(self, operation: Callable[..., object]) -> None:
        del operation
        self.run_sync_count += 1


class CleanupFailureEngine:
    def __init__(self, cleanup_error: Exception) -> None:
        self.probe_connection = ProbeConnection()
        self.setup_connection = SuccessfulDdlConnection()
        self.cleanup_connection = DdlConnection(cleanup_error)
        self.begin_count = 0
        self.dispose_count = 0

    def connect(self) -> AsyncContext:
        return AsyncContext(self.probe_connection)

    def begin(self) -> AsyncContext:
        self.begin_count += 1
        if self.begin_count == 1:
            return AsyncContext(self.setup_connection)
        return AsyncContext(self.cleanup_connection)

    async def dispose(self) -> None:
        self.dispose_count += 1


async def test_postgres_fixture_disposes_engine_when_cleanup_ddl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_error = ProgrammingError(
        "DROP TABLE assignments",
        {},
        RuntimeError("cleanup failed"),
    )
    engine = CleanupFailureEngine(cleanup_error)
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://grader:grader@localhost:5433/grader_test",
    )
    monkeypatch.setattr(conftest_module, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(
        conftest_module,
        "async_sessionmaker",
        lambda *args, **kwargs: lambda: AsyncContext(object()),
    )
    fixture = fixture_generator()
    await anext(fixture)

    with pytest.raises(ProgrammingError) as raised:
        await fixture.aclose()

    assert raised.value is cleanup_error
    assert engine.setup_connection.run_sync_count == 2
    assert len(engine.cleanup_connection.run_sync_calls) == 1
    assert engine.dispose_count == 1
