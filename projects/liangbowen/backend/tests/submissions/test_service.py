import json
import math
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import Select

from app.assignments.model import Assignment
from app.db.base import Base
from app.db.types import (
    AssignmentStatus,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)
from app.submissions.model import Submission
from app.submissions.schemas import SubmissionCreate, SubmissionRead
from app.submissions.service import (
    SubmissionAssignmentNotFound,
    SubmissionClosed,
    SubmissionTooLong,
    create_submission,
)
from app.users.model import User

MAX_CONTENT_JSON_BYTES = 100 * 1024
MAX_CONTENT_JSON_DEPTH = 32
MAX_CONTENT_JSON_NODES = 10_000


def assignment(
    *,
    assignment_id: uuid.UUID | None = None,
    status: AssignmentStatus = AssignmentStatus.PUBLISHED,
    due_at: datetime | None = None,
) -> Assignment:
    return Assignment(
        id=assignment_id or uuid.uuid4(),
        code=f"HW-{uuid.uuid4().int % 10000:04d}",
        title="Compiler construction",
        question="Explain SSA form.",
        notes="",
        rubric={"correctness": 100},
        due_at=due_at or datetime.now(UTC) + timedelta(days=1),
        status=status,
        mattermost_channel_id=None,
        created_by=uuid.uuid4(),
        created_at=datetime.now(UTC),
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )


def payload(content_text: str = "answer") -> SubmissionCreate:
    return SubmissionCreate(
        content_type=SubmissionContentType.MARKDOWN,
        content_text=content_text,
        content_json=None,
    )


class FocusedSubmissionSession:
    def __init__(
        self,
        authoritative_assignments: list[Assignment] | None = None,
        versions: dict[tuple[uuid.UUID, uuid.UUID], int] | None = None,
    ) -> None:
        self.assignments = {item.id: item for item in authoritative_assignments or []}
        self.versions = versions or {}
        self.scalar_statements: list[Select[Any]] = []
        self.added: list[Submission] = []
        self.pending: list[Submission] = []
        self.staged: list[Submission] = []
        self.persisted: list[Submission] = []
        self.events: list[str] = []
        self.flush_count = 0
        self.commit_count = 0
        self.refresh_count = 0
        self.rollback_count = 0
        self.commit_error: Exception | None = None
        self.flush_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.transaction_failed = False
        self.transaction_active = False
        self.no_autoflush = nullcontext()

    async def scalar(self, statement: Select[Any]) -> Assignment | int | None:
        self.scalar_statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        parameters = statement.compile().params
        if "FROM assignments" in sql:
            assignment_id = next(
                value for value in parameters.values() if isinstance(value, uuid.UUID)
            )
            return self.assignments.get(assignment_id)
        assignment_id, student_id = [
            value for value in parameters.values() if isinstance(value, uuid.UUID)
        ]
        staged_versions = [
            item.version
            for item in self.staged
            if item.assignment_id == assignment_id and item.student_id == student_id
        ]
        persisted_version = self.versions.get((assignment_id, student_id))
        return max(
            [version for version in [persisted_version, *staged_versions] if version is not None],
            default=None,
        )

    def add(self, submission: Submission) -> None:
        self.added.append(submission)
        self.pending.append(submission)
        self.staged.append(submission)
        self.events.append("add")
        self.transaction_active = True

    async def flush(self, objects: list[object] | None = None) -> None:
        self.events.append("flush")
        self.flush_count += 1
        if self.flush_error is not None:
            self.transaction_failed = True
            raise self.flush_error
        flushed = self.staged if objects is None else objects
        for submission in flushed:
            assert isinstance(submission, Submission)
            if submission in self.pending:
                self.pending.remove(submission)
            submission.id = submission.id or uuid.uuid4()

    async def commit(self) -> None:
        assert self.transaction_failed is False
        self.events.append("commit")
        self.commit_count += 1
        if self.commit_error is not None:
            self.transaction_failed = True
            raise self.commit_error
        self.persisted.extend(self.staged)
        for submission in self.staged:
            self.versions[(submission.assignment_id, submission.student_id)] = submission.version
        self.pending.clear()
        self.staged.clear()
        self.transaction_active = False

    async def rollback(self) -> None:
        self.events.append("rollback")
        self.rollback_count += 1
        self.pending.clear()
        self.staged.clear()
        self.transaction_failed = False
        self.transaction_active = False

    async def refresh(self, submission: Submission) -> None:
        self.events.append("refresh")
        self.refresh_count += 1
        if self.refresh_error is not None:
            self.transaction_failed = True
            raise self.refresh_error
        submission.id = submission.id or uuid.uuid4()


def test_submission_model_has_versioned_postgresql_contract() -> None:
    table = Base.metadata.tables["submissions"]

    assert table is Submission.__table__
    assert Base.metadata.tables["users"] is User.__table__
    assert set(table.c.keys()) == {
        "id",
        "assignment_id",
        "student_id",
        "version",
        "content_type",
        "content_text",
        "content_json",
        "status",
        "submitted_at",
        "source",
    }
    assert next(iter(table.c.assignment_id.foreign_keys)).target_fullname == "assignments.id"
    assert next(iter(table.c.student_id.foreign_keys)).target_fullname == "users.id"
    assert table.c.assignment_id.nullable is False
    assert table.c.student_id.nullable is False
    assert isinstance(table.c.version.type, Integer)
    assert table.c.version.nullable is False
    assert isinstance(table.c.content_type.type, Enum)
    assert table.c.content_type.type.enums == ["text", "markdown", "code", "structured"]
    assert isinstance(table.c.content_text.type, Text)
    assert table.c.content_text.nullable is False
    assert isinstance(table.c.content_json.type, JSONB)
    assert table.c.content_json.nullable is True
    assert isinstance(table.c.status.type, Enum)
    assert table.c.status.type.enums == ["submitted", "withdrawn"]
    assert table.c.status.default is not None
    assert table.c.status.default.arg is SubmissionStatus.SUBMITTED
    assert isinstance(table.c.submitted_at.type, DateTime)
    assert table.c.submitted_at.type.timezone is True
    assert table.c.submitted_at.nullable is False
    assert isinstance(table.c.source.type, Enum)
    assert table.c.source.type.enums == ["web", "mattermost"]
    unique = next(
        constraint for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
    )
    assert unique.name == "uq_submissions_assignment_student_version"
    assert [column.name for column in unique.columns] == [
        "assignment_id",
        "student_id",
        "version",
    ]
    version_check = next(
        constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint)
    )
    assert version_check.name == "ck_submissions_version_positive"
    assert str(version_check.sqltext) == "version >= 1"
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT ck_submissions_version_positive CHECK (version >= 1)" in ddl
    latest_index = next(
        index
        for index in table.indexes
        if index.name == "ix_submissions_assignment_student_version_desc"
    )
    assert [str(expression) for expression in latest_index.expressions] == [
        "submissions.assignment_id",
        "submissions.student_id",
        "submissions.version DESC",
    ]


def test_submission_create_is_strict_bounded_and_forbids_server_fields() -> None:
    accepted = SubmissionCreate(
        content_type=SubmissionContentType.STRUCTURED,
        content_text="x" * 50_000,
        content_json={"answer": [1, True, None, "四"]},
    )
    assert len(accepted.content_text) == 50_000

    with pytest.raises(ValidationError):
        SubmissionCreate(
            content_type=SubmissionContentType.TEXT,
            content_text="x" * 50_001,
        )
    with pytest.raises(ValidationError):
        SubmissionCreate.model_validate({"content_type": "text", "content_text": 123})
    for forbidden in ("student_id", "source", "version", "status", "submitted_at"):
        with pytest.raises(ValidationError):
            SubmissionCreate.model_validate(
                {
                    "content_type": "text",
                    "content_text": "answer",
                    forbidden: "forged",
                }
            )


@pytest.mark.parametrize(
    "values",
    [
        {"content_type": "structured", "content_text": "", "content_json": None},
        {"content_type": "structured", "content_text": "", "content_json": "scalar"},
        {"content_type": "structured", "content_text": "", "content_json": 7},
        {"content_type": "text", "content_text": "answer", "content_json": {}},
        {"content_type": "markdown", "content_text": " \n\t", "content_json": None},
        {"content_type": "code", "content_text": "", "content_json": None},
    ],
)
def test_submission_create_enforces_content_type_semantics(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SubmissionCreate.model_validate(values)


def test_structured_submission_accepts_object_or_list_without_text() -> None:
    for value in ({"answer": 42}, ["a", "b"]):
        created = SubmissionCreate(
            content_type=SubmissionContentType.STRUCTURED,
            content_text="",
            content_json=value,
        )

        assert created.content_json == value


@pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
def test_submission_create_rejects_non_finite_numbers_recursively(
    non_finite: float,
) -> None:
    with pytest.raises(ValidationError):
        SubmissionCreate(
            content_type=SubmissionContentType.STRUCTURED,
            content_text="",
            content_json={"outer": [1, {"unsafe": non_finite}]},
        )


def test_submission_create_limits_compact_utf8_json_bytes() -> None:
    accepted_value = {"answer": "四" * 1_000}
    assert (
        len(
            json.dumps(
                accepted_value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        < MAX_CONTENT_JSON_BYTES
    )
    accepted = SubmissionCreate(
        content_type=SubmissionContentType.STRUCTURED,
        content_text="",
        content_json=accepted_value,
    )
    assert accepted.content_json == accepted_value

    with pytest.raises(ValidationError):
        SubmissionCreate(
            content_type=SubmissionContentType.STRUCTURED,
            content_text="",
            content_json={"answer": "四" * MAX_CONTENT_JSON_BYTES},
        )


def test_submission_create_limits_json_depth_and_nodes() -> None:
    at_depth: object = "leaf"
    for _ in range(MAX_CONTENT_JSON_DEPTH - 1):
        at_depth = [at_depth]
    accepted = SubmissionCreate(
        content_type=SubmissionContentType.STRUCTURED,
        content_text="",
        content_json=at_depth,
    )
    assert accepted.content_json == at_depth

    too_deep: object = [at_depth]
    with pytest.raises(ValidationError):
        SubmissionCreate(
            content_type=SubmissionContentType.STRUCTURED,
            content_text="",
            content_json=too_deep,
        )

    with pytest.raises(ValidationError):
        SubmissionCreate(
            content_type=SubmissionContentType.STRUCTURED,
            content_text="",
            content_json=[None] * MAX_CONTENT_JSON_NODES,
        )


def test_submission_read_exposes_only_declared_fields() -> None:
    item = Submission(
        id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
        student_id=uuid.uuid4(),
        version=2,
        content_type=SubmissionContentType.CODE,
        content_text="print('ok')",
        content_json=None,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
        source=SubmissionSource.WEB,
    )
    item.internal_secret = "never expose"

    assert set(SubmissionRead.model_validate(item).model_dump()) == {
        "id",
        "assignment_id",
        "student_id",
        "version",
        "content_type",
        "content_text",
        "content_json",
        "status",
        "submitted_at",
        "source",
    }


async def test_versions_increment_per_assignment_and_student() -> None:
    first_assignment = assignment()
    second_assignment = assignment()
    first_student = uuid.uuid4()
    second_student = uuid.uuid4()
    session = FocusedSubmissionSession([first_assignment, second_assignment])

    first = await create_submission(
        session, first_assignment, first_student, payload("v1"), SubmissionSource.WEB
    )
    second = await create_submission(
        session, first_assignment, first_student, payload("v2"), SubmissionSource.WEB
    )
    other_student = await create_submission(
        session, first_assignment, second_student, payload(), SubmissionSource.WEB
    )
    other_assignment = await create_submission(
        session, second_assignment, first_student, payload(), SubmissionSource.MATTERMOST
    )

    assert (first.version, second.version) == (1, 2)
    assert other_student.version == 1
    assert other_assignment.version == 1
    assert other_assignment.source is SubmissionSource.MATTERMOST
    assert all(item.status is SubmissionStatus.SUBMITTED for item in session.added)
    assert all(item.submitted_at.tzinfo is UTC for item in session.added)


async def test_service_flushes_submission_without_owning_the_transaction() -> None:
    authoritative = assignment()
    session = FocusedSubmissionSession([authoritative])

    created = await create_submission(
        session, authoritative, uuid.uuid4(), payload(), SubmissionSource.WEB
    )

    assert session.events == ["add", "flush"]
    assert created.id is not None
    assert created.status is SubmissionStatus.SUBMITTED
    assert created.submitted_at.tzinfo is UTC
    assert session.persisted == []
    assert session.pending == []
    assert session.staged == [created]
    assert session.transaction_active is True
    assert session.commit_count == 0
    assert session.rollback_count == 0
    assert session.refresh_count == 0


async def test_service_locks_authoritative_assignment_and_refreshes_stale_state() -> None:
    assignment_id = uuid.uuid4()
    stale = assignment(assignment_id=assignment_id, status=AssignmentStatus.CLOSED)
    authoritative = assignment(
        assignment_id=assignment_id,
        status=AssignmentStatus.PUBLISHED,
    )
    session = FocusedSubmissionSession([authoritative])

    created = await create_submission(session, stale, uuid.uuid4(), payload(), SubmissionSource.WEB)

    assert created.version == 1
    lock = session.scalar_statements[0]
    assert lock._for_update_arg is not None
    assert lock.get_execution_options()["populate_existing"] is True
    assert lock.column_descriptions[0]["entity"] is Assignment
    compiled = str(lock.compile(dialect=postgresql.dialect()))
    assert "FROM assignments" in compiled
    assert "FOR UPDATE" in compiled
    assert "assignments.id" in compiled


@pytest.mark.parametrize(
    "status",
    [
        AssignmentStatus.DRAFT,
        AssignmentStatus.CLOSED,
        AssignmentStatus.ARCHIVED,
    ],
)
async def test_non_published_authoritative_assignment_is_closed(
    status: AssignmentStatus,
) -> None:
    authoritative = assignment(status=status)
    session = FocusedSubmissionSession([authoritative])

    with pytest.raises(SubmissionClosed):
        await create_submission(
            session,
            assignment(assignment_id=authoritative.id),
            uuid.uuid4(),
            payload(),
            SubmissionSource.WEB,
        )

    assert session.added == []
    assert session.rollback_count == 0


@pytest.mark.parametrize("delta", [timedelta(0), -timedelta(microseconds=1)])
async def test_deadline_at_now_or_past_is_closed(
    monkeypatch: pytest.MonkeyPatch,
    delta: timedelta,
) -> None:
    from app.submissions import service as service_module

    now = datetime(2027, 1, 1, tzinfo=UTC)
    authoritative = assignment(due_at=now + delta)
    session = FocusedSubmissionSession([authoritative])
    monkeypatch.setattr(service_module, "utc_now", lambda: now)

    with pytest.raises(SubmissionClosed):
        await create_submission(
            session, authoritative, uuid.uuid4(), payload(), SubmissionSource.WEB
        )

    assert session.added == []
    assert session.rollback_count == 0


async def test_missing_authoritative_assignment_leaves_transaction_to_caller() -> None:
    missing = assignment()
    session = FocusedSubmissionSession()

    with pytest.raises(SubmissionAssignmentNotFound):
        await create_submission(session, missing, uuid.uuid4(), payload(), SubmissionSource.WEB)

    assert session.rollback_count == 0
    assert session.added == []


async def test_service_accepts_50000_and_rejects_50001_before_lock_or_insert() -> None:
    authoritative = assignment()
    accepted_session = FocusedSubmissionSession([authoritative])
    rejected_session = FocusedSubmissionSession([authoritative])

    accepted = await create_submission(
        accepted_session,
        authoritative,
        uuid.uuid4(),
        payload("x" * 50_000),
        SubmissionSource.WEB,
    )
    too_long = SubmissionCreate.model_construct(
        content_type=SubmissionContentType.TEXT,
        content_text="x" * 50_001,
        content_json=None,
    )
    with pytest.raises(SubmissionTooLong):
        await create_submission(
            rejected_session,
            authoritative,
            uuid.uuid4(),
            too_long,
            SubmissionSource.WEB,
        )

    assert len(accepted.content_text) == 50_000
    assert rejected_session.scalar_statements == []
    assert rejected_session.added == []
    assert rejected_session.rollback_count == 0


async def test_caller_controls_atomic_commit_of_submission_and_other_work() -> None:
    authoritative = assignment()
    committed = FocusedSubmissionSession([authoritative])
    rolled_back = FocusedSubmissionSession([authoritative])

    created = await create_submission(
        committed, authoritative, uuid.uuid4(), payload(), SubmissionSource.WEB
    )
    await committed.commit()
    discarded = await create_submission(
        rolled_back, authoritative, uuid.uuid4(), payload(), SubmissionSource.WEB
    )
    await rolled_back.rollback()

    assert committed.persisted == [created]
    assert rolled_back.persisted == []
    assert discarded not in rolled_back.staged


async def test_flush_failure_is_left_for_caller_to_roll_back() -> None:
    authoritative = assignment()
    session = FocusedSubmissionSession([authoritative])
    session.flush_error = RuntimeError("flush failed")

    with pytest.raises(RuntimeError, match="flush failed"):
        await create_submission(
            session, authoritative, uuid.uuid4(), payload(), SubmissionSource.WEB
        )

    assert session.flush_count == 1
    assert session.refresh_count == 0
    assert session.commit_count == 0
    assert session.rollback_count == 0
    assert session.persisted == []
    assert len(session.pending) == 1
    assert len(session.staged) == 1
    assert session.transaction_failed is True
    await session.rollback()
    assert session.transaction_failed is False
    assert session.transaction_active is False


async def test_service_flushes_only_its_submission() -> None:
    authoritative = assignment()
    session = FocusedSubmissionSession([authoritative])
    unrelated = Submission(
        assignment_id=uuid.uuid4(),
        student_id=uuid.uuid4(),
        version=1,
        content_type=SubmissionContentType.TEXT,
        content_text="unrelated",
        content_json=None,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
        source=SubmissionSource.WEB,
    )
    session.pending.append(unrelated)

    created = await create_submission(
        session, authoritative, uuid.uuid4(), payload(), SubmissionSource.WEB
    )

    assert session.flush_count == 1
    assert session.refresh_count == 0
    assert session.commit_count == 0
    assert session.rollback_count == 0
    assert session.persisted == []
    assert session.pending == [unrelated]
    assert session.staged == [created]


async def test_schema_bypass_is_rejected_before_any_database_access() -> None:
    authoritative = assignment()
    session = FocusedSubmissionSession([authoritative])
    invalid = SubmissionCreate.model_construct(
        content_type=SubmissionContentType.STRUCTURED,
        content_text="",
        content_json={"unsafe": math.nan},
    )

    with pytest.raises(ValueError):
        await create_submission(
            session,
            authoritative,
            uuid.uuid4(),
            invalid,
            SubmissionSource.WEB,
        )

    assert session.scalar_statements == []
    assert session.added == []
    assert session.flush_count == 0
    assert session.commit_count == 0
    assert session.rollback_count == 0
