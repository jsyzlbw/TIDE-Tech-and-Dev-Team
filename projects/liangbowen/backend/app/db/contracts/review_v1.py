"""Frozen PostgreSQL review-state consistency contract for migration 0004."""

CREATE_REVIEW_SOURCE_LOCK_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION lock_review_source_report()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    review_source_ids uuid[];
BEGIN
    IF TG_TABLE_NAME = 'review_actions' THEN
        review_source_ids := ARRAY[NEW.report_id];
    ELSIF TG_TABLE_NAME = 'evaluation_jobs' THEN
        IF TG_OP = 'UPDATE' THEN
            IF OLD.reason = 'manual_retry' AND OLD.source_report_id IS NOT NULL THEN
                review_source_ids := ARRAY_APPEND(review_source_ids, OLD.source_report_id);
            END IF;
        END IF;
        IF NEW.reason = 'manual_retry' THEN
            review_source_ids := ARRAY_APPEND(review_source_ids, NEW.source_report_id);
        END IF;
    ELSIF TG_TABLE_NAME = 'evaluation_reports' THEN
        IF NEW.source_report_id IS NOT NULL THEN
            review_source_ids := ARRAY[NEW.source_report_id];
        ELSIF NEW.job_id IS NOT NULL THEN
            SELECT ARRAY[job.source_report_id] INTO review_source_ids
            FROM evaluation_jobs job
            WHERE job.id = NEW.job_id
              AND job.reason = 'manual_retry'
              AND job.source_report_id IS NOT NULL;
        END IF;
    END IF;

    IF review_source_ids IS NOT NULL THEN
        PERFORM 1
        FROM evaluation_reports report
        WHERE report.id = ANY(review_source_ids)
        ORDER BY report.id
        FOR UPDATE;
    END IF;
    RETURN NEW;
END;
$function$
"""

CREATE_REVIEW_ACTION_SOURCE_LOCK_TRIGGER_SQL = """
CREATE TRIGGER trg_00_review_actions_source_lock
BEFORE INSERT ON review_actions
FOR EACH ROW EXECUTE FUNCTION lock_review_source_report()
"""

CREATE_REVIEW_REPORT_SOURCE_LOCK_TRIGGER_SQL = """
CREATE TRIGGER trg_00_evaluation_reports_source_lock
BEFORE INSERT ON evaluation_reports
FOR EACH ROW EXECUTE FUNCTION lock_review_source_report()
"""

CREATE_REVIEW_JOB_SOURCE_LOCK_TRIGGER_SQL = """
CREATE TRIGGER trg_00_evaluation_jobs_source_lock
BEFORE INSERT OR UPDATE ON evaluation_jobs
FOR EACH ROW EXECUTE FUNCTION lock_review_source_report()
"""

CREATE_REVIEW_CONSISTENCY_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION validate_review_consistency()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    linked_job_id_text text;
    result_report_id_text text;
    pending_count integer;
    evidence_count integer;
BEGIN
    IF TG_TABLE_NAME = 'review_actions' THEN
        IF NEW.action = 'confirm' THEN
            IF NOT EXISTS (
                SELECT 1
                FROM evaluation_reports report
                WHERE report.id = NEW.report_id
                  AND report.review_status = 'confirmed'
            ) THEN
                RAISE EXCEPTION 'confirm action lacks matching report status'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM evaluation_reports report
                WHERE report.id = NEW.report_id
                  AND report.version = (
                      SELECT MAX(candidate.version)
                      FROM evaluation_reports candidate
                      WHERE candidate.submission_id = report.submission_id
                  )
            ) THEN
                RAISE EXCEPTION 'confirmation requires the current report'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM evaluation_jobs job
                WHERE job.source_report_id = NEW.report_id
                  AND job.reason = 'manual_retry'
                  AND job.status IN ('queued', 'running')
            ) THEN
                RAISE EXCEPTION 'confirmation conflicts with pending re-evaluation'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.action = 'modify' THEN
            result_report_id_text := NEW.changes #>> '{result_report_id,after}';
            IF NOT EXISTS (
                SELECT 1
                FROM evaluation_reports source
                JOIN evaluation_reports result
                  ON result.id::text = result_report_id_text
                 AND result.submission_id = source.submission_id
                WHERE source.id = NEW.report_id
                  AND source.origin = 'agent'
                  AND source.review_status = 'superseded'
                  AND result.origin = 'teacher'
                  AND result.review_status = 'modified'
                  AND result.source_report_id = source.id
                  AND result.version = source.version + 1
                  AND result.version = (
                      SELECT MAX(candidate.version)
                      FROM evaluation_reports candidate
                      WHERE candidate.submission_id = source.submission_id
                  )
            ) THEN
                RAISE EXCEPTION 'modify action lacks matching current report relation'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM evaluation_jobs job
                WHERE job.source_report_id = NEW.report_id
                  AND job.reason = 'manual_retry'
                  AND job.status IN ('queued', 'running')
            ) THEN
                RAISE EXCEPTION 'modification conflicts with pending re-evaluation'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.action = 'reevaluate' THEN
            linked_job_id_text := NEW.changes #>> '{evaluation_job_id,after}';
            IF NOT EXISTS (
                SELECT 1
                FROM evaluation_jobs job
                WHERE job.id::text = linked_job_id_text
                  AND job.reason = 'manual_retry'
                  AND job.source_report_id = NEW.report_id
                  AND job.requested_by = NEW.teacher_id
            ) THEN
                RAISE EXCEPTION 're-evaluation action lacks matching manual job'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM evaluation_jobs job
                WHERE job.id::text = linked_job_id_text
                  AND job.status IN ('queued', 'running')
            ) THEN
                SELECT COUNT(*) INTO pending_count
                FROM evaluation_jobs job
                WHERE job.source_report_id = NEW.report_id
                  AND job.reason = 'manual_retry'
                  AND job.status IN ('queued', 'running');
                IF pending_count <> 1 OR NOT EXISTS (
                    SELECT 1
                    FROM evaluation_reports source
                    WHERE source.id = NEW.report_id
                      AND source.review_status <> 'superseded'
                      AND source.review_status::text
                          = NEW.changes #>> '{source_review_status,after}'
                      AND source.version = (
                          SELECT MAX(candidate.version)
                          FROM evaluation_reports candidate
                          WHERE candidate.submission_id = source.submission_id
                      )
                ) THEN
                    RAISE EXCEPTION 'pending re-evaluation requires one current source'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF EXISTS (
                SELECT 1 FROM evaluation_jobs job
                WHERE job.id::text = linked_job_id_text
                  AND job.status IN ('failed', 'cancelled')
            ) THEN
                IF NOT EXISTS (
                    SELECT 1 FROM evaluation_reports source
                    WHERE source.id = NEW.report_id
                      AND source.review_status::text
                          = NEW.changes #>> '{source_review_status,after}'
                ) THEN
                    RAISE EXCEPTION 'unsuccessful re-evaluation changed source status'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NOT EXISTS (
                SELECT 1
                FROM evaluation_jobs job
                JOIN evaluation_reports source
                  ON source.id = job.source_report_id
                 AND source.submission_id = job.submission_id
                JOIN evaluation_reports result
                  ON result.job_id = job.id
                 AND result.submission_id = job.submission_id
                JOIN audit_logs audit
                  ON audit.job_id = job.id
                 AND audit.final_report_id = result.id
                 AND audit.submission_id = job.submission_id
                WHERE job.id::text = linked_job_id_text
                  AND job.status = 'succeeded'
                  AND source.id = NEW.report_id
                  AND source.review_status = 'superseded'
                  AND result.origin = 'agent'
                  AND result.version = source.version + 1
                  AND (
                      (
                          result.review_status = 'proposed'
                          AND result.version = (
                              SELECT MAX(candidate.version)
                              FROM evaluation_reports candidate
                              WHERE candidate.submission_id = source.submission_id
                          )
                      )
                      OR (
                          result.review_status = 'superseded'
                          AND EXISTS (
                              SELECT 1
                              FROM evaluation_jobs successor_job
                              JOIN evaluation_reports successor_result
                                ON successor_result.job_id = successor_job.id
                               AND successor_result.submission_id = result.submission_id
                              JOIN audit_logs successor_audit
                                ON successor_audit.job_id = successor_job.id
                               AND successor_audit.final_report_id = successor_result.id
                              JOIN review_actions successor_action
                                ON successor_action.report_id = result.id
                               AND successor_action.action = 'reevaluate'
                               AND successor_action.changes
                                   #>> '{evaluation_job_id,after}' = successor_job.id::text
                              WHERE successor_job.reason = 'manual_retry'
                                AND successor_job.status = 'succeeded'
                                AND successor_job.source_report_id = result.id
                                AND successor_result.origin = 'agent'
                                AND successor_result.version = result.version + 1
                          )
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'successful re-evaluation lacks exact report evidence'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NULL;
    END IF;

    IF TG_TABLE_NAME = 'evaluation_reports' THEN
        IF TG_OP = 'INSERT' AND NEW.origin = 'teacher' THEN
            IF NOT EXISTS (
                SELECT 1
                FROM review_actions action
                JOIN evaluation_reports source ON source.id = action.report_id
                WHERE action.action = 'modify'
                  AND action.changes #>> '{result_report_id,after}' = NEW.id::text
                  AND source.origin = 'agent'
                  AND source.review_status = 'superseded'
                  AND NEW.review_status = 'modified'
                  AND NEW.source_report_id = source.id
                  AND NEW.version = source.version + 1
                  AND NEW.version = (
                      SELECT MAX(candidate.version)
                      FROM evaluation_reports candidate
                      WHERE candidate.submission_id = NEW.submission_id
                  )
            ) THEN
                RAISE EXCEPTION 'teacher report lacks matching modify action'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM evaluation_jobs job
                WHERE job.source_report_id = NEW.source_report_id
                  AND job.reason = 'manual_retry'
                  AND job.status IN ('queued', 'running')
            ) THEN
                RAISE EXCEPTION 'modification conflicts with pending re-evaluation'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF TG_OP = 'INSERT' AND NEW.origin = 'agent' AND EXISTS (
            SELECT 1 FROM evaluation_jobs job
            WHERE job.id = NEW.job_id AND job.reason = 'manual_retry'
        ) THEN
            IF NOT EXISTS (
                SELECT 1
                FROM evaluation_jobs job
                JOIN evaluation_reports source
                  ON source.id = job.source_report_id
                 AND source.submission_id = job.submission_id
                JOIN audit_logs audit
                  ON audit.job_id = job.id
                 AND audit.final_report_id = NEW.id
                 AND audit.submission_id = job.submission_id
                JOIN review_actions action
                  ON action.report_id = source.id
                 AND action.action = 'reevaluate'
                 AND action.changes #>> '{evaluation_job_id,after}' = job.id::text
                WHERE job.id = NEW.job_id
                  AND job.status = 'succeeded'
                  AND source.review_status = 'superseded'
                  AND NEW.review_status = 'proposed'
                  AND NEW.version = source.version + 1
                  AND NEW.version = (
                      SELECT MAX(candidate.version)
                      FROM evaluation_reports candidate
                      WHERE candidate.submission_id = NEW.submission_id
                  )
            ) THEN
                RAISE EXCEPTION 'successful re-evaluation lacks exact report evidence'
                    USING ERRCODE = '23514';
            END IF;
        END IF;

        IF TG_OP = 'UPDATE' AND NEW.review_status IS DISTINCT FROM OLD.review_status THEN
            IF NEW.review_status = 'confirmed' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM review_actions action
                    WHERE action.report_id = NEW.id AND action.action = 'confirm'
                ) THEN
                    RAISE EXCEPTION 'review transition lacks matching evidence'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version IS DISTINCT FROM (
                    SELECT MAX(candidate.version)
                    FROM evaluation_reports candidate
                    WHERE candidate.submission_id = NEW.submission_id
                ) THEN
                    RAISE EXCEPTION 'confirmation requires the current report'
                        USING ERRCODE = '23514';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM evaluation_jobs job
                    WHERE job.source_report_id = NEW.id
                      AND job.reason = 'manual_retry'
                      AND job.status IN ('queued', 'running')
                ) THEN
                    RAISE EXCEPTION 'confirmation conflicts with pending re-evaluation'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.review_status = 'superseded' THEN
                SELECT COUNT(*) INTO evidence_count
                FROM (
                    SELECT action.id
                    FROM review_actions action
                    JOIN evaluation_reports result
                      ON result.id::text = action.changes #>> '{result_report_id,after}'
                     AND result.submission_id = NEW.submission_id
                    WHERE action.report_id = NEW.id
                      AND action.action = 'modify'
                      AND OLD.review_status = 'proposed'
                      AND result.origin = 'teacher'
                      AND result.review_status = 'modified'
                      AND result.source_report_id = NEW.id
                      AND result.version = NEW.version + 1
                      AND result.version = (
                          SELECT MAX(candidate.version)
                          FROM evaluation_reports candidate
                          WHERE candidate.submission_id = NEW.submission_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM evaluation_jobs pending
                          WHERE pending.source_report_id = NEW.id
                            AND pending.reason = 'manual_retry'
                            AND pending.status IN ('queued', 'running')
                      )
                    UNION ALL
                    SELECT action.id
                    FROM review_actions action
                    JOIN evaluation_jobs job
                      ON job.id::text = action.changes #>> '{evaluation_job_id,after}'
                     AND job.source_report_id = NEW.id
                    JOIN evaluation_reports result
                      ON result.job_id = job.id
                     AND result.submission_id = NEW.submission_id
                    JOIN audit_logs audit
                      ON audit.job_id = job.id
                     AND audit.final_report_id = result.id
                     AND audit.submission_id = NEW.submission_id
                    WHERE action.report_id = NEW.id
                      AND action.action = 'reevaluate'
                      AND OLD.review_status IN ('proposed', 'modified', 'confirmed')
                      AND job.status = 'succeeded'
                      AND result.origin = 'agent'
                      AND result.review_status = 'proposed'
                      AND result.version = NEW.version + 1
                      AND result.version = (
                          SELECT MAX(candidate.version)
                          FROM evaluation_reports candidate
                          WHERE candidate.submission_id = NEW.submission_id
                      )
                ) evidence;
                IF evidence_count = 0 THEN
                    IF EXISTS (
                        SELECT 1
                        FROM review_actions action
                        JOIN evaluation_jobs job
                          ON job.id::text = action.changes #>> '{evaluation_job_id,after}'
                        WHERE action.report_id = NEW.id
                          AND action.action = 'reevaluate'
                          AND job.status IN ('failed', 'cancelled')
                    ) THEN
                        RAISE EXCEPTION 'unsuccessful re-evaluation changed source status'
                            USING ERRCODE = '23514';
                    END IF;
                    RAISE EXCEPTION 'review transition lacks matching evidence'
                        USING ERRCODE = '23514';
                ELSIF evidence_count <> 1 THEN
                    RAISE EXCEPTION 'review transition has ambiguous evidence'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END IF;
        RETURN NULL;
    END IF;

    IF TG_TABLE_NAME = 'evaluation_jobs'
       AND TG_OP = 'UPDATE'
       AND ROW(
           NEW.id, NEW.submission_id, NEW.requested_by, NEW.source_report_id, NEW.reason
       ) IS DISTINCT FROM ROW(
           OLD.id, OLD.submission_id, OLD.requested_by, OLD.source_report_id, OLD.reason
       )
       AND EXISTS (
           SELECT 1 FROM review_actions action
           WHERE action.action = 'reevaluate'
             AND action.changes #>> '{evaluation_job_id,after}' = OLD.id::text
       ) THEN
        RAISE EXCEPTION 'review-linked evaluation job identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF TG_TABLE_NAME = 'evaluation_jobs' AND NEW.reason = 'manual_retry' THEN
        IF NOT EXISTS (
            SELECT 1 FROM review_actions action
            WHERE action.report_id = NEW.source_report_id
              AND action.teacher_id = NEW.requested_by
              AND action.action = 'reevaluate'
              AND action.changes #>> '{evaluation_job_id,after}' = NEW.id::text
        ) THEN
            RAISE EXCEPTION 'manual retry lacks matching review action'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.status IN ('queued', 'running') THEN
            SELECT COUNT(*) INTO pending_count
            FROM evaluation_jobs pending
            WHERE pending.source_report_id = NEW.source_report_id
              AND pending.reason = 'manual_retry'
              AND pending.status IN ('queued', 'running');
            IF pending_count <> 1 OR NOT EXISTS (
                SELECT 1
                FROM evaluation_reports source
                WHERE source.id = NEW.source_report_id
                  AND source.review_status <> 'superseded'
                  AND source.review_status::text = (
                      SELECT action.changes #>> '{source_review_status,after}'
                      FROM review_actions action
                      WHERE action.report_id = NEW.source_report_id
                        AND action.action = 'reevaluate'
                        AND action.changes #>> '{evaluation_job_id,after}' = NEW.id::text
                  )
            ) THEN
                RAISE EXCEPTION 'pending re-evaluation requires one current source'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.status IN ('failed', 'cancelled') THEN
            IF NOT EXISTS (
                SELECT 1 FROM evaluation_reports source
                WHERE source.id = NEW.source_report_id
                  AND source.review_status::text = (
                      SELECT action.changes #>> '{source_review_status,after}'
                      FROM review_actions action
                      WHERE action.report_id = NEW.source_report_id
                        AND action.action = 'reevaluate'
                        AND action.changes #>> '{evaluation_job_id,after}' = NEW.id::text
                  )
            ) THEN
                RAISE EXCEPTION 'unsuccessful re-evaluation changed source status'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.status = 'succeeded' THEN
            IF TG_OP = 'UPDATE' AND OLD.status = 'succeeded' THEN
                IF NOT EXISTS (
                    SELECT 1
                    FROM evaluation_reports source
                    JOIN evaluation_reports result
                      ON result.job_id = NEW.id
                     AND result.submission_id = source.submission_id
                    JOIN audit_logs audit
                      ON audit.job_id = NEW.id
                     AND audit.final_report_id = result.id
                     AND audit.submission_id = source.submission_id
                    WHERE source.id = NEW.source_report_id
                      AND source.review_status = 'superseded'
                      AND result.origin = 'agent'
                      AND result.version = source.version + 1
                      AND (
                          (
                              result.review_status = 'proposed'
                              AND result.version = (
                                  SELECT MAX(candidate.version)
                                  FROM evaluation_reports candidate
                                  WHERE candidate.submission_id = source.submission_id
                              )
                          )
                          OR (
                              result.review_status = 'superseded'
                              AND EXISTS (
                                  SELECT 1
                                  FROM evaluation_jobs successor_job
                                  JOIN evaluation_reports successor_result
                                    ON successor_result.job_id = successor_job.id
                                   AND successor_result.submission_id = result.submission_id
                                  JOIN audit_logs successor_audit
                                    ON successor_audit.job_id = successor_job.id
                                   AND successor_audit.final_report_id = successor_result.id
                                  JOIN review_actions successor_action
                                    ON successor_action.report_id = result.id
                                   AND successor_action.action = 'reevaluate'
                                   AND successor_action.changes
                                       #>> '{evaluation_job_id,after}'
                                       = successor_job.id::text
                                  WHERE successor_job.reason = 'manual_retry'
                                    AND successor_job.status = 'succeeded'
                                    AND successor_job.source_report_id = result.id
                                    AND successor_result.origin = 'agent'
                                    AND successor_result.version = result.version + 1
                              )
                          )
                      )
                ) THEN
                    RAISE EXCEPTION 'successful re-evaluation lacks exact report evidence'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NOT EXISTS (
                SELECT 1
                FROM evaluation_reports source
                JOIN evaluation_reports result
                  ON result.job_id = NEW.id
                 AND result.submission_id = source.submission_id
                JOIN audit_logs audit
                  ON audit.job_id = NEW.id
                 AND audit.final_report_id = result.id
                 AND audit.submission_id = source.submission_id
                WHERE source.id = NEW.source_report_id
                  AND source.review_status = 'superseded'
                  AND result.origin = 'agent'
                  AND result.review_status = 'proposed'
                  AND result.version = source.version + 1
                  AND result.version = (
                      SELECT MAX(candidate.version)
                      FROM evaluation_reports candidate
                      WHERE candidate.submission_id = source.submission_id
                  )
            ) THEN
                RAISE EXCEPTION 'successful re-evaluation lacks exact report evidence'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    RETURN NULL;
END;
$function$
"""

CREATE_REVIEW_ACTION_CONSTRAINT_TRIGGER_SQL = """
CREATE CONSTRAINT TRIGGER trg_review_actions_consistency
AFTER INSERT ON review_actions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_review_consistency()
"""

CREATE_REVIEW_REPORT_CONSTRAINT_TRIGGER_SQL = """
CREATE CONSTRAINT TRIGGER trg_evaluation_reports_review_consistency
AFTER INSERT OR UPDATE ON evaluation_reports
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_review_consistency()
"""

CREATE_REVIEW_JOB_CONSTRAINT_TRIGGER_SQL = """
CREATE CONSTRAINT TRIGGER trg_evaluation_jobs_review_consistency
AFTER INSERT OR UPDATE ON evaluation_jobs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_review_consistency()
"""
