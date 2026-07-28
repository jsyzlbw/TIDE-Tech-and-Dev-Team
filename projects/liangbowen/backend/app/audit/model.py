from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.contracts.evaluation_v1 import CREATE_EVIDENCE_TRUNCATE_FUNCTION_SQL
from app.db.types import enum_values
from app.evaluations.types import ValidationStatus

SAFE_ERROR_SQL = (
    "error_type IN ('configuration', 'timeout', 'network', 'authentication', "
    "'rate_limited', 'upstream', 'protocol', 'response_too_large', 'fixture', "
    "'validation_exhausted', 'internal_error', 'cancelled') AND error_message = CASE error_type "
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
PROVIDER_TERMINAL_ERRORS_SQL = (
    "'timeout', 'network', 'authentication', 'rate_limited', 'upstream', "
    "'protocol', 'response_too_large', 'fixture', 'internal_error', 'cancelled'"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_audit_logs_job_id"),
        ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.id", "evaluation_jobs.submission_id"],
            name="fk_audit_logs_job_submission",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["final_report_id", "submission_id", "job_id"],
            [
                "evaluation_reports.id",
                "evaluation_reports.submission_id",
                "evaluation_reports.job_id",
            ],
            name="fk_audit_logs_final_report_job",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "is_safe_identifier(provider, 128)",
            name="ck_audit_logs_provider_safe",
        ),
        CheckConstraint(
            "is_safe_identifier(model, 256)",
            name="ck_audit_logs_model_safe",
        ),
        CheckConstraint(
            "octet_length(prompt_template_version) BETWEEN 1 AND 64 "
            "AND prompt_template_version = btrim(prompt_template_version) "
            "AND prompt_template_version ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'",
            name="ck_audit_logs_prompt_version_safe",
        ),
        CheckConstraint("schema_version = '1.0'", name="ck_audit_logs_schema_version"),
        CheckConstraint(
            "duration_ms BETWEEN 0 AND 2147483647",
            name="ck_audit_logs_duration_range",
        ),
        CheckConstraint("retry_count BETWEEN 0 AND 2", name="ck_audit_logs_retry_range"),
        CheckConstraint(
            "jsonb_typeof(validation_failures) = 'array' "
            "AND jsonb_array_length(validation_failures) <= 3 "
            "AND pg_column_size(validation_failures) <= 4096 "
            "AND validation_failures <@ "
            '\'["invalid_json","invalid_shape","schema","semantic"]\'::jsonb',
            name="ck_audit_logs_validation_failures",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "raw_model_output IS NULL OR (octet_length(raw_model_output) BETWEEN 1 AND 2097152)",
            name="ck_audit_logs_raw_output_size",
        ),
        CheckConstraint(
            "error_type <> 'cancelled' OR raw_model_output IS NOT NULL",
            name="ck_audit_logs_cancelled_provider_evidence",
        ),
        CheckConstraint(
            "((final_report_id IS NOT NULL AND raw_model_output IS NOT NULL "
            "AND validation_status IS NOT NULL AND error_type IS NULL AND error_message IS NULL) "
            "OR (final_report_id IS NULL AND validation_status IS NULL "
            "AND error_type IS NOT NULL AND error_message IS NOT NULL))",
            name="ck_audit_logs_outcome_fields",
        ),
        CheckConstraint(
            f"error_type IS NULL OR ({SAFE_ERROR_SQL})",
            name="ck_audit_logs_error_type_safe",
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    submission_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(16),
        default="1.0",
        server_default="1.0",
        nullable=False,
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_model_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[ValidationStatus | None] = mapped_column(
        Enum(ValidationStatus, name="validation_status", values_callable=enum_values),
        nullable=True,
    )
    validation_failures: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_report_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )


Index(
    "ix_audit_logs_submission_created_id",
    AuditLog.submission_id,
    AuditLog.created_at.desc(),
    AuditLog.id.desc(),
)
Index("ix_audit_logs_created_id", AuditLog.created_at.desc(), AuditLog.id.desc())


event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(CREATE_EVIDENCE_TRUNCATE_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_evaluation_evidence_truncate()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION validate_audit_log_evidence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            job_record evaluation_jobs%%ROWTYPE;
            report_record evaluation_reports%%ROWTYPE;
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
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_audit_logs_evidence
        BEFORE INSERT ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION validate_audit_log_evidence()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_log_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'audit_logs are append-only'
                USING ERRCODE = '55000';
        END;
        $function$
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_log_mutation()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS validate_audit_log_evidence()").execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS prevent_audit_log_mutation()").execute_if(dialect="postgresql"),
)
