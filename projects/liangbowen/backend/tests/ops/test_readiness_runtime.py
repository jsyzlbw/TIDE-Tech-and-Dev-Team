from __future__ import annotations

import asyncio
import logging
import time

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.main as main_module


async def test_readiness_times_out_against_real_blackhole_tcp_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: set[asyncio.Task[None]] = set()

    async def blackhole(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            await reader.read()
        finally:
            handlers.discard(task)
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(blackhole, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    engine = create_async_engine(f"postgresql+asyncpg://grader@127.0.0.1:{port}/grader")
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "READINESS_DATABASE_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    try:
        assert await asyncio.wait_for(main_module.database_is_ready(), timeout=1.0) is False
        assert time.monotonic() - started < 1.0
    finally:
        await engine.dispose()
        server.close()
        await server.wait_closed()
        for _ in range(20):
            if not handlers:
                break
            await asyncio.sleep(0.01)

    assert not handlers


async def test_readiness_quickly_rejects_refused_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    engine = create_async_engine(f"postgresql+asyncpg://grader@127.0.0.1:{port}/grader")
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "READINESS_DATABASE_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    try:
        assert await main_module.database_is_ready() is False
        assert time.monotonic() - started < 1.0
    finally:
        await engine.dispose()


async def test_readiness_succeeds_against_real_database(
    monkeypatch: pytest.MonkeyPatch,
    postgres_session: AsyncSession,
) -> None:
    assert postgres_session.bind is not None
    monkeypatch.setattr(main_module, "engine", postgres_session.bind)

    assert await main_module.database_is_ready() is True


async def test_readiness_authentication_failure_is_logged_without_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = (
        "password authentication failed for postgresql://grader:super-secret@example.invalid/"
        "grader during SELECT password"
    )

    class FailingConnection:
        async def __aenter__(self) -> None:
            raise SQLAlchemyError(secret)

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object,
        ) -> None:
            del exc_type, exc, traceback

    class FailingEngine:
        def connect(self) -> FailingConnection:
            return FailingConnection()

    monkeypatch.setattr(main_module, "engine", FailingEngine())
    caplog.set_level(logging.WARNING, logger="app.main")

    assert await main_module.database_is_ready() is False

    log_output = caplog.text
    assert "database readiness check failed" in log_output
    assert "category=database" in log_output
    assert "error_type=SQLAlchemyError" in log_output
    assert "super-secret" not in log_output
    assert "authentication failed" not in log_output
    assert "SELECT" not in log_output
