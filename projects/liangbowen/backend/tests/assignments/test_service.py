import asyncio
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, Enum, String, Text, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, make_transient_to_detached
from sqlalchemy.sql import Select

from app.assignments.model import ASSIGNMENT_CODE_SEQUENCE, Assignment
from app.assignments.schemas import (
    AssignmentCreate,
    AssignmentListItem,
    AssignmentRead,
    AssignmentStudentRead,
)
from app.assignments.service import (
    InvalidAssignmentTransition,
    close_assignment,
    create_assignment,
    create_published_assignment_in_transaction,
    next_assignment_code,
    publish_assignment,
    reopen_assignment,
)
from app.db.base import Base
from app.db.types import AssignmentStatus, Role
from app.users.model import User


@pytest.mark.asyncio
async def test_create_published_assignment_is_caller_owned_and_rolls_back(
    postgres_session: AsyncSession,
) -> None:
    teacher = User(
        username=f"mattermost-teacher-{uuid.uuid4().hex}",
        display_name="Mattermost Teacher",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add(teacher)
    await postgres_session.commit()
    teacher_id = teacher.id

    assignment = await create_published_assignment_in_transaction(
        postgres_session,
        assignment_payload(),
        teacher_id,
        "channel-123",
    )
    assignment_id = assignment.id
    assert assignment.status is AssignmentStatus.PUBLISHED
    assert assignment.published_at is not None
    assert assignment.mattermost_channel_id == "channel-123"
    assert await postgres_session.get(Assignment, assignment_id) is assignment

    await postgres_session.rollback()
    assert await postgres_session.get(Assignment, assignment_id) is None


@pytest.mark.asyncio
async def test_create_published_assignment_revalidates_teacher_and_channel(
    postgres_session: AsyncSession,
) -> None:
    inactive = User(
        username=f"inactive-mattermost-{uuid.uuid4().hex}",
        display_name="Inactive",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=False,
    )
    postgres_session.add(inactive)
    await postgres_session.commit()
    inactive_id = inactive.id

    with pytest.raises(PermissionError, match="active teacher"):
        await create_published_assignment_in_transaction(
            postgres_session,
            assignment_payload(),
            inactive_id,
            "channel-123",
        )
    await postgres_session.rollback()

    for channel_id in ("", "x" * 129, "bad\0channel"):
        with pytest.raises(ValueError, match="channel"):
            await create_published_assignment_in_transaction(
                postgres_session,
                assignment_payload(),
                uuid.uuid4(),
                channel_id,
            )
        await postgres_session.rollback()


class FocusedAssignmentSession:
    def __init__(
        self,
        sequence_value: int = 1,
        assignments: list[Assignment] | None = None,
    ) -> None:
        self.sequence_value = sequence_value
        self.assignments = {assignment.id: assignment for assignment in assignments or []}
        self.added: list[object] = []
        self.scalar_statements: list[Select[Any]] = []
        self.execute_statements: list[object] = []
        self.lock_statements: list[Select[Any]] = []
        self.commit_count = 0
        self.flush_count = 0
        self.refresh_count = 0
        self.rollback_count = 0
        self.commit_error: Exception | None = None
        self.transaction_failed = False
        self.events: list[str] = []

    @property
    def no_autoflush(self):
        return nullcontext()

    async def execute(self, statement: object, parameters: object = None) -> None:
        del parameters
        self.execute_statements.append(statement)

    async def scalar(self, statement: Select[Any]) -> int | Assignment | None:
        self.scalar_statements.append(statement)
        if statement.column_descriptions[0].get("entity") is Assignment:
            self.lock_statements.append(statement)
            assignment_id = next(
                value
                for value in statement.compile().params.values()
                if isinstance(value, uuid.UUID)
            )
            return self.assignments.get(assignment_id)
        return self.sequence_value

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self, objects: list[object] | None = None) -> None:
        del objects
        self.events.append("flush")
        self.flush_count += 1

    async def commit(self) -> None:
        assert self.transaction_failed is False
        self.events.append("commit")
        self.commit_count += 1
        if self.commit_error is not None:
            self.transaction_failed = True
            raise self.commit_error

    async def rollback(self) -> None:
        self.events.append("rollback")
        self.rollback_count += 1
        self.transaction_failed = False

    async def refresh(self, instance: object) -> None:
        assert instance in self.added or isinstance(instance, Assignment)
        self.events.append("refresh")
        self.refresh_count += 1
        if isinstance(instance, Assignment):
            instance.id = instance.id or uuid.uuid4()
            instance.created_at = instance.created_at or datetime.now(UTC)


def assignment_payload(*, due_at: datetime | None = None) -> AssignmentCreate:
    return AssignmentCreate(
        title="  Compiler construction  ",
        question="  Explain SSA form.  ",
        notes="teacher notes",
        rubric={"correctness": 70, "clarity": 30},
        due_at=due_at or datetime.now(UTC) + timedelta(days=1),
    )


def persisted_assignment(
    *,
    status: AssignmentStatus = AssignmentStatus.DRAFT,
    due_at: datetime | None = None,
    published_at: datetime | None = None,
) -> Assignment:
    return Assignment(
        id=uuid.uuid4(),
        code="HW-0001",
        title="Compiler construction",
        question="Explain SSA form.",
        notes="",
        rubric={"correctness": 100},
        due_at=due_at or datetime.now(UTC) + timedelta(days=1),
        status=status,
        mattermost_channel_id=None,
        created_by=uuid.uuid4(),
        created_at=datetime.now(UTC),
        published_at=published_at,
    )


def test_assignment_model_uses_required_postgresql_metadata() -> None:
    table = Base.metadata.tables["assignments"]

    assert table is Assignment.__table__
    assert set(table.c.keys()) == {
        "id",
        "created_at",
        "code",
        "title",
        "question",
        "notes",
        "rubric",
        "due_at",
        "status",
        "mattermost_channel_id",
        "created_by",
        "published_at",
    }
    assert isinstance(table.c.code.type, String)
    assert table.c.code.unique is True
    assert table.c.code.index is True
    assert table.c.code.nullable is False
    assert isinstance(table.c.title.type, String)
    assert table.c.title.type.length == 200
    assert isinstance(table.c.question.type, Text)
    assert isinstance(table.c.notes.type, Text)
    assert table.c.notes.default is not None
    assert table.c.notes.default.arg == ""
    assert table.c.notes.server_default is not None
    assert str(table.c.notes.server_default.arg) == ""
    assert table.c.notes.nullable is False
    assert isinstance(table.c.rubric.type, JSONB)
    assert table.c.rubric.nullable is False
    assert isinstance(table.c.due_at.type, DateTime)
    assert table.c.due_at.type.timezone is True
    assert table.c.due_at.nullable is False
    assert isinstance(table.c.status.type, Enum)
    assert table.c.status.type.enums == ["draft", "published", "closed", "archived"]
    assert table.c.status.index is True
    assert table.c.status.nullable is False
    assert table.c.mattermost_channel_id.nullable is True
    assert table.c.created_by.nullable is False
    assert table.c.created_by.index is True
    assert next(iter(table.c.created_by.foreign_keys)).target_fullname == "users.id"
    assert isinstance(table.c.published_at.type, DateTime)
    assert table.c.published_at.type.timezone is True
    assert table.c.published_at.nullable is True
    assert ASSIGNMENT_CODE_SEQUENCE.name == "assignment_code_seq"
    assert ASSIGNMENT_CODE_SEQUENCE.start == 1
    assert ASSIGNMENT_CODE_SEQUENCE.metadata is Base.metadata
    lifecycle_index = next(
        index for index in table.indexes if index.name == "ix_assignments_status_created_at_id"
    )
    assert [str(expression) for expression in lifecycle_index.expressions] == [
        "assignments.status",
        "assignments.created_at DESC",
        "assignments.id DESC",
    ]
    teacher_index = next(
        index for index in table.indexes if index.name == "ix_assignments_created_at_id"
    )
    assert [str(expression) for expression in teacher_index.expressions] == [
        "assignments.created_at DESC",
        "assignments.id DESC",
    ]


def test_assignment_create_strips_text_and_assignment_read_keeps_lifecycle_metadata() -> None:
    payload = assignment_payload()
    assignment = persisted_assignment()

    assert payload.title == "Compiler construction"
    assert payload.question == "Explain SSA form."
    read = AssignmentRead.model_validate(assignment)
    assert read.id == assignment.id
    assert read.code == assignment.code
    assert read.status is AssignmentStatus.DRAFT
    assert read.created_by == assignment.created_by
    assert read.created_at == assignment.created_at
    assert read.published_at is None
    assert read.mattermost_channel_id is None


def test_assignment_projections_expose_only_declared_fields() -> None:
    assignment = persisted_assignment()
    assignment.rubric = {"secret_solution": "never expose"}
    assignment.mattermost_channel_id = "internal-channel"

    list_item = AssignmentListItem.model_validate(assignment).model_dump()
    student_detail = AssignmentStudentRead.model_validate(assignment).model_dump()
    teacher_detail = AssignmentRead.model_validate(assignment).model_dump()

    assert set(list_item) == {
        "id",
        "code",
        "title",
        "due_at",
        "status",
        "created_at",
        "published_at",
    }
    assert set(student_detail) == {
        "id",
        "code",
        "title",
        "question",
        "notes",
        "due_at",
        "status",
        "created_at",
        "published_at",
    }
    assert teacher_detail["rubric"] == {"secret_solution": "never expose"}
    assert teacher_detail["mattermost_channel_id"] == "internal-channel"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "\u200b\ufeff\u2060"),
        ("question", "\u200b\ufeff\u2060"),
        ("title", "\x00\x1f\u200e"),
        ("question", "\x00\x1f\u200e"),
    ],
)
def test_assignment_create_rejects_invisible_only_text(field: str, value: str) -> None:
    values = assignment_payload().model_dump()
    values[field] = value

    with pytest.raises(ValidationError):
        AssignmentCreate.model_validate(values)


def test_assignment_create_allows_multilingual_and_newline_text() -> None:
    payload = AssignmentCreate(
        title="编译原理 — مبادئ المترجمات",
        question="第一步：解释 SSA。\nSecond: give an example.",
        rubric={},
        due_at=datetime.now(UTC) + timedelta(days=1),
    )

    assert payload.title == "编译原理 — مبادئ المترجمات"
    assert "\n" in payload.question


def test_assignment_create_accepts_exact_evaluation_content_boundaries() -> None:
    title = "界" * 170 + "ab"
    question = "界" * 21_845 + "a"
    rubric = {"criterion": "x" * (128 * 1024 - len('{"criterion":""}'))}

    payload = AssignmentCreate(
        title=title,
        question=question,
        rubric=rubric,
        due_at=datetime.now(UTC) + timedelta(days=1),
    )

    assert len(payload.title.encode("utf-8")) == 512
    assert len(payload.question.encode("utf-8")) == 64 * 1024


@pytest.mark.parametrize(
    ("field", "value", "safe_fragment"),
    [
        ("title", "😀" * 129, "UTF-8 byte limit"),
        ("question", "界" * 21_846, "UTF-8 byte limit"),
        ("rubric", {"value": "x" * (128 * 1024)}, "JSON byte limit"),
    ],
)
def test_assignment_create_rejects_content_that_evaluation_cannot_accept(
    field: str,
    value: object,
    safe_fragment: str,
) -> None:
    values = assignment_payload().model_dump()
    values[field] = value

    with pytest.raises(ValidationError) as captured:
        AssignmentCreate.model_validate(values)

    messages = [error["msg"] for error in captured.value.errors(include_input=False)]
    assert any(safe_fragment in message for message in messages)
    assert all("😀" not in message and "界" not in message for message in messages)


@pytest.mark.parametrize(
    "rubric",
    [
        {
            "nested": {
                "nested": {
                    "nested": {
                        "nested": {
                            "nested": {
                                "nested": {
                                    "nested": {"nested": {"nested": {"nested": {"nested": {}}}}}
                                }
                            }
                        }
                    }
                }
            }
        },
        {"items": [None] * 10_000},
    ],
)
def test_assignment_create_rejects_rubric_structure_outside_evaluation_limits(
    rubric: dict[str, object],
) -> None:
    values = assignment_payload().model_dump()
    values["rubric"] = rubric

    with pytest.raises(ValidationError, match="rubric exceeds"):
        AssignmentCreate.model_validate(values)


def test_assignment_create_normalizes_aware_deadline_to_utc() -> None:
    china_standard_time = timezone(timedelta(hours=8))
    payload = assignment_payload(due_at=datetime(2027, 1, 2, 8, 30, tzinfo=china_standard_time))

    assert payload.due_at == datetime(2027, 1, 2, 0, 30, tzinfo=UTC)
    assert payload.due_at.tzinfo is UTC


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 50_001},
        {"notes": "x" * 10_001},
        {"due_at": "2026-01-01T12:00:00"},
    ],
)
def test_assignment_create_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "title": "Title",
        "question": "Question",
        "notes": "",
        "rubric": {},
        "due_at": datetime.now(UTC) + timedelta(days=1),
    }
    values.update(overrides)

    with pytest.raises(ValidationError):
        AssignmentCreate.model_validate(values)


@pytest.mark.parametrize(
    ("sequence_value", "expected"),
    [(1, "HW-0001"), (9999, "HW-9999"), (10_000, "HW-10000"), (123_456, "HW-123456")],
)
async def test_next_assignment_code_formats_without_truncation(
    sequence_value: int,
    expected: str,
) -> None:
    session = FocusedAssignmentSession(sequence_value)

    code = await next_assignment_code(session)  # type: ignore[arg-type]

    assert code == expected
    assert len(session.execute_statements) == 1
    assert "pg_advisory_xact_lock" in str(session.execute_statements[0])
    assert len(session.scalar_statements) == 1
    statement = session.scalar_statements[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "nextval('assignment_code_seq')" in compiled


async def test_create_assignment_persists_and_refreshes_a_draft_for_teacher() -> None:
    teacher_id = uuid.uuid4()
    session = FocusedAssignmentSession(sequence_value=42)

    assignment = await create_assignment(  # type: ignore[arg-type]
        session,
        assignment_payload(),
        teacher_id,
    )

    assert assignment.code == "HW-0042"
    assert assignment.title == "Compiler construction"
    assert assignment.question == "Explain SSA form."
    assert assignment.status is AssignmentStatus.DRAFT
    assert assignment.created_by == teacher_id
    assert assignment.published_at is None
    assert session.added == [assignment]
    assert session.events == ["flush", "refresh", "commit"]
    assert session.flush_count == 1
    assert session.commit_count == 1
    assert session.refresh_count == 1


async def test_create_assignment_propagates_database_errors() -> None:
    session = FocusedAssignmentSession()
    expected = IntegrityError("insert", {}, RuntimeError("duplicate"))
    session.commit_error = expected

    with pytest.raises(IntegrityError) as raised:
        await create_assignment(  # type: ignore[arg-type]
            session,
            assignment_payload(),
            uuid.uuid4(),
        )

    assert raised.value is expected
    assert session.commit_count == 1
    assert session.rollback_count == 1
    assert session.transaction_failed is False
    assert session.refresh_count == 1
    assert session.events == ["flush", "refresh", "commit", "rollback"]


async def test_create_assignment_failure_removes_pending_orm_object() -> None:
    orm_session = Session()
    session = FocusedAssignmentSession()
    expected = IntegrityError("insert", {}, RuntimeError("duplicate"))
    session.commit_error = expected
    fake_add = session.add
    fake_rollback = session.rollback
    captured: list[Assignment] = []

    def add_to_both_sessions(instance: object) -> None:
        fake_add(instance)
        assert isinstance(instance, Assignment)
        captured.append(instance)
        orm_session.add(instance)

    async def rollback_both_sessions() -> None:
        await fake_rollback()
        orm_session.rollback()

    session.add = add_to_both_sessions  # type: ignore[method-assign]
    session.rollback = rollback_both_sessions  # type: ignore[method-assign]

    with pytest.raises(IntegrityError) as raised:
        await create_assignment(  # type: ignore[arg-type]
            session,
            assignment_payload(),
            uuid.uuid4(),
        )

    assert raised.value is expected
    assert len(captured) == 1
    assert captured[0] not in orm_session
    assert inspect(captured[0]).transient is True
    assert list(orm_session.new) == []
    assert orm_session.in_transaction() is False
    orm_session.close()


async def test_publish_draft_with_future_deadline_sets_utc_timestamp() -> None:
    assignment = persisted_assignment()
    session = FocusedAssignmentSession(assignments=[assignment])

    result = await publish_assignment(session, assignment.id)  # type: ignore[arg-type]

    assert result is assignment
    assert assignment.status is AssignmentStatus.PUBLISHED
    assert assignment.published_at is not None
    assert assignment.published_at.tzinfo is UTC
    assert session.events == ["flush", "refresh", "commit"]
    assert session.commit_count == 1
    assert session.refresh_count == 1


async def test_publish_rejects_legacy_content_outside_evaluation_contract() -> None:
    assignment = persisted_assignment()
    assignment.question = "界" * 21_846
    session = FocusedAssignmentSession(assignments=[assignment])

    with pytest.raises(
        InvalidAssignmentTransition,
        match="assignment content cannot be evaluated",
    ):
        await publish_assignment(session, assignment.id)  # type: ignore[arg-type]

    assert assignment.status is AssignmentStatus.DRAFT
    assert assignment.published_at is None
    assert session.commit_count == 0
    assert session.rollback_count == 1


@pytest.mark.parametrize(
    "status",
    [
        AssignmentStatus.PUBLISHED,
        AssignmentStatus.CLOSED,
        AssignmentStatus.ARCHIVED,
    ],
)
async def test_publish_preserves_invalid_status_reason(status: AssignmentStatus) -> None:
    assignment = persisted_assignment(status=status)
    session = FocusedAssignmentSession(assignments=[assignment])

    with pytest.raises(
        InvalidAssignmentTransition,
        match=rf"^cannot publish assignment with status {status.value}$",
    ):
        await publish_assignment(session, assignment.id)  # type: ignore[arg-type]

    assert session.rollback_count == 1


async def test_publish_preserves_expired_deadline_reason() -> None:
    assignment = persisted_assignment(due_at=datetime.now(UTC) - timedelta(seconds=1))
    session = FocusedAssignmentSession(assignments=[assignment])

    with pytest.raises(
        InvalidAssignmentTransition,
        match=r"^cannot publish assignment after its deadline$",
    ):
        await publish_assignment(session, assignment.id)  # type: ignore[arg-type]

    assert session.rollback_count == 1


async def test_close_published_assignment() -> None:
    assignment = persisted_assignment(
        status=AssignmentStatus.PUBLISHED,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session = FocusedAssignmentSession(assignments=[assignment])

    await close_assignment(session, assignment.id)  # type: ignore[arg-type]

    assert assignment.status is AssignmentStatus.CLOSED
    assert session.events == ["flush", "refresh", "commit"]
    assert session.commit_count == 1
    assert session.refresh_count == 1


async def test_reopen_closed_assignment_preserves_original_publication_timestamp() -> None:
    original_published_at = datetime.now(UTC) - timedelta(hours=1)
    assignment = persisted_assignment(
        status=AssignmentStatus.CLOSED,
        published_at=original_published_at,
    )
    session = FocusedAssignmentSession(assignments=[assignment])

    await reopen_assignment(session, assignment.id)  # type: ignore[arg-type]

    assert assignment.status is AssignmentStatus.PUBLISHED
    assert assignment.published_at is original_published_at
    assert session.events == ["flush", "refresh", "commit"]
    assert session.commit_count == 1
    assert session.refresh_count == 1


@pytest.mark.parametrize(
    ("operation", "status"),
    [
        (publish_assignment, AssignmentStatus.PUBLISHED),
        (publish_assignment, AssignmentStatus.CLOSED),
        (publish_assignment, AssignmentStatus.ARCHIVED),
        (close_assignment, AssignmentStatus.DRAFT),
        (close_assignment, AssignmentStatus.CLOSED),
        (close_assignment, AssignmentStatus.ARCHIVED),
        (reopen_assignment, AssignmentStatus.DRAFT),
        (reopen_assignment, AssignmentStatus.PUBLISHED),
        (reopen_assignment, AssignmentStatus.ARCHIVED),
    ],
)
async def test_invalid_state_transition_does_not_mutate_or_commit(
    operation: Any,
    status: AssignmentStatus,
) -> None:
    published_at = datetime.now(UTC) - timedelta(hours=1)
    assignment = persisted_assignment(status=status, published_at=published_at)
    session = FocusedAssignmentSession(assignments=[assignment])

    with pytest.raises(InvalidAssignmentTransition):
        await operation(session, assignment.id)

    assert assignment.status is status
    assert assignment.published_at is published_at
    assert session.commit_count == 0
    assert session.refresh_count == 0
    assert session.rollback_count == 1


@pytest.mark.parametrize("operation", [publish_assignment, reopen_assignment])
async def test_deadline_sensitive_transition_rejects_expired_or_equal_deadline(
    operation: Any,
) -> None:
    for due_at in [datetime.now(UTC) - timedelta(seconds=1), datetime.now(UTC)]:
        starting_status = (
            AssignmentStatus.DRAFT if operation is publish_assignment else AssignmentStatus.CLOSED
        )
        assignment = persisted_assignment(status=starting_status, due_at=due_at)
        session = FocusedAssignmentSession(assignments=[assignment])
        original_published_at = assignment.published_at

        with pytest.raises(InvalidAssignmentTransition):
            await operation(session, assignment.id)

        assert assignment.status is starting_status
        assert assignment.published_at is original_published_at
        assert session.commit_count == 0
        assert session.refresh_count == 0
        assert session.rollback_count == 1


@pytest.mark.parametrize(
    ("operation", "status", "due_at"),
    [
        (
            close_assignment,
            AssignmentStatus.DRAFT,
            datetime.now(UTC) + timedelta(days=1),
        ),
        (
            publish_assignment,
            AssignmentStatus.DRAFT,
            datetime.now(UTC) - timedelta(seconds=1),
        ),
    ],
)
async def test_expected_transition_conflict_leaves_orm_session_clean(
    operation: Any,
    status: AssignmentStatus,
    due_at: datetime,
) -> None:
    assignment = persisted_assignment(status=status, due_at=due_at)
    make_transient_to_detached(assignment)
    orm_session = Session()
    orm_session.add(assignment)
    session = FocusedAssignmentSession(assignments=[assignment])
    fake_rollback = session.rollback

    async def rollback_both_sessions() -> None:
        await fake_rollback()
        orm_session.rollback()

    session.rollback = rollback_both_sessions  # type: ignore[method-assign]

    with pytest.raises(InvalidAssignmentTransition):
        await operation(session, assignment.id)

    assert assignment not in orm_session.dirty
    assert orm_session.is_modified(assignment) is False
    assert inspect(assignment).modified is False
    assert orm_session.in_transaction() is False
    orm_session.close()


@pytest.mark.parametrize(
    ("operation", "starting_status"),
    [
        (publish_assignment, AssignmentStatus.DRAFT),
        (close_assignment, AssignmentStatus.PUBLISHED),
        (reopen_assignment, AssignmentStatus.CLOSED),
    ],
)
async def test_transition_locks_assignment_row_before_state_check(
    operation: Any,
    starting_status: AssignmentStatus,
) -> None:
    assignment = persisted_assignment(
        status=starting_status,
        published_at=(
            datetime.now(UTC) - timedelta(hours=1)
            if starting_status is not AssignmentStatus.DRAFT
            else None
        ),
    )
    session = FocusedAssignmentSession(assignments=[assignment])

    result = await operation(session, assignment.id)

    assert result is assignment
    assert len(session.lock_statements) == 1
    compiled = str(
        session.lock_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert f"assignments.id = '{assignment.id}'" in compiled
    assert compiled.endswith("FOR UPDATE")
    assert session.lock_statements[0].get_execution_options()["populate_existing"] is True


@pytest.mark.parametrize(
    ("operation", "starting_status", "published_at"),
    [
        (publish_assignment, AssignmentStatus.DRAFT, None),
        (
            close_assignment,
            AssignmentStatus.PUBLISHED,
            datetime(2026, 1, 1, tzinfo=UTC),
        ),
        (
            reopen_assignment,
            AssignmentStatus.CLOSED,
            datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ],
)
async def test_transition_commit_failure_rolls_back_and_restores_state(
    operation: Any,
    starting_status: AssignmentStatus,
    published_at: datetime | None,
) -> None:
    assignment = persisted_assignment(status=starting_status, published_at=published_at)
    session = FocusedAssignmentSession(assignments=[assignment])
    expected = IntegrityError("update", {}, RuntimeError("database unavailable"))
    session.commit_error = expected

    with pytest.raises(IntegrityError) as raised:
        await operation(session, assignment.id)

    assert raised.value is expected
    assert assignment.status is starting_status
    assert assignment.published_at is published_at
    assert session.commit_count == 1
    assert session.rollback_count == 1
    assert session.refresh_count == 1
    assert session.transaction_failed is False


async def test_transition_commit_failure_leaves_persistent_orm_object_clean() -> None:
    assignment = persisted_assignment()
    make_transient_to_detached(assignment)
    orm_session = Session()
    orm_session.add(assignment)
    session = FocusedAssignmentSession(assignments=[assignment])
    expected = IntegrityError("update", {}, RuntimeError("database unavailable"))
    session.commit_error = expected
    fake_rollback = session.rollback

    async def rollback_both_sessions() -> None:
        await fake_rollback()
        orm_session.rollback()

    session.rollback = rollback_both_sessions  # type: ignore[method-assign]

    with pytest.raises(IntegrityError) as raised:
        await publish_assignment(session, assignment.id)  # type: ignore[arg-type]

    assert raised.value is expected
    assert assignment.status is AssignmentStatus.DRAFT
    assert assignment.published_at is None
    assert assignment not in orm_session.dirty
    assert orm_session.is_modified(assignment) is False
    assert inspect(assignment).modified is False
    assert orm_session.in_transaction() is False
    orm_session.close()


@pytest.mark.parametrize(
    "operation",
    [publish_assignment, close_assignment, reopen_assignment],
)
async def test_transition_returns_none_when_locked_assignment_is_missing(
    operation: Any,
) -> None:
    session = FocusedAssignmentSession()

    result = await operation(session, uuid.uuid4())

    assert result is None
    assert len(session.lock_statements) == 1
    assert session.commit_count == 0
    assert session.rollback_count == 1


async def test_postgresql_concurrent_publish_allows_exactly_one_success(
    postgres_session: AsyncSession,
) -> None:
    teacher = User(
        id=uuid.uuid4(),
        username=f"concurrency-teacher-{uuid.uuid4().hex}",
        display_name="Concurrency Teacher",
        role=Role.TEACHER,
        password_hash="unused",
        is_active=True,
    )
    postgres_session.add(teacher)
    await postgres_session.commit()
    created = await create_assignment(postgres_session, assignment_payload(), teacher.id)
    created_id = created.id
    assert postgres_session.in_transaction() is False
    assert postgres_session.bind is not None
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async with session_factory() as first, session_factory() as second:
        results = await asyncio.gather(
            publish_assignment(first, created_id),
            publish_assignment(second, created_id),
            return_exceptions=True,
        )
        await first.rollback()
        await second.rollback()

    successes = [result for result in results if isinstance(result, Assignment)]
    conflicts = [result for result in results if isinstance(result, InvalidAssignmentTransition)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0].status is AssignmentStatus.PUBLISHED
    assert successes[0].published_at is not None


async def test_postgresql_stale_identity_is_refreshed_before_publish(
    postgres_session: AsyncSession,
) -> None:
    teacher = User(
        id=uuid.uuid4(),
        username=f"stale-identity-teacher-{uuid.uuid4().hex}",
        display_name="Stale Identity Teacher",
        role=Role.TEACHER,
        password_hash="unused",
        is_active=True,
    )
    postgres_session.add(teacher)
    await postgres_session.commit()
    created = await create_assignment(postgres_session, assignment_payload(), teacher.id)
    created_id = created.id
    assert postgres_session.in_transaction() is False
    await postgres_session.commit()
    preloaded = await postgres_session.get(Assignment, created_id)
    assert preloaded is not None
    assert preloaded.status is AssignmentStatus.DRAFT
    await postgres_session.commit()
    assert postgres_session.bind is not None
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async with session_factory() as publisher:
        published = await publish_assignment(publisher, created_id)
        assert published is not None
        published_at = published.published_at
        await publisher.rollback()

    with pytest.raises(InvalidAssignmentTransition):
        await publish_assignment(postgres_session, created_id)

    assert postgres_session.in_transaction() is False
    async with session_factory() as verifier:
        persisted = await verifier.get(Assignment, created_id)
        assert persisted is not None
        assert persisted.status is AssignmentStatus.PUBLISHED
        assert persisted.published_at == published_at


async def test_postgresql_roundtrip_persists_enum_and_uses_real_sequence(
    postgres_session: AsyncSession,
) -> None:
    teacher = User(
        id=uuid.uuid4(),
        username=f"teacher-{uuid.uuid4().hex}",
        display_name="PostgreSQL Teacher",
        role=Role.TEACHER,
        password_hash="unused",
        is_active=True,
    )
    postgres_session.add(teacher)
    await postgres_session.commit()

    created = await create_assignment(postgres_session, assignment_payload(), teacher.id)
    loaded = await postgres_session.scalar(select(Assignment).where(Assignment.id == created.id))

    assert loaded is not None
    assert loaded.code == "HW-0001"
    assert loaded.status is AssignmentStatus.DRAFT
    assert loaded.rubric == {"correctness": 70, "clarity": 30}
    assert loaded.created_by == teacher.id
