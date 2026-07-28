# Foundation and Domain API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the runnable FastAPI foundation, PostgreSQL schema, JWT roles, assignment lifecycle, versioned submissions, summaries, migrations, and deterministic demo seed.

**Architecture:** A feature-oriented FastAPI service owns all business rules. SQLAlchemy models stay inside their feature packages, services receive an `AsyncSession`, and routers contain transport-only code. PostgreSQL is the production database; tests run against the Compose test database.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLAlchemy 2 async, Alembic, PostgreSQL 16, PyJWT, bcrypt, pytest, httpx

---

## Frozen interfaces

This plan establishes names consumed by later plans:

- API prefix: `/api/v1`
- Roles: `teacher`, `student`, `admin`
- Assignment statuses: `draft`, `published`, `closed`, `archived`
- Submission content types: `text`, `markdown`, `code`, `structured`
- Submission sources: `web`, `mattermost`
- Submission statuses: `submitted`, `withdrawn`
- Assignment code format: `HW-0001`
- Application factory: `app.main:create_app`
- Async session dependency: `app.db.session:get_session`
- Current-user dependency: `app.auth.dependencies:get_current_user`

## Task 1: Scaffold the backend and health endpoints

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/core/config.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`

- [ ] **Step 1: Create the Python project manifest**

Create `backend/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "ai-grading-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "alembic>=1.13",
  "asyncpg>=0.29",
  "bcrypt>=4.2",
  "celery[redis]>=5.4",
  "fastapi>=0.115",
  "httpx>=0.27",
  "pyjwt>=2.9",
  "pydantic-settings>=2.6",
  "sqlalchemy[asyncio]>=2.0",
  "uvicorn[standard]>=0.32"
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "pytest-cov>=6.0",
  "pytest-httpx>=0.35",
  "ruff>=0.8"
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

Run:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e 'backend[dev]'
```

Expected: installation exits `0` and imports `fastapi`, `sqlalchemy`, and `pytest`.

- [ ] **Step 2: Write the health endpoint tests**

Create `backend/tests/conftest.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client
```

Create `backend/tests/test_health.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_live_health(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_api_has_versioned_prefix(client):
    response = await client.get("/api/v1")
    assert response.status_code == 200
    assert response.json() == {"name": "AI Grading API", "version": "v1"}
```

- [ ] **Step 3: Run the tests and verify the import fails**

Run:

```bash
.venv/bin/pytest backend/tests/test_health.py -q
```

Expected: collection fails because `app.main` does not exist.

- [ ] **Step 4: Implement configuration and the app factory**

Create `backend/app/__init__.py` as an empty file.

Create `backend/app/core/config.py`:

```python
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://grader:grader@localhost:5432/grader"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: SecretStr = SecretStr("development-secret-change-me")
    jwt_exp_minutes: int = 60
    cors_origins: list[str] = ["http://localhost:5173"]

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Create `backend/app/main.py`:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AI Grading API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health/live")
    async def live_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1")
    async def api_root() -> dict[str, str]:
        return {"name": "AI Grading API", "version": "v1"}

    return app


app = create_app()
```

- [ ] **Step 5: Run focused checks**

Run:

```bash
.venv/bin/pytest backend/tests/test_health.py -q
.venv/bin/ruff check backend/app backend/tests
```

Expected: `2 passed`; Ruff exits `0`.

- [ ] **Step 6: Commit the scaffold**

```bash
git add backend
git commit -m "chore: scaffold FastAPI backend"
```

## Task 2: Add database infrastructure and shared enums

**Files:**
- Create: `backend/app/db/__init__.py`
- Create: `backend/app/db/base.py`
- Create: `backend/app/db/session.py`
- Create: `backend/app/db/types.py`
- Create: `backend/tests/db/test_types.py`
- Create: `docker-compose.yml`

- [ ] **Step 1: Write enum and grade tests**

Create `backend/tests/db/test_types.py`:

```python
import pytest

from app.db.types import AssignmentStatus, Role, grade_for_score


def test_roles_are_stable_api_values():
    assert [role.value for role in Role] == ["teacher", "student", "admin"]


def test_assignment_statuses_match_state_machine():
    assert [status.value for status in AssignmentStatus] == [
        "draft", "published", "closed", "archived"
    ]


@pytest.mark.parametrize(
    ("score", "grade"),
    [(100, "A"), (90, "A"), (89, "B"), (75, "B"), (74, "C"), (60, "C"), (59, "D"), (0, "D")],
)
def test_grade_boundaries(score, grade):
    assert grade_for_score(score).value == grade


@pytest.mark.parametrize("score", [-1, 101])
def test_grade_rejects_invalid_scores(score):
    with pytest.raises(ValueError, match="score must be between 0 and 100"):
        grade_for_score(score)
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
.venv/bin/pytest backend/tests/db/test_types.py -q
```

Expected: collection fails because `app.db.types` is missing.

- [ ] **Step 3: Implement shared types and SQLAlchemy base**

Create `backend/app/db/__init__.py` as an empty file.

Create `backend/app/db/types.py`:

```python
from enum import StrEnum


class Role(StrEnum):
    TEACHER = "teacher"
    STUDENT = "student"
    ADMIN = "admin"


class AssignmentStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CLOSED = "closed"
    ARCHIVED = "archived"


class SubmissionContentType(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    CODE = "code"
    STRUCTURED = "structured"


class SubmissionSource(StrEnum):
    WEB = "web"
    MATTERMOST = "mattermost"


class SubmissionStatus(StrEnum):
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"


class Grade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [item.value for item in enum_type]


def grade_for_score(score: int) -> Grade:
    if not 0 <= score <= 100:
        raise ValueError("score must be between 0 and 100")
    if score >= 90:
        return Grade.A
    if score >= 75:
        return Grade.B
    if score >= 60:
        return Grade.C
    return Grade.D
```

Create `backend/app/db/base.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

Create `backend/app/db/session.py`:

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
```

- [ ] **Step 4: Provision an isolated test database**

Create the initial `docker-compose.yml` before database-backed tests are introduced:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: grader
      POSTGRES_USER: grader
      POSTGRES_PASSWORD: grader
    ports: ["5432:5432"]
    volumes: ["postgres-data:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U grader -d grader"]
      interval: 5s
      timeout: 3s
      retries: 20

  test-postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: grader_test
      POSTGRES_USER: grader
      POSTGRES_PASSWORD: grader
    ports: ["5433:5432"]
    tmpfs: ["/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U grader -d grader_test"]
      interval: 2s
      timeout: 3s
      retries: 20

volumes:
  postgres-data:
```

Run `docker compose up -d --wait test-postgres`.

Expected: `test-postgres` becomes healthy on host port `5433`.

- [ ] **Step 5: Verify the shared types**

Run:

```bash
.venv/bin/pytest backend/tests/db/test_types.py -q
```

Expected: `12 passed`.

- [ ] **Step 6: Commit database infrastructure**

```bash
git add backend/app/db backend/tests/db docker-compose.yml
git commit -m "feat: add database foundation and domain enums"
```

## Task 3: Implement users, password hashing, JWT, and role guards

**Files:**
- Create: `backend/app/users/model.py`
- Create: `backend/app/auth/security.py`
- Create: `backend/app/auth/schemas.py`
- Create: `backend/app/auth/dependencies.py`
- Create: `backend/app/auth/router.py`
- Create: `backend/tests/auth/test_security.py`

- [ ] **Step 1: Write security tests**

Create `backend/tests/auth/test_security.py`:

```python
from app.auth.security import create_access_token, decode_access_token, hash_password, verify_password
from app.db.types import Role, enum_values


def test_password_round_trip():
    encoded = hash_password("teacher-pass")
    assert encoded != "teacher-pass"
    assert verify_password("teacher-pass", encoded)
    assert not verify_password("wrong", encoded)


def test_jwt_round_trip():
    token = create_access_token(subject="user-123", role=Role.TEACHER)
    claims = decode_access_token(token)
    assert claims["sub"] == "user-123"
    assert claims["role"] == "teacher"
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
.venv/bin/pytest backend/tests/auth/test_security.py -q
```

Expected: collection fails because `app.auth.security` is missing.

- [ ] **Step 3: Implement the user model and security helpers**

Create `backend/app/users/model.py`:

```python
from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import Role


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="role", values_callable=enum_values), index=True
    )
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
```

Create `backend/app/auth/security.py`:

```python
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings
from app.db.types import Role


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, encoded: str) -> bool:
    return bcrypt.checkpw(password.encode(), encoded.encode())


def create_access_token(subject: str, role: Role) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "role": role.value,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_exp_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def decode_access_token(token: str) -> dict[str, object]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=["HS256"])
```

- [ ] **Step 4: Implement auth schemas, dependency, and router**

Create `backend/app/auth/schemas.py`:

```python
from uuid import UUID

from pydantic import BaseModel

from app.db.types import Role


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CurrentUser(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: Role
```

Create `backend/app/auth/dependencies.py`:

```python
import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import CurrentUser
from app.auth.security import decode_access_token
from app.db.session import get_session
from app.db.types import Role
from app.users.model import User

bearer = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    try:
        claims = decode_access_token(credentials.credentials)
        user = await session.get(User, uuid.UUID(str(claims["sub"])))
    except (jwt.InvalidTokenError, ValueError, KeyError):
        user = None
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="invalid authentication")
    return CurrentUser(
        id=user.id, username=user.username, display_name=user.display_name, role=user.role
    )


def require_roles(*roles: Role):
    async def guard(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="insufficient role")
        return user
    return guard
```

Create `backend/app/auth/router.py` with `POST /auth/login` querying `User.username`, checking `verify_password`, and returning `TokenResponse`; add `GET /auth/me` returning `CurrentUser`. Register the router under `/api/v1` in `create_app()`.

- [ ] **Step 5: Add API tests for valid login and student role denial**

Add tests that insert a teacher and student into the test database, then assert:

```python
assert login_response.status_code == 200
assert login_response.json()["token_type"] == "bearer"
assert forbidden_response.status_code == 403
```

Run:

```bash
.venv/bin/pytest backend/tests/auth -q
```

Expected: all auth tests pass.

- [ ] **Step 6: Commit authentication**

```bash
git add backend/app/auth backend/app/users backend/app/main.py backend/tests/auth
git commit -m "feat: add JWT authentication and role guards"
```

## Task 4: Implement assignment lifecycle

**Files:**
- Create: `backend/app/assignments/model.py`
- Create: `backend/app/assignments/schemas.py`
- Create: `backend/app/assignments/service.py`
- Create: `backend/app/assignments/router.py`
- Create: `backend/tests/assignments/test_service.py`
- Create: `backend/tests/assignments/test_api.py`
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: Write lifecycle tests**

Create `backend/tests/assignments/test_service.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from app.assignments.schemas import AssignmentCreate
from app.assignments.service import InvalidAssignmentTransition, create_assignment, publish_assignment
from app.db.types import AssignmentStatus


@pytest.mark.asyncio
async def test_create_then_publish_assignment(db_session, teacher):
    assignment = await create_assignment(
        db_session,
        teacher.id,
        AssignmentCreate(
            title="图的最短路径",
            question="说明 Dijkstra 算法。",
            notes="允许伪代码",
            due_at=datetime.now(UTC) + timedelta(days=1),
            rubric={"required_points": ["非负权", "松弛"]},
        ),
    )
    assert assignment.status is AssignmentStatus.DRAFT
    await publish_assignment(db_session, assignment)
    assert assignment.status is AssignmentStatus.PUBLISHED
    assert assignment.code.startswith("HW-")


@pytest.mark.asyncio
async def test_cannot_publish_expired_assignment(db_session, teacher):
    assignment = await create_assignment(
        db_session,
        teacher.id,
        AssignmentCreate(
            title="过期作业",
            question="回答问题",
            due_at=datetime.now(UTC) - timedelta(minutes=1),
            rubric={"required_points": []},
        ),
    )
    with pytest.raises(InvalidAssignmentTransition, match="deadline must be in the future"):
        await publish_assignment(db_session, assignment)
```

- [ ] **Step 2: Run the lifecycle tests and verify failure**

Run:

```bash
.venv/bin/pytest backend/tests/assignments/test_service.py -q
```

Expected: collection fails because assignment modules are missing.

- [ ] **Step 3: Add database and teacher fixtures for all later tests**

Extend `backend/tests/conftest.py` with function-scoped PostgreSQL fixtures. Import `Assignment` and `User` before `Base.metadata.create_all()` so their tables are registered:

```python
import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.assignments.model import Assignment
from app.auth.security import hash_password
from app.db.base import Base
from app.db.session import get_session
from app.db.types import Role
from app.users.model import User

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://grader:grader@localhost:5433/grader_test",
)


@pytest.fixture
async def db_session():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def teacher(db_session):
    user = User(
        username="teacher-test",
        display_name="测试教师",
        role=Role.TEACHER,
        password_hash=hash_password("Teacher123!"),
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user
```

Change the existing `client` fixture to accept `db_session`, set `app.dependency_overrides[get_session]` to an async generator yielding that session, and clear the override after the HTTP client closes. This guarantees API tests never touch the development database.

- [ ] **Step 4: Implement model and schemas**

Create `backend/app/assignments/model.py` with `Assignment` fields from design section 8.2. Use PostgreSQL `JSONB`, timezone-aware `DateTime`, `Enum(AssignmentStatus, values_callable=enum_values)`, and a foreign key to `users.id`. Define `ASSIGNMENT_CODE_SEQUENCE = Sequence("assignment_code_seq", start=1, metadata=Base.metadata)` so concurrent publishers cannot generate the same code. Use the same `values_callable=enum_values` rule for every SQLAlchemy enum in later models so PostgreSQL stores the documented lowercase values rather than Python member names.

Create `backend/app/assignments/schemas.py`:

```python
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.types import AssignmentStatus


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=50_000)
    notes: str = Field(default="", max_length=10_000)
    due_at: datetime
    rubric: dict[str, object]


class AssignmentRead(AssignmentCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    code: str
    status: AssignmentStatus
```

- [ ] **Step 5: Implement service transitions**

Create `backend/app/assignments/service.py` with:

```python
class InvalidAssignmentTransition(ValueError):
    pass


async def next_assignment_code(session: AsyncSession) -> str:
    number = await session.scalar(select(ASSIGNMENT_CODE_SEQUENCE.next_value()))
    return f"HW-{int(number):04d}"


async def create_assignment(session, teacher_id, data):
    assignment = Assignment(
        code=await next_assignment_code(session),
        created_by=teacher_id,
        status=AssignmentStatus.DRAFT,
        **data.model_dump(),
    )
    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)
    return assignment


async def publish_assignment(session, assignment):
    if assignment.status is not AssignmentStatus.DRAFT:
        raise InvalidAssignmentTransition("only draft assignments can be published")
    if assignment.due_at <= datetime.now(UTC):
        raise InvalidAssignmentTransition("deadline must be in the future")
    assignment.status = AssignmentStatus.PUBLISHED
    assignment.published_at = datetime.now(UTC)
    await session.commit()
    return assignment
```

Add equivalent explicit `close_assignment` and `reopen_assignment` transition functions matching the design state machine.

- [ ] **Step 6: Implement and test the assignment API**

Create `backend/app/assignments/router.py` with create, list, get, publish, close, and reopen endpoints. Use `require_roles(Role.TEACHER)` for mutations. Return `409` for `InvalidAssignmentTransition`.

Create `backend/tests/assignments/test_api.py` covering:

```python
assert teacher_create.status_code == 201
assert student_create.status_code == 403
assert publish.status_code == 200
assert publish.json()["status"] == "published"
```

Run:

```bash
.venv/bin/pytest backend/tests/assignments -q
```

Expected: lifecycle and API tests pass.

- [ ] **Step 7: Commit assignments**

```bash
git add backend/app/assignments backend/app/main.py backend/tests/assignments
git commit -m "feat: add assignment lifecycle"
```

## Task 5: Implement versioned student submissions

**Files:**
- Create: `backend/app/submissions/model.py`
- Create: `backend/app/submissions/schemas.py`
- Create: `backend/app/submissions/service.py`
- Create: `backend/app/submissions/router.py`
- Create: `backend/tests/submissions/test_service.py`
- Create: `backend/tests/submissions/test_api.py`
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: Write versioning and deadline tests**

Create `backend/tests/submissions/test_service.py`:

```python
import pytest

from app.db.types import SubmissionContentType, SubmissionSource
from app.submissions.schemas import SubmissionCreate
from app.submissions.service import SubmissionClosed, create_submission


@pytest.mark.asyncio
async def test_repeat_submission_creates_versions(db_session, published_assignment, student):
    payload = SubmissionCreate(
        content_type=SubmissionContentType.MARKDOWN,
        content_text="first answer",
    )
    first = await create_submission(
        db_session, published_assignment, student.id, payload, SubmissionSource.WEB
    )
    second = await create_submission(
        db_session, published_assignment, student.id, payload, SubmissionSource.WEB
    )
    assert (first.version, second.version) == (1, 2)


@pytest.mark.asyncio
async def test_closed_assignment_rejects_submission(db_session, closed_assignment, student):
    with pytest.raises(SubmissionClosed):
        await create_submission(
            db_session,
            closed_assignment,
            student.id,
            SubmissionCreate(content_type="text", content_text="answer"),
            SubmissionSource.WEB,
        )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/pytest backend/tests/submissions/test_service.py -q
```

Expected: collection fails because submission modules are missing.

- [ ] **Step 3: Implement the model and service**

Create the `Submission` model with the fields and unique constraint specified in design section 8.2.

Implement `create_submission` using a transaction and:

```python
class SubmissionClosed(ValueError):
    pass


class SubmissionTooLong(ValueError):
    pass


await session.execute(
    select(Assignment.id).where(Assignment.id == assignment.id).with_for_update()
)
latest = await session.scalar(
    select(func.max(Submission.version)).where(
        Submission.assignment_id == assignment.id,
        Submission.student_id == student_id,
    )
)
submission = Submission(
    assignment_id=assignment.id,
    student_id=student_id,
    version=int(latest or 0) + 1,
    content_type=data.content_type,
    content_text=data.content_text,
    content_json=data.content_json,
    source=source,
)
```

Raise `SubmissionClosed` when the assignment is not `published` or its deadline has passed. Raise `SubmissionTooLong` before any insert when `content_text` exceeds 50,000 characters. The assignment-row lock serializes first and repeated submissions, so two concurrent requests cannot choose the same version; retain the database unique constraint as the final guard.

- [ ] **Step 4: Add student and assignment-state fixtures**

Extend `backend/tests/conftest.py`:

```python
from datetime import UTC, datetime, timedelta

from app.assignments.model import Assignment
from app.db.types import AssignmentStatus


@pytest.fixture
async def student(db_session):
    user = User(
        username="student-test",
        display_name="测试学生",
        role=Role.STUDENT,
        password_hash=hash_password("Student123!"),
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def published_assignment(db_session, teacher):
    assignment = Assignment(
        code="HW-9001",
        title="测试作业",
        question="说明最短路径算法。",
        notes="",
        due_at=datetime.now(UTC) + timedelta(days=1),
        rubric={"required_points": ["松弛"]},
        status=AssignmentStatus.PUBLISHED,
        created_by=teacher.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(assignment)
    await db_session.commit()
    await db_session.refresh(assignment)
    return assignment


@pytest.fixture
async def closed_assignment(db_session, teacher):
    assignment = Assignment(
        code="HW-9002",
        title="已关闭测试作业",
        question="回答问题。",
        notes="",
        due_at=datetime.now(UTC) + timedelta(days=1),
        rubric={"required_points": []},
        status=AssignmentStatus.CLOSED,
        created_by=teacher.id,
        published_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(assignment)
    await db_session.commit()
    await db_session.refresh(assignment)
    return assignment
```

- [ ] **Step 5: Implement submission endpoints and ownership checks**

Create endpoints:

```text
POST /api/v1/assignments/{assignment_id}/submissions
GET  /api/v1/assignments/{assignment_id}/submissions/me
GET  /api/v1/assignments/{assignment_id}/submissions
GET  /api/v1/submissions/{submission_id}
```

Tests must assert that a student cannot read another student's submission and a teacher can list all latest submissions.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/pytest backend/tests/submissions -q
```

Expected: versioning, deadline, ownership, and role tests pass.

- [ ] **Step 7: Commit submissions**

```bash
git add backend/app/submissions backend/app/main.py backend/tests/submissions
git commit -m "feat: add versioned student submissions"
```

## Task 6: Implement assignment summary projections

**Files:**
- Create: `backend/app/assignments/summary.py`
- Create: `backend/tests/assignments/test_summary.py`
- Modify: `backend/app/assignments/router.py`

- [ ] **Step 1: Write a projection test using multiple submission versions**

Create a test with three students: one submits twice, one submits once, and one does not submit. Assert:

```python
assert summary.total_students == 3
assert summary.submitted_students == 2
assert summary.missing_students == 1
assert summary.latest_submission_versions == {student_a.id: 2, student_b.id: 1}
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```bash
.venv/bin/pytest backend/tests/assignments/test_summary.py -q
```

Expected: import fails because `assignments.summary` is missing.

- [ ] **Step 3: Implement the summary query**

Create a window-function query using:

```python
row_number().over(
    partition_by=(Submission.assignment_id, Submission.student_id),
    order_by=Submission.version.desc(),
)
```

Return a typed Pydantic model containing student identity, latest submission, and nullable reserved evaluation-status fields consumed by Plan 2.

- [ ] **Step 4: Expose and verify the teacher-only endpoint**

Add `GET /api/v1/assignments/{id}/summary`; assert teacher gets `200` and student gets `403`.

Run:

```bash
.venv/bin/pytest backend/tests/assignments/test_summary.py -q
```

Expected: all summary tests pass.

- [ ] **Step 5: Commit summary projections**

```bash
git add backend/app/assignments backend/tests/assignments
git commit -m "feat: add assignment submission summaries"
```

## Task 7: Add Alembic migration and deterministic seed

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/migrations/env.py`
- Create: `backend/migrations/versions/0001_domain.py`
- Create: `backend/scripts/seed_demo.py`
- Create: `backend/tests/scripts/test_seed_demo.py`

- [ ] **Step 1: Write the seed idempotency test**

```python
@pytest.mark.asyncio
async def test_seed_is_idempotent(db_session):
    await seed_demo(db_session)
    await seed_demo(db_session)
    assert await count_users(db_session) == 4
    assert await count_assignments(db_session) == 1
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
.venv/bin/pytest backend/tests/scripts/test_seed_demo.py -q
```

Expected: import fails because `seed_demo` is missing.

- [ ] **Step 3: Create the migration**

Configure Alembic to import all model modules and use `Base.metadata`. Create the first migration for users, assignments, and submissions, including enum types, foreign keys, unique constraints, and indexes.

Run:

```bash
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/alembic downgrade base
../.venv/bin/alembic upgrade head
```

Expected: all three commands exit `0`.

- [ ] **Step 4: Implement deterministic seed data**

Create users:

```python
DEMO_USERS = [
    ("teacher", "演示教师", Role.TEACHER, "Teacher123!"),
    ("student1", "学生甲", Role.STUDENT, "Student123!"),
    ("student2", "学生乙", Role.STUDENT, "Student123!"),
    ("student3", "学生丙", Role.STUDENT, "Student123!"),
]
```

Create one published Dijkstra assignment with the rubric from the design document. Use upserts keyed by username and assignment code.

- [ ] **Step 5: Verify seed idempotency**

Run:

```bash
.venv/bin/pytest backend/tests/scripts/test_seed_demo.py -q
```

Expected: seed test passes after two consecutive runs.

- [ ] **Step 6: Commit migration and seed**

```bash
git add backend/alembic.ini backend/migrations backend/scripts backend/tests/scripts
git commit -m "feat: add initial migration and demo seed"
```

## Task 8: Add local runtime and quality commands

**Files:**
- Create: `.env.example`
- Modify: `docker-compose.yml`
- Create: `Makefile`
- Create: `backend/Dockerfile`
- Create: `.gitignore`

- [ ] **Step 1: Add environment contract**

Create `.env.example` with non-secret defaults:

```dotenv
APP_ENV=development
DATABASE_URL=postgresql+asyncpg://grader:grader@postgres:5432/grader
REDIS_URL=redis://redis:6379/0
JWT_SECRET=replace-with-a-random-local-secret
JWT_EXP_MINUTES=60
AGENT_PROVIDER=mock
```

- [ ] **Step 2: Create the backend image**

Create `backend/Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
RUN pip install --no-cache-dir .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Create Compose services**

Extend `docker-compose.yml` with `redis:7`, `api`, and `worker` while preserving the existing development and test PostgreSQL services. Add Redis and API health checks; make API and worker depend on healthy dependencies. The worker command is:

```yaml
command: celery -A app.evaluations.worker.celery_app worker --loglevel=INFO
```

The evaluation module is created in Plan 2; until then, start only `postgres` and `api`.

- [ ] **Step 4: Add repeatable developer commands**

Create `Makefile` targets:

```makefile
install:
	python3.12 -m venv .venv
	.venv/bin/pip install -e 'backend[dev]'

db-up:
	docker compose up -d --wait postgres test-postgres redis

migrate:
	cd backend && ../.venv/bin/alembic upgrade head

seed:
	PYTHONPATH=backend .venv/bin/python backend/scripts/seed_demo.py

test-backend:
	.venv/bin/pytest backend/tests -q

lint-backend:
	.venv/bin/ruff check backend/app backend/tests
```

- [ ] **Step 5: Run the foundation verification**

Run:

```bash
cp .env.example .env
make db-up
make migrate
make seed
make test-backend
make lint-backend
docker compose up -d api
curl --fail http://localhost:8000/health/live
```

Expected: all commands exit `0`; health response is `{"status":"ok"}`.

- [ ] **Step 6: Commit runtime files**

```bash
git add .env.example .gitignore docker-compose.yml Makefile backend/Dockerfile
git commit -m "chore: add local backend runtime"
```

## Plan 1 completion gate

Run:

```bash
make test-backend
make lint-backend
cd backend && ../.venv/bin/alembic upgrade head
curl --fail http://localhost:8000/health/live
```

Expected:

- Backend test suite has zero failures.
- Ruff reports no errors.
- Migration reaches head.
- Health endpoint returns HTTP 200.
- `git status --short` is empty after the final task commit.
