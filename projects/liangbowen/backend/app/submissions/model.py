import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.types import (
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
    enum_values,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Submission(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "student_id",
            "version",
            name="uq_submissions_assignment_student_version",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_submissions_version_positive",
        ),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assignments.id"),
        nullable=False,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[SubmissionContentType] = mapped_column(
        Enum(
            SubmissionContentType,
            name="submission_content_type",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(
            SubmissionStatus,
            name="submission_status",
            values_callable=enum_values,
        ),
        default=SubmissionStatus.SUBMITTED,
        server_default=SubmissionStatus.SUBMITTED.value,
        nullable=False,
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )
    source: Mapped[SubmissionSource] = mapped_column(
        Enum(
            SubmissionSource,
            name="submission_source",
            values_callable=enum_values,
        ),
        nullable=False,
    )


Index(
    "ix_submissions_assignment_student_version_desc",
    Submission.assignment_id,
    Submission.student_id,
    Submission.version.desc(),
)
Index(
    "ix_submissions_assignment_submitted_at_id",
    Submission.assignment_id,
    Submission.submitted_at.desc(),
    Submission.id.desc(),
)
Index(
    "ix_submissions_assignment_student_submitted_latest",
    Submission.assignment_id,
    Submission.student_id,
    Submission.version.desc(),
    Submission.submitted_at.desc(),
    Submission.id.desc(),
    postgresql_where=Submission.status == SubmissionStatus.SUBMITTED,
)
