"""Add Mattermost notification delivery and interactive-action evidence.

Revision ID: 0006_mattermost_delivery
Revises: 0005_mattermost
Create Date: 2026-07-27 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_gate import MIGRATION_GATE_EXCLUSIVE_SQL_TEXT
from app.integrations.mattermost.ids import MATTERMOST_ID_SQL

revision: str = "0006_mattermost_delivery"
down_revision: str | None = "0005_mattermost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ATTEMPT_CHECK = (
    "((kind = 'dispatch' AND attempt_count BETWEEN 0 AND 1000) OR "
    "(kind = 'notification' AND attempt_count BETWEEN 0 AND 3))"
)
TERMINAL_CHECK = (
    "((kind = 'dispatch' AND failed_at IS NULL AND delivery_ref IS NULL "
    "AND last_error_summary IS NULL) OR (kind = 'notification' AND ("
    "(delivered_at IS NULL AND failed_at IS NULL AND delivery_ref IS NULL "
    "AND ((attempt_count = 0 AND last_error_type IS NULL "
    "AND last_error_summary IS NULL) OR (attempt_count BETWEEN 1 AND 2 "
    "AND last_error_type IS NOT NULL AND last_error_summary IS NOT NULL))) OR "
    "(delivered_at IS NOT NULL AND failed_at IS NULL AND delivery_ref IS NOT NULL "
    "AND last_error_type IS NULL AND last_error_summary IS NULL "
    "AND claim_token IS NULL AND claim_expires_at IS NULL) OR "
    "(delivered_at IS NULL AND failed_at IS NOT NULL AND delivery_ref IS NULL "
    "AND attempt_count BETWEEN 1 AND 3 AND last_error_type IS NOT NULL "
    "AND last_error_summary IS NOT NULL AND claim_token IS NULL "
    "AND claim_expires_at IS NULL))))"
)
EVENT_TYPE_CHECK = (
    "event_type IN ('slash_command', 'interactive_action', "
    "'notification_delivered', 'notification_failed')"
)
LEGACY_MATTERMOST_ID_CHECK = (
    "octet_length(mattermost_user_id) BETWEEN 1 AND 128 "
    "AND mattermost_user_id = btrim(mattermost_user_id)"
)


def upgrade() -> None:
    op.execute(MIGRATION_GATE_EXCLUSIVE_SQL_TEXT)
    op.execute(
        "LOCK TABLE evaluation_outbox, integration_events, mattermost_identities "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        f"""
        DO $function$
        DECLARE
            invalid_count bigint;
        BEGIN
            SELECT count(*) INTO invalid_count
            FROM mattermost_identities
            WHERE mattermost_user_id !~ '{MATTERMOST_ID_SQL}';

            IF invalid_count > 0 THEN
                RAISE EXCEPTION 'invalid Mattermost identity count: %', invalid_count
                    USING ERRCODE = '23514';
            END IF;
        END;
        $function$
        """
    )
    op.drop_constraint(
        "ck_mattermost_identities_user_id_safe",
        "mattermost_identities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_mattermost_identities_user_id_safe",
        "mattermost_identities",
        f"mattermost_user_id ~ '{MATTERMOST_ID_SQL}'",
    )

    op.add_column(
        "evaluation_outbox",
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "evaluation_outbox",
        sa.Column("delivery_ref", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "evaluation_outbox",
        sa.Column("last_error_summary", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        UPDATE evaluation_outbox
        SET attempt_count = LEAST(attempt_count, CASE WHEN delivered_at IS NULL THEN 2 ELSE 3 END),
            delivery_ref = CASE WHEN delivered_at IS NULL THEN NULL ELSE 'legacy-unverified' END,
            last_error_type = CASE WHEN delivered_at IS NULL THEN last_error_type ELSE NULL END,
            last_error_summary = CASE
                WHEN delivered_at IS NULL AND attempt_count > 0
                    THEN 'Mattermost delivery failed'
                ELSE NULL
            END
        WHERE kind = 'notification'
        """
    )
    op.drop_constraint(
        "ck_evaluation_outbox_attempt_count",
        "evaluation_outbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_evaluation_outbox_attempt_count",
        "evaluation_outbox",
        ATTEMPT_CHECK,
    )
    op.create_check_constraint(
        "ck_evaluation_outbox_failure_order",
        "evaluation_outbox",
        "failed_at IS NULL OR failed_at >= created_at",
    )
    op.create_check_constraint(
        "ck_evaluation_outbox_delivery_ref_safe",
        "evaluation_outbox",
        f"delivery_ref IS NULL OR delivery_ref ~ '{MATTERMOST_ID_SQL}'",
    )
    op.create_check_constraint(
        "ck_evaluation_outbox_error_summary_safe",
        "evaluation_outbox",
        "last_error_summary IS NULL OR last_error_summary = 'Mattermost delivery failed'",
    )
    op.create_check_constraint(
        "ck_evaluation_outbox_terminal_state",
        "evaluation_outbox",
        TERMINAL_CHECK,
    )
    op.drop_index("ix_evaluation_outbox_pending_next_id", table_name="evaluation_outbox")
    op.create_index(
        "ix_evaluation_outbox_pending_next_id",
        "evaluation_outbox",
        ["next_attempt_at", "id"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL AND failed_at IS NULL"),
    )

    op.drop_constraint(
        "ck_integration_events_event_type",
        "integration_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_integration_events_event_type",
        "integration_events",
        EVENT_TYPE_CHECK,
    )


def downgrade() -> None:
    op.execute(MIGRATION_GATE_EXCLUSIVE_SQL_TEXT)
    op.execute(
        "LOCK TABLE evaluation_outbox, integration_events, mattermost_identities "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $function$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM evaluation_outbox
                WHERE failed_at IS NOT NULL
                   OR (delivery_ref IS NOT NULL AND delivery_ref <> 'legacy-unverified')
            ) OR EXISTS (
                SELECT 1 FROM integration_events
                WHERE event_type <> 'slash_command'
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while A8 Mattermost evidence exists'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $function$
        """
    )
    op.drop_constraint(
        "ck_mattermost_identities_user_id_safe",
        "mattermost_identities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_mattermost_identities_user_id_safe",
        "mattermost_identities",
        LEGACY_MATTERMOST_ID_CHECK,
    )
    op.drop_constraint(
        "ck_integration_events_event_type",
        "integration_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_integration_events_event_type",
        "integration_events",
        "event_type = 'slash_command'",
    )

    op.drop_index("ix_evaluation_outbox_pending_next_id", table_name="evaluation_outbox")
    op.create_index(
        "ix_evaluation_outbox_pending_next_id",
        "evaluation_outbox",
        ["next_attempt_at", "id"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.drop_constraint(
        "ck_evaluation_outbox_terminal_state",
        "evaluation_outbox",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_outbox_error_summary_safe",
        "evaluation_outbox",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_outbox_delivery_ref_safe",
        "evaluation_outbox",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_outbox_failure_order",
        "evaluation_outbox",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_outbox_attempt_count",
        "evaluation_outbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_evaluation_outbox_attempt_count",
        "evaluation_outbox",
        "attempt_count BETWEEN 0 AND 1000",
    )
    op.drop_column("evaluation_outbox", "last_error_summary")
    op.drop_column("evaluation_outbox", "delivery_ref")
    op.drop_column("evaluation_outbox", "failed_at")
