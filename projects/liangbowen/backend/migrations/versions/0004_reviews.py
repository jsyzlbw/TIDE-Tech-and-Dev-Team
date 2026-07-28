"""Add audited teacher reviews and exact manual-retry lineage.

Revision ID: 0004_reviews
Revises: 0003_evaluation_delivery
Create Date: 2026-07-26 20:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

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

revision: str = "0004_reviews"
down_revision: str | None = "0003_evaluation_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROTECT_AUDITED_JOB_V4 = """
CREATE OR REPLACE FUNCTION protect_audited_evaluation_job()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF EXISTS (SELECT 1 FROM audit_logs WHERE job_id = OLD.id)
       AND ROW(
           NEW.id, NEW.submission_id, NEW.requested_by, NEW.source_report_id,
           NEW.reason, NEW.status, NEW.idempotency_key, NEW.attempt_count,
           NEW.provider, NEW.model, NEW.execution_token, NEW.execution_generation,
           NEW.error_code, NEW.error_message, NEW.queued_at,
           NEW.started_at, NEW.finished_at
       ) IS DISTINCT FROM ROW(
           OLD.id, OLD.submission_id, OLD.requested_by, OLD.source_report_id,
           OLD.reason, OLD.status, OLD.idempotency_key, OLD.attempt_count,
           OLD.provider, OLD.model, OLD.execution_token, OLD.execution_generation,
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

PROTECT_AUDITED_JOB_V3 = """
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

REPORT_LINEAGE_V4 = """
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

REPORT_LINEAGE_V3 = REPORT_LINEAGE_V4.replace(
    "\n              AND is_active",
    "",
).replace("report editor must be an active teacher", "report editor must be a teacher")

REPORT_PROTECTION_V4 = """
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

REPORT_PROTECTION_V3 = REPORT_PROTECTION_V4.replace(
    """    IF NEW.review_status IS DISTINCT FROM OLD.review_status
       AND NOT (
           (OLD.review_status = 'proposed'
            AND NEW.review_status IN ('confirmed', 'superseded'))
           OR (OLD.review_status IN ('modified', 'confirmed')
               AND NEW.review_status = 'superseded')
       ) THEN
        RAISE EXCEPTION 'invalid evaluation report review status transition'
            USING ERRCODE = '55000';
    END IF;
""",
    "",
)

VALIDATE_REVIEW_ACTION = """
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
        WHERE id = NEW.teacher_id AND role = 'teacher' AND is_active
    ) THEN
        RAISE EXCEPTION 'review action requires an active teacher'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM evaluation_reports WHERE id = NEW.report_id) THEN
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
                   'major_issues', 'result_report_id', 'review_status', 'score', 'suggestions'
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


def upgrade() -> None:
    op.add_column(
        "evaluation_jobs",
        sa.Column("source_report_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        UPDATE evaluation_jobs AS job
        SET source_report_id = (
            SELECT report.id
            FROM evaluation_reports AS report
            WHERE report.submission_id = job.submission_id
              AND report.created_at <= job.queued_at
              AND report.job_id IS DISTINCT FROM job.id
            ORDER BY report.version DESC, report.id DESC
            LIMIT 1
        )
        WHERE job.reason = 'manual_retry'
        """
    )
    op.execute(
        """
        DO $function$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM evaluation_jobs
                WHERE reason = 'manual_retry' AND source_report_id IS NULL
            ) THEN
                RAISE EXCEPTION 'cannot prove source report for existing manual retry job'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $function$
        """
    )
    op.create_check_constraint(
        "ck_evaluation_jobs_manual_source",
        "evaluation_jobs",
        "((reason = 'manual_retry' AND source_report_id IS NOT NULL) OR "
        "(reason <> 'manual_retry' AND source_report_id IS NULL))",
    )
    op.create_foreign_key(
        "fk_evaluation_jobs_source_submission",
        "evaluation_jobs",
        "evaluation_reports",
        ["source_report_id", "submission_id"],
        ["id", "submission_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_evaluation_jobs_source_report_id",
        "evaluation_jobs",
        ["source_report_id"],
        unique=False,
    )
    op.execute(PROTECT_AUDITED_JOB_V4)
    op.execute(REPORT_LINEAGE_V4)
    op.execute(REPORT_PROTECTION_V4)

    review_action_type = postgresql.ENUM(
        "confirm",
        "modify",
        "reevaluate",
        name="review_action_type",
        create_type=False,
    )
    review_action_type.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "review_actions",
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("teacher_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", review_action_type, nullable=False),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("comment", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(changes) = 'object' AND pg_column_size(changes) <= 2097152",
            name="ck_review_actions_changes_object",
        ),
        sa.CheckConstraint(
            "octet_length(comment) <= 8000",
            name="ck_review_actions_comment_bytes",
        ),
        sa.CheckConstraint(
            "char_length(comment) <= 4000",
            name="ck_review_actions_comment_characters",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["evaluation_reports.id"],
            name="fk_review_actions_report_id_evaluation_reports",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
            name="fk_review_actions_teacher_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_actions"),
        sa.UniqueConstraint(
            "report_id",
            "action",
            name="uq_review_actions_report_action",
        ),
    )
    op.execute(
        """
        DO $function$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM evaluation_jobs job
                LEFT JOIN users reviewer ON reviewer.id = job.requested_by
                WHERE job.reason = 'manual_retry'
                  AND (
                      reviewer.id IS NULL
                      OR reviewer.role <> 'teacher'
                      OR NOT reviewer.is_active
                  )
            ) THEN
                RAISE EXCEPTION 'cannot backfill review action without active teacher'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $function$
        """
    )
    op.execute(
        """
        INSERT INTO review_actions (
            id, report_id, teacher_id, action, changes, comment
        )
        SELECT
            gen_random_uuid(),
            job.source_report_id,
            job.requested_by,
            'reevaluate',
            jsonb_build_object(
                'evaluation_job_id', jsonb_build_object(
                    'before', NULL,
                    'after', job.id::text
                ),
                'source_review_status', jsonb_build_object(
                    'before', CASE
                        WHEN source.review_status <> 'superseded'
                            THEN source.review_status::text
                        WHEN source.origin = 'agent' THEN 'proposed'
                        ELSE 'modified'
                    END,
                    'after', CASE
                        WHEN source.review_status <> 'superseded'
                            THEN source.review_status::text
                        WHEN source.origin = 'agent' THEN 'proposed'
                        ELSE 'modified'
                    END
                )
            ),
            ''
        FROM evaluation_jobs job
        JOIN evaluation_reports source ON source.id = job.source_report_id
        WHERE job.reason = 'manual_retry'
        """
    )
    op.execute(VALIDATE_REVIEW_ACTION)
    op.execute(CREATE_REVIEW_SOURCE_LOCK_FUNCTION_SQL)
    op.execute(CREATE_REVIEW_ACTION_SOURCE_LOCK_TRIGGER_SQL)
    op.execute(CREATE_REVIEW_REPORT_SOURCE_LOCK_TRIGGER_SQL)
    op.execute(CREATE_REVIEW_JOB_SOURCE_LOCK_TRIGGER_SQL)
    op.execute(
        """
        CREATE TRIGGER trg_review_actions_validate
        BEFORE INSERT ON review_actions
        FOR EACH ROW EXECUTE FUNCTION validate_review_action()
        """
    )
    op.execute(CREATE_REVIEW_CONSISTENCY_FUNCTION_SQL)
    op.execute(CREATE_REVIEW_ACTION_CONSTRAINT_TRIGGER_SQL)
    op.execute(CREATE_REVIEW_REPORT_CONSTRAINT_TRIGGER_SQL)
    op.execute(CREATE_REVIEW_JOB_CONSTRAINT_TRIGGER_SQL)
    op.execute("UPDATE evaluation_jobs SET status = status WHERE reason = 'manual_retry'")
    op.execute(
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
    )
    op.execute(
        """
        CREATE TRIGGER trg_review_actions_append_only
        BEFORE UPDATE OR DELETE ON review_actions
        FOR EACH ROW EXECUTE FUNCTION protect_review_action()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_review_actions_no_truncate
        BEFORE TRUNCATE ON review_actions
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_evaluation_evidence_truncate()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $function$
        BEGIN
            IF EXISTS (SELECT 1 FROM review_actions)
               OR EXISTS (
                   SELECT 1 FROM evaluation_jobs WHERE source_report_id IS NOT NULL
               ) THEN
                RAISE EXCEPTION 'cannot downgrade while review evidence exists'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $function$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_evaluation_jobs_review_consistency ON evaluation_jobs")
    op.execute("DROP TRIGGER IF EXISTS trg_00_evaluation_jobs_source_lock ON evaluation_jobs")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evaluation_reports_review_consistency ON evaluation_reports"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_00_evaluation_reports_source_lock ON evaluation_reports")
    op.drop_table("review_actions")
    op.execute("DROP FUNCTION IF EXISTS validate_review_consistency()")
    op.execute("DROP FUNCTION IF EXISTS protect_review_action()")
    op.execute("DROP FUNCTION IF EXISTS validate_review_action()")
    op.execute("DROP FUNCTION IF EXISTS lock_review_source_report()")
    postgresql.ENUM(name="review_action_type").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_evaluation_jobs_source_report_id", table_name="evaluation_jobs")
    op.drop_constraint(
        "fk_evaluation_jobs_source_submission",
        "evaluation_jobs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_evaluation_jobs_manual_source",
        "evaluation_jobs",
        type_="check",
    )
    op.drop_column("evaluation_jobs", "source_report_id")
    op.execute(PROTECT_AUDITED_JOB_V3)
    op.execute(REPORT_LINEAGE_V3)
    op.execute(REPORT_PROTECTION_V3)
