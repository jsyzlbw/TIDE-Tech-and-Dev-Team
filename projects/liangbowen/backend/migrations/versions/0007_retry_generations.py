"""Allow append-only re-evaluation request generations.

Revision ID: 0007_retry_generations
Revises: 0006_mattermost_delivery
Create Date: 2026-07-28 06:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_retry_generations"
down_revision: str | None = "0006_mattermost_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_review_actions_report_action",
        "review_actions",
        type_="unique",
    )
    op.create_index(
        "uq_review_actions_report_non_reevaluate",
        "review_actions",
        ["report_id", "action"],
        unique=True,
        postgresql_where="action <> 'reevaluate'",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $function$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM review_actions
                GROUP BY report_id, action
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'retry generation evidence exists; downgrade refused'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $function$
        """
    )
    op.drop_index(
        "uq_review_actions_report_non_reevaluate",
        table_name="review_actions",
    )
    op.create_unique_constraint(
        "uq_review_actions_report_action",
        "review_actions",
        ["report_id", "action"],
    )
