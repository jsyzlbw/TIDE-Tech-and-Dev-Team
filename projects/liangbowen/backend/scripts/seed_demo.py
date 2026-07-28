from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.assignments.model import Assignment
from app.assignments.service import acquire_assignment_code_lock
from app.audit.model import AuditLog
from app.auth.security import hash_password, verify_password
from app.core.config import get_settings
from app.db.migration_gate import migration_gated_sessionmaker
from app.db.types import (
    AssignmentStatus,
    Grade,
    Role,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)
from app.demo.scenarios import DEMO_SCENARIOS, DemoScenario
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationService, evaluation_key
from app.evaluations.types import JobReason, JobStatus, ReportOrigin
from app.reviews.model import ReviewAction
from app.submissions.model import Submission
from app.users.model import User

logger = logging.getLogger(__name__)

DEMO_USER_IDS = {
    "admin": uuid.UUID("00000000-0000-4000-8000-000000000005"),
    "teacher": uuid.UUID("00000000-0000-4000-8000-000000000001"),
    "student1": uuid.UUID("00000000-0000-4000-8000-000000000002"),
    "student2": uuid.UUID("00000000-0000-4000-8000-000000000003"),
    "student3": uuid.UUID("00000000-0000-4000-8000-000000000004"),
}
DEMO_ASSIGNMENT_ID = uuid.UUID("00000000-0000-4000-8000-000000000101")
DEMO_CREATED_AT = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
DEMO_PUBLISHED_AT = datetime(2026, 7, 25, 0, 1, tzinfo=UTC)
DEMO_DUE_AT = datetime(2099, 1, 1, 0, 0, tzinfo=UTC)
DEMO_MARKER_KEY = "ai-grading-acceptance-v1"
DEMO_SEED_LOCK_KEY = 8_408_044_677_639_071_103
DEMO_SUBMISSION_NAMESPACE = uuid.UUID("2c915ac1-e00b-43ad-8a9f-f025a902bf30")
DEMO_ASSIGNMENT_TITLE = "图的最短路径"
DEMO_ASSIGNMENT_QUESTION = "说明 Dijkstra 算法的核心思想，并分析复杂度。"
DEMO_ASSIGNMENT_NOTES = "允许使用伪代码。"

DEMO_USERS = (
    ("admin", "系统管理员", Role.ADMIN, "Admin123!Secure"),
    ("teacher", "演示教师", Role.TEACHER, "Teacher123!"),
    ("student1", "学生甲", Role.STUDENT, "Student123!"),
    ("student2", "学生乙", Role.STUDENT, "Student123!"),
    ("student3", "学生丙", Role.STUDENT, "Student123!"),
)


@dataclass(frozen=True, slots=True)
class DemoRubricSpec:
    required_points: tuple[str, ...]
    grading_notes: str
    marker_key: str


_DEMO_RUBRIC_SPEC = DemoRubricSpec(
    required_points=(
        "适用非负权图",
        "贪心选择未确定的最短距离顶点",
        "进行松弛操作",
        "给出复杂度",
    ),
    grading_notes="若未说明非负权限制，正确性不得评为完全正确。",
    marker_key=DEMO_MARKER_KEY,
)
DEMO_RUBRIC = {
    "required_points": list(_DEMO_RUBRIC_SPEC.required_points),
    "grading_notes": _DEMO_RUBRIC_SPEC.grading_notes,
}


def build_demo_rubric() -> dict[str, object]:
    return {
        "required_points": list(_DEMO_RUBRIC_SPEC.required_points),
        "grading_notes": _DEMO_RUBRIC_SPEC.grading_notes,
        "metadata": {"demo_key": _DEMO_RUBRIC_SPEC.marker_key},
    }


def build_legacy_demo_rubric() -> dict[str, object]:
    return {
        "required_points": list(_DEMO_RUBRIC_SPEC.required_points),
        "grading_notes": _DEMO_RUBRIC_SPEC.grading_notes,
    }


@dataclass(frozen=True)
class SeedSummary:
    users: int
    assignments: int


@dataclass(frozen=True, slots=True)
class ScenarioSeedSummary:
    fixture_key: str
    submission_id: uuid.UUID
    report_id: uuid.UUID
    score: int
    grade: Grade


@dataclass(frozen=True, slots=True)
class AcceptanceSeedSummary:
    users: int
    assignments: int
    assignment_id: uuid.UUID
    scenarios: tuple[ScenarioSeedSummary, ...]


class DemoSeedError(RuntimeError):
    """Base class for safe, user-actionable demo seed failures."""


class DemoSeedEnvironmentError(DemoSeedError):
    """Raised when demo credentials are forbidden in the current environment."""


class DemoSeedConflict(DemoSeedError):
    """Raised when deterministic demo identifiers belong to unrelated records."""


@asynccontextmanager
async def _demo_seed_lock(engine: AsyncEngine) -> AsyncIterator[None]:
    connection = await engine.connect()
    acquired = False
    invalidate = False
    try:
        await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": DEMO_SEED_LOCK_KEY})
        acquired = True
        try:
            await connection.commit()
        except BaseException as exc:
            invalidate = True
            logger.warning(
                "demo seed advisory lock commit failed error_type=%s",
                type(exc).__name__,
            )
            raise
        yield
    finally:
        try:
            if acquired and not connection.closed:
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": DEMO_SEED_LOCK_KEY},
                    )
                    await connection.commit()
                except BaseException as exc:  # never pool a possibly locked session
                    logger.warning(
                        "demo seed advisory unlock failed error_type=%s",
                        type(exc).__name__,
                    )
                    invalidate = True
                    if not isinstance(exc, Exception):
                        raise
        finally:
            try:
                if invalidate and not connection.closed:
                    await connection.invalidate()
            finally:
                await connection.close()


@asynccontextmanager
async def _demo_orchestration_lock(lock_engine: AsyncEngine) -> AsyncIterator[None]:
    async with _demo_seed_lock(lock_engine):
        yield


def _require_safe_environment() -> None:
    if get_settings().app_env not in {"development", "test"}:
        raise DemoSeedEnvironmentError("demo seed is disabled outside development and test")


def _password_matches(password: str, password_hash: str) -> bool:
    try:
        return verify_password(password, password_hash)
    except (TypeError, ValueError):
        return False


async def seed_demo(session: AsyncSession) -> SeedSummary:
    """Upsert deterministic demo records without owning the caller's transaction."""
    _require_safe_environment()
    users: dict[str, User] = {}
    with session.no_autoflush:
        await acquire_assignment_code_lock(session)

        fixed_id_users = (
            await session.scalars(select(User).where(User.id.in_(tuple(DEMO_USER_IDS.values()))))
        ).all()
        expected_username_by_id = {user_id: username for username, user_id in DEMO_USER_IDS.items()}
        if any(user.username != expected_username_by_id[user.id] for user in fixed_id_users):
            raise DemoSeedConflict(
                "a fixed demo user ID belongs to another username; "
                "resolve the conflict before seeding"
            )

        username_users = (
            await session.scalars(select(User).where(User.username.in_(tuple(DEMO_USER_IDS))))
        ).all()
        expected_id_by_username = DEMO_USER_IDS
        if any(user.id != expected_id_by_username[user.username] for user in username_users):
            raise DemoSeedConflict(
                "a demo username belongs to another user ID; resolve the conflict before seeding"
            )

        fixed_id_assignment = await session.get(Assignment, DEMO_ASSIGNMENT_ID)
        legacy_assignment = (
            fixed_id_assignment
            if fixed_id_assignment is not None
            and _assignment_matches_legacy_manifest(fixed_id_assignment)
            else None
        )
        if (
            fixed_id_assignment is not None
            and not _assignment_matches_reset_manifest(fixed_id_assignment)
            and legacy_assignment is None
        ):
            raise DemoSeedConflict(
                "the fixed demo assignment ID does not match the manifest; "
                "resolve the conflict before seeding"
            )

        assignment = await session.scalar(select(Assignment).where(Assignment.code == "HW-0001"))
        if assignment is not None and assignment.id != DEMO_ASSIGNMENT_ID:
            raise DemoSeedConflict(
                "HW-0001 belongs to a non-demo assignment; resolve the conflict before seeding"
            )

        for username, display_name, role, password in DEMO_USERS:
            user = await session.get(User, DEMO_USER_IDS[username])
            if user is None:
                user = User(
                    id=DEMO_USER_IDS[username],
                    username=username,
                    display_name=display_name,
                    role=role,
                    password_hash=hash_password(password),
                    is_active=True,
                    created_at=DEMO_CREATED_AT,
                )
                session.add(user)
            else:
                user.display_name = display_name
                user.role = role
                user.is_active = True
                if not _password_matches(password, user.password_hash):
                    user.password_hash = hash_password(password)
            users[username] = user
        await session.flush(list(users.values()))

        if assignment is None:
            assignment = Assignment(
                id=DEMO_ASSIGNMENT_ID,
                code="HW-0001",
                created_at=DEMO_CREATED_AT,
                title=DEMO_ASSIGNMENT_TITLE,
                question=DEMO_ASSIGNMENT_QUESTION,
                notes=DEMO_ASSIGNMENT_NOTES,
                rubric=build_demo_rubric(),
                due_at=DEMO_DUE_AT,
                status=AssignmentStatus.PUBLISHED,
                created_by=users["teacher"].id,
                published_at=DEMO_PUBLISHED_AT,
                mattermost_channel_id=None,
            )
            session.add(assignment)
        elif legacy_assignment is not None:
            assignment.rubric = build_demo_rubric()

        await session.flush([assignment])
        sequence_state = (
            await session.execute(text("SELECT last_value, is_called FROM assignment_code_seq"))
        ).one()
        if sequence_state.last_value == 1 and sequence_state.is_called is False:
            await session.scalar(text("SELECT nextval('assignment_code_seq')"))
    return SeedSummary(users=len(DEMO_USERS), assignments=1)


def _submission_id(assignment_id: uuid.UUID, scenario: DemoScenario) -> uuid.UUID:
    return uuid.uuid5(
        DEMO_SUBMISSION_NAMESPACE,
        f"{assignment_id}:{scenario.fixture_key}:{scenario.student_username}",
    )


def _assignment_matches_manifest_fields(assignment: Assignment) -> bool:
    return (
        assignment.id == DEMO_ASSIGNMENT_ID
        and assignment.code == "HW-0001"
        and assignment.created_by == DEMO_USER_IDS["teacher"]
        and assignment.created_at == DEMO_CREATED_AT
        and assignment.title == DEMO_ASSIGNMENT_TITLE
        and assignment.question == DEMO_ASSIGNMENT_QUESTION
        and assignment.notes == DEMO_ASSIGNMENT_NOTES
        and assignment.due_at == DEMO_DUE_AT
        and assignment.status is AssignmentStatus.PUBLISHED
        and assignment.published_at == DEMO_PUBLISHED_AT
        and assignment.mattermost_channel_id is None
    )


def _assignment_matches_reset_manifest(assignment: Assignment) -> bool:
    return _assignment_matches_manifest_fields(assignment) and assignment.rubric == (
        build_demo_rubric()
    )


def _assignment_matches_legacy_manifest(assignment: Assignment) -> bool:
    return _assignment_matches_manifest_fields(assignment) and assignment.rubric == (
        build_legacy_demo_rubric()
    )


def _users_match_reset_manifest(users: tuple[User, ...]) -> bool:
    expected_roles = {username: role for username, _, role, _ in DEMO_USERS}
    return len(users) == len(DEMO_USERS) and all(
        user.id == DEMO_USER_IDS.get(user.username)
        and user.role is expected_roles.get(user.username)
        and user.is_active is True
        for user in users
    )


def _submission_matches_manifest(
    submission: Submission,
    *,
    assignment_id: uuid.UUID,
    student_id: uuid.UUID,
    scenario: DemoScenario,
) -> bool:
    return (
        submission.id == _submission_id(assignment_id, scenario)
        and submission.assignment_id == assignment_id
        and submission.student_id == student_id
        and submission.version == 1
        and submission.content_type is SubmissionContentType.MARKDOWN
        and submission.content_text == scenario.answer
        and submission.content_json is None
        and submission.status is SubmissionStatus.SUBMITTED
        and submission.source is SubmissionSource.WEB
    )


async def _ensure_scenario_submission(
    session: AsyncSession,
    *,
    assignment: Assignment,
    student: User,
    scenario: DemoScenario,
) -> Submission:
    expected_id = _submission_id(uuid.UUID(str(assignment.id)), scenario)
    fixed_id_row = await session.get(Submission, expected_id)
    if fixed_id_row is not None and (
        fixed_id_row.assignment_id != assignment.id or fixed_id_row.student_id != student.id
    ):
        raise DemoSeedConflict(
            "a fixed demo submission ID belongs to another record; "
            "resolve the conflict before seeding"
        )
    submissions = (
        await session.scalars(
            select(Submission)
            .where(
                Submission.assignment_id == assignment.id,
                Submission.student_id == student.id,
            )
            .with_for_update()
        )
    ).all()
    if not submissions:
        submission = Submission(
            id=expected_id,
            assignment_id=assignment.id,
            student_id=student.id,
            version=1,
            content_type=SubmissionContentType.MARKDOWN,
            content_text=scenario.answer,
            content_json=None,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=DEMO_PUBLISHED_AT,
            source=SubmissionSource.WEB,
        )
        session.add(submission)
        await session.flush([submission])
        return submission
    if len(submissions) != 1:
        raise DemoSeedConflict("demo scenario has unexpected submission versions")
    submission = submissions[0]
    if not _submission_matches_manifest(
        submission,
        assignment_id=uuid.UUID(str(assignment.id)),
        student_id=uuid.UUID(str(student.id)),
        scenario=scenario,
    ):
        raise DemoSeedConflict("demo scenario submission does not match its manifest")
    return submission


async def _recover_terminal_demo_job(
    session: AsyncSession,
    *,
    submission: Submission,
    teacher_id: uuid.UUID,
) -> None:
    reports = (
        await session.scalars(
            select(EvaluationReport).where(EvaluationReport.submission_id == submission.id)
        )
    ).all()
    jobs = (
        await session.scalars(
            select(EvaluationJob)
            .where(EvaluationJob.submission_id == submission.id)
            .with_for_update()
        )
    ).all()
    if reports or not jobs:
        return
    if len(jobs) != 1:
        raise DemoSeedConflict("demo scenario has unexpected evaluation jobs")
    job = jobs[0]
    if job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
        return
    expected_key = evaluation_key(submission.id, submission.version, JobReason.INITIAL)
    if (
        job.requested_by != teacher_id
        or job.reason is not JobReason.INITIAL
        or job.source_report_id is not None
        or job.idempotency_key != expected_key
        or job.provider != "mock"
        or job.model != "fixture-v1"
        or job.execution_token is not None
    ):
        raise DemoSeedConflict("terminal demo evaluation job does not match its manifest")
    audits = (await session.scalars(select(AuditLog).where(AuditLog.job_id == job.id))).all()
    outbox = (
        await session.scalars(select(EvaluationOutbox).where(EvaluationOutbox.job_id == job.id))
    ).all()
    outbox_matches = (
        len(outbox) == 1
        and outbox[0].kind == "dispatch"
        and outbox[0].report_id is None
        and outbox[0].attempt_count == 0
        and outbox[0].delivered_at is None
        and outbox[0].failed_at is None
        and outbox[0].delivery_ref is None
        and outbox[0].last_error_type is None
        and outbox[0].last_error_summary is None
        and outbox[0].claim_token is None
        and outbox[0].claim_expires_at is None
    )
    if job.status is JobStatus.FAILED:
        audit_matches = (
            len(audits) == 1
            and audits[0].submission_id == submission.id
            and audits[0].provider == "mock"
            and audits[0].model == "fixture-v1"
            and audits[0].prompt_template_version == "evaluation-v1"
            and audits[0].schema_version == "1.0"
            and audits[0].retry_count == max(job.attempt_count - 1, 0)
            and audits[0].validation_status is None
            and audits[0].error_type == job.error_code
            and audits[0].error_message == job.error_message
            and audits[0].final_report_id is None
        )
    else:
        audit_matches = (
            not audits
            and job.attempt_count == 0
            and job.error_code is None
            and job.error_message is None
        )
    if not audit_matches or not outbox_matches:
        raise DemoSeedConflict("terminal demo evaluation evidence does not match its manifest")
    await session.execute(delete(EvaluationOutbox).where(EvaluationOutbox.job_id == job.id))
    if audits:
        await session.execute(
            text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
        )
        await session.execute(delete(AuditLog).where(AuditLog.job_id == job.id))
        await session.execute(
            text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
        )
    await session.execute(delete(EvaluationJob).where(EvaluationJob.id == job.id))


async def _seed_scenario(
    engine: AsyncEngine,
    *,
    assignment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    scenario: DemoScenario,
) -> ScenarioSeedSummary:
    session_factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        assignment = await session.get(Assignment, assignment_id)
        student = await session.scalar(
            select(User).where(User.username == scenario.student_username)
        )
        if (
            assignment is None
            or not _assignment_matches_reset_manifest(assignment)
            or student is None
            or student.id != DEMO_USER_IDS[scenario.student_username]
            or student.role is not Role.STUDENT
            or student.is_active is not True
        ):
            raise DemoSeedConflict("demo manifest ownership could not be verified")
        submission = await _ensure_scenario_submission(
            session,
            assignment=assignment,
            student=student,
            scenario=scenario,
        )
        await _recover_terminal_demo_job(
            session,
            submission=submission,
            teacher_id=teacher_id,
        )
        submission_id = uuid.UUID(str(submission.id))

    async with session_factory() as session:
        existing_reports = (
            await session.scalars(
                select(EvaluationReport).where(EvaluationReport.submission_id == submission_id)
            )
        ).all()
    if len(existing_reports) > 1:
        raise DemoSeedConflict("demo scenario has unexpected evaluation report versions")
    if existing_reports:
        report = existing_reports[0]
    else:
        async with session_factory() as service_session:
            service = EvaluationService(
                service_session,
                EvaluationEngine(MockEvaluationProvider()),
                requested_by=teacher_id,
                dispatch=None,
                mock_fixture_key=scenario.fixture_key,
            )
            report = await service.evaluate_now(submission_id, JobReason.INITIAL)
    if (
        report.score != scenario.expected_score
        or report.grade is not scenario.expected_grade
        or report.version != 1
        or report.origin is not ReportOrigin.AGENT
        or report.job_id is None
    ):
        raise DemoSeedConflict("demo scenario evaluation does not match its fixture contract")
    async with session_factory() as session:
        jobs = (
            await session.scalars(
                select(EvaluationJob).where(EvaluationJob.submission_id == submission_id)
            )
        ).all()
        job = jobs[0] if len(jobs) == 1 else None
        if (
            job is None
            or job.id != report.job_id
            or job.submission_id != submission_id
            or job.requested_by != teacher_id
            or job.reason is not JobReason.INITIAL
            or job.status is not JobStatus.SUCCEEDED
            or job.provider != "mock"
            or job.model != "fixture-v1"
        ):
            raise DemoSeedConflict("demo scenario evaluation job does not match its manifest")
    return ScenarioSeedSummary(
        fixture_key=scenario.fixture_key,
        submission_id=submission_id,
        report_id=uuid.UUID(str(report.id)),
        score=report.score,
        grade=report.grade,
    )


def _validate_orchestration_engines(
    data_engine: AsyncEngine,
    lock_engine: AsyncEngine,
) -> None:
    if not isinstance(data_engine, AsyncEngine):
        raise TypeError("data_engine must be an AsyncEngine")
    if not isinstance(lock_engine, AsyncEngine):
        raise TypeError("lock_engine must be an AsyncEngine")
    if data_engine is lock_engine:
        raise ValueError("data_engine and lock_engine must be distinct")
    data_pool = data_engine.sync_engine.pool
    lock_pool = lock_engine.sync_engine.pool
    if data_pool is lock_pool:
        raise ValueError("data_engine and lock_engine must not share a pool")
    if not isinstance(lock_pool, NullPool):
        raise TypeError("lock_engine must use NullPool")


async def seed_acceptance_demo(
    data_engine: AsyncEngine,
    lock_engine: AsyncEngine,
) -> AcceptanceSeedSummary:
    """Seed the complete acceptance demo through the production evaluation path."""
    _require_safe_environment()
    _validate_orchestration_engines(data_engine, lock_engine)
    session_factory = migration_gated_sessionmaker(data_engine, expire_on_commit=False)
    async with _demo_orchestration_lock(lock_engine):
        async with session_factory.begin() as seed_session:
            await seed_demo(seed_session)
            assignment = await seed_session.scalar(
                select(Assignment).where(Assignment.code == "HW-0001")
            )
            teacher = await seed_session.scalar(select(User).where(User.username == "teacher"))
            if (
                assignment is None
                or not _assignment_matches_reset_manifest(assignment)
                or teacher is None
                or teacher.id != DEMO_USER_IDS["teacher"]
                or teacher.role is not Role.TEACHER
                or teacher.is_active is not True
            ):
                raise DemoSeedConflict("demo manifest ownership could not be verified")
            assignment_id = uuid.UUID(str(assignment.id))
            teacher_id = uuid.UUID(str(teacher.id))
        scenarios = tuple(
            [
                await _seed_scenario(
                    data_engine,
                    assignment_id=assignment_id,
                    teacher_id=teacher_id,
                    scenario=scenario,
                )
                for scenario in DEMO_SCENARIOS
            ]
        )
    return AcceptanceSeedSummary(
        users=len(DEMO_USERS),
        assignments=1,
        assignment_id=assignment_id,
        scenarios=scenarios,
    )


async def reset_acceptance_demo(
    data_engine: AsyncEngine,
    lock_engine: AsyncEngine,
) -> None:
    """Delete only an acceptance-demo assignment tree whose ownership is proven."""
    _require_safe_environment()
    _validate_orchestration_engines(data_engine, lock_engine)
    session_factory = migration_gated_sessionmaker(data_engine, expire_on_commit=False)
    async with (
        _demo_orchestration_lock(lock_engine),
        session_factory() as reset_session,
        reset_session.begin(),
    ):
        assignment = await reset_session.scalar(
            select(Assignment).where(Assignment.code == "HW-0001").with_for_update()
        )
        if assignment is None:
            return
        if not _assignment_matches_reset_manifest(assignment):
            raise DemoSeedConflict("demo reset ownership could not be verified")
        expected_students = {scenario.student_username: scenario for scenario in DEMO_SCENARIOS}
        manifest_users = tuple(
            await reset_session.scalars(
                select(User).where(
                    or_(
                        User.id.in_(tuple(DEMO_USER_IDS.values())),
                        User.username.in_(tuple(DEMO_USER_IDS)),
                    )
                )
            )
        )
        if not _users_match_reset_manifest(manifest_users):
            raise DemoSeedConflict("demo reset ownership could not be verified")
        students = {
            user.username: user for user in manifest_users if user.username in expected_students
        }
        expected_owner_by_submission_id = {
            _submission_id(uuid.UUID(str(assignment.id)), expected_students[username]): (
                student.id,
                expected_students[username],
            )
            for username, student in students.items()
        }
        submissions = (
            await reset_session.scalars(
                select(Submission)
                .where(Submission.assignment_id == assignment.id)
                .with_for_update()
            )
        ).all()
        if any(
            submission.id not in expected_owner_by_submission_id
            or not _submission_matches_manifest(
                submission,
                assignment_id=uuid.UUID(str(assignment.id)),
                student_id=expected_owner_by_submission_id[submission.id][0],
                scenario=expected_owner_by_submission_id[submission.id][1],
            )
            for submission in submissions
        ):
            raise DemoSeedConflict("demo reset submission manifest did not match")
        submission_ids = tuple(submission.id for submission in submissions)
        if submission_ids:
            job_ids = tuple(
                (
                    await reset_session.scalars(
                        select(EvaluationJob.id).where(
                            EvaluationJob.submission_id.in_(submission_ids)
                        )
                    )
                ).all()
            )
            reports = tuple(
                (
                    await reset_session.scalars(
                        select(EvaluationReport)
                        .where(EvaluationReport.submission_id.in_(submission_ids))
                        .order_by(EvaluationReport.version.desc())
                    )
                ).all()
            )
            report_ids = tuple(report.id for report in reports)
            if report_ids:
                await reset_session.execute(
                    text(
                        "ALTER TABLE review_actions DISABLE TRIGGER trg_review_actions_append_only"
                    )
                )
                await reset_session.execute(
                    delete(ReviewAction).where(ReviewAction.report_id.in_(report_ids))
                )
                await reset_session.execute(
                    text("ALTER TABLE review_actions ENABLE TRIGGER trg_review_actions_append_only")
                )
            if job_ids:
                await reset_session.execute(
                    delete(EvaluationOutbox).where(EvaluationOutbox.job_id.in_(job_ids))
                )
                await reset_session.execute(
                    text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only")
                )
                await reset_session.execute(delete(AuditLog).where(AuditLog.job_id.in_(job_ids)))
                await reset_session.execute(
                    text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
                )
            report_job_ids = {report.job_id for report in reports if report.job_id is not None}
            orphaned_job_ids = tuple(job_id for job_id in job_ids if job_id not in report_job_ids)
            if orphaned_job_ids:
                await reset_session.execute(
                    delete(EvaluationJob).where(EvaluationJob.id.in_(orphaned_job_ids))
                )
            if reports:
                await reset_session.execute(
                    text(
                        "ALTER TABLE evaluation_reports DISABLE TRIGGER "
                        "trg_evaluation_reports_immutable"
                    )
                )
                for report in reports:
                    await reset_session.execute(
                        delete(EvaluationReport).where(EvaluationReport.id == report.id)
                    )
                    if report.job_id is not None:
                        await reset_session.execute(
                            delete(EvaluationJob).where(EvaluationJob.id == report.job_id)
                        )
                await reset_session.execute(
                    text(
                        "ALTER TABLE evaluation_reports ENABLE TRIGGER "
                        "trg_evaluation_reports_immutable"
                    )
                )
            await reset_session.execute(delete(Submission).where(Submission.id.in_(submission_ids)))
        await reset_session.delete(assignment)


def _create_orchestration_engines_from_settings() -> tuple[AsyncEngine, AsyncEngine]:
    settings = get_settings()
    engine_options: dict[str, object] = {"pool_pre_ping": True}
    data_engine = create_async_engine(settings.database_url, **engine_options)
    lock_engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        **engine_options,
    )
    return data_engine, lock_engine


async def _seed_from_settings() -> AcceptanceSeedSummary:
    _require_safe_environment()
    data_engine, lock_engine = _create_orchestration_engines_from_settings()
    try:
        return await seed_acceptance_demo(data_engine, lock_engine)
    finally:
        try:
            await data_engine.dispose()
        finally:
            await lock_engine.dispose()


async def _reset_from_settings() -> None:
    _require_safe_environment()
    data_engine, lock_engine = _create_orchestration_engines_from_settings()
    try:
        await reset_acceptance_demo(data_engine, lock_engine)
    finally:
        try:
            await data_engine.dispose()
        finally:
            await lock_engine.dispose()


def _summary_payload(summary: AcceptanceSeedSummary) -> dict[str, object]:
    return {
        "users": summary.users,
        "assignments": summary.assignments,
        "assignment_id": str(summary.assignment_id),
        "scenarios": {
            item.fixture_key: {
                "submission_id": str(item.submission_id),
                "report_id": str(item.report_id),
            }
            for item in summary.scenarios
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed the local acceptance demo, or reset it without reseeding"
    )
    parser.add_argument(
        "--reset-demo",
        action="store_true",
        help="delete the verified demo tree and exit without reseeding",
    )
    arguments = parser.parse_args()
    try:
        if arguments.reset_demo:
            asyncio.run(_reset_from_settings())
            payload: dict[str, object] = {"reset": True}
        else:
            summary = asyncio.run(_seed_from_settings())
            payload = _summary_payload(summary)
    except DemoSeedError as exc:
        print(f"demo seed refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - sanitize all CLI failures at this boundary.
        print(f"demo seed failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    if arguments.reset_demo:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
