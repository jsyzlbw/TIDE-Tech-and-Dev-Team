"""Create the initial users, assignments, and submissions domain.

Revision ID: 0001_domain
Revises:
Create Date: 2026-07-26 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_domain"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

role = postgresql.ENUM("teacher", "student", "admin", name="role", create_type=False)
assignment_status = postgresql.ENUM(
    "draft",
    "published",
    "closed",
    "archived",
    name="assignment_status",
    create_type=False,
)
submission_content_type = postgresql.ENUM(
    "text",
    "markdown",
    "code",
    "structured",
    name="submission_content_type",
    create_type=False,
)
submission_status = postgresql.ENUM(
    "submitted",
    "withdrawn",
    name="submission_status",
    create_type=False,
)
submission_source = postgresql.ENUM(
    "web",
    "mattermost",
    name="submission_source",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    role.create(bind, checkfirst=False)
    assignment_status.create(bind, checkfirst=False)
    submission_content_type.create(bind, checkfirst=False)
    submission_status.create(bind, checkfirst=False)
    submission_source.create(bind, checkfirst=False)

    op.execute(sa.schema.CreateSequence(sa.Sequence("assignment_code_seq", start=1)))
    op.create_table(
        "users",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role", role, nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("ix_users_role", "users", ["role"], unique=False)
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "assignments",
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("rubric", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", assignment_status, nullable=False),
        sa.Column("mattermost_channel_id", sa.String(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_assignments_created_by_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignments"),
    )
    op.create_index("ix_assignments_code", "assignments", ["code"], unique=True)
    op.create_index(
        "ix_assignments_created_at_id",
        "assignments",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index("ix_assignments_created_by", "assignments", ["created_by"], unique=False)
    op.create_index("ix_assignments_status", "assignments", ["status"], unique=False)
    op.create_index(
        "ix_assignments_status_created_at_id",
        "assignments",
        ["status", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )

    op.create_table(
        "submissions",
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_type", submission_content_type, nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            submission_status,
            server_default="submitted",
            nullable=False,
        ),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source", submission_source, nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_submissions_version_positive"),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["assignments.id"],
            name="fk_submissions_assignment_id_assignments",
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
            name="fk_submissions_student_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_submissions"),
        sa.UniqueConstraint(
            "assignment_id",
            "student_id",
            "version",
            name="uq_submissions_assignment_student_version",
        ),
    )
    op.create_index(
        "ix_submissions_assignment_student_submitted_latest",
        "submissions",
        [
            "assignment_id",
            "student_id",
            sa.text("version DESC"),
            sa.text("submitted_at DESC"),
            sa.text("id DESC"),
        ],
        unique=False,
        postgresql_where=sa.text("status = 'submitted'"),
    )
    op.create_index(
        "ix_submissions_assignment_student_version_desc",
        "submissions",
        ["assignment_id", "student_id", sa.text("version DESC")],
        unique=False,
    )
    op.create_index(
        "ix_submissions_assignment_submitted_at_id",
        "submissions",
        ["assignment_id", sa.text("submitted_at DESC"), sa.text("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("submissions")
    op.drop_table("assignments")
    op.drop_table("users")
    op.execute(sa.schema.DropSequence(sa.Sequence("assignment_code_seq")))

    bind = op.get_bind()
    submission_source.drop(bind, checkfirst=False)
    submission_status.drop(bind, checkfirst=False)
    submission_content_type.drop(bind, checkfirst=False)
    assignment_status.drop(bind, checkfirst=False)
    role.drop(bind, checkfirst=False)
