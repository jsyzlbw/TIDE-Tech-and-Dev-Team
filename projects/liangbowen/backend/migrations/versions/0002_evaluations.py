"""Persist evaluation jobs, versioned reports, and immutable audit evidence.

Revision ID: 0002_evaluations
Revises: 0001_domain
Create Date: 2026-07-26 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# This released revision imports only its immutable v1 contract. Future
# identifier changes must add a new versioned module and migration.
from app.db.contracts.evaluation_v1 import (
    CREATE_EVIDENCE_TRUNCATE_FUNCTION_SQL,
    CREATE_SAFE_IDENTIFIER_FUNCTION_SQL,
    DROP_EVIDENCE_TRUNCATE_FUNCTION_SQL,
    DROP_SAFE_IDENTIFIER_FUNCTION_SQL,
)

revision: str = "0002_evaluations"
down_revision: str | None = "0001_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

job_reason = postgresql.ENUM(
    "initial", "manual_retry", "provider_retry", name="job_reason", create_type=False
)
job_status = postgresql.ENUM(
    "queued", "running", "succeeded", "failed", "cancelled", name="job_status", create_type=False
)
report_origin = postgresql.ENUM("agent", "teacher", name="report_origin", create_type=False)
review_status = postgresql.ENUM(
    "proposed", "confirmed", "modified", "superseded", name="review_status", create_type=False
)
validation_status = postgresql.ENUM(
    "valid", "repaired", name="validation_status", create_type=False
)
grade = postgresql.ENUM("A", "B", "C", "D", name="grade", create_type=False)
JOB_SAFE_ERROR_SQL = (
    "error_code IN ('configuration', 'timeout', 'network', 'authentication', "
    "'rate_limited', 'upstream', 'protocol', 'response_too_large', 'fixture', "
    "'validation_exhausted', 'internal_error') AND error_message = CASE error_code "
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
    "WHEN 'internal_error' THEN 'evaluation failed' END"
)
AUDIT_SAFE_ERROR_SQL = JOB_SAFE_ERROR_SQL.replace("error_code", "error_type")
JOB_TERMINAL_ATTEMPT_SQL = (
    "status NOT IN ('succeeded', 'failed') "
    "OR (status = 'succeeded' AND attempt_count BETWEEN 1 AND 3) "
    "OR (status = 'failed' AND ("
    "(error_code = 'configuration' AND attempt_count = 0) OR "
    "(error_code IN ('timeout', 'network', 'authentication', 'rate_limited', "
    "'upstream', 'protocol', 'response_too_large', 'fixture', "
    "'validation_exhausted', 'internal_error') AND attempt_count BETWEEN 1 AND 3)))"
)
PROVIDER_TERMINAL_ERRORS_SQL = (
    "'timeout', 'network', 'authentication', 'rate_limited', 'upstream', "
    "'protocol', 'response_too_large', 'fixture', 'internal_error'"
)


def upgrade() -> None:
    bind = op.get_bind()
    job_reason.create(bind, checkfirst=False)
    job_status.create(bind, checkfirst=False)
    report_origin.create(bind, checkfirst=False)
    review_status.create(bind, checkfirst=False)
    validation_status.create(bind, checkfirst=False)
    grade.create(bind, checkfirst=False)

    op.execute(CREATE_SAFE_IDENTIFIER_FUNCTION_SQL)
    op.create_table(
        "evaluation_jobs",
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", job_reason, nullable=False),
        sa.Column("status", job_status, server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3", name="ck_evaluation_jobs_attempt_count_range"
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'", name="ck_evaluation_jobs_idempotency_key"
        ),
        sa.CheckConstraint(
            "is_safe_identifier(provider, 128)",
            name="ck_evaluation_jobs_provider_safe",
        ),
        sa.CheckConstraint(
            "is_safe_identifier(model, 256)",
            name="ck_evaluation_jobs_model_safe",
        ),
        sa.CheckConstraint(
            "((status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) "
            "OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL))",
            name="ck_evaluation_jobs_error_fields",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR ({JOB_SAFE_ERROR_SQL})",
            name="ck_evaluation_jobs_error_safe",
        ),
        sa.CheckConstraint(
            JOB_TERMINAL_ATTEMPT_SQL,
            name="ck_evaluation_jobs_terminal_attempt_contract",
        ),
        sa.CheckConstraint(
            "((status = 'queued' AND started_at IS NULL AND finished_at IS NULL) "
            "OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) "
            "OR (status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL) "
            "OR (status IN ('failed', 'cancelled') AND finished_at IS NOT NULL))",
            name="ck_evaluation_jobs_status_timestamps",
        ),
        sa.CheckConstraint(
            "(started_at IS NULL OR started_at >= queued_at) "
            "AND (finished_at IS NULL OR finished_at >= queued_at) "
            "AND (started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at)",
            name="ck_evaluation_jobs_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_evaluation_jobs_submission_id_submissions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_evaluation_jobs_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_jobs"),
        sa.UniqueConstraint("id", "submission_id", name="uq_evaluation_jobs_id_submission"),
        sa.UniqueConstraint("idempotency_key", name="uq_evaluation_jobs_idempotency_key"),
    )
    op.create_index(
        "ix_evaluation_jobs_submission_queued_id",
        "evaluation_jobs",
        ["submission_id", sa.text("queued_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_jobs_status_queued_id",
        "evaluation_jobs",
        ["status", "queued_at", "id"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION validate_evaluation_job_requester()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM users
                WHERE id = NEW.requested_by AND role IN ('teacher', 'admin')
            ) THEN
                RAISE EXCEPTION 'evaluation job requester must be teacher or admin'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_jobs_requester
        BEFORE INSERT OR UPDATE OF requested_by ON evaluation_jobs
        FOR EACH ROW EXECUTE FUNCTION validate_evaluation_job_requester()
        """
    )

    op.create_table(
        "evaluation_reports",
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", report_origin, nullable=False),
        sa.Column("created_by_teacher_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), server_default="1.0", nullable=False),
        sa.Column("completeness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correctness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("major_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("suggestions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("grade", grade, nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_model_output", sa.Text(), nullable=True),
        sa.Column("validation_status", validation_status, nullable=False),
        sa.Column("review_status", review_status, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_evaluation_reports_version_positive"),
        sa.CheckConstraint("schema_version = '1.0'", name="ck_evaluation_reports_schema_version"),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name="ck_evaluation_reports_score_range"),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_evaluation_reports_confidence_range"
        ),
        sa.CheckConstraint(
            "score NOT BETWEEN 0 AND 100 OR ((score >= 90 AND grade = 'A') OR "
            "(score BETWEEN 75 AND 89 AND grade = 'B') OR "
            "(score BETWEEN 60 AND 74 AND grade = 'C') OR "
            "(score BETWEEN 0 AND 59 AND grade = 'D'))",
            name="ck_evaluation_reports_score_grade",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(completeness) = 'object' AND pg_column_size(completeness) <= 65536",
            name="ck_evaluation_reports_completeness_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(correctness) = 'object' AND pg_column_size(correctness) <= 65536",
            name="ck_evaluation_reports_correctness_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(major_issues) = 'array' AND pg_column_size(major_issues) <= 262144",
            name="ck_evaluation_reports_major_issues_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(suggestions) = 'array' AND pg_column_size(suggestions) <= 262144",
            name="ck_evaluation_reports_suggestions_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(limitations) = 'array' AND pg_column_size(limitations) <= 65536",
            name="ck_evaluation_reports_limitations_array",
        ),
        sa.CheckConstraint(
            "((origin = 'agent' AND job_id IS NOT NULL AND source_report_id IS NULL "
            "AND created_by_teacher_id IS NULL AND raw_model_output IS NOT NULL) "
            "OR (origin = 'teacher' AND job_id IS NULL AND source_report_id IS NOT NULL "
            "AND created_by_teacher_id IS NOT NULL AND raw_model_output IS NULL))",
            name="ck_evaluation_reports_origin_fields",
        ),
        sa.CheckConstraint(
            "raw_model_output IS NULL OR (octet_length(raw_model_output) BETWEEN 1 AND 2097152)",
            name="ck_evaluation_reports_raw_output_size",
        ),
        sa.CheckConstraint(
            "source_report_id IS NULL OR source_report_id <> id",
            name="ck_evaluation_reports_source_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_evaluation_reports_submission_id_submissions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_teacher_id"],
            ["users.id"],
            name="fk_evaluation_reports_created_by_teacher_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.id", "evaluation_jobs.submission_id"],
            name="fk_evaluation_reports_job_submission",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_report_id", "submission_id"],
            ["evaluation_reports.id", "evaluation_reports.submission_id"],
            name="fk_evaluation_reports_source_submission",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_reports"),
        sa.UniqueConstraint(
            "submission_id", "version", name="uq_evaluation_reports_submission_version"
        ),
        sa.UniqueConstraint("job_id", name="uq_evaluation_reports_job_id"),
        sa.UniqueConstraint("id", "submission_id", name="uq_evaluation_reports_id_submission"),
        sa.UniqueConstraint(
            "id",
            "submission_id",
            "job_id",
            name="uq_evaluation_reports_id_submission_job",
        ),
    )
    op.create_index(
        "ix_evaluation_reports_submission_version_desc",
        "evaluation_reports",
        ["submission_id", sa.text("version DESC")],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_reports_review_status_created_id",
        "evaluation_reports",
        ["review_status", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.execute(CREATE_EVIDENCE_TRUNCATE_FUNCTION_SQL)
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_reports_no_truncate
        BEFORE TRUNCATE ON evaluation_reports
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_evaluation_evidence_truncate()
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_evaluation_report_version_sequence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            expected_version integer;
        BEGIN
            IF NEW.version < 1 THEN
                RETURN NEW;
            END IF;
            PERFORM 1 FROM submissions WHERE id = NEW.submission_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN NEW;
            END IF;
            SELECT COALESCE(MAX(version), 0) + 1 INTO expected_version
            FROM evaluation_reports WHERE submission_id = NEW.submission_id;
            IF NEW.version IS DISTINCT FROM expected_version THEN
                RAISE EXCEPTION 'evaluation report version must be the next version'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_reports_version_sequence
        BEFORE INSERT ON evaluation_reports
        FOR EACH ROW EXECUTE FUNCTION enforce_evaluation_report_version_sequence()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_evaluation_report_lineage()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF NEW.origin = 'teacher' THEN
                IF NEW.created_by_teacher_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM users
                    WHERE id = NEW.created_by_teacher_id AND role = 'teacher'
                ) THEN
                    RAISE EXCEPTION 'report editor must be a teacher'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.source_report_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM evaluation_reports
                    WHERE id = NEW.source_report_id
                      AND submission_id = NEW.submission_id
                      AND origin = 'agent'
                ) THEN
                    RAISE EXCEPTION 'teacher reports must reference an agent report'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_reports_lineage
        BEFORE INSERT OR UPDATE OF origin, source_report_id, created_by_teacher_id, submission_id
        ON evaluation_reports
        FOR EACH ROW EXECUTE FUNCTION validate_evaluation_report_lineage()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_evaluation_report_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'evaluation report versions cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(
                NEW.id, NEW.submission_id, NEW.job_id, NEW.source_report_id, NEW.origin,
                NEW.created_by_teacher_id, NEW.version, NEW.schema_version,
                NEW.completeness, NEW.correctness, NEW.major_issues, NEW.suggestions,
                NEW.score, NEW.grade, NEW.confidence, NEW.limitations,
                NEW.raw_model_output, NEW.validation_status, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.id, OLD.submission_id, OLD.job_id, OLD.source_report_id, OLD.origin,
                OLD.created_by_teacher_id, OLD.version, OLD.schema_version,
                OLD.completeness, OLD.correctness, OLD.major_issues, OLD.suggestions,
                OLD.score, OLD.grade, OLD.confidence, OLD.limitations,
                OLD.raw_model_output, OLD.validation_status, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'evaluation report versions are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_reports_immutable
        BEFORE UPDATE OR DELETE ON evaluation_reports
        FOR EACH ROW EXECUTE FUNCTION protect_evaluation_report_version()
        """
    )

    op.create_table(
        "audit_logs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), server_default="1.0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("raw_model_output", sa.Text(), nullable=True),
        sa.Column("validation_status", validation_status, nullable=True),
        sa.Column(
            "validation_failures",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("final_report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "is_safe_identifier(provider, 128)",
            name="ck_audit_logs_provider_safe",
        ),
        sa.CheckConstraint(
            "is_safe_identifier(model, 256)",
            name="ck_audit_logs_model_safe",
        ),
        sa.CheckConstraint(
            "octet_length(prompt_template_version) BETWEEN 1 AND 64 "
            "AND prompt_template_version = btrim(prompt_template_version) "
            "AND prompt_template_version ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'",
            name="ck_audit_logs_prompt_version_safe",
        ),
        sa.CheckConstraint("schema_version = '1.0'", name="ck_audit_logs_schema_version"),
        sa.CheckConstraint(
            "duration_ms BETWEEN 0 AND 2147483647", name="ck_audit_logs_duration_range"
        ),
        sa.CheckConstraint("retry_count BETWEEN 0 AND 2", name="ck_audit_logs_retry_range"),
        sa.CheckConstraint(
            "jsonb_typeof(validation_failures) = 'array' "
            "AND jsonb_array_length(validation_failures) <= 3 "
            "AND pg_column_size(validation_failures) <= 4096 "
            "AND validation_failures <@ "
            '\'["invalid_json","invalid_shape","schema","semantic"]\'::jsonb',
            name="ck_audit_logs_validation_failures",
        ),
        sa.CheckConstraint(
            "((final_report_id IS NOT NULL AND "
            "((validation_status = 'valid' AND retry_count = 0 "
            "AND jsonb_array_length(validation_failures) = 0) OR "
            "(validation_status = 'repaired' AND retry_count BETWEEN 1 AND 2 "
            "AND jsonb_array_length(validation_failures) = retry_count))) OR "
            "(final_report_id IS NULL AND ((error_type = 'configuration' "
            "AND retry_count = 0 AND jsonb_array_length(validation_failures) = 0) OR "
            "(error_type = 'validation_exhausted' "
            "AND jsonb_array_length(validation_failures) = retry_count + 1) OR "
            f"(error_type IN ({PROVIDER_TERMINAL_ERRORS_SQL}) "
            "AND jsonb_array_length(validation_failures) = retry_count))))",
            name="ck_audit_logs_retry_evidence",
        ),
        sa.CheckConstraint(
            "raw_model_output IS NULL OR (octet_length(raw_model_output) BETWEEN 1 AND 2097152)",
            name="ck_audit_logs_raw_output_size",
        ),
        sa.CheckConstraint(
            "((final_report_id IS NOT NULL AND raw_model_output IS NOT NULL "
            "AND validation_status IS NOT NULL AND error_type IS NULL AND error_message IS NULL) "
            "OR (final_report_id IS NULL AND validation_status IS NULL "
            "AND error_type IS NOT NULL AND error_message IS NOT NULL))",
            name="ck_audit_logs_outcome_fields",
        ),
        sa.CheckConstraint(
            f"error_type IS NULL OR ({AUDIT_SAFE_ERROR_SQL})",
            name="ck_audit_logs_error_type_safe",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.id", "evaluation_jobs.submission_id"],
            name="fk_audit_logs_job_submission",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["final_report_id", "submission_id", "job_id"],
            [
                "evaluation_reports.id",
                "evaluation_reports.submission_id",
                "evaluation_reports.job_id",
            ],
            name="fk_audit_logs_final_report_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        sa.UniqueConstraint("job_id", name="uq_audit_logs_job_id"),
    )
    op.create_index(
        "ix_audit_logs_submission_created_id",
        "audit_logs",
        ["submission_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_created_id",
        "audit_logs",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )

    op.execute(CREATE_EVIDENCE_TRUNCATE_FUNCTION_SQL)
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_evaluation_evidence_truncate()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_audit_log_evidence()
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
                    OR NOT (
                        NEW.validation_status IS NOT DISTINCT FROM report_record.validation_status
                    )
                ) THEN
                    RAISE EXCEPTION 'audit evidence does not match final report'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                IF job_record.status <> 'failed' THEN
                    RAISE EXCEPTION 'failed audit requires a failed job'
                        USING ERRCODE = '23514';
                END IF;
                IF NOT (NEW.error_type IS NOT DISTINCT FROM job_record.error_code)
                   OR NOT (NEW.error_message IS NOT DISTINCT FROM job_record.error_message) THEN
                    RAISE EXCEPTION 'audit evidence does not match evaluation job error'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_evidence
        BEFORE INSERT ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION validate_audit_log_evidence()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_audited_evaluation_job()
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
    )
    op.execute(
        """
        CREATE TRIGGER trg_evaluation_jobs_audited_immutable
        BEFORE UPDATE ON evaluation_jobs
        FOR EACH ROW EXECUTE FUNCTION protect_audited_evaluation_job()
        """
    )

    op.execute(
        """
        CREATE FUNCTION prevent_audit_log_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'audit_logs are append-only'
                USING ERRCODE = '55000';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_log_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_audit_logs_no_truncate ON audit_logs")
    op.execute("DROP TRIGGER trg_evaluation_reports_no_truncate ON evaluation_reports")
    op.execute(DROP_EVIDENCE_TRUNCATE_FUNCTION_SQL)
    op.execute("DROP TRIGGER trg_audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION prevent_audit_log_mutation()")
    op.execute("DROP TRIGGER trg_audit_logs_evidence ON audit_logs")
    op.execute("DROP FUNCTION validate_audit_log_evidence()")
    op.execute("DROP TRIGGER trg_evaluation_jobs_audited_immutable ON evaluation_jobs")
    op.execute("DROP FUNCTION protect_audited_evaluation_job()")
    op.drop_table("audit_logs")
    op.execute("DROP TRIGGER trg_evaluation_reports_immutable ON evaluation_reports")
    op.execute("DROP FUNCTION protect_evaluation_report_version()")
    op.execute("DROP TRIGGER trg_evaluation_reports_lineage ON evaluation_reports")
    op.execute("DROP FUNCTION validate_evaluation_report_lineage()")
    op.execute("DROP TRIGGER trg_evaluation_reports_version_sequence ON evaluation_reports")
    op.execute("DROP FUNCTION enforce_evaluation_report_version_sequence()")
    op.execute("DROP TRIGGER trg_evaluation_jobs_requester ON evaluation_jobs")
    op.execute("DROP FUNCTION validate_evaluation_job_requester()")
    op.drop_table("evaluation_reports")
    op.drop_table("evaluation_jobs")
    op.execute(DROP_SAFE_IDENTIFIER_FUNCTION_SQL)

    bind = op.get_bind()
    grade.drop(bind, checkfirst=False)
    validation_status.drop(bind, checkfirst=False)
    review_status.drop(bind, checkfirst=False)
    report_origin.drop(bind, checkfirst=False)
    job_status.drop(bind, checkfirst=False)
    job_reason.drop(bind, checkfirst=False)
