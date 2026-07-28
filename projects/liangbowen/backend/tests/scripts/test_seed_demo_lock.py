from __future__ import annotations

import asyncio
import logging

import pytest

from scripts.seed_demo import _demo_seed_lock


class FakeConnection:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.closed = False
        self.invalidated = False
        self.commits = 0
        self.executions = 0

    async def execute(self, statement, parameters):
        del statement, parameters
        self.executions += 1
        if self.failure == "unlock" and self.executions == 2:
            raise RuntimeError("sensitive-unlock-value")
        if self.failure == "cancel-unlock" and self.executions == 2:
            raise asyncio.CancelledError

    async def commit(self) -> None:
        self.commits += 1
        if self.failure == "acquire-commit" and self.commits == 1:
            raise RuntimeError("sensitive-acquire-commit-value")
        if self.failure == "commit" and self.commits == 2:
            raise RuntimeError("sensitive-commit-value")
        if self.failure == "cancel-commit" and self.commits == 2:
            raise asyncio.CancelledError

    async def invalidate(self) -> None:
        self.invalidated = True

    async def close(self) -> None:
        self.closed = True


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def connect(self) -> FakeConnection:
        return self.connection


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unlock", "commit"])
async def test_demo_seed_lock_invalidates_and_closes_on_cleanup_failure(
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = FakeConnection(failure)
    caplog.set_level(logging.WARNING, logger="scripts.seed_demo")

    async with _demo_seed_lock(FakeEngine(connection)):  # type: ignore[arg-type]
        pass

    assert connection.invalidated is True
    assert connection.closed is True
    assert "error_type=RuntimeError" in caplog.text
    assert "sensitive-unlock-value" not in caplog.text
    assert "sensitive-commit-value" not in caplog.text


@pytest.mark.asyncio
async def test_demo_seed_lock_invalidates_on_acquisition_commit_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = FakeConnection("acquire-commit")
    caplog.set_level(logging.WARNING, logger="scripts.seed_demo")

    with pytest.raises(RuntimeError, match="sensitive-acquire-commit-value"):
        async with _demo_seed_lock(FakeEngine(connection)):  # type: ignore[arg-type]
            raise AssertionError("lock body must not run")

    assert connection.invalidated is True
    assert connection.closed is True
    assert "error_type=RuntimeError" in caplog.text
    assert "sensitive-acquire-commit-value" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel-unlock", "cancel-commit"])
async def test_demo_seed_lock_invalidates_and_propagates_cleanup_cancellation(
    failure: str,
) -> None:
    connection = FakeConnection(failure)

    with pytest.raises(asyncio.CancelledError):
        async with _demo_seed_lock(FakeEngine(connection)):  # type: ignore[arg-type]
            pass

    assert connection.invalidated is True
    assert connection.closed is True
