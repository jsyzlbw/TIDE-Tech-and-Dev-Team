import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from app.assignments.model import Assignment
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
from app.submissions.router import router as submission_router
from app.users.model import User

MAX_SUBMISSION_REQUEST_BYTES = 384 * 1024


def user(role: Role, *, user_id: uuid.UUID | None = None) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        username=f"{role.value}-{uuid.uuid4().hex[:6]}",
        display_name=role.value.title(),
        role=role,
        password_hash="unused",
        is_active=True,
    )


def assignment(
    *,
    assignment_id: uuid.UUID | None = None,
    status: AssignmentStatus = AssignmentStatus.PUBLISHED,
) -> Assignment:
    return Assignment(
        id=assignment_id or uuid.uuid4(),
        code=f"HW-{uuid.uuid4().int % 10000:04d}",
        title="Compiler construction",
        question="Explain SSA form.",
        notes="",
        rubric={"secret": "teacher only"},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=status,
        mattermost_channel_id=None,
        created_by=uuid.uuid4(),
        created_at=datetime.now(UTC),
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )


def submission(
    existing_assignment: Assignment,
    student: User,
    version: int,
    *,
    submitted_at: datetime | None = None,
) -> Submission:
    return Submission(
        id=uuid.uuid4(),
        assignment_id=existing_assignment.id,
        student_id=student.id,
        version=version,
        content_type=SubmissionContentType.MARKDOWN,
        content_text=f"answer v{version}",
        content_json={"version": version},
        status=SubmissionStatus.SUBMITTED,
        submitted_at=submitted_at or datetime.now(UTC),
        source=SubmissionSource.WEB,
    )


class ScalarSubmissions:
    def __init__(self, values: list[Submission]) -> None:
        self.values = values

    def scalars(self) -> "ScalarSubmissions":
        return self

    def all(self) -> list[Submission]:
        return self.values


class SubmissionApiSession:
    def __init__(
        self,
        assignments: list[Assignment] | None = None,
        submissions: list[Submission] | None = None,
    ) -> None:
        self.assignments = {item.id: item for item in assignments or []}
        self.submissions = list(submissions or [])
        self.scalar_statements: list[Select[Any]] = []
        self.execute_statements: list[Select[Any]] = []
        self.get_calls: list[tuple[type[Any], uuid.UUID]] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.flush_error: Exception | None = None
        self.commit_error: Exception | None = None
        self.transaction_failed = False
        self.no_autoflush = nullcontext()
        self.initial_submission_count = len(self.submissions)

    async def get(self, model: type[Any], identity: uuid.UUID) -> Any | None:
        self.get_calls.append((model, identity))
        if model is Assignment:
            return self.assignments.get(identity)
        assert model is Submission
        return next((item for item in self.submissions if item.id == identity), None)

    async def scalar(self, statement: Select[Any]) -> Assignment | int | None:
        self.scalar_statements.append(statement)
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        uuid_values = [value for value in compiled.params.values() if isinstance(value, uuid.UUID)]
        if "FROM assignments" in sql:
            found = self.assignments.get(uuid_values[0])
            if found is not None and "assignments.status IN" in sql:
                visible_statuses = next(
                    value for value in compiled.params.values() if isinstance(value, list)
                )
                if found.status not in visible_statuses:
                    return None
            return found
        assignment_id, student_id = uuid_values
        versions = [
            item.version
            for item in self.submissions
            if item.assignment_id == assignment_id and item.student_id == student_id
        ]
        return max(versions, default=None)

    def add(self, item: Submission) -> None:
        self.submissions.append(item)

    async def flush(self, objects: list[object] | None = None) -> None:
        if self.flush_error is not None:
            self.transaction_failed = True
            raise self.flush_error
        for item in self.submissions:
            item.id = item.id or uuid.uuid4()

    async def commit(self) -> None:
        self.commit_count += 1
        if self.commit_error is not None:
            self.transaction_failed = True
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollback_count += 1
        self.transaction_failed = False
        del self.submissions[self.initial_submission_count :]

    async def refresh(self, item: Submission) -> None:
        item.id = item.id or uuid.uuid4()

    async def execute(self, statement: Select[Any]) -> ScalarSubmissions:
        self.execute_statements.append(statement)
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        parameters = compiled.params
        assignment_id = next(value for value in parameters.values() if isinstance(value, uuid.UUID))
        values = [item for item in self.submissions if item.assignment_id == assignment_id]
        if "submissions.student_id =" in sql and "JOIN" not in sql:
            student_ids = [
                value
                for value in parameters.values()
                if isinstance(value, uuid.UUID) and value != assignment_id
            ]
            values = [item for item in values if item.student_id == student_ids[0]]
            values.sort(key=lambda item: (item.version, item.id), reverse=True)
        else:
            latest: dict[uuid.UUID, Submission] = {}
            for item in values:
                if item.student_id not in latest or item.version > latest[item.student_id].version:
                    latest[item.student_id] = item
            values = sorted(
                latest.values(),
                key=lambda item: (item.submitted_at, item.id),
                reverse=True,
            )
        offset = statement._offset_clause.value if statement._offset_clause is not None else 0
        limit = statement._limit_clause.value if statement._limit_clause is not None else None
        return ScalarSubmissions(values[offset : offset + limit if limit is not None else None])


@asynccontextmanager
async def api_client(
    current_user: User,
    session: SubmissionApiSession,
    *,
    raise_app_exceptions: bool = True,
) -> AsyncIterator[AsyncClient]:
    async def override_current_user() -> User:
        return current_user

    async def override_session() -> AsyncIterator[SubmissionApiSession]:
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


async def test_student_post_uses_database_identity_and_server_owned_fields() -> None:
    student = user(Role.STUDENT)
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])

    async with api_client(student, session) as client:
        response = await client.post(
            f"/api/v1/assignments/{existing_assignment.id}/submissions",
            json={
                "content_type": "code",
                "content_text": "print('hello')",
            },
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": str(session.submissions[0].id),
        "assignment_id": str(existing_assignment.id),
        "student_id": str(student.id),
        "version": 1,
        "content_type": "code",
        "content_text": "print('hello')",
        "content_json": None,
        "status": "submitted",
        "submitted_at": session.submissions[0].submitted_at.isoformat().replace("+00:00", "Z"),
        "source": "web",
    }
    assert session.commit_count == 1
    assert session.rollback_count == 0


@pytest.mark.parametrize(
    "forged",
    [
        {"student_id": str(uuid.uuid4())},
        {"source": "mattermost"},
        {"version": 99},
        {"status": "withdrawn"},
        {"submitted_at": datetime.now(UTC).isoformat()},
    ],
)
async def test_student_cannot_forge_server_owned_fields(forged: dict[str, object]) -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])
    body: dict[str, object] = {"content_type": "text", "content_text": "answer"}
    body.update(forged)

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.post(
            f"/api/v1/assignments/{existing_assignment.id}/submissions",
            json=body,
        )

    assert response.status_code == 422
    assert session.submissions == []


async def test_student_history_is_own_versions_in_stable_descending_order() -> None:
    existing_assignment = assignment()
    student = user(Role.STUDENT)
    another = user(Role.STUDENT)
    records = [
        submission(existing_assignment, student, 1),
        submission(existing_assignment, another, 1),
        submission(existing_assignment, student, 2),
        submission(existing_assignment, student, 3),
    ]
    session = SubmissionApiSession([existing_assignment], records)

    async with api_client(student, session) as client:
        response = await client.get(
            f"/api/v1/assignments/{existing_assignment.id}/submissions/me?limit=2&offset=1"
        )

    assert response.status_code == 200
    assert [item["version"] for item in response.json()] == [2, 1]
    statement = session.execute_statements[0]
    assert [str(item) for item in statement._order_by_clauses] == [
        "submissions.version DESC",
        "submissions.id DESC",
    ]


async def test_teacher_lists_only_each_students_latest_version() -> None:
    existing_assignment = assignment()
    first_student = user(Role.STUDENT)
    second_student = user(Role.STUDENT)
    now = datetime.now(UTC)
    records = [
        submission(existing_assignment, first_student, 1, submitted_at=now - timedelta(hours=2)),
        submission(existing_assignment, first_student, 2, submitted_at=now),
        submission(existing_assignment, second_student, 1, submitted_at=now - timedelta(hours=1)),
    ]

    session = SubmissionApiSession([existing_assignment], records)
    async with api_client(user(Role.TEACHER), session) as client:
        response = await client.get(f"/api/v1/assignments/{existing_assignment.id}/submissions")
    assert response.status_code == 200
    assert [(item["student_id"], item["version"]) for item in response.json()] == [
        (str(first_student.id), 2),
        (str(second_student.id), 1),
    ]
    statement = session.execute_statements[0]
    assert [str(item) for item in statement._order_by_clauses] == [
        "submissions.submitted_at DESC",
        "submissions.id DESC",
    ]


async def test_admin_cannot_list_or_read_class_submissions() -> None:
    existing_assignment = assignment()
    student = user(Role.STUDENT)
    record = submission(existing_assignment, student, 1)
    session = SubmissionApiSession([existing_assignment], [record])

    async with api_client(user(Role.ADMIN), session) as client:
        response = await client.get(f"/api/v1/assignments/{existing_assignment.id}/submissions")

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
    assert record.content_text not in response.text
    assert session.get_calls == []
    assert session.execute_statements == []


async def test_student_cannot_list_class_submissions() -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.get(f"/api/v1/assignments/{existing_assignment.id}/submissions")

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}


async def test_submission_detail_enforces_ownership_and_teacher_access() -> None:
    existing_assignment = assignment()
    owner = user(Role.STUDENT)
    record = submission(existing_assignment, owner, 2)

    owner_session = SubmissionApiSession([existing_assignment], [record])
    async with api_client(owner, owner_session) as client:
        owner_response = await client.get(f"/api/v1/submissions/{record.id}")
    assert owner_response.status_code == 200
    assert owner_response.json()["content_text"] == "answer v2"
    assert owner_response.json()["student_id"] == str(owner.id)

    stranger_session = SubmissionApiSession([existing_assignment], [record])
    async with api_client(user(Role.STUDENT), stranger_session) as client:
        hidden = await client.get(f"/api/v1/submissions/{record.id}")
        missing = await client.get(f"/api/v1/submissions/{uuid.uuid4()}")
    assert hidden.status_code == 404
    assert hidden.json() == missing.json() == {"detail": "submission not found"}

    teacher_session = SubmissionApiSession([existing_assignment], [record])
    async with api_client(user(Role.TEACHER), teacher_session) as client:
        response = await client.get(f"/api/v1/submissions/{record.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(record.id)


async def test_admin_cannot_read_submission_detail() -> None:
    existing_assignment = assignment()
    owner = user(Role.STUDENT)
    record = submission(existing_assignment, owner, 2)
    session = SubmissionApiSession([existing_assignment], [record])

    async with api_client(user(Role.ADMIN), session) as client:
        response = await client.get(f"/api/v1/submissions/{record.id}")

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
    assert record.content_text not in response.text
    assert session.get_calls == []


@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN])
async def test_only_students_can_create_or_read_me(role: Role) -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])

    async with api_client(user(role), session) as client:
        created = await client.post(
            f"/api/v1/assignments/{existing_assignment.id}/submissions",
            json={"content_type": "text", "content_text": "answer"},
        )
        mine = await client.get(f"/api/v1/assignments/{existing_assignment.id}/submissions/me")

    assert created.status_code == mine.status_code == 403


async def test_student_assignment_visibility_is_indistinguishable_across_paths() -> None:
    student = user(Role.STUDENT)
    missing_id = uuid.uuid4()
    hidden = [
        assignment(status=AssignmentStatus.DRAFT),
        assignment(status=AssignmentStatus.ARCHIVED),
    ]
    session = SubmissionApiSession(hidden)

    async with api_client(student, session) as client:
        for assignment_id in [missing_id, *(item.id for item in hidden)]:
            detail = await client.get(f"/api/v1/assignments/{assignment_id}")
            submitted = await client.post(
                f"/api/v1/assignments/{assignment_id}/submissions",
                json={"content_type": "text", "content_text": "answer"},
            )
            mine = await client.get(f"/api/v1/assignments/{assignment_id}/submissions/me")

            assert detail.status_code == submitted.status_code == mine.status_code == 404
            assert (
                detail.json()
                == submitted.json()
                == mine.json()
                == {"detail": "assignment not found"}
            )


async def test_closed_assignment_history_remains_visible_but_rejects_new_submission() -> None:
    student = user(Role.STUDENT)
    closed = assignment(status=AssignmentStatus.CLOSED)
    existing = submission(closed, student, 1)
    session = SubmissionApiSession([closed], [existing])

    async with api_client(student, session) as client:
        detail = await client.get(f"/api/v1/assignments/{closed.id}")
        mine = await client.get(f"/api/v1/assignments/{closed.id}/submissions/me")
        submitted = await client.post(
            f"/api/v1/assignments/{closed.id}/submissions",
            json={"content_type": "text", "content_text": "answer"},
        )

    assert detail.status_code == mine.status_code == 200
    assert [item["id"] for item in mine.json()] == [str(existing.id)]
    assert submitted.status_code == 409
    assert submitted.json() == {"detail": "assignment is not accepting submissions"}
    assert session.rollback_count == 1


@pytest.mark.parametrize("failure_stage", ["flush", "commit"])
async def test_submission_route_rolls_back_and_cleans_failed_unit_of_work(
    failure_stage: str,
) -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])
    failure = RuntimeError(f"{failure_stage} failed")
    setattr(session, f"{failure_stage}_error", failure)

    async with api_client(
        user(Role.STUDENT),
        session,
        raise_app_exceptions=False,
    ) as client:
        response = await client.post(
            f"/api/v1/assignments/{existing_assignment.id}/submissions",
            json={"content_type": "text", "content_text": "answer"},
        )

    assert response.status_code == 500
    assert session.rollback_count == 1
    assert session.transaction_failed is False
    assert session.submissions == []


@pytest.mark.parametrize(
    "body",
    [
        {"content_type": "text", "content_text": "SENSITIVE-ANSWER", "content_json": {}},
        {"content_type": "markdown", "content_text": "SENSITIVE-ANSWER" * 5_000},
    ],
)
async def test_invalid_submission_payload_is_sanitized_and_never_touches_database(
    body: dict[str, object],
) -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.post(
            f"/api/v1/assignments/{existing_assignment.id}/submissions",
            json=body,
        )

    assert response.status_code == 422
    assert "SENSITIVE-ANSWER" not in response.text
    assert session.scalar_statements == []
    assert session.submissions == []


async def test_non_finite_json_error_is_sanitized_and_never_touches_database() -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])
    raw = b'{"content_type":"structured","content_text":"","content_json":{"n":NaN}}'

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.post(
            f"/api/v1/assignments/{existing_assignment.id}/submissions",
            content=raw,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert "NaN" not in response.text
    assert session.scalar_statements == []
    assert session.submissions == []


async def test_submission_request_body_limit_ignores_forged_content_length_and_chunks() -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])
    answer = "SENSITIVE-CHUNK" * (MAX_SUBMISSION_REQUEST_BYTES // 8)
    raw_body = ('{"content_type":"text","content_text":"' + answer + '"}').encode()

    async def chunks() -> AsyncIterator[bytes]:
        midpoint = len(raw_body) // 2
        yield raw_body[:midpoint]
        yield raw_body[midpoint:]

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.post(
            f"/api/v1/assignments/{existing_assignment.id}/submissions",
            content=chunks(),
            headers={"content-type": "application/json", "content-length": "1"},
        )

    assert len(raw_body) > MAX_SUBMISSION_REQUEST_BYTES
    assert response.status_code == 413
    assert "SENSITIVE-CHUNK" not in response.text
    assert session.scalar_statements == []
    assert session.submissions == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/assignments/not-a-uuid/submissions",
        "/api/v1/assignments/not-a-uuid/submissions/me",
        "/api/v1/submissions/not-a-uuid",
    ],
)
async def test_invalid_uuid_returns_validation_error(path: str) -> None:
    session = SubmissionApiSession()
    async with api_client(user(Role.STUDENT), session) as client:
        if path.endswith("/submissions"):
            response = await client.post(
                path,
                json={"content_type": "text", "content_text": "answer"},
            )
        else:
            response = await client.get(path)
    assert response.status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001"])
async def test_pagination_has_safe_bounds(query: str) -> None:
    existing_assignment = assignment()
    session = SubmissionApiSession([existing_assignment])
    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.get(
            f"/api/v1/assignments/{existing_assignment.id}/submissions/me?{query}"
        )
    assert response.status_code == 422
    assert session.execute_statements == []


async def test_submission_endpoint_requires_authentication() -> None:
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/v1/submissions/{uuid.uuid4()}")
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authentication"}


def test_openapi_uses_explicit_submission_response_schemas() -> None:
    paths = create_app().openapi()["paths"]
    assert paths["/api/v1/assignments/{assignment_id}/submissions"]["post"]["responses"]["201"][
        "content"
    ]["application/json"]["schema"] == {"$ref": "#/components/schemas/SubmissionRead"}
    for path in (
        "/api/v1/assignments/{assignment_id}/submissions/me",
        "/api/v1/assignments/{assignment_id}/submissions",
    ):
        schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["items"] == {"$ref": "#/components/schemas/SubmissionRead"}
    assert paths["/api/v1/submissions/{submission_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SubmissionRead"}


def test_submission_routes_are_registered_exactly_once() -> None:
    application = create_app()
    included = [
        route
        for route in application.routes
        if getattr(route, "original_router", None) is submission_router
    ]
    assert len(included) == 1
    expected = {
        ("/assignments/{assignment_id}/submissions", "POST"),
        ("/assignments/{assignment_id}/submissions/me", "GET"),
        ("/assignments/{assignment_id}/submissions", "GET"),
        ("/submissions/{submission_id}", "GET"),
    }
    for path, method in expected:
        assert (
            sum(
                getattr(route, "path", None) == path and method in getattr(route, "methods", set())
                for route in submission_router.routes
            )
            == 1
        )
