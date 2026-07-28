"""Add Mattermost identities and durable integration events.

Revision ID: 0005_mattermost
Revises: 0004_reviews
Create Date: 2026-07-26 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.integrations.mattermost.model import (
    CREATE_INTEGRATION_EVENT_GUARD_FUNCTION_SQL,
    CREATE_INTEGRATION_EVENT_GUARD_TRIGGER_SQL,
    CREATE_INTEGRATION_EVENT_TRUNCATE_FUNCTION_SQL,
    CREATE_INTEGRATION_EVENT_TRUNCATE_TRIGGER_SQL,
    CREATE_MATTERMOST_IDENTITY_GUARD_FUNCTION_SQL,
    CREATE_MATTERMOST_IDENTITY_GUARD_TRIGGER_SQL,
    CREATE_MATTERMOST_IDENTITY_TRUNCATE_FUNCTION_SQL,
    CREATE_MATTERMOST_IDENTITY_TRUNCATE_TRIGGER_SQL,
)

revision: str = "0005_mattermost"
down_revision: str | None = "0004_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    event_status = postgresql.ENUM(
        "processing",
        "completed",
        "deterministic_error",
        name="integration_event_status",
        create_type=False,
    )
    event_status.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "mattermost_identities",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mattermost_user_id", sa.String(length=128), nullable=False),
        sa.Column("mattermost_username", sa.String(length=128), nullable=False),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(mattermost_user_id) BETWEEN 1 AND 128 "
            "AND mattermost_user_id = btrim(mattermost_user_id)",
            name="ck_mattermost_identities_user_id_safe",
        ),
        sa.CheckConstraint(
            "octet_length(mattermost_username) BETWEEN 1 AND 128 "
            "AND mattermost_username = btrim(mattermost_username)",
            name="ck_mattermost_identities_username_safe",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_mattermost_identities_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_mattermost_identities"),
        sa.UniqueConstraint(
            "mattermost_user_id",
            name="uq_mattermost_identities_mattermost_user_id",
        ),
    )
    op.execute(CREATE_MATTERMOST_IDENTITY_GUARD_FUNCTION_SQL)
    op.execute(CREATE_MATTERMOST_IDENTITY_GUARD_TRIGGER_SQL)
    op.execute(CREATE_MATTERMOST_IDENTITY_TRUNCATE_FUNCTION_SQL)
    op.execute(CREATE_MATTERMOST_IDENTITY_TRUNCATE_TRIGGER_SQL)

    op.create_table(
        "integration_events",
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "source",
            sa.String(length=32),
            server_default="mattermost",
            nullable=False,
        ),
        sa.Column(
            "event_type",
            sa.String(length=32),
            server_default="slash_command",
            nullable=False,
        ),
        sa.Column(
            "status",
            event_status,
            server_default="processing",
            nullable=False,
        ),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "business_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "arrived_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_integration_events_request_hash",
        ),
        sa.CheckConstraint(
            "source = 'mattermost'",
            name="ck_integration_events_source",
        ),
        sa.CheckConstraint(
            "event_type = 'slash_command'",
            name="ck_integration_events_event_type",
        ),
        sa.CheckConstraint(
            "response IS NULL OR (jsonb_typeof(response) = 'object' "
            "AND pg_column_size(response) <= 65536)",
            name="ck_integration_events_response_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(business_refs) = 'object' AND pg_column_size(business_refs) <= 8192",
            name="ck_integration_events_business_refs_object",
        ),
        sa.CheckConstraint(
            "((status = 'processing' AND response IS NULL AND completed_at IS NULL) OR "
            "(status IN ('completed', 'deterministic_error') "
            "AND response IS NOT NULL AND completed_at IS NOT NULL))",
            name="ck_integration_events_status_coherence",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= arrived_at",
            name="ck_integration_events_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_integration_events_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_events"),
        sa.UniqueConstraint(
            "request_hash",
            name="uq_integration_events_request_hash",
        ),
    )
    op.execute(CREATE_INTEGRATION_EVENT_GUARD_FUNCTION_SQL)
    op.execute(CREATE_INTEGRATION_EVENT_GUARD_TRIGGER_SQL)
    op.execute(CREATE_INTEGRATION_EVENT_TRUNCATE_FUNCTION_SQL)
    op.execute(CREATE_INTEGRATION_EVENT_TRUNCATE_TRIGGER_SQL)


def downgrade() -> None:
    op.execute("LOCK TABLE integration_events, mattermost_identities IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $function$
        BEGIN
            IF EXISTS (SELECT 1 FROM integration_events)
               OR EXISTS (SELECT 1 FROM mattermost_identities) THEN
                RAISE EXCEPTION 'cannot downgrade while Mattermost integration evidence exists'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $function$
        """
    )
    op.drop_table("integration_events")
    op.drop_table("mattermost_identities")
    op.execute("DROP FUNCTION IF EXISTS guard_integration_event_evidence()")
    op.execute("DROP FUNCTION IF EXISTS prevent_integration_event_truncate()")
    op.execute("DROP FUNCTION IF EXISTS guard_mattermost_identity_authority()")
    op.execute("DROP FUNCTION IF EXISTS prevent_mattermost_identity_truncate()")
    postgresql.ENUM(name="integration_event_status").drop(op.get_bind(), checkfirst=False)
