import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.assignments.model import Assignment
from app.assignments.schemas import AssignmentCreate
from app.assignments.service import create_assignment, publish_assignment
from app.db.types import Role, SubmissionContentType, SubmissionSource
from app.submissions.model import Submission
from app.submissions.schemas import SubmissionCreate
from app.submissions.service import create_submission
from app.users.model import User


@dataclass(frozen=True, slots=True)
class AcceptanceSubject:
    teacher: User
    student: User
    assignment: Assignment
    submission: Submission


SubjectFactory = Callable[..., Awaitable[AcceptanceSubject]]


@pytest_asyncio.fixture
async def acceptance_subject_factory(postgres_session: AsyncSession) -> SubjectFactory:
    counter = 0

    async def create_subject(
        answer: str,
        *,
        rubric: dict[str, object] | None = None,
        content_type: SubmissionContentType = SubmissionContentType.TEXT,
        content_json: object | None = None,
    ) -> AcceptanceSubject:
        nonlocal counter
        counter += 1
        teacher = User(
            username=f"acceptance-teacher-{counter}-{uuid.uuid4()}",
            display_name="Acceptance Teacher",
            role=Role.TEACHER,
            password_hash="test-hash",
            is_active=True,
        )
        student = User(
            username=f"acceptance-student-{counter}-{uuid.uuid4()}",
            display_name="Acceptance Student",
            role=Role.STUDENT,
            password_hash="test-hash",
            is_active=True,
        )
        postgres_session.add_all((teacher, student))
        await postgres_session.commit()

        assignment = await create_assignment(
            postgres_session,
            AssignmentCreate(
                title="Dijkstra 算法验收题",
                question="说明 Dijkstra 算法的步骤、复杂度与适用条件。",
                notes="验收矩阵中的确定性题目。",
                rubric=(
                    {
                        "required_points": [
                            "最小暂定距离顶点",
                            "松弛",
                            "O((V+E)logV)",
                            "非负权",
                        ]
                    }
                    if rubric is None
                    else rubric
                ),
                due_at=datetime.now(UTC) + timedelta(days=7),
            ),
            teacher.id,
        )
        published = await publish_assignment(postgres_session, assignment.id)
        assert published is not None
        submission = await create_submission(
            postgres_session,
            published,
            student.id,
            SubmissionCreate(
                content_type=content_type,
                content_text=answer,
                content_json=content_json,
            ),
            SubmissionSource.WEB,
        )
        await postgres_session.commit()
        return AcceptanceSubject(
            teacher=teacher,
            student=student,
            assignment=published,
            submission=submission,
        )

    return create_subject
