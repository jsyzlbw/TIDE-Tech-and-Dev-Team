import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Sequence, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import AssignmentStatus, enum_values

ASSIGNMENT_CODE_SEQUENCE = Sequence("assignment_code_seq", start=1, metadata=Base.metadata)


class Assignment(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "assignments"

    code: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    rubric: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(
            AssignmentStatus,
            name="assignment_status",
            values_callable=enum_values,
        ),
        index=True,
        nullable=False,
    )
    mattermost_channel_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        index=True,
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


Index(
    "ix_assignments_status_created_at_id",
    Assignment.status,
    Assignment.created_at.desc(),
    Assignment.id.desc(),
)
Index(
    "ix_assignments_created_at_id",
    Assignment.created_at.desc(),
    Assignment.id.desc(),
)
