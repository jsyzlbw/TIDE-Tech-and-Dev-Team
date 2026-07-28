import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.sql import Select

from app.assignments.model import Assignment
from app.assignments.router import router as assignment_router
from app.auth.dependencies import get_current_user
from app.db.session import get_session
from app.db.types import AssignmentStatus, Role
from app.main import create_app
from app.users.model import User


class MappingAssignments:
    def __init__(self, assignments: list[Assignment]) -> None:
        self._assignments = assignments

    def mappings(self) -> "MappingAssignments":
        return self

    def all(self) -> list[dict[str, object]]:
        return [
            {
                "id": assignment.id,
                "code": assignment.code,
                "title": assignment.title,
                "due_at": assignment.due_at,
                "status": assignment.status,
                "created_at": assignment.created_at,
                "published_at": assignment.published_at,
            }
            for assignment in self._assignments
        ]


class AssignmentApiSession:
    def __init__(self, assignments: list[Assignment] | None = None) -> None:
        self.assignments = assignments or []
        self.sequence_value = 1
        self.commit_count = 0
        self.rollback_count = 0
        self.add_count = 0
        self.list_statements: list[Select[Any]] = []

    @property
    def no_autoflush(self):
        return nullcontext()

    async def scalar(self, statement: Select[Any]) -> int | Assignment | None:
        if statement.column_descriptions[0].get("entity") is Assignment:
            assignment_id = next(
                value
                for value in statement.compile().params.values()
                if isinstance(value, uuid.UUID)
            )
            found = next(
                (item for item in self.assignments if item.id == assignment_id),
                None,
            )
            if found is not None and statement._for_update_arg is None:
                visible_statuses = next(
                    value
                    for value in statement.compile().params.values()
                    if isinstance(value, list)
                )
                if found.status not in visible_statuses:
                    return None
            return found
        assert "assignment_code_seq" in str(statement)
        value = self.sequence_value
        self.sequence_value += 1
        return value

    def add(self, assignment: Assignment) -> None:
        self.add_count += 1
        self.assignments.append(assignment)

    async def flush(self, objects: list[object] | None = None) -> None:
        del objects

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def refresh(self, assignment: Assignment) -> None:
        assignment.id = assignment.id or uuid.uuid4()
        assignment.created_at = assignment.created_at or datetime.now(UTC)

    async def get(
        self,
        model: type[Assignment],
        identity: uuid.UUID,
    ) -> Assignment | None:
        assert model is Assignment
        return next((item for item in self.assignments if item.id == identity), None)

    async def execute(
        self,
        statement: Select[Any] | object,
        parameters: object = None,
    ) -> MappingAssignments | None:
        del parameters
        if "pg_advisory_xact_lock" in str(statement):
            return None
        assert isinstance(statement, Select)
        assert statement.column_descriptions[0]["entity"] is Assignment
        self.list_statements.append(statement)
        if statement.whereclause is None:
            visible = list(self.assignments)
        else:
            values = next(
                value for value in statement.compile().params.values() if isinstance(value, list)
            )
            assert set(values) == {AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED}
            visible = [item for item in self.assignments if item.status in values]
        visible.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        offset = statement._offset_clause.value if statement._offset_clause is not None else 0
        limit = statement._limit_clause.value if statement._limit_clause is not None else None
        visible = visible[offset : offset + limit if limit is not None else None]
        return MappingAssignments(visible)


def user(role: Role) -> User:
    return User(
        id=uuid.uuid4(),
        username=f"{role.value}-{uuid.uuid4().hex[:6]}",
        display_name=role.value.title(),
        role=role,
        password_hash="unused",
        is_active=True,
    )


def assignment(
    status: AssignmentStatus,
    *,
    due_at: datetime | None = None,
) -> Assignment:
    published_at = (
        datetime.now(UTC) - timedelta(hours=1)
        if status in {AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED}
        else None
    )
    return Assignment(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        code=f"HW-{uuid.uuid4().int % 10000:04d}",
        title=f"{status.value} assignment",
        question="Question with a large student-facing explanation" * 100,
        notes="Student-visible guidance",
        rubric={"correctness": 100, "secret_solution": "internal answer"},
        due_at=due_at or datetime.now(UTC) + timedelta(days=1),
        status=status,
        mattermost_channel_id="internal-channel-id",
        created_by=uuid.uuid4(),
        published_at=published_at,
    )


@asynccontextmanager
async def api_client(
    current_user: User,
    session: AssignmentApiSession,
) -> AsyncIterator[AsyncClient]:
    async def override_current_user() -> User:
        return current_user

    async def override_session() -> AsyncIterator[AssignmentApiSession]:
        yield session

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        yield client


def create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "Compiler construction",
        "question": "Explain SSA form.",
        "notes": "Use diagrams where helpful.",
        "rubric": {"correctness": 70, "clarity": 30},
        "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    payload.update(overrides)
    return payload


async def test_teacher_can_create_draft_assignment() -> None:
    teacher = user(Role.TEACHER)
    session = AssignmentApiSession()

    async with api_client(teacher, session) as client:
        response = await client.post("/api/v1/assignments", json=create_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["code"] == "HW-0001"
    assert body["status"] == "draft"
    assert body["created_by"] == str(teacher.id)
    assert body["created_at"] is not None
    assert body["published_at"] is None
    assert body["mattermost_channel_id"] is None
    assert session.add_count == 1
    assert session.commit_count == 1


async def test_assignment_create_body_limit_ignores_content_length_and_chunks() -> None:
    teacher = user(Role.TEACHER)
    session = AssignmentApiSession()
    raw_body = (
        '{"title":"Safe title","question":"'
        + ("SENSITIVE-ASSIGNMENT" * 20_000)
        + '","rubric":{},"due_at":"2027-01-01T00:00:00Z"}'
    ).encode()

    async def chunks() -> AsyncIterator[bytes]:
        midpoint = len(raw_body) // 2
        yield raw_body[:midpoint]
        yield raw_body[midpoint:]

    async with api_client(teacher, session) as client:
        response = await client.post(
            "/api/v1/assignments",
            content=chunks(),
            headers={"content-type": "application/json", "content-length": "1"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body is too large"}
    assert "SENSITIVE-ASSIGNMENT" not in response.text
    assert session.add_count == 0


async def test_student_cannot_create_assignment() -> None:
    session = AssignmentApiSession()

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.post("/api/v1/assignments", json=create_payload())

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
    assert session.add_count == 0
    assert session.commit_count == 0


@pytest.mark.parametrize(
    ("path", "starting_status", "expected_status"),
    [
        ("publish", AssignmentStatus.DRAFT, AssignmentStatus.PUBLISHED),
        ("close", AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED),
        ("reopen", AssignmentStatus.CLOSED, AssignmentStatus.PUBLISHED),
    ],
)
async def test_teacher_can_use_each_transition_endpoint(
    path: str,
    starting_status: AssignmentStatus,
    expected_status: AssignmentStatus,
) -> None:
    existing = assignment(starting_status)
    original_published_at = existing.published_at
    session = AssignmentApiSession([existing])

    async with api_client(user(Role.TEACHER), session) as client:
        response = await client.post(f"/api/v1/assignments/{existing.id}/{path}")

    assert response.status_code == 200
    assert response.json()["status"] == expected_status.value
    assert session.commit_count == 1
    if path == "publish":
        assert response.json()["published_at"] is not None
    if path == "reopen":
        assert existing.published_at is original_published_at


@pytest.mark.parametrize(
    ("path", "starting_status", "expired"),
    [
        ("publish", AssignmentStatus.PUBLISHED, False),
        ("publish", AssignmentStatus.ARCHIVED, False),
        ("publish", AssignmentStatus.DRAFT, True),
        ("close", AssignmentStatus.DRAFT, False),
        ("close", AssignmentStatus.ARCHIVED, False),
        ("reopen", AssignmentStatus.PUBLISHED, False),
        ("reopen", AssignmentStatus.ARCHIVED, False),
        ("reopen", AssignmentStatus.CLOSED, True),
    ],
)
async def test_invalid_transition_returns_sanitized_conflict_without_commit(
    path: str,
    starting_status: AssignmentStatus,
    expired: bool,
) -> None:
    due_at = datetime.now(UTC) - timedelta(minutes=1) if expired else None
    existing = assignment(starting_status, due_at=due_at)
    session = AssignmentApiSession([existing])

    async with api_client(user(Role.TEACHER), session) as client:
        response = await client.post(f"/api/v1/assignments/{existing.id}/{path}")

    assert response.status_code == 409
    assert response.json() == {"detail": "invalid assignment transition"}
    assert existing.status is starting_status
    assert session.commit_count == 0
    assert session.rollback_count == 1


@pytest.mark.parametrize("suffix", ["", "/publish", "/close", "/reopen"])
async def test_missing_assignment_returns_not_found(suffix: str) -> None:
    missing_id = uuid.uuid4()

    async with api_client(user(Role.TEACHER), AssignmentApiSession()) as client:
        method = client.get if suffix == "" else client.post
        response = await method(f"/api/v1/assignments/{missing_id}{suffix}")

    assert response.status_code == 404
    assert response.json() == {"detail": "assignment not found"}


async def test_teacher_list_and_get_include_every_assignment_status() -> None:
    assignments = [assignment(status) for status in AssignmentStatus]
    session = AssignmentApiSession(assignments)

    async with api_client(user(Role.TEACHER), session) as client:
        listed = await client.get("/api/v1/assignments")
        fetched = [
            await client.get(f"/api/v1/assignments/{existing.id}") for existing in assignments
        ]

    assert listed.status_code == 200
    assert {item["status"] for item in listed.json()} == {
        status.value for status in AssignmentStatus
    }
    assert all(response.status_code == 200 for response in fetched)
    assert all(
        set(item)
        == {
            "id",
            "code",
            "title",
            "due_at",
            "status",
            "created_at",
            "published_at",
        }
        for item in listed.json()
    )
    assert all("secret_solution" not in str(item) for item in listed.json())
    assert all("Question with a large" not in str(item) for item in listed.json())
    assert fetched[0].json()["rubric"]["secret_solution"] == "internal answer"
    assert fetched[0].json()["mattermost_channel_id"] == "internal-channel-id"


async def test_student_list_and_get_only_expose_published_and_closed() -> None:
    assignments = {status: assignment(status) for status in AssignmentStatus}
    session = AssignmentApiSession(list(assignments.values()))

    async with api_client(user(Role.STUDENT), session) as client:
        listed = await client.get("/api/v1/assignments")
        fetched = {
            status: await client.get(f"/api/v1/assignments/{existing.id}")
            for status, existing in assignments.items()
        }
        missing = await client.get(f"/api/v1/assignments/{uuid.uuid4()}")

    assert listed.status_code == 200
    assert {item["status"] for item in listed.json()} == {"published", "closed"}
    assert fetched[AssignmentStatus.PUBLISHED].status_code == 200
    assert fetched[AssignmentStatus.CLOSED].status_code == 200
    assert fetched[AssignmentStatus.DRAFT].status_code == 404
    assert fetched[AssignmentStatus.ARCHIVED].status_code == 404
    assert (
        fetched[AssignmentStatus.DRAFT].json()
        == missing.json()
        == {"detail": "assignment not found"}
    )
    list_items = listed.json()
    assert all(
        set(item)
        == {
            "id",
            "code",
            "title",
            "due_at",
            "status",
            "created_at",
            "published_at",
        }
        for item in list_items
    )
    for item in list_items:
        assert "rubric" not in item
        assert "secret_solution" not in str(item)
        assert "mattermost_channel_id" not in item
        assert "internal-channel-id" not in str(item)
        assert "created_by" not in item
        assert "question" not in item
        assert "notes" not in item

    for status in (AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED):
        body = fetched[status].json()
        assert set(body) == {
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
        assert "rubric" not in body
        assert "secret_solution" not in str(body)
        assert "mattermost_channel_id" not in body
        assert "internal-channel-id" not in str(body)
        assert "created_by" not in body


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/assignments",
        pytest.param(
            "/api/v1/assignments/{assignment_id}",
            id="assignment-detail",
        ),
        pytest.param(
            "/api/v1/assignments/{assignment_id}/summary",
            id="assignment-summary",
        ),
    ],
)
async def test_admin_cannot_read_course_data(path: str) -> None:
    existing = assignment(AssignmentStatus.PUBLISHED)
    session = AssignmentApiSession([existing])

    async with api_client(user(Role.ADMIN), session) as client:
        response = await client.get(path.format(assignment_id=existing.id))

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
    assert session.list_statements == []


async def test_assignment_list_defaults_to_fifty_compact_items() -> None:
    created_at = datetime.now(UTC)
    assignments = [assignment(AssignmentStatus.PUBLISHED) for _ in range(60)]
    for number, existing in enumerate(assignments, start=1):
        existing.id = uuid.UUID(int=number)
        existing.created_at = created_at
    session = AssignmentApiSession(assignments)

    async with api_client(user(Role.TEACHER), session) as client:
        response = await client.get("/api/v1/assignments")

    assert response.status_code == 200
    assert len(response.json()) == 50
    assert [item["id"] for item in response.json()[:3]] == [
        str(uuid.UUID(int=60)),
        str(uuid.UUID(int=59)),
        str(uuid.UUID(int=58)),
    ]


async def test_assignment_list_honors_limit_offset_and_stable_tie_breaking() -> None:
    created_at = datetime.now(UTC)
    assignments = [assignment(AssignmentStatus.PUBLISHED) for _ in range(5)]
    for number, existing in enumerate(assignments, start=1):
        existing.id = uuid.UUID(int=number)
        existing.created_at = created_at
    session = AssignmentApiSession(assignments)

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.get("/api/v1/assignments?limit=2&offset=1")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [
        str(uuid.UUID(int=4)),
        str(uuid.UUID(int=3)),
    ]
    statement = session.list_statements[0]
    assert [column.name for column in statement.selected_columns] == [
        "id",
        "code",
        "title",
        "due_at",
        "status",
        "created_at",
        "published_at",
    ]
    assert [str(order) for order in statement._order_by_clauses] == [
        "assignments.created_at DESC",
        "assignments.id DESC",
    ]
    assert statement._limit_clause.value == 2
    assert statement._offset_clause.value == 1


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "offset=-1",
        "offset=10001",
    ],
)
async def test_assignment_list_validates_pagination_bounds(query: str) -> None:
    session = AssignmentApiSession()

    async with api_client(user(Role.TEACHER), session) as client:
        response = await client.get(f"/api/v1/assignments?{query}")

    assert response.status_code == 422
    assert session.list_statements == []


def test_assignment_detail_openapi_declares_role_specific_response_union() -> None:
    schema = create_app().openapi()["paths"]["/api/v1/assignments/{assignment_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]

    assert schema
    assert {item["$ref"] for item in schema["anyOf"]} == {
        "#/components/schemas/AssignmentRead",
        "#/components/schemas/AssignmentStudentRead",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"question": "   "},
        {"question": "x" * 50_001},
        {"notes": "x" * 10_001},
        {"due_at": "2026-01-01T12:00:00"},
    ],
)
async def test_create_validates_text_bounds_and_timezone_aware_deadline(
    overrides: dict[str, object],
) -> None:
    session = AssignmentApiSession()

    async with api_client(user(Role.TEACHER), session) as client:
        response = await client.post(
            "/api/v1/assignments",
            json=create_payload(**overrides),
        )

    assert response.status_code == 422
    assert session.add_count == 0
    assert session.commit_count == 0


@pytest.mark.parametrize("suffix", ["publish", "close", "reopen"])
async def test_student_cannot_use_transition_endpoints(suffix: str) -> None:
    existing = assignment(AssignmentStatus.DRAFT)
    session = AssignmentApiSession([existing])

    async with api_client(user(Role.STUDENT), session) as client:
        response = await client.post(f"/api/v1/assignments/{existing.id}/{suffix}")

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
    assert session.commit_count == 0


def test_assignment_routes_are_registered_exactly_once() -> None:
    application = create_app()
    expected = {
        ("/api/v1/assignments", "POST"),
        ("/api/v1/assignments", "GET"),
        ("/api/v1/assignments/{assignment_id}", "GET"),
        ("/api/v1/assignments/{assignment_id}/publish", "POST"),
        ("/api/v1/assignments/{assignment_id}/close", "POST"),
        ("/api/v1/assignments/{assignment_id}/reopen", "POST"),
    }

    included_assignment_routers = [
        route
        for route in application.routes
        if getattr(route, "original_router", None) is assignment_router
    ]
    assert len(included_assignment_routers) == 1

    for path, method in expected:
        local_path = path.removeprefix("/api/v1")
        matches = [
            route
            for route in assignment_router.routes
            if getattr(route, "path", None) == local_path
            and method in getattr(route, "methods", set())
        ]
        assert len(matches) == 1
