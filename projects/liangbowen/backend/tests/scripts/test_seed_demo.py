from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.assignments.model import Assignment
from app.assignments.service import next_assignment_code
from app.audit.model import AuditLog
from app.auth.security import hash_password, verify_password
from app.core.config import get_settings
from app.db.types import (
    AssignmentStatus,
    Role,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)
from app.demo.scenarios import DEMO_SCENARIOS
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.providers.base import ProviderIdentity
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationInProgress, EvaluationJobFailed, EvaluationService
from app.evaluations.types import JobReason, JobStatus, ReviewStatus
from app.integrations.mattermost.model import MattermostIdentity
from app.reviews.model import ReviewAction
from app.reviews.schemas import ConfirmRequest, ReevaluateRequest, ReportPatch
from app.reviews.service import ReviewService
from app.reviews.types import ReviewActionType
from app.submissions.model import Submission
from app.users.model import User
from scripts.seed_demo import (
    DEMO_ASSIGNMENT_ID,
    DEMO_USER_IDS,
    DEMO_USERS,
    DemoSeedConflict,
    build_demo_rubric,
    reset_acceptance_demo,
    seed_acceptance_demo,
    seed_demo,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _new_test_lock_engine(data_engine: AsyncEngine) -> AsyncEngine:
    return create_async_engine(
        data_engine.url,
        poolclass=NullPool,
        pool_pre_ping=True,
    )


async def _seed_acceptance(data_engine: AsyncEngine):
    lock_engine = _new_test_lock_engine(data_engine)
    try:
        return await seed_acceptance_demo(data_engine, lock_engine)
    finally:
        await lock_engine.dispose()


async def _reset_acceptance(data_engine: AsyncEngine) -> None:
    lock_engine = _new_test_lock_engine(data_engine)
    try:
        await reset_acceptance_demo(data_engine, lock_engine)
    finally:
        await lock_engine.dispose()


def test_demo_rubric_builder_returns_deep_independent_json_values() -> None:
    import scripts.seed_demo as seed_module

    first = seed_module.build_demo_rubric()
    required_points = first["required_points"]
    metadata = first["metadata"]
    assert isinstance(required_points, list)
    assert isinstance(metadata, dict)
    required_points.append("tampered")
    metadata["demo_key"] = "tampered"

    second = seed_module.build_demo_rubric()

    assert second == {
        "required_points": [
            "适用非负权图",
            "贪心选择未确定的最短距离顶点",
            "进行松弛操作",
            "给出复杂度",
        ],
        "grading_notes": "若未说明非负权限制，正确性不得评为完全正确。",
        "metadata": {"demo_key": "ai-grading-acceptance-v1"},
    }


@pytest.mark.asyncio
async def test_public_legacy_rubric_mutation_cannot_pollute_production_manifest(
    postgres_session,
) -> None:
    import scripts.seed_demo as seed_module

    expected_legacy = {
        "required_points": [
            "适用非负权图",
            "贪心选择未确定的最短距离顶点",
            "进行松弛操作",
            "给出复杂度",
        ],
        "grading_notes": "若未说明非负权限制，正确性不得评为完全正确。",
    }
    assert isinstance(seed_module.DEMO_RUBRIC, dict)
    assert seed_module.DEMO_RUBRIC == expected_legacy
    original = copy.deepcopy(seed_module.DEMO_RUBRIC)
    try:
        required_points = seed_module.DEMO_RUBRIC["required_points"]
        assert isinstance(required_points, list)
        required_points.append("tampered")
        seed_module.DEMO_RUBRIC["grading_notes"] = "tampered"
        seed_module.DEMO_RUBRIC["metadata"] = {"demo_key": "tampered"}

        assert seed_module.build_demo_rubric() == {
            "required_points": expected_legacy["required_points"],
            "grading_notes": expected_legacy["grading_notes"],
            "metadata": {"demo_key": "ai-grading-acceptance-v1"},
        }
        await seed_demo(postgres_session)
        await postgres_session.commit()
        await seed_demo(postgres_session)
        assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
        assert assignment is not None
        assert assignment.rubric == seed_module.build_demo_rubric()
    finally:
        seed_module.DEMO_RUBRIC.clear()
        seed_module.DEMO_RUBRIC.update(original)


async def _user_snapshot(session) -> dict[str, tuple[uuid.UUID, str, str, Role, bool]]:
    users = (await session.scalars(select(User).where(User.username.in_(DEMO_USER_IDS)))).all()
    return {
        user.username: (
            user.id,
            user.password_hash,
            user.display_name,
            user.role,
            user.is_active,
        )
        for user in users
    }


def test_demo_user_manifest_includes_exact_local_admin() -> None:
    assert DEMO_USER_IDS == {
        "admin": uuid.UUID("00000000-0000-4000-8000-000000000005"),
        "teacher": uuid.UUID("00000000-0000-4000-8000-000000000001"),
        "student1": uuid.UUID("00000000-0000-4000-8000-000000000002"),
        "student2": uuid.UUID("00000000-0000-4000-8000-000000000003"),
        "student3": uuid.UUID("00000000-0000-4000-8000-000000000004"),
    }
    assert DEMO_USERS == (
        ("admin", "系统管理员", Role.ADMIN, "Admin123!Secure"),
        ("teacher", "演示教师", Role.TEACHER, "Teacher123!"),
        ("student1", "学生甲", Role.STUDENT, "Student123!"),
        ("student2", "学生乙", Role.STUDENT, "Student123!"),
        ("student3", "学生丙", Role.STUDENT, "Student123!"),
    )


@pytest.mark.asyncio
async def test_seed_contains_local_admin_without_mattermost_identity(postgres_session) -> None:
    summary = await seed_demo(postgres_session)
    admin = await postgres_session.scalar(select(User).where(User.username == "admin"))

    assert summary.users == 5
    assert admin is not None
    assert admin.id == uuid.UUID("00000000-0000-4000-8000-000000000005")
    assert admin.display_name == "系统管理员"
    assert admin.role is Role.ADMIN
    assert admin.is_active is True
    assert verify_password("Admin123!Secure", admin.password_hash)
    assert (
        await postgres_session.scalar(
            select(MattermostIdentity).where(MattermostIdentity.user_id == admin.id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_preserves_hashes(postgres_session) -> None:
    await seed_demo(postgres_session)
    await postgres_session.commit()
    first_users = await _user_snapshot(postgres_session)
    first_assignment = await postgres_session.scalar(
        select(Assignment).where(Assignment.code == "HW-0001")
    )
    assert first_assignment is not None
    first_assignment_snapshot = (
        first_assignment.id,
        first_assignment.created_at,
        first_assignment.published_at,
    )

    await seed_demo(postgres_session)
    await postgres_session.commit()
    second_users = await _user_snapshot(postgres_session)
    second_assignment = await postgres_session.scalar(
        select(Assignment).where(Assignment.code == "HW-0001")
    )

    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 5
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 1
    assert {username: values[0] for username, values in first_users.items()} == DEMO_USER_IDS
    assert first_users == second_users
    assert second_assignment is not None
    assert second_assignment.id == DEMO_ASSIGNMENT_ID
    assert (
        second_assignment.id,
        second_assignment.created_at,
        second_assignment.published_at,
    ) == first_assignment_snapshot
    assert second_assignment.created_by == second_users["teacher"][0]
    assert second_assignment.status is AssignmentStatus.PUBLISHED
    assert second_assignment.due_at == datetime(2099, 1, 1, tzinfo=UTC)
    assert second_assignment.rubric == {
        "required_points": [
            "适用非负权图",
            "贪心选择未确定的最短距离顶点",
            "进行松弛操作",
            "给出复杂度",
        ],
        "grading_notes": "若未说明非负权限制，正确性不得评为完全正确。",
        "metadata": {"demo_key": "ai-grading-acceptance-v1"},
    }
    assert verify_password("Teacher123!", second_users["teacher"][1])
    assert second_users["teacher"][2:] == ("演示教师", Role.TEACHER, True)
    assert verify_password("Admin123!Secure", second_users["admin"][1])
    assert second_users["admin"][2:] == ("系统管理员", Role.ADMIN, True)
    for username, display_name in (
        ("student1", "学生甲"),
        ("student2", "学生乙"),
        ("student3", "学生丙"),
    ):
        assert verify_password("Student123!", second_users[username][1])
        assert second_users[username][2:] == (display_name, Role.STUDENT, True)
    assert await postgres_session.scalar(text("SELECT nextval('assignment_code_seq')")) == 2


@pytest.mark.asyncio
async def test_seed_refuses_matching_names_with_non_manifest_primary_keys(
    postgres_session,
) -> None:
    existing_teacher_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    existing_assignment_id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    unrelated_teacher_id = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    unrelated_assignment_id = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    postgres_session.add_all(
        [
            User(
                id=existing_teacher_id,
                username="teacher",
                display_name="Old",
                role=Role.STUDENT,
                password_hash=hash_password("wrong"),
                is_active=False,
            ),
            User(
                id=unrelated_teacher_id,
                username="other",
                display_name="Other",
                role=Role.TEACHER,
                password_hash=hash_password("untouched"),
                is_active=True,
            ),
        ]
    )
    await postgres_session.flush()
    postgres_session.add_all(
        [
            Assignment(
                id=existing_assignment_id,
                code="HW-0001",
                title="Old",
                question="Old",
                notes="Old",
                rubric={},
                due_at=datetime(2030, 1, 1, tzinfo=UTC),
                status=AssignmentStatus.DRAFT,
                created_by=existing_teacher_id,
            ),
            Assignment(
                id=unrelated_assignment_id,
                code="HW-9999",
                title="Unrelated",
                question="Do not delete",
                notes="",
                rubric={},
                due_at=datetime(2099, 1, 1, tzinfo=UTC),
                status=AssignmentStatus.PUBLISHED,
                created_by=unrelated_teacher_id,
            ),
        ]
    )
    await postgres_session.commit()

    with pytest.raises(DemoSeedConflict, match="demo username belongs to another user ID"):
        await seed_demo(postgres_session)
    await postgres_session.rollback()

    teacher = await postgres_session.scalar(select(User).where(User.username == "teacher"))
    assignment = await postgres_session.scalar(
        select(Assignment).where(Assignment.code == "HW-0001")
    )
    unrelated = await postgres_session.get(Assignment, unrelated_assignment_id)
    assert teacher is not None
    assert teacher.id == existing_teacher_id
    assert teacher.display_name == "Old"
    assert teacher.role is Role.STUDENT
    assert teacher.is_active is False
    assert assignment is not None
    assert assignment.id == existing_assignment_id
    assert assignment.created_by == existing_teacher_id
    assert unrelated is not None
    assert unrelated.title == "Unrelated"
    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 2
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ["staging", "production"])
async def test_seed_service_refuses_unsafe_environments_before_touching_database_or_hashing(
    app_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.seed_demo as seed_module

    class UntouchedSession:
        async def execute(self, *args, **kwargs):
            raise AssertionError("database must not be touched")

    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("JWT_SECRET", "strong-secret-for-seed-environment-test")
    get_settings.cache_clear()
    monkeypatch.setattr(
        seed_module,
        "hash_password",
        lambda password: (_ for _ in ()).throw(AssertionError("password must not be hashed")),
    )

    with pytest.raises(Exception) as caught:
        await seed_demo(UntouchedSession())  # type: ignore[arg-type]
    assert type(caught.value).__name__ == "DemoSeedEnvironmentError"
    assert "demo seed is disabled" in str(caught.value)


@pytest.mark.asyncio
async def test_seed_cli_refuses_production_without_leaking_secrets() -> None:
    sensitive_values = (
        "sensitive-user",
        "sensitive-password",
        "production-secret-for-seed-cli-test",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "production",
            "DATABASE_URL": (
                "postgresql+asyncpg://sensitive-user:sensitive-password@127.0.0.1:1/production"
            ),
            "JWT_SECRET": "production-secret-for-seed-cli-test",
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.seed_demo",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode != 0
    output = (stdout + stderr).decode()
    assert "disabled" in output
    assert all(secret not in output for secret in sensitive_values)


@pytest.mark.asyncio
async def test_seed_does_not_autoflush_external_pending_objects(postgres_session) -> None:
    valid_pending = User(
        username="outside-valid",
        display_name="Outside",
        role=Role.STUDENT,
        password_hash=hash_password("outside"),
        is_active=True,
    )
    invalid_pending = User(username="outside-invalid")
    postgres_session.add_all([valid_pending, invalid_pending])

    await seed_demo(postgres_session)

    assert inspect(valid_pending).pending
    assert inspect(invalid_pending).pending
    with postgres_session.no_autoflush:
        assert (
            await postgres_session.scalar(
                select(func.count()).select_from(User).where(User.username.like("outside-%"))
            )
            == 0
        )
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_seed_rejects_fixed_user_id_owned_by_another_username(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.seed_demo as seed_module

    postgres_session.add(
        User(
            id=DEMO_USER_IDS["teacher"],
            username="unrelated",
            display_name="Unrelated",
            role=Role.TEACHER,
            password_hash=hash_password("untouched"),
            is_active=True,
        )
    )
    await postgres_session.commit()
    monkeypatch.setattr(
        seed_module,
        "hash_password",
        lambda password: (_ for _ in ()).throw(AssertionError("must preflight before hashing")),
    )

    with pytest.raises(Exception) as caught:
        await seed_demo(postgres_session)
    assert type(caught.value).__name__ == "DemoSeedConflict"
    assert str(caught.value) == (
        "a fixed demo user ID belongs to another username; resolve the conflict before seeding"
    )
    assert postgres_session.in_transaction()
    await postgres_session.rollback()
    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio
async def test_seed_rejects_fixed_assignment_id_owned_by_another_code(postgres_session) -> None:
    teacher = User(
        username="other-teacher",
        display_name="Other",
        role=Role.TEACHER,
        password_hash=hash_password("untouched"),
        is_active=True,
    )
    postgres_session.add(teacher)
    await postgres_session.flush([teacher])
    postgres_session.add(
        Assignment(
            id=DEMO_ASSIGNMENT_ID,
            code="HW-9999",
            title="Unrelated",
            question="Do not overwrite",
            notes="",
            rubric={},
            due_at=datetime(2099, 1, 1, tzinfo=UTC),
            status=AssignmentStatus.PUBLISHED,
            created_by=teacher.id,
        )
    )
    await postgres_session.commit()

    with pytest.raises(Exception) as caught:
        await seed_demo(postgres_session)
    assert type(caught.value).__name__ == "DemoSeedConflict"
    assert str(caught.value) == (
        "the fixed demo assignment ID does not match the manifest; "
        "resolve the conflict before seeding"
    )
    await postgres_session.rollback()
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    assert assignment is not None
    assert assignment.code == "HW-9999"
    assert assignment.title == "Unrelated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("created_at", datetime(2026, 7, 26, tzinfo=UTC)),
        ("title", "被篡改的演示作业"),
        ("question", "被篡改的问题"),
        ("notes", "被篡改的说明"),
        (
            "rubric",
            {
                **build_demo_rubric(),
                "grading_notes": "tampered but marker preserved",
            },
        ),
        ("due_at", datetime(2099, 1, 2, tzinfo=UTC)),
        ("status", AssignmentStatus.DRAFT),
        ("published_at", datetime(2026, 7, 26, tzinfo=UTC)),
        ("mattermost_channel_id", "tampered-channel"),
    ],
)
async def test_seed_rejects_tampered_owned_assignment_before_any_mutation(
    postgres_session,
    field: str,
    tampered_value: object,
) -> None:
    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    assert assignment is not None
    setattr(assignment, field, tampered_value)
    await postgres_session.commit()

    with pytest.raises(DemoSeedConflict, match="fixed demo assignment ID.*manifest"):
        await seed_demo(postgres_session)
    await postgres_session.rollback()

    postgres_session.expire_all()
    preserved = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    assert preserved is not None
    assert getattr(preserved, field) == tampered_value
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0


@pytest.mark.asyncio
async def test_seed_atomically_upgrades_exact_legacy_assignment_rubric(postgres_session) -> None:
    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    assert assignment is not None
    assignment.rubric = {
        "required_points": [
            "适用非负权图",
            "贪心选择未确定的最短距离顶点",
            "进行松弛操作",
            "给出复杂度",
        ],
        "grading_notes": "若未说明非负权限制，正确性不得评为完全正确。",
    }
    unchanged_fields = (
        assignment.id,
        assignment.code,
        assignment.created_by,
        assignment.created_at,
        assignment.title,
        assignment.question,
        assignment.notes,
        assignment.due_at,
        assignment.status,
        assignment.published_at,
        assignment.mattermost_channel_id,
    )
    await postgres_session.commit()

    await seed_demo(postgres_session)
    await postgres_session.commit()

    postgres_session.expire_all()
    upgraded = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    assert upgraded is not None
    assert upgraded.rubric == build_demo_rubric()
    assert (
        upgraded.id,
        upgraded.code,
        upgraded.created_by,
        upgraded.created_at,
        upgraded.title,
        upgraded.question,
        upgraded.notes,
        upgraded.due_at,
        upgraded.status,
        upgraded.published_at,
        upgraded.mattermost_channel_id,
    ) == unchanged_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "near_legacy_rubric",
    [
        {
            "required_points": ["适用非负权图", "贪心选择未确定的最短距离顶点", "进行松弛操作"],
            "grading_notes": "若未说明非负权限制，正确性不得评为完全正确。",
        },
        {
            "required_points": [
                "适用非负权图",
                "贪心选择未确定的最短距离顶点",
                "进行松弛操作",
                "给出复杂度",
            ],
            "grading_notes": "tampered",
        },
        {
            "required_points": [
                "适用非负权图",
                "贪心选择未确定的最短距离顶点",
                "进行松弛操作",
                "给出复杂度",
            ],
            "grading_notes": "若未说明非负权限制，正确性不得评为完全正确。",
            "extra": True,
        },
        {
            "required_points": [
                "适用非负权图",
                "贪心选择未确定的最短距离顶点",
                "进行松弛操作",
                "给出复杂度",
            ],
            "grading_notes": "若未说明非负权限制，正确性不得评为完全正确。",
            "metadata": {"demo_key": "wrong"},
        },
    ],
)
async def test_seed_rejects_near_legacy_assignment_before_user_mutation(
    postgres_session,
    near_legacy_rubric: dict[str, object],
) -> None:
    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    teacher = await postgres_session.get(User, DEMO_USER_IDS["teacher"])
    assert assignment is not None and teacher is not None
    assignment.rubric = near_legacy_rubric
    teacher.display_name = "preflight sentinel"
    await postgres_session.commit()

    with pytest.raises(DemoSeedConflict, match="fixed demo assignment ID.*manifest"):
        await seed_demo(postgres_session)
    await postgres_session.rollback()

    postgres_session.expire_all()
    preserved_assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    preserved_teacher = await postgres_session.get(User, DEMO_USER_IDS["teacher"])
    assert preserved_assignment is not None and preserved_teacher is not None
    assert preserved_assignment.rubric == near_legacy_rubric
    assert preserved_teacher.display_name == "preflight sentinel"
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0


@pytest.mark.asyncio
async def test_seed_rejects_unrelated_assignment_that_claimed_hw_0001(postgres_session) -> None:
    teacher = User(
        username="other-teacher",
        display_name="Other",
        role=Role.TEACHER,
        password_hash=hash_password("untouched"),
        is_active=True,
    )
    postgres_session.add(teacher)
    await postgres_session.flush([teacher])
    code = await next_assignment_code(postgres_session)
    assert code == "HW-0001"
    assignment = Assignment(
        code=code,
        title="Unrelated",
        question="Do not overwrite",
        notes="",
        rubric={},
        due_at=datetime(2099, 1, 1, tzinfo=UTC),
        status=AssignmentStatus.DRAFT,
        created_by=teacher.id,
    )
    postgres_session.add(assignment)
    await postgres_session.commit()

    with pytest.raises(Exception) as caught:
        await seed_demo(postgres_session)
    assert type(caught.value).__name__ == "DemoSeedConflict"
    assert str(caught.value) == (
        "HW-0001 belongs to a non-demo assignment; resolve the conflict before seeding"
    )
    await postgres_session.rollback()
    await postgres_session.refresh(assignment)
    assert assignment.title == "Unrelated"
    assert assignment.status is AssignmentStatus.DRAFT


@pytest.mark.asyncio
async def test_seed_cli_reports_fixed_id_conflict_without_leaking_secrets(
    postgres_session,
) -> None:
    postgres_session.add(
        User(
            id=DEMO_USER_IDS["teacher"],
            username="unrelated",
            display_name="Unrelated",
            role=Role.TEACHER,
            password_hash=hash_password("stored-secret"),
            is_active=True,
        )
    )
    await postgres_session.commit()
    database_url = postgres_session.bind.url
    database_password = database_url.password
    assert database_password
    protected_url = database_url.set(password=database_password).render_as_string(
        hide_password=False
    )
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": protected_url,
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.seed_demo",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode != 0
    output = (stdout + stderr).decode()
    assert "resolve the conflict before seeding" in output
    database_credentials_are_hidden = (
        database_password not in output and protected_url not in output
    )
    assert database_credentials_are_hidden
    assert "stored-secret" not in output
    assert "$2" not in output


@pytest.mark.asyncio
async def test_seed_and_code_generation_share_lock_without_sequence_rewind(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.seed_demo as seed_module

    await seed_demo(postgres_session)
    await postgres_session.commit()
    monkeypatch.setattr(seed_module, "verify_password", lambda password, password_hash: True)
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    generated_codes: list[str] = []

    async def generate_codes(count: int) -> None:
        for _ in range(count):
            async with session_factory.begin() as session:
                generated_codes.append(await next_assignment_code(session))

    async def repeat_seed(count: int) -> None:
        for _ in range(count):
            async with session_factory.begin() as session:
                await seed_demo(session)

    await asyncio.gather(
        *(generate_codes(100) for _ in range(8)),
        *(repeat_seed(25) for _ in range(4)),
    )

    assert len(generated_codes) == 800
    assert len(set(generated_codes)) == 800
    assert min(generated_codes) == "HW-0002"
    assert max(int(code.removeprefix("HW-")) for code in generated_codes) == 801
    assert await postgres_session.scalar(text("SELECT nextval('assignment_code_seq')")) == 802


@pytest.mark.asyncio
async def test_seed_does_not_rewind_a_higher_assignment_sequence(postgres_session) -> None:
    await postgres_session.execute(text("SELECT setval('assignment_code_seq', 41, true)"))
    await seed_demo(postgres_session)
    assert await postgres_session.scalar(text("SELECT nextval('assignment_code_seq')")) == 42


@pytest.mark.asyncio
async def test_seed_leaves_transaction_ownership_with_the_caller(postgres_session) -> None:
    await seed_demo(postgres_session)
    assert postgres_session.in_transaction()
    await postgres_session.rollback()

    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 0


@pytest.mark.asyncio
async def test_seed_failure_is_atomic_when_caller_rolls_back(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_execute = postgres_session.execute

    async def fail_after_records_are_flushed(statement, *args, **kwargs):
        if "last_value" in str(statement):
            raise RuntimeError("planned seed failure after flush")
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(postgres_session, "execute", fail_after_records_are_flushed)
    with pytest.raises(RuntimeError, match="planned seed failure"):
        await seed_demo(postgres_session)
    assert postgres_session.in_transaction()
    await postgres_session.rollback()

    monkeypatch.setattr(postgres_session, "execute", real_execute)
    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 0


@pytest.mark.asyncio
async def test_two_concurrent_seeds_converge_without_unique_races(postgres_session) -> None:
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async def run_seed() -> None:
        async with session_factory.begin() as session:
            await seed_demo(session)

    await asyncio.gather(run_seed(), run_seed())

    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 5
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 1
    assert await postgres_session.scalar(text("SELECT nextval('assignment_code_seq')")) == 2


@pytest.mark.asyncio
async def test_seed_cli_owns_transaction_and_prints_only_safe_summary(postgres_session) -> None:
    database_url = postgres_session.bind.url.render_as_string(hide_password=False)
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": database_url,
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.seed_demo",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode()
    summary = json.loads(stdout)
    assert summary["assignment_id"] == str(DEMO_ASSIGNMENT_ID)
    assert summary["assignments"] == 1
    assert summary["users"] == 5
    assert tuple(summary["scenarios"]) == ("complete", "partial", "incorrect")
    assert all(
        set(item) == {"submission_id", "report_id"} for item in summary["scenarios"].values()
    )
    output = (stdout + stderr).decode()
    assert "Teacher123!" not in output
    assert "Student123!" not in output
    assert "Admin123!Secure" not in output
    assert "$2" not in output
    postgres_session.expire_all()
    assert await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID) is not None


@pytest.mark.asyncio
async def test_reset_cli_only_resets_then_plain_cli_rebuilds_demo(postgres_session) -> None:
    await _seed_acceptance(postgres_session.bind)
    unrelated_teacher = User(
        username="reset-cli-unrelated-teacher",
        display_name="Unrelated",
        role=Role.TEACHER,
        password_hash=hash_password("unrelated"),
        is_active=True,
    )
    postgres_session.add(unrelated_teacher)
    await postgres_session.flush([unrelated_teacher])
    unrelated_assignment = Assignment(
        code="HW-9999",
        title="Unrelated",
        question="Preserve me",
        notes="",
        rubric={"owner": "unrelated"},
        due_at=datetime(2099, 1, 1, tzinfo=UTC),
        status=AssignmentStatus.PUBLISHED,
        created_by=unrelated_teacher.id,
    )
    postgres_session.add(unrelated_assignment)
    await postgres_session.commit()
    unrelated_assignment_id = unrelated_assignment.id
    database_url = postgres_session.bind.url.render_as_string(hide_password=False)
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": database_url,
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )

    reset_process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.seed_demo",
        "--reset-demo",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    reset_stdout, reset_stderr = await reset_process.communicate()

    assert reset_process.returncode == 0, reset_stderr.decode()
    assert reset_stdout == b'{"reset":true}\n'
    assert reset_stderr == b""
    postgres_session.expire_all()
    assert await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID) is None
    assert await postgres_session.get(Assignment, unrelated_assignment_id) is not None
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 6

    seed_process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.seed_demo",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    seed_stdout, seed_stderr = await seed_process.communicate()

    assert seed_process.returncode == 0, seed_stderr.decode()
    assert json.loads(seed_stdout)["assignment_id"] == str(DEMO_ASSIGNMENT_ID)
    assert seed_stderr == b""
    postgres_session.expire_all()
    assert await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID) is not None
    assert await postgres_session.get(Assignment, unrelated_assignment_id) is not None
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 6


@pytest.mark.asyncio
async def test_acceptance_orchestration_requires_an_explicit_engine(postgres_session) -> None:
    with pytest.raises(TypeError, match="data_engine must be an AsyncEngine"):
        await seed_acceptance_demo(postgres_session, postgres_session.bind)
    with pytest.raises(TypeError, match="lock_engine must be an AsyncEngine"):
        await reset_acceptance_demo(postgres_session.bind, postgres_session)


@pytest.mark.asyncio
async def test_orchestration_rejects_distinct_wrappers_that_share_a_pool() -> None:
    import scripts.seed_demo as seed_module

    data_engine = create_async_engine("postgresql+asyncpg://unused/unused")
    shared_pool_wrapper = data_engine.execution_options(test_wrapper=True)
    try:
        with pytest.raises(ValueError, match="data_engine and lock_engine must not share a pool"):
            seed_module._validate_orchestration_engines(data_engine, shared_pool_wrapper)
    finally:
        await data_engine.dispose()


@pytest.mark.asyncio
async def test_orchestration_rejects_independent_queue_pool_lock_engine() -> None:
    import scripts.seed_demo as seed_module

    data_engine = create_async_engine("postgresql+asyncpg://unused/unused")
    queue_pool_lock_engine = create_async_engine("postgresql+asyncpg://unused/unused")
    try:
        with pytest.raises(TypeError, match="lock_engine must use NullPool"):
            seed_module._validate_orchestration_engines(data_engine, queue_pool_lock_engine)
    finally:
        await data_engine.dispose()
        await queue_pool_lock_engine.dispose()


@pytest.mark.asyncio
async def test_orchestration_accepts_distinct_null_pool_lock_engine() -> None:
    import scripts.seed_demo as seed_module

    data_engine = create_async_engine("postgresql+asyncpg://unused/unused")
    lock_engine = create_async_engine(
        "postgresql+asyncpg://unused/unused",
        poolclass=NullPool,
    )
    try:
        seed_module._validate_orchestration_engines(data_engine, lock_engine)
    finally:
        await data_engine.dispose()
        await lock_engine.dispose()


@pytest.mark.asyncio
async def test_acceptance_uses_supplied_configured_lock_engine_without_disposal(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.seed_demo as seed_module

    data_engine = create_async_engine(
        postgres_session.bind.url,
        pool_size=1,
        max_overflow=1,
        pool_timeout=1,
        connect_args={
            "timeout": 1,
            "server_settings": {
                "application_name": "provided-demo-data",
                "statement_timeout": "5000",
            },
        },
    )
    lock_engine = create_async_engine(
        postgres_session.bind.url,
        poolclass=NullPool,
        connect_args={
            "timeout": 1,
            "server_settings": {
                "application_name": "provided-demo-lock",
                "statement_timeout": "5000",
            },
        },
    )
    original_evaluate = MockEvaluationProvider.evaluate
    original_dispose = AsyncEngine.dispose
    disposed: list[AsyncEngine] = []
    data_statements: list[str] = []
    lock_statements: list[str] = []
    observed_configured_engines = False

    def record_data_statement(connection, cursor, statement, parameters, context, many):
        del connection, cursor, parameters, context, many
        data_statements.append(statement)

    def record_lock_statement(connection, cursor, statement, parameters, context, many):
        del connection, cursor, parameters, context, many
        lock_statements.append(statement)

    async def observe_dispose(self, *args, **kwargs):
        disposed.append(self)
        return await original_dispose(self, *args, **kwargs)

    async def observe_provider(self, request):
        nonlocal observed_configured_engines
        async with postgres_session.bind.connect() as connection:
            application_counts = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT application_name, count(*) FROM pg_stat_activity "
                            "WHERE application_name IN "
                            "('provided-demo-data', 'provided-demo-lock') "
                            "GROUP BY application_name"
                        )
                    )
                ).all()
            )
            lock_advisory_locks = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity AS activity "
                    "JOIN pg_locks AS lock ON lock.pid = activity.pid "
                    "WHERE activity.application_name = 'provided-demo-lock' "
                    "AND lock.locktype = 'advisory' AND lock.granted"
                )
            )
        assert application_counts["provided-demo-data"] >= 1
        assert application_counts["provided-demo-lock"] >= 1
        assert lock_advisory_locks >= 1
        observed_configured_engines = True
        return await original_evaluate(self, request)

    event.listen(data_engine.sync_engine, "before_cursor_execute", record_data_statement)
    event.listen(lock_engine.sync_engine, "before_cursor_execute", record_lock_statement)
    monkeypatch.setattr(AsyncEngine, "dispose", observe_dispose)
    monkeypatch.setattr(MockEvaluationProvider, "evaluate", observe_provider)
    try:
        seeded = await seed_acceptance_demo(data_engine, lock_engine)
        assert len(seeded.scenarios) == 3
        assert observed_configured_engines is True
        assert any("INSERT INTO evaluation_reports" in statement for statement in data_statements)
        assert not any(
            "evaluation_reports" in statement or "evaluation_jobs" in statement
            for statement in lock_statements
        )
        assert disposed == []

        async def fail_scenario(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("planned orchestration failure")

        monkeypatch.setattr(seed_module, "_seed_scenario", fail_scenario)
        with pytest.raises(RuntimeError, match="planned orchestration failure"):
            await seed_acceptance_demo(data_engine, lock_engine)
        assert disposed == []
        with pytest.raises(ValueError, match="data_engine and lock_engine must be distinct"):
            await seed_acceptance_demo(data_engine, data_engine)
        with pytest.raises(ValueError, match="data_engine and lock_engine must be distinct"):
            await reset_acceptance_demo(data_engine, data_engine)
        assert disposed == []
    finally:
        event.remove(data_engine.sync_engine, "before_cursor_execute", record_data_statement)
        event.remove(lock_engine.sync_engine, "before_cursor_execute", record_lock_statement)
        monkeypatch.setattr(AsyncEngine, "dispose", original_dispose)
        await data_engine.dispose()
        await lock_engine.dispose()


@pytest.mark.asyncio
async def test_acceptance_orchestration_completes_with_one_connection_pool(
    postgres_session,
) -> None:
    engine = create_async_engine(
        postgres_session.bind.url,
        pool_size=1,
        max_overflow=1,
        pool_timeout=1,
    )
    lock_engine = _new_test_lock_engine(engine)
    try:
        seeded = await asyncio.wait_for(
            seed_acceptance_demo(engine, lock_engine),
            timeout=10,
        )
        assert len(seeded.scenarios) == 3
        await asyncio.wait_for(
            reset_acceptance_demo(engine, lock_engine),
            timeout=10,
        )
    finally:
        try:
            await engine.dispose()
        finally:
            await lock_engine.dispose()


@pytest.mark.asyncio
async def test_acceptance_seed_uses_real_provider_path_and_is_repeatably_idempotent(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_keys: list[str | None] = []
    evaluate = MockEvaluationProvider.evaluate

    async def observe_provider(self, request):
        fixture_keys.append(request.fixture_key)
        return await evaluate(self, request)

    monkeypatch.setattr(MockEvaluationProvider, "evaluate", observe_provider)

    first = await _seed_acceptance(postgres_session.bind)
    second = await _seed_acceptance(postgres_session.bind)

    assert first == second
    assert first.assignment_id == DEMO_ASSIGNMENT_ID
    assert tuple(item.fixture_key for item in first.scenarios) == (
        "complete",
        "partial",
        "incorrect",
    )
    assert tuple((item.score, item.grade) for item in first.scenarios) == (
        (94, "A"),
        (72, "C"),
        (35, "D"),
    )
    assert fixture_keys == ["complete", "partial", "incorrect"]

    postgres_session.expire_all()
    submissions = (
        await postgres_session.scalars(
            select(Submission).where(Submission.assignment_id == first.assignment_id)
        )
    ).all()
    reports = (await postgres_session.scalars(select(EvaluationReport))).all()
    jobs = (await postgres_session.scalars(select(EvaluationJob))).all()
    assert len(submissions) == len(reports) == len(jobs) == 3
    assert all(item.version == 1 for item in submissions)
    assert all(item.status is SubmissionStatus.SUBMITTED for item in submissions)
    assert {item.id for item in submissions} == {item.submission_id for item in first.scenarios}
    assert {item.id for item in reports} == {item.report_id for item in first.scenarios}
    assert {(item.score, item.grade.value) for item in reports} == {
        (94, "A"),
        (72, "C"),
        (35, "D"),
    }
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 6


@pytest.mark.asyncio
async def test_acceptance_seed_recovers_after_a_committed_scenario(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.seed_demo as seed_module

    seed_scenario = seed_module._seed_scenario
    attempted = 0

    async def fail_after_first(*args, **kwargs):
        nonlocal attempted
        attempted += 1
        if attempted == 2:
            raise RuntimeError("planned interruption")
        return await seed_scenario(*args, **kwargs)

    monkeypatch.setattr(seed_module, "_seed_scenario", fail_after_first)
    with pytest.raises(RuntimeError, match="planned interruption"):
        await _seed_acceptance(postgres_session.bind)

    postgres_session.expire_all()
    partial_submission_ids = set(
        (
            await postgres_session.scalars(
                select(Submission.id).where(Submission.assignment_id == DEMO_ASSIGNMENT_ID)
            )
        ).all()
    )
    assert len(partial_submission_ids) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 1

    monkeypatch.setattr(seed_module, "_seed_scenario", seed_scenario)
    recovered = await _seed_acceptance(postgres_session.bind)

    assert partial_submission_ids <= {item.submission_id for item in recovered.scenarios}
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 3


@pytest.mark.asyncio
async def test_acceptance_seed_replaces_verified_failed_initial_job_without_garbage(
    postgres_session,
) -> None:
    import scripts.seed_demo as seed_module

    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    teacher = await postgres_session.get(User, DEMO_USER_IDS["teacher"])
    student = await postgres_session.get(User, DEMO_USER_IDS["student1"])
    assert assignment is not None and teacher is not None and student is not None
    submission = await seed_module._ensure_scenario_submission(
        postgres_session,
        assignment=assignment,
        student=student,
        scenario=DEMO_SCENARIOS[0],
    )
    submission_id = submission.id
    await postgres_session.commit()

    failing = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        mock_fixture_key="missing-demo-fixture",
    )
    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await failing.evaluate_now(submission_id, JobReason.INITIAL)

    postgres_session.expire_all()
    failed_job = await postgres_session.scalar(
        select(EvaluationJob).where(EvaluationJob.submission_id == submission_id)
    )
    assert failed_job is not None
    assert failed_job.status is JobStatus.FAILED
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 1

    result = await _seed_acceptance(postgres_session.bind)

    assert tuple((item.score, item.grade) for item in result.scenarios) == (
        (94, "A"),
        (72, "C"),
        (35, "D"),
    )
    jobs = (await postgres_session.scalars(select(EvaluationJob))).all()
    assert len(jobs) == 3
    assert all(job.status is JobStatus.SUCCEEDED for job in jobs)
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 6


@pytest.mark.asyncio
async def test_acceptance_seed_replaces_real_cancelled_initial_job_without_garbage(
    postgres_session,
) -> None:
    import scripts.seed_demo as seed_module

    provider_started = asyncio.Event()
    provider_never_finishes = asyncio.Event()

    class BlockingMockProvider:
        @property
        def identity(self) -> ProviderIdentity:
            return ProviderIdentity(provider="mock", model="fixture-v1")

        async def evaluate(self, request):
            provider_started.set()
            await provider_never_finishes.wait()
            raise AssertionError("cancelled provider must not finish")

    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    teacher = await postgres_session.get(User, DEMO_USER_IDS["teacher"])
    student = await postgres_session.get(User, DEMO_USER_IDS["student1"])
    assert assignment is not None and teacher is not None and student is not None
    submission = await seed_module._ensure_scenario_submission(
        postgres_session,
        assignment=assignment,
        student=student,
        scenario=DEMO_SCENARIOS[0],
    )
    submission_id = submission.id
    teacher_id = teacher.id
    await postgres_session.commit()

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(BlockingMockProvider()),
        requested_by=teacher_id,
        dispatch=None,
        mock_fixture_key="complete",
    )
    evaluation = asyncio.create_task(service.evaluate_now(submission_id, JobReason.INITIAL))
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    evaluation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await evaluation

    postgres_session.expire_all()
    cancelled_job = await postgres_session.scalar(
        select(EvaluationJob).where(EvaluationJob.submission_id == submission_id)
    )
    assert cancelled_job is not None
    cancelled_job_id = cancelled_job.id
    assert cancelled_job.status is JobStatus.CANCELLED
    assert cancelled_job.reason is JobReason.INITIAL
    assert cancelled_job.requested_by == teacher_id
    assert cancelled_job.source_report_id is None
    assert cancelled_job.provider == "mock"
    assert cancelled_job.model == "fixture-v1"
    assert cancelled_job.execution_token is None
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    cancelled_outbox = (
        await postgres_session.scalars(
            select(EvaluationOutbox).where(EvaluationOutbox.job_id == cancelled_job_id)
        )
    ).all()
    assert len(cancelled_outbox) == 1
    assert cancelled_outbox[0].kind == "dispatch"
    assert cancelled_outbox[0].report_id is None
    assert cancelled_outbox[0].attempt_count == 0
    assert cancelled_outbox[0].delivered_at is None
    assert cancelled_outbox[0].failed_at is None
    assert cancelled_outbox[0].delivery_ref is None
    assert cancelled_outbox[0].last_error_type is None
    assert cancelled_outbox[0].last_error_summary is None
    assert cancelled_outbox[0].claim_token is None
    assert cancelled_outbox[0].claim_expires_at is None

    result = await _seed_acceptance(postgres_session.bind)

    assert tuple((item.score, item.grade) for item in result.scenarios) == (
        (94, "A"),
        (72, "C"),
        (35, "D"),
    )
    jobs = (await postgres_session.scalars(select(EvaluationJob))).all()
    assert len(jobs) == 3
    assert cancelled_job_id not in {job.id for job in jobs}
    assert all(job.status is JobStatus.SUCCEEDED for job in jobs)
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 3
    outbox = (await postgres_session.scalars(select(EvaluationOutbox))).all()
    assert len(outbox) == 6
    assert [row.kind for row in outbox].count("dispatch") == 3
    assert [row.kind for row in outbox].count("notification") == 3


@pytest.mark.asyncio
async def test_acceptance_seed_preserves_non_stale_running_job(postgres_session) -> None:
    import scripts.seed_demo as seed_module

    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    teacher = await postgres_session.get(User, DEMO_USER_IDS["teacher"])
    student = await postgres_session.get(User, DEMO_USER_IDS["student1"])
    assert assignment is not None and teacher is not None and student is not None
    submission = await seed_module._ensure_scenario_submission(
        postgres_session,
        assignment=assignment,
        student=student,
        scenario=DEMO_SCENARIOS[0],
    )
    submission_id = submission.id
    await postgres_session.commit()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        mock_fixture_key="complete",
    )
    job = await service.request(submission_id, JobReason.INITIAL)
    persisted_job = await postgres_session.get(EvaluationJob, job.id)
    assert persisted_job is not None
    persisted_job.status = JobStatus.RUNNING
    persisted_job.started_at = datetime.now(UTC)
    persisted_job.execution_token = uuid.uuid4()
    persisted_job.execution_generation = 1
    await postgres_session.commit()

    with pytest.raises(EvaluationInProgress, match="already running"):
        await _seed_acceptance(postgres_session.bind)

    postgres_session.expire_all()
    preserved = await postgres_session.get(EvaluationJob, job.id)
    assert preserved is not None
    assert preserved.status is JobStatus.RUNNING
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 1


@pytest.mark.asyncio
async def test_reset_deletes_only_verified_demo_assignment_tree_and_preserves_users(
    postgres_session,
) -> None:
    seeded = await _seed_acceptance(postgres_session.bind)
    unrelated_teacher = User(
        username="unrelated-teacher",
        display_name="Unrelated",
        role=Role.TEACHER,
        password_hash=hash_password("unrelated"),
        is_active=True,
    )
    postgres_session.add(unrelated_teacher)
    await postgres_session.flush([unrelated_teacher])
    unrelated_assignment = Assignment(
        code="HW-9999",
        title="Unrelated",
        question="Preserve me",
        notes="",
        rubric={"owner": "unrelated"},
        due_at=datetime(2099, 1, 1, tzinfo=UTC),
        status=AssignmentStatus.PUBLISHED,
        created_by=unrelated_teacher.id,
    )
    postgres_session.add(unrelated_assignment)
    await postgres_session.commit()
    unrelated_id = unrelated_assignment.id

    await _reset_acceptance(postgres_session.bind)

    postgres_session.expire_all()
    assert await postgres_session.get(Assignment, seeded.assignment_id) is None
    assert await postgres_session.get(Assignment, unrelated_id) is not None
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 6


@pytest.mark.asyncio
async def test_reset_deletes_complete_real_review_lineages_and_reseeds(
    postgres_session,
) -> None:
    seeded = await _seed_acceptance(postgres_session.bind)
    teacher_id = DEMO_USER_IDS["teacher"]
    initial_a = await postgres_session.get(EvaluationReport, seeded.scenarios[0].report_id)
    initial_b = await postgres_session.get(EvaluationReport, seeded.scenarios[1].report_id)
    initial_c = await postgres_session.get(EvaluationReport, seeded.scenarios[2].report_id)
    assert initial_a is not None and initial_b is not None and initial_c is not None
    initial_a_id = initial_a.id
    initial_b_id = initial_b.id
    initial_c_id = initial_c.id

    reviews = ReviewService(postgres_session)
    modified = await reviews.modify(
        initial_a.id,
        teacher_id,
        ReportPatch(score=88, comment="teacher correction"),
    )
    modified_id = modified.id
    retry_a_service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher_id,
        dispatch=None,
        mock_fixture_key="complete",
    )
    retry_a_job = await reviews.reevaluate(
        modified.id,
        teacher_id,
        ReevaluateRequest(comment="retry modified report"),
        retry_a_service,
    )
    retry_a_job_id = retry_a_job.id
    retry_a_report = await retry_a_service.execute_job(retry_a_job.id)
    retry_a_report_id = retry_a_report.id

    confirmed = await reviews.confirm(
        initial_b.id,
        teacher_id,
        ConfirmRequest(comment="confirmed before retry"),
    )
    retry_b_service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher_id,
        dispatch=None,
        mock_fixture_key="partial",
    )
    retry_b_job = await reviews.reevaluate(
        confirmed.id,
        teacher_id,
        ReevaluateRequest(comment="retry confirmed report"),
        retry_b_service,
    )
    retry_b_job_id = retry_b_job.id
    retry_b_report = await retry_b_service.execute_job(retry_b_job.id)
    retry_b_report_id = retry_b_report.id

    failed_retry_service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher_id,
        dispatch=None,
        mock_fixture_key="missing-demo-fixture",
    )
    failed_retry_job = await reviews.reevaluate(
        initial_c.id,
        teacher_id,
        ReevaluateRequest(comment="capture failed retry evidence"),
        failed_retry_service,
    )
    failed_retry_job_id = failed_retry_job.id
    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await failed_retry_service.execute_job(failed_retry_job.id)

    postgres_session.expire_all()
    stored_initial_a = await postgres_session.get(EvaluationReport, initial_a_id)
    stored_modified = await postgres_session.get(EvaluationReport, modified_id)
    stored_confirmed = await postgres_session.get(EvaluationReport, initial_b_id)
    stored_retry_a = await postgres_session.get(EvaluationReport, retry_a_report_id)
    stored_retry_b = await postgres_session.get(EvaluationReport, retry_b_report_id)
    stored_retry_a_job = await postgres_session.get(EvaluationJob, retry_a_job_id)
    stored_retry_b_job = await postgres_session.get(EvaluationJob, retry_b_job_id)
    stored_failed_job = await postgres_session.get(EvaluationJob, failed_retry_job_id)
    assert stored_initial_a is not None and stored_modified is not None
    assert stored_confirmed is not None and stored_retry_a is not None
    assert stored_retry_b is not None and stored_failed_job is not None
    assert stored_retry_a_job is not None and stored_retry_b_job is not None
    assert stored_initial_a.review_status is ReviewStatus.SUPERSEDED
    assert stored_modified.source_report_id == stored_initial_a.id
    assert stored_retry_a.source_report_id is None
    assert stored_retry_a_job.source_report_id == stored_modified.id
    assert stored_confirmed.review_status is ReviewStatus.SUPERSEDED
    assert stored_retry_b.source_report_id is None
    assert stored_retry_b_job.source_report_id == stored_confirmed.id
    assert stored_failed_job.status is JobStatus.FAILED
    assert stored_failed_job.source_report_id == initial_c_id
    actions = (await postgres_session.scalars(select(ReviewAction))).all()
    assert {action.action for action in actions} == {
        ReviewActionType.CONFIRM,
        ReviewActionType.MODIFY,
        ReviewActionType.REEVALUATE,
    }
    assert len(actions) == 5
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 6
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 6
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 6
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 11

    unrelated_teacher = User(
        username="lineage-unrelated-teacher",
        display_name="Unrelated",
        role=Role.TEACHER,
        password_hash=hash_password("unrelated"),
        is_active=True,
    )
    postgres_session.add(unrelated_teacher)
    await postgres_session.flush([unrelated_teacher])
    unrelated_assignment = Assignment(
        code="HW-9999",
        title="Unrelated",
        question="Preserve me",
        notes="",
        rubric={"owner": "unrelated"},
        due_at=datetime(2099, 1, 1, tzinfo=UTC),
        status=AssignmentStatus.PUBLISHED,
        created_by=unrelated_teacher.id,
    )
    postgres_session.add(unrelated_assignment)
    await postgres_session.commit()
    unrelated_assignment_id = unrelated_assignment.id

    await _reset_acceptance(postgres_session.bind)

    postgres_session.expire_all()
    assert await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID) is None
    assert await postgres_session.get(Assignment, unrelated_assignment_id) is not None
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(ReviewAction)) == 0
    trigger_states = (
        await postgres_session.execute(
            text(
                "SELECT tgname, tgenabled FROM pg_trigger "
                "WHERE tgname IN ('trg_review_actions_append_only', "
                "'trg_audit_logs_append_only', 'trg_evaluation_reports_immutable')"
            )
        )
    ).all()
    assert {name for name, _ in trigger_states} == {
        "trg_review_actions_append_only",
        "trg_audit_logs_append_only",
        "trg_evaluation_reports_immutable",
    }
    assert all(state in {"O", b"O"} for _, state in trigger_states)

    rebuilt = await _seed_acceptance(postgres_session.bind)
    assert rebuilt.assignment_id == DEMO_ASSIGNMENT_ID
    assert len(rebuilt.scenarios) == 3
    assert await postgres_session.get(Assignment, unrelated_assignment_id) is not None
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 3


@pytest.mark.asyncio
async def test_reset_refuses_when_demo_marker_is_missing(postgres_session) -> None:
    await _seed_acceptance(postgres_session.bind)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    assert assignment is not None
    assignment.rubric = {"required_points": ["tampered"]}
    await postgres_session.commit()

    with pytest.raises(DemoSeedConflict, match="ownership could not be verified"):
        await _reset_acceptance(postgres_session.bind)

    assert await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID) is not None
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 3


@pytest.mark.asyncio
async def test_reset_refuses_spoofed_marker_on_non_manifest_assignment_id(
    postgres_session,
) -> None:
    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    assert assignment is not None
    await postgres_session.delete(assignment)
    await postgres_session.flush()
    spoofed_id = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    postgres_session.add(
        Assignment(
            id=spoofed_id,
            code="HW-0001",
            created_at=datetime(2026, 7, 25, tzinfo=UTC),
            title="图的最短路径",
            question="说明 Dijkstra 算法的核心思想，并分析复杂度。",
            notes="允许使用伪代码。",
            rubric=build_demo_rubric(),
            due_at=datetime(2099, 1, 1, tzinfo=UTC),
            status=AssignmentStatus.PUBLISHED,
            created_by=DEMO_USER_IDS["teacher"],
            published_at=datetime(2026, 7, 25, 0, 1, tzinfo=UTC),
        )
    )
    await postgres_session.commit()

    with pytest.raises(DemoSeedConflict, match="ownership could not be verified"):
        await _reset_acceptance(postgres_session.bind)

    assert await postgres_session.get(Assignment, spoofed_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["assignment", "submission", "student-role"])
async def test_reset_refuses_tampered_manifest_records(postgres_session, tamper: str) -> None:
    seeded = await _seed_acceptance(postgres_session.bind)
    if tamper == "assignment":
        assignment = await postgres_session.get(Assignment, seeded.assignment_id)
        assert assignment is not None
        assignment.title = "伪造的演示作业"
    elif tamper == "submission":
        submission = await postgres_session.get(Submission, seeded.scenarios[0].submission_id)
        assert submission is not None
        submission.content_text = "tampered answer"
    else:
        student = await postgres_session.get(User, DEMO_USER_IDS["student2"])
        assert student is not None
        student.role = Role.TEACHER
    await postgres_session.commit()

    with pytest.raises(DemoSeedConflict, match="manifest|ownership"):
        await _reset_acceptance(postgres_session.bind)

    assert await postgres_session.get(Assignment, seeded.assignment_id) is not None


@pytest.mark.asyncio
async def test_failed_job_recovery_rolls_back_trigger_disable_on_delete_error(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.seed_demo as seed_module

    await seed_demo(postgres_session)
    assignment = await postgres_session.get(Assignment, DEMO_ASSIGNMENT_ID)
    teacher = await postgres_session.get(User, DEMO_USER_IDS["teacher"])
    student = await postgres_session.get(User, DEMO_USER_IDS["student1"])
    assert assignment is not None and teacher is not None and student is not None
    submission = await seed_module._ensure_scenario_submission(
        postgres_session,
        assignment=assignment,
        student=student,
        scenario=DEMO_SCENARIOS[0],
    )
    submission_id = submission.id
    await postgres_session.commit()
    failing = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        mock_fixture_key="missing-demo-fixture",
    )
    with pytest.raises(EvaluationJobFailed):
        await failing.evaluate_now(submission_id, JobReason.INITIAL)

    execute = AsyncSession.execute

    async def fail_audit_delete(self, statement, *args, **kwargs):
        if str(statement).startswith("DELETE FROM audit_logs"):
            raise RuntimeError("planned cleanup failure")
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_audit_delete)
    with pytest.raises(RuntimeError, match="planned cleanup failure"):
        await _seed_acceptance(postgres_session.bind)
    monkeypatch.setattr(AsyncSession, "execute", execute)

    trigger_state = await postgres_session.scalar(
        text("SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_audit_logs_append_only'")
    )
    assert trigger_state in {"O", b"O"}
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 1


@pytest.mark.asyncio
async def test_reset_rolls_back_trigger_disable_on_report_delete_error(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_acceptance(postgres_session.bind)
    execute = AsyncSession.execute

    async def fail_report_delete(self, statement, *args, **kwargs):
        if str(statement).startswith("DELETE FROM evaluation_reports"):
            raise RuntimeError("planned reset failure")
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_report_delete)
    with pytest.raises(RuntimeError, match="planned reset failure"):
        await _reset_acceptance(postgres_session.bind)
    monkeypatch.setattr(AsyncSession, "execute", execute)

    trigger_state = await postgres_session.scalar(
        text("SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_evaluation_reports_immutable'")
    )
    assert trigger_state in {"O", b"O"}
    assert await postgres_session.get(Assignment, seeded.assignment_id) is not None
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 3


@pytest.mark.asyncio
async def test_reset_cli_refuses_production_before_database_access() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql+asyncpg://secret:password@127.0.0.1:1/production",
            "JWT_SECRET": "x" * 40,
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.seed_demo",
        "--reset-demo",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode != 0
    output = (stdout + stderr).decode()
    assert "disabled outside development and test" in output
    assert "ConnectionRefused" not in output
    assert "secret" not in output
    assert "password" not in output


@pytest.mark.asyncio
async def test_two_concurrent_acceptance_seeds_converge(postgres_session) -> None:
    async def run_seed():
        return await _seed_acceptance(postgres_session.bind)

    first, second = await asyncio.gather(run_seed(), run_seed())

    assert first == second
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 3
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 3


@pytest.mark.asyncio
async def test_acceptance_seed_refuses_fixed_submission_id_collision_without_overwrite(
    postgres_session,
) -> None:
    import scripts.seed_demo as seed_module

    await seed_demo(postgres_session)
    unrelated_teacher = User(
        username="collision-teacher",
        display_name="Collision",
        role=Role.TEACHER,
        password_hash=hash_password("collision"),
        is_active=True,
    )
    postgres_session.add(unrelated_teacher)
    await postgres_session.flush([unrelated_teacher])
    unrelated_assignment = Assignment(
        code="HW-9999",
        title="Unrelated",
        question="Do not overwrite",
        notes="",
        rubric={},
        due_at=datetime(2099, 1, 1, tzinfo=UTC),
        status=AssignmentStatus.PUBLISHED,
        created_by=unrelated_teacher.id,
    )
    postgres_session.add(unrelated_assignment)
    await postgres_session.flush([unrelated_assignment])
    student = await postgres_session.scalar(select(User).where(User.username == "student1"))
    assert student is not None
    colliding_id = seed_module._submission_id(DEMO_ASSIGNMENT_ID, DEMO_SCENARIOS[0])
    unrelated_submission = Submission(
        id=colliding_id,
        assignment_id=unrelated_assignment.id,
        student_id=student.id,
        version=1,
        content_type=SubmissionContentType.MARKDOWN,
        content_text="unrelated answer",
        content_json=None,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
        source=SubmissionSource.WEB,
    )
    postgres_session.add(unrelated_submission)
    await postgres_session.commit()
    unrelated_assignment_id = unrelated_assignment.id

    with pytest.raises(DemoSeedConflict, match="fixed demo submission ID"):
        await _seed_acceptance(postgres_session.bind)

    postgres_session.expire_all()
    preserved = await postgres_session.get(Submission, colliding_id)
    assert preserved is not None
    assert preserved.assignment_id == unrelated_assignment_id
    assert preserved.content_text == "unrelated answer"
