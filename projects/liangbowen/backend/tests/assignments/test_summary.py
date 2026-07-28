import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateIndex

from app.assignments import summary as summary_module
from app.assignments.model import Assignment
from app.assignments.router import router as assignment_router
from app.assignments.summary import AssignmentSummary, build_assignment_summary
from app.auth.dependencies import get_current_user
from app.db.session import get_session
from app.db.types import (
    AssignmentStatus,
    Role,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)
from app.main import create_app
from app.submissions.model import Submission
from app.users.model import User
from app.users.schemas import UserCreate
from app.users.service import create_user


def make_user(
    username: str,
    role: Role = Role.STUDENT,
    *,
    display_name: str | None = None,
    active: bool = True,
) -> User:
    return User(
        id=uuid.uuid4(),
        username=username,
        display_name=display_name or username.title(),
        role=role,
        password_hash="not-exposed",
        is_active=active,
    )


def make_assignment(teacher: User, *, code: str, status: AssignmentStatus) -> Assignment:
    return Assignment(
        id=uuid.uuid4(),
        code=code,
        title="Summary projection",
        question="Explain the result.",
        notes="teacher notes",
        rubric={"secret_solution": "never expose this"},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=status,
        mattermost_channel_id="private-channel",
        created_by=teacher.id,
        published_at=None,
    )


def make_submission(
    assignment: Assignment,
    student: User,
    version: int,
    submitted_at: datetime,
    *,
    status: SubmissionStatus = SubmissionStatus.SUBMITTED,
) -> Submission:
    return Submission(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        student_id=student.id,
        version=version,
        content_type=SubmissionContentType.MARKDOWN,
        content_text=f"answer {assignment.code} v{version}",
        content_json=None,
        status=status,
        submitted_at=submitted_at,
        source=SubmissionSource.WEB,
    )


async def seed(
    session: AsyncSession,
    *objects: User | Assignment | Submission,
) -> None:
    for model in (User, Assignment, Submission):
        batch = [item for item in objects if isinstance(item, model)]
        if batch:
            session.add_all(batch)
            await session.flush()


@asynccontextmanager
async def api_client(
    current_user: User,
    session: Any,
    *,
    raise_app_exceptions: bool = True,
) -> AsyncIterator[AsyncClient]:
    async def override_current_user() -> User:
        return current_user

    async def override_session() -> AsyncIterator[Any]:
        yield session

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(
            app=application,
            raise_app_exceptions=raise_app_exceptions,
        ),
        base_url="http://test",
    ) as client:
        yield client


async def test_summary_selects_latest_submitted_per_student_without_cross_assignment_leaks(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("teacher", Role.TEACHER)
    student_a = make_user("alice", display_name="Alice")
    student_b = make_user("bob", display_name="Bob")
    student_c = make_user("carol", display_name="Carol")
    inactive = make_user("inactive", active=False)
    admin = make_user("admin", Role.ADMIN)
    other_teacher = make_user("other-teacher", Role.TEACHER)
    target = make_assignment(teacher, code="HW-1001", status=AssignmentStatus.DRAFT)
    other = make_assignment(teacher, code="HW-1002", status=AssignmentStatus.PUBLISHED)
    now = datetime.now(UTC)
    await seed(
        postgres_session,
        teacher,
        student_a,
        student_b,
        student_c,
        inactive,
        admin,
        other_teacher,
        target,
        other,
        make_submission(target, student_a, 1, now - timedelta(hours=4)),
        make_submission(target, student_a, 2, now - timedelta(hours=2)),
        make_submission(
            target, student_a, 3, now - timedelta(hours=1), status=SubmissionStatus.WITHDRAWN
        ),
        make_submission(target, student_b, 1, now - timedelta(hours=3)),
        make_submission(other, student_a, 99, now),
        make_submission(target, inactive, 7, now),
    )

    summary = await build_assignment_summary(postgres_session, target.id, limit=50, offset=0)

    assert summary.assignment_id == target.id
    assert (summary.total_students, summary.submitted_students, summary.missing_students) == (
        3,
        2,
        1,
    )
    assert summary.latest_submission_versions == {student_a.id: 2, student_b.id: 1}
    assert summary.latest_submission_at == now - timedelta(hours=2)
    assert [row.student_id for row in summary.students] == [
        student_a.id,
        student_b.id,
        student_c.id,
    ]
    assert [row.latest_version for row in summary.students] == [2, 1, None]
    assert summary.students[0].latest_submission is not None
    assert summary.students[0].latest_submission.content_text == "answer HW-1001 v2"
    assert summary.students[2].latest_submission is None
    assert all(
        (
            row.evaluation_status,
            row.report_status,
            row.score,
            row.grade,
            row.evaluation_error,
            row.evaluation_error_code,
            row.latest_report_id,
        )
        == (None, None, None, None, None, None, None)
        for row in summary.students
    )
    assert (
        summary.pending_evaluation,
        summary.evaluating,
        summary.pending_review,
        summary.reviewed,
        summary.failed,
    ) == (2, 0, 0, 0, 0)


async def test_created_active_student_immediately_joins_existing_assignment_population(
    postgres_session: AsyncSession,
) -> None:
    admin = make_user("roster-admin", Role.ADMIN)
    teacher = make_user("roster-teacher", Role.TEACHER)
    existing_student = make_user("roster-existing")
    assignment = make_assignment(
        teacher,
        code="HW-ROSTER-1",
        status=AssignmentStatus.PUBLISHED,
    )
    await seed(postgres_session, admin, teacher, existing_student, assignment)
    await postgres_session.commit()

    before = await build_assignment_summary(
        postgres_session,
        assignment.id,
        limit=50,
        offset=0,
    )
    created = await create_user(
        postgres_session,
        admin.id,
        UserCreate(
            username="roster.new-student",
            display_name="Roster New Student",
            role=Role.STUDENT,
            password="Course2026!Secure",
        ),
        "summary-roster-request",
    )
    after = await build_assignment_summary(
        postgres_session,
        assignment.id,
        limit=50,
        offset=0,
    )

    assert created.is_active is True
    assert after.total_students == before.total_students + 1
    assert after.missing_students == before.missing_students + 1
    assert after.submitted_students == before.submitted_students
    assert created.id in {row.student_id for row in after.students}


async def test_summary_paginates_stably_but_keeps_full_counts_and_version_map(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("teacher", Role.TEACHER)
    students = [
        make_user("z-user", display_name="Same"),
        make_user("a-user", display_name="Same"),
        make_user("middle", display_name="Middle"),
    ]
    assignment = make_assignment(teacher, code="HW-2001", status=AssignmentStatus.ARCHIVED)
    now = datetime.now(UTC)
    await seed(postgres_session, teacher, *students, assignment)
    await seed(
        postgres_session,
        *(
            make_submission(assignment, student, 1, now + timedelta(minutes=index))
            for index, student in enumerate(students)
        ),
    )

    summary = await build_assignment_summary(postgres_session, assignment.id, limit=1, offset=1)

    assert summary.limit == 1
    assert summary.offset == 1
    assert summary.total_students == summary.submitted_students == 3
    assert summary.missing_students == 0
    assert summary.latest_submission_versions == {student.id: 1 for student in students}
    assert [(row.display_name, row.username) for row in summary.students] == [("Same", "a-user")]
    assert summary.latest_submission_at == now + timedelta(minutes=2)


async def test_summary_handles_no_active_students_in_one_windowed_query(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("teacher", Role.TEACHER)
    assignment = make_assignment(teacher, code="HW-3001", status=AssignmentStatus.DRAFT)
    await seed(postgres_session, teacher, assignment)
    statements: list[str] = []

    def record_sql(*args: object) -> None:
        statements.append(str(args[2]))

    engine = postgres_session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        summary = await build_assignment_summary(
            postgres_session, assignment.id, limit=50, offset=0
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert summary == AssignmentSummary(
        assignment_id=assignment.id,
        total_students=0,
        submitted_students=0,
        missing_students=0,
        latest_submission_at=None,
        latest_submission_versions={},
        pending_evaluation=0,
        queued=0,
        evaluating=0,
        pending_review=0,
        reviewed=0,
        failed=0,
        limit=50,
        offset=0,
        students=[],
    )
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(selects) == 1
    normalized = " ".join(selects).lower()
    assert "row_number() over" in normalized
    assert "partition by submissions.assignment_id, submissions.student_id" in normalized
    assert "submissions.version desc" in normalized


async def test_teacher_endpoint_has_exact_safe_response_and_allows_draft(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("teacher", Role.TEACHER)
    student = make_user("student", display_name="Student Name")
    assignment = make_assignment(teacher, code="HW-4001", status=AssignmentStatus.DRAFT)
    submitted_at = datetime.now(UTC)
    submission = make_submission(assignment, student, 1, submitted_at)
    await seed(postgres_session, teacher, student, assignment, submission)

    async with api_client(teacher, postgres_session) as client:
        response = await client.get(f"/api/v1/assignments/{assignment.id}/summary")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "assignment_id",
        "total_students",
        "submitted_students",
        "missing_students",
        "latest_submission_at",
        "latest_submission_versions",
        "pending_evaluation",
        "queued",
        "evaluating",
        "pending_review",
        "reviewed",
        "failed",
        "limit",
        "offset",
        "students",
    }
    assert set(body["students"][0]) == {
        "student_id",
        "username",
        "display_name",
        "latest_submission",
        "latest_version",
        "submitted_at",
        "evaluation_status",
        "report_status",
        "score",
        "grade",
        "evaluation_error",
        "evaluation_error_code",
        "latest_report_id",
    }
    assert body["latest_submission_versions"] == {str(student.id): 1}
    assert set(body["students"][0]["latest_submission"]) == {
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
    assert "secret_solution" not in response.text
    assert "not-exposed" not in response.text


async def test_teacher_summary_endpoint_uses_one_business_select(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("bounded-teacher", Role.TEACHER)
    student = make_user("bounded-student")
    assignment = make_assignment(teacher, code="HW-4002", status=AssignmentStatus.ARCHIVED)
    submission = make_submission(assignment, student, 1, datetime.now(UTC))
    await seed(postgres_session, teacher, student, assignment, submission)
    postgres_session.expunge_all()
    statements: list[str] = []

    def record_sql(*args: object) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    engine = postgres_session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        async with api_client(teacher, postgres_session) as client:
            response = await client.get(f"/api/v1/assignments/{assignment.id}/summary")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert response.status_code == 200
    assert len(statements) == 1, "\n\n".join(statements)


async def test_missing_summary_endpoint_uses_one_select_and_returns_404(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("missing-teacher", Role.TEACHER)
    await seed(postgres_session, teacher)
    statements: list[str] = []

    def record_sql(*args: object) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    engine = postgres_session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        async with api_client(teacher, postgres_session) as client:
            response = await client.get(f"/api/v1/assignments/{uuid.uuid4()}/summary")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert response.status_code == 404
    assert len(statements) == 1


class InterleavingSession:
    def __init__(
        self,
        delegate: AsyncSession,
        engine: AsyncEngine,
        newcomer: User,
    ) -> None:
        self.delegate = delegate
        self.engine = engine
        self.newcomer = newcomer
        self.execute_count = 0

    async def execute(self, statement: object) -> object:
        result = await self.delegate.execute(statement)
        self.execute_count += 1
        if self.execute_count == 1:
            factory = async_sessionmaker(self.engine, expire_on_commit=False)
            async with factory() as concurrent_session:
                concurrent_session.add(self.newcomer)
                await concurrent_session.commit()
        return result


async def test_summary_response_remains_internally_consistent_during_concurrent_insert(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("snapshot-teacher", Role.TEACHER)
    initial = make_user("snapshot-initial")
    newcomer = make_user("snapshot-newcomer")
    assignment = make_assignment(teacher, code="HW-4003", status=AssignmentStatus.PUBLISHED)
    submission = make_submission(assignment, initial, 1, datetime.now(UTC))
    await seed(postgres_session, teacher, initial, assignment, submission)
    await postgres_session.commit()
    engine = postgres_session.bind
    assert isinstance(engine, AsyncEngine)
    interleaving_session = InterleavingSession(postgres_session, engine, newcomer)

    async with api_client(teacher, interleaving_session) as client:
        response = await client.get(f"/api/v1/assignments/{assignment.id}/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["total_students"] == len(body["students"])
    assert body["submitted_students"] == sum(
        row["latest_submission"] is not None for row in body["students"]
    )
    assert interleaving_session.execute_count == 1


class RecordingSession:
    def __init__(self, delegate: AsyncSession) -> None:
        self.delegate = delegate
        self.statements: list[object] = []

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return await self.delegate.execute(statement)


def walk_plan(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(child for plan in node.get("Plans", []) for child in walk_plan(plan))]


async def test_summary_explain_keeps_wide_content_out_of_full_window(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("plan-teacher", Role.TEACHER)
    students = [make_user(f"plan-student-{number}") for number in range(3)]
    assignment = make_assignment(teacher, code="HW-4004", status=AssignmentStatus.PUBLISHED)
    now = datetime.now(UTC)
    submissions = [
        make_submission(assignment, student, version, now + timedelta(seconds=version))
        for student in students
        for version in range(1, 4)
    ]
    for submission in submissions:
        submission.content_text = "wide-content" * 1_000
        submission.content_json = {"wide": "json-content" * 1_000}
    await seed(postgres_session, teacher, *students, assignment, *submissions)
    recording_session = RecordingSession(postgres_session)

    await build_assignment_summary(recording_session, assignment.id, limit=2, offset=0)

    statement = recording_session.statements[-1]
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    plan_result = await postgres_session.execute(text(f"EXPLAIN (VERBOSE, FORMAT JSON) {compiled}"))
    plan = plan_result.scalar_one()[0]["Plan"]
    window_nodes = [node for node in walk_plan(plan) if node["Node Type"] == "WindowAgg"]

    assert window_nodes
    window_output = " ".join(output for node in window_nodes for output in node.get("Output", []))
    assert "content_text" not in window_output
    assert "content_json" not in window_output
    sql = str(compiled).lower()
    assert sql.index("row_number() over") < sql.index("content_text")
    assert sql.index("limit 2") < sql.index("submissions.content_text")


async def test_summary_has_submitted_latest_partial_index_and_roster_boundary(
    postgres_session: AsyncSession,
) -> None:
    index = next(
        index
        for index in Submission.__table__.indexes
        if index.name == "ix_submissions_assignment_student_submitted_latest"
    )
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect())).lower()
    installed_ddl = await postgres_session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'submissions' "
            "AND indexname = 'ix_submissions_assignment_student_submitted_latest'"
        )
    )

    assert "assignment_id, student_id, version desc, submitted_at desc, id desc" in ddl
    assert "where status = 'submitted'" in ddl
    assert installed_ddl is not None
    assert "WHERE (status = 'submitted'::submission_status)" in installed_ddl
    assert summary_module.SUMMARY_MAX_ACTIVE_STUDENTS == 10_000


async def test_ten_thousand_student_result_carries_aggregate_map_once(
    postgres_session: AsyncSession,
) -> None:
    teacher = make_user("scale-teacher", Role.TEACHER)
    assignment = make_assignment(teacher, code="HW-4005", status=AssignmentStatus.PUBLISHED)
    await seed(postgres_session, teacher, assignment)
    student_ids = [uuid.uuid4() for _ in range(summary_module.SUMMARY_MAX_ACTIVE_STUDENTS)]
    await postgres_session.execute(
        insert(User),
        [
            {
                "id": student_id,
                "username": f"scale-{position:05d}",
                "display_name": f"Scale {position:05d}",
                "role": Role.STUDENT,
                "password_hash": "not-exposed",
                "is_active": True,
            }
            for position, student_id in enumerate(student_ids)
        ],
    )
    submitted_at = datetime.now(UTC)
    await postgres_session.execute(
        insert(Submission),
        [
            {
                "id": uuid.uuid4(),
                "assignment_id": assignment.id,
                "student_id": student_id,
                "version": 1,
                "content_type": SubmissionContentType.MARKDOWN,
                "content_text": "answer",
                "content_json": None,
                "status": SubmissionStatus.SUBMITTED,
                "submitted_at": submitted_at,
                "source": SubmissionSource.WEB,
            }
            for student_id in student_ids
        ],
    )
    statement = summary_module._summary_statement(assignment.id, limit=100, offset=0)

    result = await postgres_session.execute(statement)
    rows = result.mappings().all()
    encoded_maps = [
        json.dumps(row["latest_submission_versions"], separators=(",", ":")).encode()
        for row in rows
        if row["latest_submission_versions"] is not None
    ]

    assert len(encoded_maps) == 1
    assert len(rows) == 101
    assert 400_000 < len(encoded_maps[0]) < 600_000
    assert sum(map(len, encoded_maps)) == len(encoded_maps[0])

    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    plan_result = await postgres_session.execute(
        text(f"EXPLAIN (ANALYZE, VERBOSE, BUFFERS, FORMAT JSON) {compiled}")
    )
    plan = plan_result.scalar_one()[0]["Plan"]
    disk_sorts = [
        node
        for node in walk_plan(plan)
        if node["Node Type"] == "Sort" and node.get("Sort Space Type") == "Disk"
    ]
    assert disk_sorts == []


class RejectDataSession:
    def __init__(
        self, *, assignment: Assignment | None = None, error: Exception | None = None
    ) -> None:
        self.assignment = assignment
        self.error = error
        self.get_count = 0
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def get(self, model: type[Assignment], identity: uuid.UUID) -> Assignment | None:
        del model, identity
        self.get_count += 1
        return self.assignment

    async def execute(self, statement: object) -> object:
        del statement
        self.execute_count += 1
        if self.error is not None:
            raise self.error
        return EmptyMappingResult()


class EmptyMappingResult:
    def mappings(self) -> "EmptyMappingResult":
        return self

    def all(self) -> list[object]:
        return []


@pytest.mark.parametrize("role", [Role.STUDENT, Role.ADMIN])
async def test_summary_rejects_non_teachers_before_assignment_or_summary_queries(
    role: Role,
) -> None:
    session = RejectDataSession()
    async with api_client(make_user(role.value, role), session) as client:
        response = await client.get(f"/api/v1/assignments/{uuid.uuid4()}/summary")

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
    assert session.get_count == session.execute_count == 0


async def test_summary_returns_404_without_page_query_and_propagates_database_errors() -> None:
    missing_session = RejectDataSession()
    async with api_client(make_user("teacher", Role.TEACHER), missing_session) as client:
        missing = await client.get(f"/api/v1/assignments/{uuid.uuid4()}/summary")
    assert missing.status_code == 404
    assert missing_session.get_count == 0
    assert missing_session.execute_count == 1

    teacher = make_user("teacher2", Role.TEACHER)
    assignment = make_assignment(teacher, code="HW-5001", status=AssignmentStatus.DRAFT)
    database_error = RuntimeError("database unavailable")
    failing_session = RejectDataSession(assignment=assignment, error=database_error)
    with pytest.raises(RuntimeError, match="database unavailable"):
        async with api_client(teacher, failing_session) as client:
            await client.get(f"/api/v1/assignments/{assignment.id}/summary")
    assert failing_session.commit_count == failing_session.rollback_count == 0


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001"])
async def test_summary_validates_pagination_before_database_access(query: str) -> None:
    session = RejectDataSession()
    async with api_client(make_user("teacher", Role.TEACHER), session) as client:
        response = await client.get(f"/api/v1/assignments/{uuid.uuid4()}/summary?{query}")
    assert response.status_code == 422
    assert session.get_count == session.execute_count == 0


async def test_summary_auth_uuid_404_and_openapi_contract() -> None:
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        unauthenticated = await client.get(f"/api/v1/assignments/{uuid.uuid4()}/summary")
    assert unauthenticated.status_code == 401

    session = RejectDataSession()
    async with api_client(make_user("teacher", Role.TEACHER), session) as client:
        invalid_uuid = await client.get("/api/v1/assignments/not-a-uuid/summary")
    assert invalid_uuid.status_code == 422
    assert session.get_count == 0

    operation = application.openapi()["paths"]["/api/v1/assignments/{assignment_id}/summary"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AssignmentSummary"
    }
    schema = application.openapi()["components"]["schemas"]["AssignmentSummary"]
    assert set(schema["required"]) == set(schema["properties"])


def test_summary_route_is_explicitly_mounted_once() -> None:
    routes = [
        route
        for route in assignment_router.routes
        if route.path == "/assignments/{assignment_id}/summary"
    ]
    assert len(routes) == 1
    assert routes[0].methods == {"GET"}
    assert routes[0].response_model is AssignmentSummary
