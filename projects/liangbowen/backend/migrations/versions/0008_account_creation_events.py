"""Persist immutable account-creation evidence.

Revision ID: 0008_account_creation_events
Revises: 0007_retry_generations
Create Date: 2026-07-28 08:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_account_creation_events"
down_revision: str | None = "0007_retry_generations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

role = postgresql.ENUM(
    "teacher",
    "student",
    "admin",
    name="role",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "account_creation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_role", role, nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            "target_role IN ('teacher', 'student')",
            name="ck_account_creation_events_target_role",
        ),
        sa.CheckConstraint(
            "octet_length(request_id) BETWEEN 1 AND 128 "
            "AND request_id = btrim(request_id) "
            "AND request_id !~ '[[:cntrl:]]'",
            name="ck_account_creation_events_request_id_safe",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_account_creation_events_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["users.id"],
            name="fk_account_creation_events_target_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_creation_events"),
        sa.UniqueConstraint(
            "target_user_id",
            name="uq_account_creation_events_target_user_id",
        ),
    )
    op.create_index(
        "ix_account_creation_events_actor_created_id",
        "account_creation_events",
        ["actor_user_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_account_creation_events_created_id",
        "account_creation_events",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION prevent_account_creation_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'account creation events are append-only'
                USING ERRCODE = '55000';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_account_creation_events_no_update
        BEFORE UPDATE ON account_creation_events
        FOR EACH ROW EXECUTE FUNCTION prevent_account_creation_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_account_creation_events_no_delete
        BEFORE DELETE ON account_creation_events
        FOR EACH ROW EXECUTE FUNCTION prevent_account_creation_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_account_creation_events_no_truncate
        BEFORE TRUNCATE ON account_creation_events
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_account_creation_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_account_creation_events_no_truncate ON account_creation_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_account_creation_events_no_delete ON account_creation_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_account_creation_events_no_update ON account_creation_events"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_account_creation_event_mutation()")
    op.drop_table("account_creation_events")
