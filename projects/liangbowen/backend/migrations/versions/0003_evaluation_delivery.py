"""Add durable evaluation delivery and cancelled provider evidence.

Revision ID: 0003_evaluation_delivery
Revises: 0002_evaluations
Create Date: 2026-07-26 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_evaluation_delivery"
down_revision: str | None = "0002_evaluations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_SAFE_ERROR_V2_SQL = (
    "error_type IN ('configuration', 'timeout', 'network', 'authentication', "
    "'rate_limited', 'upstream', 'protocol', 'response_too_large', 'fixture', "
    "'validation_exhausted', 'internal_error', 'cancelled') AND error_message = "
    "CASE error_type "
    "WHEN 'configuration' THEN 'evaluation provider configuration is invalid' "
    "WHEN 'timeout' THEN 'evaluation provider timed out' "
    "WHEN 'network' THEN 'evaluation provider network unavailable' "
    "WHEN 'authentication' THEN 'evaluation provider authentication failed' "
    "WHEN 'rate_limited' THEN 'evaluation provider rate limited' "
    "WHEN 'upstream' THEN 'evaluation provider unavailable' "
    "WHEN 'protocol' THEN 'evaluation provider returned an invalid response' "
    "WHEN 'response_too_large' THEN 'evaluation provider response too large' "
    "WHEN 'fixture' THEN 'evaluation fixture is invalid' "
    "WHEN 'validation_exhausted' THEN 'evaluation output validation exhausted' "
    "WHEN 'internal_error' THEN 'evaluation failed' "
    "WHEN 'cancelled' THEN 'evaluation cancelled after provider execution' END"
)
AUDIT_SAFE_ERROR_V1_SQL = AUDIT_SAFE_ERROR_V2_SQL.replace(", 'cancelled'", "").replace(
    " WHEN 'cancelled' THEN 'evaluation cancelled after provider execution'", ""
)
PROVIDER_TERMINAL_ERRORS_V2_SQL = (
    "'timeout', 'network', 'authentication', 'rate_limited', 'upstream', "
    "'protocol', 'response_too_large', 'fixture', 'internal_error', 'cancelled'"
)
PROVIDER_TERMINAL_ERRORS_V1_SQL = PROVIDER_TERMINAL_ERRORS_V2_SQL.replace(", 'cancelled'", "")


def _retry_evidence_sql(provider_errors: str) -> str:
    return (
        "((final_report_id IS NOT NULL AND "
        "((validation_status = 'valid' AND retry_count = 0 "
        "AND jsonb_array_length(validation_failures) = 0) OR "
        "(validation_status = 'repaired' AND retry_count BETWEEN 1 AND 2 "
        "AND jsonb_array_length(validation_failures) = retry_count))) OR "
        "(final_report_id IS NULL AND ((error_type = 'configuration' "
        "AND retry_count = 0 AND jsonb_array_length(validation_failures) = 0) OR "
        "(error_type = 'validation_exhausted' "
        "AND jsonb_array_length(validation_failures) = retry_count + 1) OR "
        f"(error_type IN ({provider_errors}) "
        "AND jsonb_array_length(validation_failures) = retry_count))))"
    )


AUDIT_EVIDENCE_FUNCTION_V2 = """
CREATE OR REPLACE FUNCTION validate_audit_log_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    job_record evaluation_jobs%ROWTYPE;
    report_record evaluation_reports%ROWTYPE;
BEGIN
    SELECT * INTO job_record FROM evaluation_jobs
    WHERE id = NEW.job_id AND submission_id = NEW.submission_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;
    IF NOT (NEW.provider IS NOT DISTINCT FROM job_record.provider)
       OR NOT (NEW.model IS NOT DISTINCT FROM job_record.model) THEN
        RAISE EXCEPTION 'audit evidence does not match evaluation job'
            USING ERRCODE = '23514';
    END IF;
    IF NOT (
        NEW.retry_count IS NOT DISTINCT FROM GREATEST(job_record.attempt_count - 1, 0)
    ) THEN
        RAISE EXCEPTION 'audit retry count does not match job attempts'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.final_report_id IS NOT NULL THEN
        IF job_record.status <> 'succeeded' THEN
            RAISE EXCEPTION 'successful audit requires a succeeded job'
                USING ERRCODE = '23514';
        END IF;
        SELECT * INTO report_record FROM evaluation_reports
        WHERE id = NEW.final_report_id
          AND submission_id = NEW.submission_id
          AND job_id = NEW.job_id;
        IF FOUND AND (
            NOT (NEW.raw_model_output IS NOT DISTINCT FROM report_record.raw_model_output)
            OR NOT (NEW.schema_version IS NOT DISTINCT FROM report_record.schema_version)
            OR NOT (NEW.validation_status IS NOT DISTINCT FROM report_record.validation_status)
        ) THEN
            RAISE EXCEPTION 'audit evidence does not match final report'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF job_record.status NOT IN ('failed', 'cancelled') THEN
            RAISE EXCEPTION 'terminal audit requires a failed or cancelled job'
                USING ERRCODE = '23514';
        END IF;
        IF job_record.status = 'cancelled' THEN
            IF NEW.error_type <> 'cancelled'
               OR NEW.error_message <> 'evaluation cancelled after provider execution'
               OR job_record.error_code IS NOT NULL
               OR job_record.error_message IS NOT NULL
               OR job_record.attempt_count < 1
               OR NEW.raw_model_output IS NULL
               OR octet_length(NEW.raw_model_output) < 1 THEN
                RAISE EXCEPTION 'cancelled audit evidence does not match evaluation job'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            IF NOT (NEW.error_type IS NOT DISTINCT FROM job_record.error_code)
               OR NOT (NEW.error_message IS NOT DISTINCT FROM job_record.error_message) THEN
                RAISE EXCEPTION 'audit evidence does not match evaluation job error'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
"""

AUDIT_EVIDENCE_FUNCTION_V1 = (
    AUDIT_EVIDENCE_FUNCTION_V2.replace(
        "job_record.status NOT IN ('failed', 'cancelled')",
        "job_record.status <> 'failed'",
    )
    .replace(
        "terminal audit requires a failed or cancelled job",
        "failed audit requires a failed job",
    )
    .replace(
        """        IF job_record.status = 'cancelled' THEN
            IF NEW.error_type <> 'cancelled'
               OR NEW.error_message <> 'evaluation cancelled after provider execution'
               OR job_record.error_code IS NOT NULL
               OR job_record.error_message IS NOT NULL
               OR job_record.attempt_count < 1 THEN
                RAISE EXCEPTION 'cancelled audit evidence does not match evaluation job'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            IF NOT (NEW.error_type IS NOT DISTINCT FROM job_record.error_code)
               OR NOT (NEW.error_message IS NOT DISTINCT FROM job_record.error_message) THEN
                RAISE EXCEPTION 'audit evidence does not match evaluation job error'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
""",
        """        IF NOT (NEW.error_type IS NOT DISTINCT FROM job_record.error_code)
           OR NOT (NEW.error_message IS NOT DISTINCT FROM job_record.error_message) THEN
            RAISE EXCEPTION 'audit evidence does not match evaluation job error'
                USING ERRCODE = '23514';
        END IF;
""",
    )
)

PROTECT_AUDITED_JOB_V2 = """
CREATE OR REPLACE FUNCTION protect_audited_evaluation_job()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF EXISTS (SELECT 1 FROM audit_logs WHERE job_id = OLD.id)
       AND ROW(
           NEW.id, NEW.submission_id, NEW.requested_by, NEW.reason, NEW.status,
           NEW.idempotency_key, NEW.attempt_count, NEW.provider, NEW.model,
           NEW.execution_token, NEW.execution_generation,
           NEW.error_code, NEW.error_message, NEW.queued_at,
           NEW.started_at, NEW.finished_at
       ) IS DISTINCT FROM ROW(
           OLD.id, OLD.submission_id, OLD.requested_by, OLD.reason, OLD.status,
           OLD.idempotency_key, OLD.attempt_count, OLD.provider, OLD.model,
           OLD.execution_token, OLD.execution_generation,
           OLD.error_code, OLD.error_message, OLD.queued_at,
           OLD.started_at, OLD.finished_at
       ) THEN
        RAISE EXCEPTION 'audited evaluation job evidence is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$function$
"""

PROTECT_AUDITED_JOB_V1 = """
CREATE OR REPLACE FUNCTION protect_audited_evaluation_job()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF EXISTS (SELECT 1 FROM audit_logs WHERE job_id = OLD.id)
       AND ROW(
           NEW.id, NEW.submission_id, NEW.requested_by, NEW.reason, NEW.status,
           NEW.idempotency_key, NEW.attempt_count, NEW.provider, NEW.model,
           NEW.error_code, NEW.error_message, NEW.queued_at,
           NEW.started_at, NEW.finished_at
       ) IS DISTINCT FROM ROW(
           OLD.id, OLD.submission_id, OLD.requested_by, OLD.reason, OLD.status,
           OLD.idempotency_key, OLD.attempt_count, OLD.provider, OLD.model,
           OLD.error_code, OLD.error_message, OLD.queued_at,
           OLD.started_at, OLD.finished_at
       ) THEN
        RAISE EXCEPTION 'audited evaluation job evidence is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$function$
"""


def _replace_audit_constraints(*, safe_error_sql: str, provider_errors: str) -> None:
    op.drop_constraint("ck_audit_logs_retry_evidence", "audit_logs", type_="check")
    op.drop_constraint("ck_audit_logs_error_type_safe", "audit_logs", type_="check")
    op.create_check_constraint(
        "ck_audit_logs_retry_evidence",
        "audit_logs",
        _retry_evidence_sql(provider_errors),
    )
    op.create_check_constraint(
        "ck_audit_logs_error_type_safe",
        "audit_logs",
        f"error_type IS NULL OR ({safe_error_sql})",
    )


def upgrade() -> None:
    op.add_column(
        "evaluation_jobs",
        sa.Column("execution_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "evaluation_jobs",
        sa.Column(
            "execution_generation",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE evaluation_jobs
        SET execution_token = md5('execution:' || id::text)::uuid,
            execution_generation = 1
        WHERE status = 'running'
        """
    )
    op.create_check_constraint(
        "ck_evaluation_jobs_execution_generation",
        "evaluation_jobs",
        "execution_generation >= 0",
    )
    op.create_check_constraint(
        "ck_evaluation_jobs_execution_token_state",
        "evaluation_jobs",
        "((status = 'running' AND execution_token IS NOT NULL) OR "
        "(status <> 'running' AND execution_token IS NULL))",
    )
    op.execute(PROTECT_AUDITED_JOB_V2)
    _replace_audit_constraints(
        safe_error_sql=AUDIT_SAFE_ERROR_V2_SQL,
        provider_errors=PROVIDER_TERMINAL_ERRORS_V2_SQL,
    )
    op.execute(
        "ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS ck_audit_logs_cancelled_provider_evidence"
    )
    op.create_check_constraint(
        "ck_audit_logs_cancelled_provider_evidence",
        "audit_logs",
        "error_type <> 'cancelled' OR raw_model_output IS NOT NULL",
    )
    op.execute(AUDIT_EVIDENCE_FUNCTION_V2)
    op.create_unique_constraint(
        "uq_evaluation_reports_id_job",
        "evaluation_reports",
        ["id", "job_id"],
    )
    op.create_table(
        "evaluation_outbox",
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('dispatch', 'notification')",
            name="ck_evaluation_outbox_kind",
        ),
        sa.CheckConstraint(
            "((kind = 'dispatch' AND report_id IS NULL) OR "
            "(kind = 'notification' AND report_id IS NOT NULL))",
            name="ck_evaluation_outbox_kind_payload",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 1000",
            name="ck_evaluation_outbox_attempt_count",
        ),
        sa.CheckConstraint(
            "last_error_type IS NULL OR is_safe_identifier(last_error_type, 128)",
            name="ck_evaluation_outbox_error_type_safe",
        ),
        sa.CheckConstraint(
            "delivered_at IS NULL OR delivered_at >= created_at",
            name="ck_evaluation_outbox_delivery_order",
        ),
        sa.CheckConstraint(
            "((claim_token IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claim_expires_at IS NOT NULL))",
            name="ck_evaluation_outbox_claim_state",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["evaluation_jobs.id"],
            name="fk_evaluation_outbox_job_id_evaluation_jobs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "job_id", name="uq_evaluation_outbox_kind_job"),
    )
    op.create_index(
        "ix_evaluation_outbox_pending_next_id",
        "evaluation_outbox",
        ["next_attempt_at", "id"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.execute(
        """
        INSERT INTO evaluation_outbox
            (id, kind, job_id, report_id, attempt_count, next_attempt_at,
             delivered_at, created_at)
        SELECT md5('dispatch:' || id::text)::uuid, 'dispatch', id, NULL, 0, now(),
               CASE WHEN status IN ('queued', 'running') THEN NULL ELSE now() END,
               queued_at
        FROM evaluation_jobs
        ON CONFLICT (kind, job_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO evaluation_outbox
            (id, kind, job_id, report_id, attempt_count, next_attempt_at,
             delivered_at, created_at)
        SELECT md5('notification:' || job.id::text)::uuid, 'notification', job.id,
               report.id, 0, now(), now(), report.created_at
        FROM evaluation_jobs AS job
        JOIN evaluation_reports AS report ON report.job_id = job.id
        WHERE job.status = 'succeeded'
        ON CONFLICT (kind, job_id) DO NOTHING
        """
    )
    op.execute(
        """
        DO $function$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM evaluation_outbox AS event
                LEFT JOIN evaluation_reports AS report
                  ON report.id = event.report_id AND report.job_id = event.job_id
                WHERE event.report_id IS NOT NULL AND report.id IS NULL
            ) THEN
                RAISE EXCEPTION 'evaluation outbox report must belong to its job'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $function$
        """
    )
    op.create_foreign_key(
        "fk_evaluation_outbox_report_job",
        "evaluation_outbox",
        "evaluation_reports",
        ["report_id", "job_id"],
        ["id", "job_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_outbox_pending_next_id", table_name="evaluation_outbox")
    op.drop_table("evaluation_outbox")
    op.drop_constraint(
        "uq_evaluation_reports_id_job",
        "evaluation_reports",
        type_="unique",
    )
    op.execute(PROTECT_AUDITED_JOB_V1)
    op.drop_constraint(
        "ck_evaluation_jobs_execution_token_state",
        "evaluation_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_jobs_execution_generation",
        "evaluation_jobs",
        type_="check",
    )
    op.drop_column("evaluation_jobs", "execution_generation")
    op.drop_column("evaluation_jobs", "execution_token")
    # Keep the cancelled-compatible audit function and checks at revision 0002.
    # Audit rows are append-only, so weakening these constraints would either fail
    # the downgrade or require mutating evidence. 0002 code remains compatible.
