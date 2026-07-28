import importlib
import sys
import uuid
from contextlib import aclosing
from datetime import datetime

import pytest
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.schema import DefaultClause

import app.db as db_package
from app.core.config import get_settings
from app.db import migration_gate
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import (
    AssignmentStatus,
    Grade,
    Role,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
    enum_values,
    grade_for_score,
)


class TestBase(DeclarativeBase):
    __test__ = False


class SampleRecord(UUIDPrimaryKeyMixin, CreatedAtMixin, TestBase):
    __tablename__ = "sample_records"


@pytest.mark.parametrize(
    ("enum_type", "expected_values"),
    [
        (Role, ["teacher", "student", "admin"]),
        (AssignmentStatus, ["draft", "published", "closed", "archived"]),
        (SubmissionContentType, ["text", "markdown", "code", "structured"]),
        (SubmissionSource, ["web", "mattermost"]),
        (SubmissionStatus, ["submitted", "withdrawn"]),
        (Grade, ["A", "B", "C", "D"]),
    ],
)
def test_enum_values(enum_type: type, expected_values: list[str]) -> None:
    assert enum_values(enum_type) == expected_values


def test_role_members() -> None:
    assert Role.TEACHER == "teacher"
    assert Role.STUDENT == "student"
    assert Role.ADMIN == "admin"


def test_assignment_status_members() -> None:
    assert AssignmentStatus.DRAFT == "draft"
    assert AssignmentStatus.PUBLISHED == "published"
    assert AssignmentStatus.CLOSED == "closed"
    assert AssignmentStatus.ARCHIVED == "archived"


def test_submission_content_type_members() -> None:
    assert SubmissionContentType.TEXT == "text"
    assert SubmissionContentType.MARKDOWN == "markdown"
    assert SubmissionContentType.CODE == "code"
    assert SubmissionContentType.STRUCTURED == "structured"


def test_submission_source_members() -> None:
    assert SubmissionSource.WEB == "web"
    assert SubmissionSource.MATTERMOST == "mattermost"


def test_submission_status_members() -> None:
    assert SubmissionStatus.SUBMITTED == "submitted"
    assert SubmissionStatus.WITHDRAWN == "withdrawn"


def test_grade_members() -> None:
    assert Grade.A == "A"
    assert Grade.B == "B"
    assert Grade.C == "C"
    assert Grade.D == "D"


@pytest.mark.parametrize(
    ("score", "expected_grade"),
    [
        (100, Grade.A),
        (90, Grade.A),
        (89, Grade.B),
        (75, Grade.B),
        (74, Grade.C),
        (60, Grade.C),
        (59, Grade.D),
        (0, Grade.D),
    ],
)
def test_grade_for_score_boundaries(score: int, expected_grade: Grade) -> None:
    assert grade_for_score(score) is expected_grade


@pytest.mark.parametrize("score", [-1, 101])
def test_grade_for_score_rejects_scores_outside_valid_range(score: int) -> None:
    with pytest.raises(ValueError, match="^score must be between 0 and 100$"):
        grade_for_score(score)


@pytest.mark.parametrize("score", [True, False, 89.5, 74.9, "90", None])
def test_grade_for_score_rejects_non_integer_scores(score: object) -> None:
    with pytest.raises(TypeError, match="^score must be an integer$"):
        grade_for_score(score)


def test_base_exposes_shared_metadata_and_registry() -> None:
    assert Base.metadata is Base.registry.metadata


def test_synthetic_record_uses_isolated_test_metadata() -> None:
    assert TestBase.metadata.tables["sample_records"] is SampleRecord.__table__
    assert SampleRecord.__mapper__.registry is TestBase.registry


def test_synthetic_record_does_not_pollute_production_metadata() -> None:
    assert "sample_records" not in Base.metadata.tables


def test_uuid_primary_key_mixin_metadata() -> None:
    id_column = SampleRecord.__table__.c.id

    assert isinstance(id_column.type, PostgreSQLUUID)
    assert id_column.type.as_uuid is True
    assert id_column.primary_key is True
    assert id_column.default is not None
    assert isinstance(id_column.default.arg(None), uuid.UUID)


def test_created_at_mixin_metadata() -> None:
    created_at_column = SampleRecord.__table__.c.created_at

    assert isinstance(created_at_column.type, DateTime)
    assert created_at_column.type.timezone is True
    assert created_at_column.nullable is False
    assert isinstance(created_at_column.server_default, DefaultClause)
    assert str(created_at_column.server_default.arg) == "now()"
    assert SampleRecord.created_at.type.python_type is datetime


def test_async_engine_and_session_factory_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = object()
    original_module = sys.modules.get("app.db.session", missing)
    original_package_attribute = vars(db_package).get("session", missing)
    engine_sentinel = object()
    factory_sentinel = object()
    engine_calls: list[tuple[str, dict[str, object]]] = []
    factory_calls: list[tuple[object, dict[str, object]]] = []

    def fake_create_async_engine(url: str, **kwargs: object) -> object:
        engine_calls.append((url, kwargs))
        return engine_sentinel

    def fake_migration_gated_sessionmaker(bind: object, **kwargs: object) -> object:
        factory_calls.append((bind, kwargs))
        return factory_sentinel

    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(
        migration_gate,
        "migration_gated_sessionmaker",
        fake_migration_gated_sessionmaker,
    )
    sys.modules.pop("app.db.session", None)

    try:
        session_module = importlib.import_module("app.db.session")

        assert session_module.settings is get_settings()
        assert session_module.engine is engine_sentinel
        assert session_module.async_session_factory is factory_sentinel
        assert engine_calls == [(session_module.settings.database_url, {"pool_pre_ping": True})]
        assert factory_calls == [(engine_sentinel, {"expire_on_commit": False})]
    finally:
        if original_module is missing:
            sys.modules.pop("app.db.session", None)
        else:
            sys.modules["app.db.session"] = original_module

        if original_package_attribute is missing:
            vars(db_package).pop("session", None)
        else:
            db_package.session = original_package_attribute

    assert sys.modules.get("app.db.session", missing) is original_module
    assert vars(db_package).get("session", missing) is original_package_attribute


async def test_get_session_yields_session_and_exits_context_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import session as session_module

    assert sys.modules["app.db.session"] is session_module
    assert db_package.session is session_module
    assert isinstance(session_module.engine, AsyncEngine)

    session = AsyncSession()

    class RecordingSessionContext:
        entered = False
        exited = False

        async def __aenter__(self) -> AsyncSession:
            self.entered = True
            return session

        async def __aexit__(self, *args: object) -> None:
            self.exited = True
            await session.close()

    session_context = RecordingSessionContext()
    monkeypatch.setattr(session_module, "async_session_factory", lambda: session_context)

    async with aclosing(session_module.get_session()) as session_generator:
        yielded_session = await anext(session_generator)

        assert yielded_session is session
        assert session_context.entered is True
        assert session_context.exited is False

    assert session_context.exited is True
