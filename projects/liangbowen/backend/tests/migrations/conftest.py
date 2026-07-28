from __future__ import annotations

import pytest

from app.db.migration_gate import (
    LEGACY_QUIESCENCE_CONFIRMATION_ENV,
    LEGACY_QUIESCENCE_CONFIRMATION_VALUE,
)


@pytest.fixture(autouse=True)
def confirm_test_legacy_process_quiescence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration tests run alone against a disposable, process-local database."""

    monkeypatch.setenv(
        LEGACY_QUIESCENCE_CONFIRMATION_ENV,
        LEGACY_QUIESCENCE_CONFIRMATION_VALUE,
    )
