from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.contracts.review_v1 import (
    CREATE_REVIEW_ACTION_CONSTRAINT_TRIGGER_SQL,
    CREATE_REVIEW_ACTION_SOURCE_LOCK_TRIGGER_SQL,
    CREATE_REVIEW_CONSISTENCY_FUNCTION_SQL,
    CREATE_REVIEW_JOB_CONSTRAINT_TRIGGER_SQL,
    CREATE_REVIEW_JOB_SOURCE_LOCK_TRIGGER_SQL,
    CREATE_REVIEW_REPORT_CONSTRAINT_TRIGGER_SQL,
    CREATE_REVIEW_REPORT_SOURCE_LOCK_TRIGGER_SQL,
    CREATE_REVIEW_SOURCE_LOCK_FUNCTION_SQL,
)
from app.db.types import enum_values
from app.reviews.types import ReviewActionType


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ReviewAction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "review_actions"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(changes) = 'object' AND pg_column_size(changes) <= 2097152",
            name="ck_review_actions_changes_object",
        ),
        CheckConstraint(
            "octet_length(comment) <= 8000",
            name="ck_review_actions_comment_bytes",
        ),
        CheckConstraint(
            "char_length(comment) <= 4000",
            name="ck_review_actions_comment_characters",
        ),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_reports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[ReviewActionType] = mapped_column(
        Enum(
            ReviewActionType,
            name="review_action_type",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    changes: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    comment: Mapped[str] = mapped_column(
        Text,
        default="",
        server_default="",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )


Index(
    "uq_review_actions_report_non_reevaluate",
    ReviewAction.report_id,
    ReviewAction.action,
    unique=True,
    postgresql_where=ReviewAction.action != ReviewActionType.REEVALUATE,
)


event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION validate_review_action()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            changed_keys text[];
            expected_changed_keys text[];
            linked_job_id uuid;
            result_report_id uuid;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM users
                WHERE id = NEW.teacher_id
                  AND role = 'teacher'
                  AND is_active
            ) THEN
                RAISE EXCEPTION 'review action requires an active teacher'
                    USING ERRCODE = '23514';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM evaluation_reports WHERE id = NEW.report_id
            ) THEN
                RAISE EXCEPTION 'review action report does not exist'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM jsonb_each(NEW.changes) AS item(key, value)
                WHERE jsonb_typeof(value) <> 'object'
                   OR (SELECT ARRAY_AGG(part ORDER BY part)
                       FROM jsonb_object_keys(value) AS parts(part))
                      IS DISTINCT FROM ARRAY['after', 'before']::text[]
            ) THEN
                RAISE EXCEPTION 'review changes must contain exact before and after values'
                    USING ERRCODE = '23514';
            END IF;

            SELECT ARRAY_AGG(key ORDER BY key) INTO changed_keys
            FROM jsonb_object_keys(NEW.changes) AS keys(key);

            IF NEW.action = 'confirm' THEN
                IF changed_keys IS DISTINCT FROM ARRAY['review_status']::text[]
                   OR NEW.changes #>> '{review_status,before}' <> 'proposed'
                   OR NEW.changes #>> '{review_status,after}' <> 'confirmed'
                   OR NOT EXISTS (
                       SELECT 1 FROM evaluation_reports
                       WHERE id = NEW.report_id AND review_status = 'confirmed'
                   ) THEN
                    RAISE EXCEPTION 'invalid confirm review changes'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.action = 'modify' THEN
                IF NOT (NEW.changes ? 'review_status')
                   OR NOT (NEW.changes ? 'result_report_id')
                   OR EXISTS (
                       SELECT 1 FROM unnest(changed_keys) AS changed(key)
                       WHERE key <> ALL(ARRAY[
                           'comment', 'completeness', 'correctness', 'grade', 'limitations',
                           'major_issues', 'result_report_id', 'review_status', 'score',
                           'suggestions'
                       ]::text[])
                   )
                   OR NEW.changes #>> '{review_status,before}' <> 'proposed'
                   OR NEW.changes #>> '{review_status,after}' <> 'superseded' THEN
                    RAISE EXCEPTION 'invalid modify review changes'
                        USING ERRCODE = '23514';
                END IF;
                BEGIN
                    result_report_id := (NEW.changes #>> '{result_report_id,after}')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'invalid modify result report id'
                        USING ERRCODE = '23514';
                END;
                SELECT ARRAY(
                    SELECT candidate.key
                    FROM (VALUES
                        ('review_status', TRUE),
                        ('result_report_id', TRUE),
                        ('completeness', source.completeness IS DISTINCT FROM result.completeness),
                        ('correctness', source.correctness IS DISTINCT FROM result.correctness),
                        ('major_issues', source.major_issues IS DISTINCT FROM result.major_issues),
                        ('suggestions', source.suggestions IS DISTINCT FROM result.suggestions),
                        ('score', source.score IS DISTINCT FROM result.score),
                        ('grade', source.grade IS DISTINCT FROM result.grade),
                        ('limitations', source.limitations IS DISTINCT FROM result.limitations),
                        ('comment', NEW.comment <> '')
                    ) AS candidate(key, is_changed)
                    WHERE candidate.is_changed
                    ORDER BY candidate.key
                ) INTO expected_changed_keys
                FROM evaluation_reports source
                JOIN evaluation_reports result
                  ON result.id = result_report_id
                 AND result.submission_id = source.submission_id
                WHERE source.id = NEW.report_id
                  AND source.origin = 'agent'
                  AND source.review_status = 'superseded'
                  AND result.origin = 'teacher'
                  AND result.review_status = 'modified'
                  AND result.created_by_teacher_id = NEW.teacher_id
                  AND result.source_report_id = source.id
                  AND result.job_id IS NULL
                  AND result.version = source.version + 1
                  AND result.raw_model_output IS NULL
                  AND result.schema_version IS NOT DISTINCT FROM source.schema_version
                  AND result.confidence IS NOT DISTINCT FROM source.confidence
                  AND result.validation_status IS NOT DISTINCT FROM source.validation_status;
                IF expected_changed_keys IS NULL
                   OR changed_keys IS DISTINCT FROM expected_changed_keys THEN
                    RAISE EXCEPTION 'invalid modify review changes'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.changes #> '{result_report_id,before}' <> 'null'::jsonb
                   OR NOT EXISTS (
                       SELECT 1
                       FROM evaluation_reports source
                       JOIN evaluation_reports result
                         ON result.id = result_report_id
                        AND result.submission_id = source.submission_id
                       WHERE source.id = NEW.report_id
                         AND source.review_status = 'superseded'
                         AND result.origin = 'teacher'
                         AND result.review_status = 'modified'
                         AND result.created_by_teacher_id = NEW.teacher_id
                         AND result.source_report_id = source.id
                         AND (
                             NOT (NEW.changes ? 'completeness')
                             OR (
                                 NEW.changes #> '{completeness,before}'
                                     IS NOT DISTINCT FROM source.completeness
                                 AND NEW.changes #> '{completeness,after}'
                                     IS NOT DISTINCT FROM result.completeness
                             )
                         )
                         AND (
                             NOT (NEW.changes ? 'correctness')
                             OR (
                                 NEW.changes #> '{correctness,before}'
                                     IS NOT DISTINCT FROM source.correctness
                                 AND NEW.changes #> '{correctness,after}'
                                     IS NOT DISTINCT FROM result.correctness
                             )
                         )
                         AND (
                             NOT (NEW.changes ? 'major_issues')
                             OR (
                                 NEW.changes #> '{major_issues,before}'
                                     IS NOT DISTINCT FROM source.major_issues
                                 AND NEW.changes #> '{major_issues,after}'
                                     IS NOT DISTINCT FROM result.major_issues
                             )
                         )
                         AND (
                             NOT (NEW.changes ? 'suggestions')
                             OR (
                                 NEW.changes #> '{suggestions,before}'
                                     IS NOT DISTINCT FROM source.suggestions
                                 AND NEW.changes #> '{suggestions,after}'
                                     IS NOT DISTINCT FROM result.suggestions
                             )
                         )
                         AND (
                             NOT (NEW.changes ? 'score')
                             OR (
                                 NEW.changes #> '{score,before}'
                                     IS NOT DISTINCT FROM to_jsonb(source.score)
                                 AND NEW.changes #> '{score,after}'
                                     IS NOT DISTINCT FROM to_jsonb(result.score)
                             )
                         )
                         AND (
                             NOT (NEW.changes ? 'grade')
                             OR (
                                 NEW.changes #> '{grade,before}'
                                     IS NOT DISTINCT FROM to_jsonb(source.grade::text)
                                 AND NEW.changes #> '{grade,after}'
                                     IS NOT DISTINCT FROM to_jsonb(result.grade::text)
                             )
                         )
                         AND (
                             NOT (NEW.changes ? 'limitations')
                             OR (
                                 NEW.changes #> '{limitations,before}'
                                     IS NOT DISTINCT FROM source.limitations
                                 AND NEW.changes #> '{limitations,after}'
                                     IS NOT DISTINCT FROM result.limitations
                             )
                         )
                         AND (
                             NOT (NEW.changes ? 'comment')
                             OR (
                                 NEW.changes #> '{comment,before}'
                                     IS NOT DISTINCT FROM to_jsonb(''::text)
                                 AND NEW.changes #> '{comment,after}'
                                     IS NOT DISTINCT FROM to_jsonb(NEW.comment)
                             )
                         )
                   ) THEN
                    RAISE EXCEPTION 'modify result report does not match source'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.action = 'reevaluate' THEN
                IF changed_keys IS DISTINCT FROM
                       ARRAY['evaluation_job_id', 'source_review_status']::text[]
                   OR NEW.changes #> '{evaluation_job_id,before}' <> 'null'::jsonb
                   OR NEW.changes #>> '{source_review_status,before}'
                      IS DISTINCT FROM NEW.changes #>> '{source_review_status,after}' THEN
                    RAISE EXCEPTION 'invalid reevaluate review changes'
                        USING ERRCODE = '23514';
                END IF;
                BEGIN
                    linked_job_id := (NEW.changes #>> '{evaluation_job_id,after}')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'invalid reevaluation job id'
                        USING ERRCODE = '23514';
                END;
                IF NOT EXISTS (
                    SELECT 1
                    FROM evaluation_jobs job
                    JOIN evaluation_reports report
                      ON report.id = NEW.report_id
                     AND report.submission_id = job.submission_id
                    WHERE job.id = linked_job_id
                      AND job.reason = 'manual_retry'
                      AND job.requested_by = NEW.teacher_id
                      AND job.source_report_id = NEW.report_id
                      AND report.review_status <> 'superseded'
                      AND NEW.changes #>> '{source_review_status,after}'
                          = report.review_status::text
                ) THEN
                    RAISE EXCEPTION 'reevaluation job does not match review source'
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
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_CONSISTENCY_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_SOURCE_LOCK_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_ACTION_SOURCE_LOCK_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_REPORT_SOURCE_LOCK_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_JOB_SOURCE_LOCK_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_ACTION_CONSTRAINT_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_REPORT_CONSTRAINT_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(CREATE_REVIEW_JOB_CONSTRAINT_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_review_actions_validate
        BEFORE INSERT ON review_actions
        FOR EACH ROW EXECUTE FUNCTION validate_review_action()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION protect_review_action()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'review actions are append-only'
                USING ERRCODE = '55000';
        END;
        $function$
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_review_actions_append_only
        BEFORE UPDATE OR DELETE ON review_actions
        FOR EACH ROW EXECUTE FUNCTION protect_review_action()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_review_actions_no_truncate
        BEFORE TRUNCATE ON review_actions
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_evaluation_evidence_truncate()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "before_drop",
    DDL("DROP TRIGGER IF EXISTS trg_00_evaluation_jobs_source_lock ON evaluation_jobs").execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    ReviewAction.__table__,
    "before_drop",
    DDL(
        "DROP TRIGGER IF EXISTS trg_00_evaluation_reports_source_lock ON evaluation_reports"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "before_drop",
    DDL(
        "DROP TRIGGER IF EXISTS trg_evaluation_jobs_review_consistency ON evaluation_jobs"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "before_drop",
    DDL(
        "DROP TRIGGER IF EXISTS trg_evaluation_reports_review_consistency ON evaluation_reports"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS protect_review_action()").execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS validate_review_action()").execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS validate_review_consistency()").execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAction.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS lock_review_source_report()").execute_if(dialect="postgresql"),
)
