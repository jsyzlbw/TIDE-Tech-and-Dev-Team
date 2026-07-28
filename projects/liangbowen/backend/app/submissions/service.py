import uuid
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assignments.model import Assignment
from app.db.types import AssignmentStatus, SubmissionSource, SubmissionStatus
from app.submissions.model import Submission
from app.submissions.schemas import SubmissionCreate

MAX_SUBMISSION_CHARACTERS = 50_000


class SubmissionAssignmentNotFound(LookupError):
    """Raised when the authoritative assignment row no longer exists."""


class SubmissionClosed(ValueError):
    """Raised when an assignment is not accepting submissions."""


class SubmissionTooLong(ValueError):
    """Raised when service callers bypass the schema's content bound."""


class SubmissionInvalid(ValueError):
    """Raised when service callers bypass submission schema validation."""


def utc_now() -> datetime:
    return datetime.now(UTC)


async def create_submission(
    session: AsyncSession,
    assignment: Assignment,
    student_id: uuid.UUID,
    data: SubmissionCreate,
    source: SubmissionSource,
) -> Submission:
    if not isinstance(data.content_text, str) or len(data.content_text) > MAX_SUBMISSION_CHARACTERS:
        raise SubmissionTooLong("submission content exceeds the character limit")

    try:
        validated = SubmissionCreate.model_validate(data.model_dump())
    except ValidationError as exc:
        raise SubmissionInvalid("submission payload is invalid") from exc

    with session.no_autoflush:
        authoritative = await session.scalar(
            select(Assignment)
            .where(Assignment.id == assignment.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if authoritative is None:
            raise SubmissionAssignmentNotFound("assignment does not exist")

        submitted_at = utc_now()
        if (
            authoritative.status is not AssignmentStatus.PUBLISHED
            or authoritative.due_at <= submitted_at
        ):
            raise SubmissionClosed("assignment is not accepting submissions")

        latest_version = await session.scalar(
            select(func.max(Submission.version)).where(
                Submission.assignment_id == authoritative.id,
                Submission.student_id == student_id,
            )
        )
    submission = Submission(
        assignment_id=authoritative.id,
        student_id=student_id,
        version=int(latest_version or 0) + 1,
        content_type=validated.content_type,
        content_text=validated.content_text,
        content_json=validated.content_json,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=submitted_at,
        source=source,
    )
    session.add(submission)
    await session.flush([submission])

    return submission
