from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.contracts.evaluation_v1 import (
    CREATE_EVIDENCE_TRUNCATE_FUNCTION_SQL,
    CREATE_SAFE_IDENTIFIER_FUNCTION_SQL,
    DROP_EVIDENCE_TRUNCATE_FUNCTION_SQL,
    DROP_SAFE_IDENTIFIER_FUNCTION_SQL,
)
from app.db.types import Grade, enum_values
from app.evaluations.types import (
    JobReason,
    JobStatus,
    ReportOrigin,
    ReviewStatus,
    ValidationStatus,
)
from app.integrations.mattermost.ids import MATTERMOST_ID_SQL

MAX_JOB_ATTEMPTS = 3
MAX_RAW_MODEL_OUTPUT_BYTES = 2 * 1024 * 1024
SAFE_ERROR_SQL = (
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
TERMINAL_ATTEMPT_SQL = (
    "status NOT IN ('succeeded', 'failed') "
    "OR (status = 'succeeded' AND attempt_count BETWEEN 1 AND 3) "
    "OR (status = 'failed' AND ("
    "(error_code = 'configuration' AND attempt_count = 0) OR "
    "(error_code IN ('timeout', 'network', 'authentication', 'rate_limited', "
    "'upstream', 'protocol', 'response_too_large', 'fixture', "
    "'validation_exhausted', 'internal_error') AND attempt_count BETWEEN 1 AND 3)))"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EvaluationJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "submission_id",
            name="uq_evaluation_jobs_id_submission",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_evaluation_jobs_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["source_report_id", "submission_id"],
            ["evaluation_reports.id", "evaluation_reports.submission_id"],
            name="fk_evaluation_jobs_source_submission",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "((reason = 'manual_retry' AND source_report_id IS NOT NULL) OR "
            "(reason <> 'manual_retry' AND source_report_id IS NULL))",
            name="ck_evaluation_jobs_manual_source",
        ),
        CheckConstraint(
            f"attempt_count BETWEEN 0 AND {MAX_JOB_ATTEMPTS}",
            name="ck_evaluation_jobs_attempt_count_range",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_jobs_idempotency_key",
        ),
        CheckConstraint(
            "is_safe_identifier(provider, 128)",
            name="ck_evaluation_jobs_provider_safe",
        ),
        CheckConstraint(
            "is_safe_identifier(model, 256)",
            name="ck_evaluation_jobs_model_safe",
        ),
        CheckConstraint(
            "((status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) "
            "OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL))",
            name="ck_evaluation_jobs_error_fields",
        ),
        CheckConstraint(
            f"error_code IS NULL OR ({SAFE_ERROR_SQL})",
            name="ck_evaluation_jobs_error_safe",
        ),
        CheckConstraint(
            TERMINAL_ATTEMPT_SQL,
            name="ck_evaluation_jobs_terminal_attempt_contract",
        ),
        CheckConstraint(
            "execution_generation >= 0",
            name="ck_evaluation_jobs_execution_generation",
        ),
        CheckConstraint(
            "((status = 'running' AND execution_token IS NOT NULL) OR "
            "(status <> 'running' AND execution_token IS NULL))",
            name="ck_evaluation_jobs_execution_token_state",
        ),
        CheckConstraint(
            "((status = 'queued' AND started_at IS NULL AND finished_at IS NULL) "
            "OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) "
            "OR (status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL) "
            "OR (status IN ('failed', 'cancelled') AND finished_at IS NOT NULL))",
            name="ck_evaluation_jobs_status_timestamps",
        ),
        CheckConstraint(
            "(started_at IS NULL OR started_at >= queued_at) "
            "AND (finished_at IS NULL OR finished_at >= queued_at) "
            "AND (started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at)",
            name="ck_evaluation_jobs_timestamp_order",
        ),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_report_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    reason: Mapped[JobReason] = mapped_column(
        Enum(JobReason, name="job_reason", values_callable=enum_values),
        nullable=False,
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=enum_values),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    execution_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    execution_generation: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index(
    "ix_evaluation_jobs_submission_queued_id",
    EvaluationJob.submission_id,
    EvaluationJob.queued_at.desc(),
    EvaluationJob.id.desc(),
)
Index(
    "ix_evaluation_jobs_status_queued_id",
    EvaluationJob.status,
    EvaluationJob.queued_at,
    EvaluationJob.id,
)
Index(
    "ix_evaluation_jobs_source_report_id",
    EvaluationJob.source_report_id,
)


event.listen(
    EvaluationJob.__table__,
    "before_create",
    DDL(CREATE_SAFE_IDENTIFIER_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationJob.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION validate_evaluation_job_requester()
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
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationJob.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_evaluation_jobs_requester
        BEFORE INSERT OR UPDATE OF requested_by ON evaluation_jobs
        FOR EACH ROW EXECUTE FUNCTION validate_evaluation_job_requester()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationJob.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION protect_audited_evaluation_job()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF EXISTS (SELECT 1 FROM audit_logs WHERE job_id = OLD.id)
               AND ROW(
                   NEW.id, NEW.submission_id, NEW.requested_by, NEW.source_report_id,
                   NEW.reason, NEW.status,
                   NEW.idempotency_key, NEW.attempt_count, NEW.provider, NEW.model,
                   NEW.execution_token, NEW.execution_generation,
                   NEW.error_code, NEW.error_message, NEW.queued_at,
                   NEW.started_at, NEW.finished_at
               ) IS DISTINCT FROM ROW(
                   OLD.id, OLD.submission_id, OLD.requested_by, OLD.source_report_id,
                   OLD.reason, OLD.status,
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
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationJob.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_evaluation_jobs_audited_immutable
        BEFORE UPDATE ON evaluation_jobs
        FOR EACH ROW EXECUTE FUNCTION protect_audited_evaluation_job()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationJob.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS protect_audited_evaluation_job()").execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    EvaluationJob.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS validate_evaluation_job_requester()").execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    EvaluationJob.__table__,
    "after_drop",
    DDL(DROP_SAFE_IDENTIFIER_FUNCTION_SQL).execute_if(dialect="postgresql"),
)


class EvaluationReport(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evaluation_reports"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "version",
            name="uq_evaluation_reports_submission_version",
        ),
        UniqueConstraint("job_id", name="uq_evaluation_reports_job_id"),
        UniqueConstraint(
            "id",
            "submission_id",
            name="uq_evaluation_reports_id_submission",
        ),
        UniqueConstraint(
            "id",
            "submission_id",
            "job_id",
            name="uq_evaluation_reports_id_submission_job",
        ),
        UniqueConstraint(
            "id",
            "job_id",
            name="uq_evaluation_reports_id_job",
        ),
        ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.id", "evaluation_jobs.submission_id"],
            name="fk_evaluation_reports_job_submission",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_report_id", "submission_id"],
            ["evaluation_reports.id", "evaluation_reports.submission_id"],
            name="fk_evaluation_reports_source_submission",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version >= 1", name="ck_evaluation_reports_version_positive"),
        CheckConstraint("schema_version = '1.0'", name="ck_evaluation_reports_schema_version"),
        CheckConstraint("score BETWEEN 0 AND 100", name="ck_evaluation_reports_score_range"),
        CheckConstraint(
            "confidence BETWEEN 0 AND 1",
            name="ck_evaluation_reports_confidence_range",
        ),
        CheckConstraint(
            "score NOT BETWEEN 0 AND 100 OR ((score >= 90 AND grade = 'A') OR "
            "(score BETWEEN 75 AND 89 AND grade = 'B') OR "
            "(score BETWEEN 60 AND 74 AND grade = 'C') OR "
            "(score BETWEEN 0 AND 59 AND grade = 'D'))",
            name="ck_evaluation_reports_score_grade",
        ),
        CheckConstraint(
            "jsonb_typeof(completeness) = 'object' AND pg_column_size(completeness) <= 65536",
            name="ck_evaluation_reports_completeness_object",
        ),
        CheckConstraint(
            "jsonb_typeof(correctness) = 'object' AND pg_column_size(correctness) <= 65536",
            name="ck_evaluation_reports_correctness_object",
        ),
        CheckConstraint(
            "jsonb_typeof(major_issues) = 'array' AND pg_column_size(major_issues) <= 262144",
            name="ck_evaluation_reports_major_issues_array",
        ),
        CheckConstraint(
            "jsonb_typeof(suggestions) = 'array' AND pg_column_size(suggestions) <= 262144",
            name="ck_evaluation_reports_suggestions_array",
        ),
        CheckConstraint(
            "jsonb_typeof(limitations) = 'array' AND pg_column_size(limitations) <= 65536",
            name="ck_evaluation_reports_limitations_array",
        ),
        CheckConstraint(
            "((origin = 'agent' AND job_id IS NOT NULL AND source_report_id IS NULL "
            "AND created_by_teacher_id IS NULL AND raw_model_output IS NOT NULL) "
            "OR (origin = 'teacher' AND job_id IS NULL AND source_report_id IS NOT NULL "
            "AND created_by_teacher_id IS NOT NULL AND raw_model_output IS NULL))",
            name="ck_evaluation_reports_origin_fields",
        ),
        CheckConstraint(
            f"raw_model_output IS NULL OR (octet_length(raw_model_output) BETWEEN 1 AND "
            f"{MAX_RAW_MODEL_OUTPUT_BYTES})",
            name="ck_evaluation_reports_raw_output_size",
        ),
        CheckConstraint(
            "source_report_id IS NULL OR source_report_id <> id",
            name="ck_evaluation_reports_source_not_self",
        ),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    source_report_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    origin: Mapped[ReportOrigin] = mapped_column(
        Enum(ReportOrigin, name="report_origin", values_callable=enum_values),
        nullable=False,
    )
    created_by_teacher_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(16),
        default="1.0",
        server_default="1.0",
        nullable=False,
    )
    completeness: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    correctness: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    major_issues: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    suggestions: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    grade: Mapped[Grade] = mapped_column(
        Enum(Grade, name="grade", values_callable=enum_values),
        nullable=False,
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    limitations: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    raw_model_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[ValidationStatus] = mapped_column(
        Enum(ValidationStatus, name="validation_status", values_callable=enum_values),
        nullable=False,
    )
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status", values_callable=enum_values),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )


Index(
    "ix_evaluation_reports_submission_version_desc",
    EvaluationReport.submission_id,
    EvaluationReport.version.desc(),
)
Index(
    "ix_evaluation_reports_review_status_created_id",
    EvaluationReport.review_status,
    EvaluationReport.created_at.desc(),
    EvaluationReport.id.desc(),
)


event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(CREATE_EVIDENCE_TRUNCATE_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_evaluation_reports_no_truncate
        BEFORE TRUNCATE ON evaluation_reports
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_evaluation_evidence_truncate()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION validate_evaluation_report_lineage()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF NEW.origin = 'teacher' THEN
                IF NEW.created_by_teacher_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM users
                    WHERE id = NEW.created_by_teacher_id
                      AND role = 'teacher'
                      AND is_active
                ) THEN
                    RAISE EXCEPTION 'report editor must be an active teacher'
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
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION enforce_evaluation_report_version_sequence()
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
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_evaluation_reports_version_sequence
        BEFORE INSERT ON evaluation_reports
        FOR EACH ROW EXECUTE FUNCTION enforce_evaluation_report_version_sequence()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION protect_evaluation_report_version()
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
            IF NEW.review_status IS DISTINCT FROM OLD.review_status
               AND NOT (
                   (OLD.review_status = 'proposed'
                    AND NEW.review_status IN ('confirmed', 'superseded'))
                   OR (OLD.review_status IN ('modified', 'confirmed')
                       AND NEW.review_status = 'superseded')
               ) THEN
                RAISE EXCEPTION 'invalid evaluation report review status transition'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_evaluation_reports_immutable
        BEFORE UPDATE OR DELETE ON evaluation_reports
        FOR EACH ROW EXECUTE FUNCTION protect_evaluation_report_version()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_evaluation_reports_lineage
        BEFORE INSERT OR UPDATE OF origin, source_report_id, created_by_teacher_id, submission_id
        ON evaluation_reports
        FOR EACH ROW EXECUTE FUNCTION validate_evaluation_report_lineage()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    EvaluationReport.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS enforce_evaluation_report_version_sequence()").execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    EvaluationReport.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS protect_evaluation_report_version()").execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    EvaluationReport.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS validate_evaluation_report_lineage()").execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    EvaluationReport.__table__,
    "after_drop",
    DDL(DROP_EVIDENCE_TRUNCATE_FUNCTION_SQL).execute_if(dialect="postgresql"),
)


class EvaluationOutbox(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evaluation_outbox"
    __table_args__ = (
        UniqueConstraint("kind", "job_id", name="uq_evaluation_outbox_kind_job"),
        ForeignKeyConstraint(
            ["report_id", "job_id"],
            ["evaluation_reports.id", "evaluation_reports.job_id"],
            name="fk_evaluation_outbox_report_job",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('dispatch', 'notification')",
            name="ck_evaluation_outbox_kind",
        ),
        CheckConstraint(
            "((kind = 'dispatch' AND report_id IS NULL) OR "
            "(kind = 'notification' AND report_id IS NOT NULL))",
            name="ck_evaluation_outbox_kind_payload",
        ),
        CheckConstraint(
            "((kind = 'dispatch' AND attempt_count BETWEEN 0 AND 1000) OR "
            "(kind = 'notification' AND attempt_count BETWEEN 0 AND 3))",
            name="ck_evaluation_outbox_attempt_count",
        ),
        CheckConstraint(
            "last_error_type IS NULL OR is_safe_identifier(last_error_type, 128)",
            name="ck_evaluation_outbox_error_type_safe",
        ),
        CheckConstraint(
            "delivered_at IS NULL OR delivered_at >= created_at",
            name="ck_evaluation_outbox_delivery_order",
        ),
        CheckConstraint(
            "failed_at IS NULL OR failed_at >= created_at",
            name="ck_evaluation_outbox_failure_order",
        ),
        CheckConstraint(
            f"delivery_ref IS NULL OR delivery_ref ~ '{MATTERMOST_ID_SQL}'",
            name="ck_evaluation_outbox_delivery_ref_safe",
        ),
        CheckConstraint(
            "last_error_summary IS NULL OR last_error_summary = 'Mattermost delivery failed'",
            name="ck_evaluation_outbox_error_summary_safe",
        ),
        CheckConstraint(
            "((claim_token IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claim_expires_at IS NOT NULL))",
            name="ck_evaluation_outbox_claim_state",
        ),
        CheckConstraint(
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
            "AND claim_expires_at IS NULL))))",
            name="ck_evaluation_outbox_terminal_state",
        ),
    )

    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivery_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )


Index(
    "ix_evaluation_outbox_pending_next_id",
    EvaluationOutbox.next_attempt_at,
    EvaluationOutbox.id,
    postgresql_where=(
        EvaluationOutbox.delivered_at.is_(None) & EvaluationOutbox.failed_at.is_(None)
    ),
)
