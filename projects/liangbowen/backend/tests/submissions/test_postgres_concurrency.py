import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.assignments.model import Assignment
from app.auth.dependencies import get_current_user
from app.db.session import get_session
from app.db.types import (
    AssignmentStatus,
    Role,
    SubmissionContentType,
    SubmissionSource,
)
from app.main import create_app
from app.submissions.model import Submission
from app.submissions.schemas import SubmissionCreate
from app.submissions.service import create_submission
from app.users.model import User


async def create_user(session: AsyncSession, role: Role, suffix: str) -> User:
    item = User(
        username=f"pg-{role.value}-{suffix}-{uuid.uuid4().hex[:6]}",
        display_name=role.value,
        role=role,
        password_hash="unused",
        is_active=True,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def test_postgres_concurrent_submissions_receive_distinct_versions(
    postgres_session: AsyncSession,
) -> None:
    teacher = await create_user(postgres_session, Role.TEACHER, "teacher")
    student = await create_user(postgres_session, Role.STUDENT, "student")
    assignment = Assignment(
        code="HW-PG-CONCURRENT",
        title="Concurrency",
        question="Submit concurrently.",
        notes="",
        rubric={},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=AssignmentStatus.PUBLISHED,
        mattermost_channel_id=None,
        created_by=teacher.id,
        published_at=datetime.now(UTC),
    )
    postgres_session.add(assignment)
    await postgres_session.commit()
    await postgres_session.refresh(assignment)
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    data = SubmissionCreate(
        content_type=SubmissionContentType.TEXT,
        content_text="concurrent answer",
    )

    async def submit() -> int:
        async with session_factory() as session:
            created = await create_submission(
                session,
                assignment,
                student.id,
                data,
                SubmissionSource.WEB,
            )
            await session.commit()
            assert created.id is not None
            assert inspect(created).expired_attributes == set()
            assert session.in_transaction() is False
            return created.version

    versions = await asyncio.gather(submit(), submit())

    assert sorted(versions) == [1, 2]
    rows = await postgres_session.scalars(
        select(Submission)
        .where(
            Submission.assignment_id == assignment.id,
            Submission.student_id == student.id,
        )
        .order_by(Submission.version)
    )
    assert [item.version for item in rows] == [1, 2]


async def test_postgres_populate_existing_refreshes_stale_assignment_identity(
    postgres_session: AsyncSession,
) -> None:
    teacher = await create_user(postgres_session, Role.TEACHER, "stale-teacher")
    student = await create_user(postgres_session, Role.STUDENT, "stale-student")
    item = Assignment(
        code="HW-PG-STALE",
        title="Stale identity",
        question="Refresh state.",
        notes="",
        rubric={},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=AssignmentStatus.DRAFT,
        mattermost_channel_id=None,
        created_by=teacher.id,
        published_at=None,
    )
    postgres_session.add(item)
    await postgres_session.commit()
    await postgres_session.refresh(item)
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async with session_factory() as stale_session:
        stale = await stale_session.get(Assignment, item.id)
        assert stale is not None
        assert stale.status is AssignmentStatus.DRAFT
        async with session_factory() as publishing_session:
            authoritative = await publishing_session.get(Assignment, item.id)
            assert authoritative is not None
            authoritative.status = AssignmentStatus.PUBLISHED
            authoritative.published_at = datetime.now(UTC)
            await publishing_session.commit()

        created = await create_submission(
            stale_session,
            stale,
            student.id,
            SubmissionCreate(
                content_type=SubmissionContentType.TEXT,
                content_text="fresh state accepted",
            ),
            SubmissionSource.WEB,
        )
        await stale_session.commit()

    assert created.version == 1


async def test_postgres_service_does_not_autoflush_or_resolve_caller_transaction(
    postgres_session: AsyncSession,
) -> None:
    teacher = await create_user(postgres_session, Role.TEACHER, "atomic-teacher")
    student = await create_user(postgres_session, Role.STUDENT, "atomic-student")
    item = Assignment(
        code="HW-PG-UOW",
        title="Unit of work",
        question="Keep caller state isolated.",
        notes="",
        rubric={},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=AssignmentStatus.PUBLISHED,
        mattermost_channel_id=None,
        created_by=teacher.id,
        published_at=datetime.now(UTC),
    )
    postgres_session.add(item)
    await postgres_session.commit()
    await postgres_session.refresh(item)
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async with session_factory() as session:
        unrelated = User(
            username=f"pending-{uuid.uuid4().hex}",
            display_name="Pending caller work",
            role=Role.STUDENT,
            password_hash="unused",
            is_active=True,
        )
        session.add(unrelated)
        created = await create_submission(
            session,
            item,
            student.id,
            SubmissionCreate(
                content_type=SubmissionContentType.TEXT,
                content_text="caller decides",
            ),
            SubmissionSource.WEB,
        )

        assert list(session.new) == [unrelated]
        assert unrelated.id is None
        assert list(session.dirty) == []
        assert session.in_transaction() is True

        async with session_factory() as verifier:
            assert await verifier.get(Submission, created.id) is None

        await session.rollback()
        assert list(session.new) == []
        assert session.in_transaction() is False

    async with session_factory() as verification_session:
        persisted_count = await verification_session.scalar(
            select(func.count(Submission.id)).where(
                Submission.assignment_id == item.id,
                Submission.student_id == student.id,
            )
        )
    assert persisted_count == 0


async def test_postgres_caller_commits_submission_and_other_work_atomically(
    postgres_session: AsyncSession,
) -> None:
    teacher = await create_user(postgres_session, Role.TEACHER, "commit-teacher")
    student = await create_user(postgres_session, Role.STUDENT, "commit-student")
    item = Assignment(
        code="HW-PG-COMMIT-UOW",
        title="Atomic caller commit",
        question="Commit together.",
        notes="",
        rubric={},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=AssignmentStatus.PUBLISHED,
        mattermost_channel_id=None,
        created_by=teacher.id,
        published_at=datetime.now(UTC),
    )
    postgres_session.add(item)
    await postgres_session.commit()
    session_factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async with session_factory() as session:
        unrelated = User(
            username=f"atomic-{uuid.uuid4().hex}",
            display_name="Atomic caller work",
            role=Role.STUDENT,
            password_hash="unused",
            is_active=True,
        )
        session.add(unrelated)
        created = await create_submission(
            session,
            item,
            student.id,
            SubmissionCreate(
                content_type=SubmissionContentType.TEXT,
                content_text="commit together",
            ),
            SubmissionSource.WEB,
        )
        await session.commit()

    async with session_factory() as verifier:
        assert await verifier.get(User, unrelated.id) is not None
        assert await verifier.get(Submission, created.id) is not None


async def test_postgres_latest_submission_api_is_scoped_to_assignment_and_student(
    postgres_session: AsyncSession,
) -> None:
    teacher = await create_user(postgres_session, Role.TEACHER, "latest-teacher")
    first_student = await create_user(postgres_session, Role.STUDENT, "latest-first")
    second_student = await create_user(postgres_session, Role.STUDENT, "latest-second")
    assignments = [
        Assignment(
            code=code,
            title=code,
            question="Latest answer.",
            notes="",
            rubric={},
            due_at=datetime.now(UTC) + timedelta(days=1),
            status=AssignmentStatus.PUBLISHED,
            mattermost_channel_id=None,
            created_by=teacher.id,
            published_at=datetime.now(UTC),
        )
        for code in ("HW-PG-LATEST-TARGET", "HW-PG-LATEST-OTHER")
    ]
    postgres_session.add_all(assignments)
    await postgres_session.flush()
    target, other = assignments
    rows = [
        Submission(
            assignment_id=assignment_id,
            student_id=student_id,
            version=version,
            content_type=SubmissionContentType.TEXT,
            content_text=content,
            content_json=None,
            submitted_at=datetime.now(UTC) + timedelta(microseconds=order),
            source=SubmissionSource.WEB,
        )
        for order, (assignment_id, student_id, version, content) in enumerate(
            [
                (target.id, first_student.id, 1, "target first v1"),
                (target.id, first_student.id, 2, "target first v2"),
                (target.id, second_student.id, 1, "target second v1"),
                (other.id, first_student.id, 2, "other first v2"),
                (other.id, second_student.id, 1, "other second v1"),
            ]
        )
    ]
    postgres_session.add_all(rows)
    await postgres_session.commit()

    async def override_current_user() -> User:
        return teacher

    async def override_session():  # type: ignore[no-untyped-def]
        yield postgres_session

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/v1/assignments/{target.id}/submissions")

    assert response.status_code == 200
    assert {
        (item["student_id"], item["version"], item["content_text"]) for item in response.json()
    } == {
        (str(first_student.id), 2, "target first v2"),
        (str(second_student.id), 1, "target second v1"),
    }
