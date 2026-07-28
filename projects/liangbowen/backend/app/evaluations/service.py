import asyncio
import hashlib
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.assignments.model import Assignment
from app.audit.service import SAFE_AUDIT_ERROR_MESSAGES, AuditLogCreate, append_audit_log
from app.db.migration_gate import migration_gated_sessionmaker
from app.db.types import AssignmentStatus, Grade, Role, SubmissionStatus
from app.evaluations.engine import EngineFailureEvidence, EngineResult, EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.outbox import (
    OUTBOX_DISPATCH,
    OUTBOX_NOTIFICATION,
    Dispatch,
    Notification,
    enqueue_dispatch_outbox_batch,
    enqueue_evaluation_outbox,
    redrive_evaluation_outbox,
)
from app.evaluations.providers.base import EvaluationRequest, ProviderIdentity, RepairErrorCode
from app.evaluations.types import (
    JobReason,
    JobStatus,
    ReportOrigin,
    ReviewStatus,
)
from app.submissions.model import Submission
from app.users.model import User

logger = logging.getLogger(__name__)
PROMPT_TEMPLATE_VERSION = "evaluation-v1"
MAX_EVALUATION_BATCH = 10_000
DATABASE_WRITE_CHUNK = 500
MAX_IMMEDIATE_DISPATCH = 100
WORKER_STALE_AFTER = timedelta(minutes=15)
_WORKER_EXECUTION_TOKEN = object()
_TRY_JOB_LOCK_SQL = text("SELECT pg_try_advisory_lock(hashtextextended(:job_id, 0))")
_UNLOCK_JOB_SQL = text("SELECT pg_advisory_unlock(hashtextextended(:job_id, 0))")


class EvaluationSubjectNotFound(LookupError):
    """The submitted version is absent or no longer eligible for evaluation."""


class EvaluationInProgress(RuntimeError):
    """A duplicate delivery observed a job that another worker is running."""


class EvaluationJobFailed(RuntimeError):
    """A sanitized terminal failure surfaced to an inline caller."""


class EvaluationRecoveryRequired(RuntimeError):
    """The broker must requeue because a fatal attempt could not be finalized."""


class EvaluationBatchTooLarge(RuntimeError):
    """The assignment contains more eligible submissions than one batch allows."""


class _EvaluationSubjectWithdrawn(EvaluationSubjectNotFound):
    """Authoritative terminal-lock evidence that the submission was withdrawn."""


@dataclass(frozen=True, slots=True)
class EvaluationBatch:
    batch_id: uuid.UUID
    queued: int
    skipped: int
    job_ids: tuple[uuid.UUID, ...]


def evaluation_key(
    submission_id: uuid.UUID,
    version: int,
    reason: JobReason,
    *,
    generation: int = 0,
) -> str:
    """Return the frozen job idempotency key for one submission version and reason."""
    if not isinstance(submission_id, uuid.UUID):
        raise TypeError("submission_id must be a UUID")
    if type(version) is not int or version < 1:
        raise ValueError("version must be a positive integer")
    if type(reason) is not JobReason:
        raise TypeError("reason must be a JobReason")
    if type(generation) is not int or generation < 0:
        raise ValueError("generation must be a non-negative integer")
    raw = f"{submission_id}:{version}:{reason.value}"
    if generation:
        raw = f"{raw}:generation:{generation}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scoped_evaluation_key(
    submission_id: uuid.UUID,
    version: int,
    reason: JobReason,
    source_report_id: uuid.UUID | None,
    *,
    generation: int = 0,
) -> str:
    if reason is not JobReason.MANUAL_RETRY:
        return evaluation_key(submission_id, version, reason, generation=generation)
    if not isinstance(source_report_id, uuid.UUID):
        raise EvaluationSubjectNotFound("evaluation source not found")
    raw = f"{submission_id}:{version}:{reason.value}:{source_report_id}"
    if generation:
        raw = f"{raw}:generation:{generation}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _engine_identity(engine: EvaluationEngine) -> ProviderIdentity:
    identity = getattr(engine, "provider_identity", None)
    if type(identity) is not ProviderIdentity:
        raise ValueError("evaluation engine provider identity is unavailable")
    return ProviderIdentity(provider=identity.provider, model=identity.model)


class EvaluationService:
    """Transaction-owning entry point shared by REST, workers and acceptance tests.

    The supplied session is used only to discover its engine. Each public operation
    creates and owns a separate session, so it never flushes, commits or rolls back
    unrelated objects pending in the caller's identity map.
    """

    def __init__(
        self,
        session: AsyncSession,
        engine: EvaluationEngine,
        *,
        requested_by: uuid.UUID,
        dispatch: Dispatch | None,
        notification: Notification | None = None,
        mock_fixture_key: str = "partial",
        _worker_job_id: uuid.UUID | None = None,
        _worker_redelivered: bool = False,
        _worker_token: object | None = None,
    ) -> None:
        if not isinstance(session, AsyncSession):
            raise TypeError("session must be an AsyncSession")
        bind = session.bind
        if not isinstance(bind, AsyncEngine):
            raise TypeError("session must be bound to an AsyncEngine")
        if not isinstance(requested_by, uuid.UUID):
            raise TypeError("requested_by must be a UUID")
        if dispatch is not None and not callable(dispatch):
            raise TypeError("dispatch must be callable or None")
        if notification is not None and not callable(notification):
            raise TypeError("notification must be callable or None")
        if type(mock_fixture_key) is not str or not mock_fixture_key:
            raise ValueError("mock_fixture_key must be a non-empty string")
        if _worker_job_id is not None:
            if _worker_token is not _WORKER_EXECUTION_TOKEN:
                raise PermissionError("worker execution scope is internal")
            if not isinstance(_worker_job_id, uuid.UUID):
                raise TypeError("worker job scope must be a UUID")
        if type(_worker_redelivered) is not bool:
            raise TypeError("worker redelivery state must be a boolean")
        self._database_engine = bind
        self._session_factory = migration_gated_sessionmaker(bind, expire_on_commit=False)
        self._engine = engine
        self._identity = _engine_identity(engine)
        self._requested_by = uuid.UUID(str(requested_by))
        self._dispatch = dispatch
        self._notification = notification
        self._mock_fixture_key = mock_fixture_key
        self._worker_job_id = _worker_job_id
        self._worker_redelivered = _worker_redelivered
        self._active_execution_token: uuid.UUID | None = None

    @classmethod
    def for_persisted_job(
        cls,
        session: AsyncSession,
        engine: EvaluationEngine,
        *,
        job_id: uuid.UUID,
        requested_by: uuid.UUID,
        redelivered: bool,
        notification: Notification,
        mock_fixture_key: str = "partial",
    ) -> "EvaluationService":
        return cls(
            session,
            engine,
            requested_by=requested_by,
            dispatch=None,
            notification=notification,
            mock_fixture_key=mock_fixture_key,
            _worker_job_id=job_id,
            _worker_redelivered=redelivered,
            _worker_token=_WORKER_EXECUTION_TOKEN,
        )

    def _require_actor_mode(self) -> None:
        if self._worker_job_id is not None:
            raise PermissionError("worker execution cannot create evaluation jobs")

    @property
    def requested_by(self) -> uuid.UUID:
        return self._requested_by

    async def _require_teacher(self, session: AsyncSession) -> None:
        actor = await session.scalar(
            select(User).where(
                User.id == self._requested_by,
                User.role == Role.TEACHER,
                User.is_active.is_(True),
            )
        )
        if actor is None:
            raise PermissionError("teacher role required")

    async def request(
        self,
        submission_id: uuid.UUID,
        reason: JobReason,
        *,
        source_report_id: uuid.UUID | None = None,
    ) -> EvaluationJob:
        async with self._session_factory() as session, session.begin():
            job = await self.enqueue_in_transaction(
                session,
                submission_id,
                reason,
                source_report_id=source_report_id,
            )

        await self._redrive_after_commit((uuid.UUID(str(job.id)),))
        return job

    async def enqueue_in_transaction(
        self,
        session: AsyncSession,
        submission_id: uuid.UUID,
        reason: JobReason,
        *,
        source_report_id: uuid.UUID | None = None,
    ) -> EvaluationJob:
        """Persist one idempotent job/outbox in the caller-owned transaction.

        This method deliberately never commits and never dispatches. Adapters that
        compose additional evidence in the same transaction must call
        ``redrive_after_commit`` only after their transaction exits successfully.
        """
        self._require_actor_mode()
        if not isinstance(session, AsyncSession):
            raise TypeError("session must be an AsyncSession")
        if session.bind is not self._database_engine:
            raise ValueError("session must use the evaluation service database engine")
        if not isinstance(submission_id, uuid.UUID):
            raise TypeError("submission_id must be a UUID")
        if type(reason) is not JobReason:
            raise TypeError("reason must be a JobReason")
        if reason is not JobReason.MANUAL_RETRY and source_report_id is not None:
            raise ValueError("source_report_id is only valid for manual retry")

        await self._require_teacher(session)
        submission_query = select(Submission).where(
            Submission.id == submission_id,
            Submission.status == SubmissionStatus.SUBMITTED,
        )
        if reason in {JobReason.PROVIDER_RETRY, JobReason.MANUAL_RETRY}:
            submission_query = submission_query.with_for_update()
        submission = await session.scalar(submission_query)
        if submission is None:
            raise EvaluationSubjectNotFound("submission not found")

        generation = 0
        retry_conditions = [
            EvaluationJob.submission_id == submission.id,
            EvaluationJob.reason == reason,
        ]
        if reason is JobReason.MANUAL_RETRY:
            retry_conditions.append(EvaluationJob.source_report_id == source_report_id)
        elif reason is JobReason.PROVIDER_RETRY:
            retry_conditions.append(EvaluationJob.source_report_id.is_(None))
        if reason in {JobReason.PROVIDER_RETRY, JobReason.MANUAL_RETRY}:
            latest_retry = await session.scalar(
                select(EvaluationJob)
                .where(*retry_conditions)
                .order_by(EvaluationJob.queued_at.desc(), EvaluationJob.id.desc())
                .limit(1)
            )
            if latest_retry is not None and latest_retry.status in {
                JobStatus.QUEUED,
                JobStatus.RUNNING,
                JobStatus.SUCCEEDED,
            }:
                return latest_retry
            if latest_retry is not None:
                generation = int(
                    await session.scalar(
                        select(func.count(EvaluationJob.id)).where(*retry_conditions)
                    )
                    or 0
                )

        key = _scoped_evaluation_key(
            submission.id,
            submission.version,
            reason,
            source_report_id,
            generation=generation,
        )
        if reason is JobReason.MANUAL_RETRY:
            if not isinstance(source_report_id, uuid.UUID):
                raise EvaluationSubjectNotFound("evaluation source not found")
            await self._validate_manual_source(
                session,
                submission_id=submission.id,
                source_report_id=source_report_id,
            )
        job_id = uuid.uuid4()
        queued_at = datetime.now(UTC)
        result = await session.execute(
            insert(EvaluationJob)
            .values(
                id=job_id,
                submission_id=submission.id,
                requested_by=self._requested_by,
                source_report_id=source_report_id,
                reason=reason,
                status="queued",
                idempotency_key=key,
                attempt_count=0,
                provider=self._identity.provider,
                model=self._identity.model,
                queued_at=queued_at,
            )
            .on_conflict_do_nothing(index_elements=[EvaluationJob.idempotency_key])
            .returning(EvaluationJob.id)
        )
        inserted_id = result.scalar_one_or_none()
        if inserted_id is None:
            job = await session.scalar(
                select(EvaluationJob).where(EvaluationJob.idempotency_key == key)
            )
            if job is None:  # pragma: no cover - defensive against external corruption
                raise RuntimeError("idempotent evaluation job disappeared")
        else:
            job = await session.get(EvaluationJob, inserted_id)
            if job is None:  # pragma: no cover - INSERT ... RETURNING invariant
                raise RuntimeError("created evaluation job disappeared")
            await enqueue_evaluation_outbox(
                session,
                kind=OUTBOX_DISPATCH,
                job_id=uuid.UUID(str(job.id)),
            )
        return job

    async def redrive_after_commit(self, job_ids: tuple[uuid.UUID, ...]) -> None:
        if not job_ids or any(not isinstance(job_id, uuid.UUID) for job_id in job_ids):
            raise ValueError("job_ids must be a non-empty UUID tuple")
        await self._redrive_after_commit(job_ids)

    async def _validate_manual_source(
        self,
        session: AsyncSession,
        *,
        submission_id: uuid.UUID,
        source_report_id: uuid.UUID,
    ) -> None:
        latest_version = await session.scalar(
            select(func.max(EvaluationReport.version)).where(
                EvaluationReport.submission_id == submission_id
            )
        )
        source = await session.scalar(
            select(EvaluationReport).where(
                EvaluationReport.id == source_report_id,
                EvaluationReport.submission_id == submission_id,
                EvaluationReport.version == latest_version,
                EvaluationReport.review_status != ReviewStatus.SUPERSEDED,
            )
        )
        if source is None:
            raise EvaluationSubjectNotFound("evaluation source not found")
        if source.origin is ReportOrigin.TEACHER and source.review_status not in {
            ReviewStatus.CONFIRMED,
            ReviewStatus.MODIFIED,
        }:
            raise EvaluationSubjectNotFound("evaluation source not found")
        lineage = source
        seen: set[uuid.UUID] = set()
        for _ in range(100):
            lineage_id = uuid.UUID(str(lineage.id))
            if lineage_id in seen:
                break
            seen.add(lineage_id)
            if lineage.origin is ReportOrigin.AGENT:
                return
            if lineage.origin is not ReportOrigin.TEACHER or lineage.source_report_id is None:
                break
            lineage = await session.scalar(
                select(EvaluationReport).where(
                    EvaluationReport.id == lineage.source_report_id,
                    EvaluationReport.submission_id == submission_id,
                )
            )
            if lineage is None:
                break
        raise EvaluationSubjectNotFound("evaluation source not found")

    async def get_latest_job(self, submission_id: uuid.UUID) -> EvaluationJob | None:
        self._require_actor_mode()
        if not isinstance(submission_id, uuid.UUID):
            raise TypeError("submission_id must be a UUID")
        async with self._session_factory() as session:
            await self._require_teacher(session)
            return await session.scalar(
                select(EvaluationJob)
                .where(EvaluationJob.submission_id == submission_id)
                .order_by(EvaluationJob.queued_at.desc(), EvaluationJob.id.desc())
                .limit(1)
            )

    async def request_assignment(
        self,
        assignment_id: uuid.UUID,
        reason: JobReason,
    ) -> EvaluationBatch:
        async with self._session_factory() as session, session.begin():
            batch, created_ids = await self._enqueue_assignment_in_transaction(
                session,
                assignment_id,
                reason,
            )

        immediate_ids = created_ids[:MAX_IMMEDIATE_DISPATCH]
        if immediate_ids:
            await self._redrive_after_commit(immediate_ids)
        return batch

    async def enqueue_assignment_in_transaction(
        self,
        session: AsyncSession,
        assignment_id: uuid.UUID,
        reason: JobReason,
    ) -> EvaluationBatch:
        """Persist a bounded assignment batch without commit, dispatch, or Agent work."""
        batch, _ = await self._enqueue_assignment_in_transaction(
            session,
            assignment_id,
            reason,
        )
        return batch

    async def _enqueue_assignment_in_transaction(
        self,
        session: AsyncSession,
        assignment_id: uuid.UUID,
        reason: JobReason,
    ) -> tuple[EvaluationBatch, tuple[uuid.UUID, ...]]:
        self._require_actor_mode()
        if not isinstance(session, AsyncSession):
            raise TypeError("session must be an AsyncSession")
        if session.bind is not self._database_engine:
            raise ValueError("session must use the evaluation service database engine")
        if not isinstance(assignment_id, uuid.UUID):
            raise TypeError("assignment_id must be a UUID")
        if type(reason) is not JobReason:
            raise TypeError("reason must be a JobReason")
        if reason is JobReason.MANUAL_RETRY:
            raise EvaluationSubjectNotFound("evaluation source not found")
        assignment_id = uuid.UUID(str(assignment_id))
        created_ids: list[uuid.UUID] = []
        await self._require_teacher(session)
        exists = await session.scalar(select(Assignment.id).where(Assignment.id == assignment_id))
        if exists is None:
            raise EvaluationSubjectNotFound("assignment not found")
        ranked = (
            select(
                Submission.id,
                Submission.version,
                func.row_number()
                .over(
                    partition_by=Submission.student_id,
                    order_by=(
                        Submission.version.desc(),
                        Submission.submitted_at.desc(),
                        Submission.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(
                Submission.assignment_id == assignment_id,
                Submission.status == SubmissionStatus.SUBMITTED,
            )
            .cte("batch_latest_submission")
        )
        subjects = (
            await session.execute(
                select(ranked.c.id, ranked.c.version)
                .where(ranked.c.rank == 1)
                .order_by(ranked.c.id)
                .limit(MAX_EVALUATION_BATCH + 1)
            )
        ).all()
        if len(subjects) > MAX_EVALUATION_BATCH:
            raise EvaluationBatchTooLarge("evaluation batch is too large")
        if not subjects:
            return (
                EvaluationBatch(batch_id=uuid.uuid4(), queued=0, skipped=0, job_ids=()),
                (),
            )

        now = datetime.now(UTC)
        rows: list[dict[str, object]] = []
        ordered_keys: list[str] = []
        for submission_id, version in subjects:
            key = evaluation_key(submission_id, version, reason)
            ordered_keys.append(key)
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "submission_id": submission_id,
                    "requested_by": self._requested_by,
                    "reason": reason,
                    "status": JobStatus.QUEUED,
                    "idempotency_key": key,
                    "attempt_count": 0,
                    "provider": self._identity.provider,
                    "model": self._identity.model,
                    "queued_at": now,
                }
            )
        ids_by_key: dict[str, uuid.UUID] = {}
        for offset in range(0, len(rows), DATABASE_WRITE_CHUNK):
            chunk = rows[offset : offset + DATABASE_WRITE_CHUNK]
            inserted = (
                await session.execute(
                    insert(EvaluationJob)
                    .values(chunk)
                    .on_conflict_do_nothing(index_elements=[EvaluationJob.idempotency_key])
                    .returning(EvaluationJob.id, EvaluationJob.idempotency_key)
                )
            ).all()
            chunk_created_ids = [uuid.UUID(str(row.id)) for row in inserted]
            created_ids.extend(chunk_created_ids)
            await enqueue_dispatch_outbox_batch(session, chunk_created_ids)
        for offset in range(0, len(ordered_keys), DATABASE_WRITE_CHUNK):
            key_chunk = ordered_keys[offset : offset + DATABASE_WRITE_CHUNK]
            all_jobs = (
                await session.execute(
                    select(EvaluationJob.id, EvaluationJob.idempotency_key).where(
                        EvaluationJob.idempotency_key.in_(key_chunk)
                    )
                )
            ).all()
            ids_by_key.update({row.idempotency_key: uuid.UUID(str(row.id)) for row in all_jobs})
        job_ids = tuple(ids_by_key[key] for key in ordered_keys)
        created_job_ids = tuple(created_ids)
        return (
            EvaluationBatch(
                batch_id=uuid.uuid4(),
                queued=len(created_job_ids),
                skipped=len(job_ids) - len(created_job_ids),
                job_ids=job_ids,
            ),
            created_job_ids,
        )

    async def _redrive_after_commit(self, job_ids: tuple[uuid.UUID, ...]) -> None:
        await redrive_evaluation_outbox(
            self._database_engine,
            dispatch=self._dispatch,
            notification=None,
            job_ids=job_ids,
            limit=min(len(job_ids) * 2, 1_000),
        )
        if self._notification is None:
            return
        session_factory = migration_gated_sessionmaker(
            self._database_engine,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            pending = (
                await session.execute(
                    select(EvaluationOutbox.job_id, EvaluationOutbox.report_id).where(
                        EvaluationOutbox.kind == OUTBOX_NOTIFICATION,
                        EvaluationOutbox.job_id.in_(job_ids),
                        EvaluationOutbox.delivered_at.is_(None),
                        EvaluationOutbox.failed_at.is_(None),
                    )
                )
            ).all()
        for job_id, report_id in pending:
            if report_id is None:  # pragma: no cover - database constraint
                continue
            try:
                outcome = self._notification(
                    uuid.UUID(str(job_id)),
                    uuid.UUID(str(report_id)),
                )
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:  # noqa: BLE001 - durable outbox remains pending
                logger.warning(
                    "evaluation notification scheduling failed job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )

    async def evaluate_now(
        self,
        submission_id: uuid.UUID,
        reason: JobReason,
        *,
        source_report_id: uuid.UUID | None = None,
    ) -> EvaluationReport:
        job = await self.request(
            submission_id,
            reason,
            source_report_id=source_report_id,
        )
        return await self.execute_job(job.id)

    async def execute_job(self, job_id: uuid.UUID) -> EvaluationReport:
        if not isinstance(job_id, uuid.UUID):
            raise TypeError("job_id must be a UUID")
        job_id = uuid.UUID(str(job_id))
        if self._worker_job_id is not None and job_id != self._worker_job_id:
            raise PermissionError("worker execution is bound to one persisted job")
        async with self._execution_fence(job_id) as acquired:
            if not acquired:
                return await self._existing_job_outcome(job_id)
            try:
                return await self._execute_job_fenced(job_id)
            except (SystemExit, MemoryError):
                if self._worker_job_id is not None:
                    await self._retry_fatal_compensation()
                raise

    @asynccontextmanager
    async def _execution_fence(self, job_id: uuid.UUID) -> AsyncIterator[bool]:
        connection: AsyncConnection = await self._database_engine.connect()
        acquired = False
        try:
            acquired = bool(await connection.scalar(_TRY_JOB_LOCK_SQL, {"job_id": str(job_id)}))
            await connection.commit()
            yield acquired
        finally:
            if acquired and not connection.closed:
                try:
                    await connection.execute(_UNLOCK_JOB_SQL, {"job_id": str(job_id)})
                    await connection.commit()
                except Exception as exc:  # noqa: BLE001 - never return a locked session to pool
                    logger.warning(
                        "evaluation advisory unlock failed job_id=%s error_type=%s",
                        job_id,
                        type(exc).__name__,
                    )
                    await connection.invalidate()
            await connection.close()

    async def _execute_job_fenced(self, job_id: uuid.UUID) -> EvaluationReport:
        started_at = datetime.now(UTC)
        stale_before = started_at - WORKER_STALE_AFTER
        execution_token = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            if self._worker_job_id is None:
                await self._require_teacher(session)
            claimed_id = await session.scalar(
                update(EvaluationJob)
                .where(
                    EvaluationJob.id == job_id,
                    EvaluationJob.requested_by == self._requested_by,
                    or_(
                        EvaluationJob.status == JobStatus.QUEUED,
                        and_(
                            EvaluationJob.status == JobStatus.RUNNING,
                            or_(
                                EvaluationJob.started_at < stale_before,
                                self._worker_redelivered,
                            ),
                            ~select(EvaluationReport.id)
                            .where(EvaluationReport.job_id == EvaluationJob.id)
                            .exists(),
                        ),
                    ),
                )
                .values(
                    status=JobStatus.RUNNING,
                    started_at=started_at,
                    execution_token=execution_token,
                    execution_generation=EvaluationJob.execution_generation + 1,
                )
                .returning(EvaluationJob.id)
            )
        if claimed_id is None:
            return await self._existing_job_outcome(job_id)
        self._active_execution_token = execution_token

        try:
            request = await self._build_request(job_id)
        except asyncio.CancelledError:
            await self._cancel_running_job(job_id)
            raise
        except EvaluationSubjectNotFound:
            await self._cancel_running_job(job_id)
            raise EvaluationJobFailed("evaluation failed") from None
        except (TypeError, ValueError, UnicodeError, RecursionError):
            await self._persist_configuration_failure(job_id)
            raise EvaluationJobFailed("evaluation failed") from None

        try:
            engine_outcome = await self._engine.execute_with_evidence(request)
        except asyncio.CancelledError:
            await self._cancel_running_job(job_id)
            raise
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - unexpected provider defects become fixed safe evidence
            await self._persist_internal_failure(job_id)
            raise EvaluationJobFailed("evaluation failed") from None

        if type(engine_outcome) is EngineFailureEvidence:
            await self._persist_failure(job_id, engine_outcome)
            raise EvaluationJobFailed("evaluation failed") from None
        engine_result = engine_outcome

        if (engine_result.provider, engine_result.model) != (
            self._identity.provider,
            self._identity.model,
        ):
            await self._persist_internal_failure(job_id)
            raise EvaluationJobFailed("evaluation failed")

        try:
            report = await self._persist_success(job_id, engine_result)
        except _EvaluationSubjectWithdrawn:
            await self._persist_cancelled_evidence(job_id, engine_result)
            raise EvaluationJobFailed("evaluation failed") from None
        except EvaluationSubjectNotFound:
            await self._persist_internal_failure(job_id)
            raise EvaluationJobFailed("evaluation failed") from None
        except EvaluationInProgress:
            raise
        except Exception:  # noqa: BLE001 - persistence failures must not leak DB details
            await self._persist_internal_failure(job_id)
            raise EvaluationJobFailed("evaluation failed") from None
        await self._redrive_after_commit((job_id,))
        return report

    async def fail_worker_job_internal(self) -> None:
        if self._worker_job_id is None:
            raise PermissionError("internal failure compensation is worker-only")
        await self._persist_internal_failure(self._worker_job_id)

    async def _retry_fatal_compensation(self) -> None:
        for attempt in range(3):
            try:
                await self.fail_worker_job_internal()
                return
            except Exception as exc:  # noqa: BLE001 - task must remain unacknowledged on outage
                logger.warning(
                    "evaluation fatal compensation failed job_id=%s attempt=%s error_type=%s",
                    self._worker_job_id,
                    attempt + 1,
                    type(exc).__name__,
                )
                if attempt == 2:
                    raise EvaluationRecoveryRequired("evaluation recovery required") from None
                await asyncio.sleep(0)

    def _execution_token(self) -> uuid.UUID:
        if self._active_execution_token is None:
            raise EvaluationInProgress("evaluation execution is not owned")
        return self._active_execution_token

    async def _existing_job_outcome(self, job_id: uuid.UUID) -> EvaluationReport:
        async with self._session_factory() as session:
            job = await session.scalar(
                select(EvaluationJob).where(
                    EvaluationJob.id == job_id,
                    EvaluationJob.requested_by == self._requested_by,
                )
            )
            if job is None:
                raise EvaluationSubjectNotFound("evaluation job not found")
            if job.status is JobStatus.SUCCEEDED:
                report = await session.scalar(
                    select(EvaluationReport).where(EvaluationReport.job_id == job_id)
                )
                if report is None:
                    raise EvaluationJobFailed("evaluation failed")
                return report
            if job.status is JobStatus.FAILED:
                raise EvaluationJobFailed("evaluation failed")
            if job.status is JobStatus.CANCELLED:
                raise EvaluationJobFailed("evaluation failed")
            raise EvaluationInProgress("evaluation is already running")

    async def _build_request(self, job_id: uuid.UUID) -> EvaluationRequest:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EvaluationJob, Submission, Assignment)
                    .join(Submission, Submission.id == EvaluationJob.submission_id)
                    .join(Assignment, Assignment.id == Submission.assignment_id)
                    .where(
                        EvaluationJob.id == job_id,
                        EvaluationJob.status == JobStatus.RUNNING,
                        Submission.status == SubmissionStatus.SUBMITTED,
                    )
                )
            ).one_or_none()
            if row is None:
                raise EvaluationSubjectNotFound("evaluation subject not found")
            job, submission, assignment = row
            if (job.provider, job.model) != (
                self._identity.provider,
                self._identity.model,
            ):
                raise ValueError("evaluation provider identity mismatch")
            student_answer = submission.content_text
            if submission.content_json is not None:
                student_answer = json.dumps(
                    {
                        "content_text": submission.content_text,
                        "content_json": submission.content_json,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            return EvaluationRequest(
                assignment_title=assignment.title,
                question=assignment.question,
                rubric=assignment.rubric,
                student_answer=student_answer,
                fixture_key=(self._mock_fixture_key if self._identity.provider == "mock" else None),
            )

    async def _persist_success(
        self,
        job_id: uuid.UUID,
        result: EngineResult,
    ) -> EvaluationReport:
        output = result.output.model_dump(mode="json")
        finished_at = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(EvaluationJob)
                    .where(
                        EvaluationJob.id == job_id,
                        EvaluationJob.status == JobStatus.RUNNING,
                        EvaluationJob.execution_token == self._execution_token(),
                    )
                    .with_for_update()
                )
                if job is None:
                    raise EvaluationInProgress("evaluation job is no longer running")
                submission = await session.scalar(
                    select(Submission).where(Submission.id == job.submission_id).with_for_update()
                )
                if submission is None:
                    raise EvaluationSubjectNotFound("evaluation subject not found")
                if submission.status is SubmissionStatus.WITHDRAWN:
                    raise _EvaluationSubjectWithdrawn("evaluation subject not found")
                assignment = await session.scalar(
                    select(Assignment)
                    .where(Assignment.id == submission.assignment_id)
                    .with_for_update()
                )
                if assignment is None or assignment.status not in {
                    AssignmentStatus.PUBLISHED,
                    AssignmentStatus.CLOSED,
                }:
                    raise EvaluationSubjectNotFound("evaluation assignment unavailable")
                latest_version = await session.scalar(
                    select(func.coalesce(func.max(EvaluationReport.version), 0)).where(
                        EvaluationReport.submission_id == submission.id
                    )
                )
                source: EvaluationReport | None = None
                if job.reason is JobReason.MANUAL_RETRY:
                    if job.source_report_id is None:
                        raise EvaluationSubjectNotFound("evaluation source not found")
                    source = await session.scalar(
                        select(EvaluationReport)
                        .where(
                            EvaluationReport.id == job.source_report_id,
                            EvaluationReport.submission_id == submission.id,
                        )
                        .with_for_update()
                    )
                    if (
                        source is None
                        or source.review_status is ReviewStatus.SUPERSEDED
                        or source.version != latest_version
                    ):
                        raise EvaluationSubjectNotFound("evaluation source not found")
                version = latest_version + 1
                report = EvaluationReport(
                    submission_id=submission.id,
                    job_id=job.id,
                    source_report_id=None,
                    origin=ReportOrigin.AGENT,
                    created_by_teacher_id=None,
                    version=version,
                    schema_version=output["schema_version"],
                    completeness=output["answer_completeness"],
                    correctness=output["correctness"],
                    major_issues=output["major_issues"],
                    suggestions=output["suggestions"],
                    score=output["score"]["value"],
                    grade=Grade(output["score"]["grade"]),
                    confidence=Decimal(str(output["score"]["confidence"])),
                    limitations=output["limitations"],
                    raw_model_output=result.raw_text,
                    validation_status=result.validation_status,
                    review_status=ReviewStatus.PROPOSED,
                )
                session.add(report)
                await session.flush([report])
                if source is not None:
                    source.review_status = ReviewStatus.SUPERSEDED
                job.status = JobStatus.SUCCEEDED
                job.attempt_count = result.attempt_count
                job.execution_token = None
                job.finished_at = finished_at
                await session.flush([job])
                await append_audit_log(
                    session,
                    AuditLogCreate(
                        job_id=uuid.UUID(str(job.id)),
                        submission_id=uuid.UUID(str(submission.id)),
                        provider=result.provider,
                        model=result.model,
                        prompt_template_version=PROMPT_TEMPLATE_VERSION,
                        schema_version=output["schema_version"],
                        duration_ms=result.duration_ms,
                        retry_count=result.attempt_count - 1,
                        raw_model_output=result.raw_text,
                        validation_status=result.validation_status,
                        validation_failures=tuple(failure.code for failure in result.failures),
                        error_type=None,
                        error_message=None,
                        final_report_id=uuid.UUID(str(report.id)),
                    ),
                )
                await enqueue_evaluation_outbox(
                    session,
                    kind=OUTBOX_NOTIFICATION,
                    job_id=uuid.UUID(str(job.id)),
                    report_id=uuid.UUID(str(report.id)),
                )
            return report

    async def _persist_failure(self, job_id: uuid.UUID, failure: EngineFailureEvidence) -> None:
        await self._write_failed_terminal(
            job_id,
            error_type=failure.error_type,
            error_message=SAFE_AUDIT_ERROR_MESSAGES[failure.error_type],
            attempt_count=failure.attempt_count,
            retry_count=failure.retry_count,
            duration_ms=failure.duration_ms,
            raw_model_output=failure.last_raw_text,
            failures=tuple(item.code for item in failure.failures),
        )

    async def _persist_internal_failure(self, job_id: uuid.UUID) -> None:
        await self._write_failed_terminal(
            job_id,
            error_type="internal_error",
            error_message="evaluation failed",
            attempt_count=1,
            retry_count=0,
            duration_ms=0,
            raw_model_output=None,
            failures=(),
        )

    async def _persist_configuration_failure(self, job_id: uuid.UUID) -> None:
        await self._write_failed_terminal(
            job_id,
            error_type="configuration",
            error_message="evaluation provider configuration is invalid",
            attempt_count=0,
            retry_count=0,
            duration_ms=0,
            raw_model_output=None,
            failures=(),
        )

    async def _persist_cancelled_evidence(
        self,
        job_id: uuid.UUID,
        result: EngineResult,
    ) -> None:
        for attempt in range(3):
            try:
                async with self._session_factory() as session, session.begin():
                    job = await session.scalar(
                        select(EvaluationJob)
                        .where(
                            EvaluationJob.id == job_id,
                            EvaluationJob.status == JobStatus.RUNNING,
                            EvaluationJob.execution_token == self._execution_token(),
                        )
                        .with_for_update()
                    )
                    if job is None:
                        return
                    submission_status = await session.scalar(
                        select(Submission.status)
                        .where(Submission.id == job.submission_id)
                        .with_for_update()
                    )
                    if submission_status is not SubmissionStatus.WITHDRAWN:
                        raise EvaluationSubjectNotFound("evaluation subject not found")
                    job.status = JobStatus.CANCELLED
                    job.attempt_count = result.attempt_count
                    job.execution_token = None
                    job.error_code = None
                    job.error_message = None
                    job.finished_at = datetime.now(UTC)
                    await session.flush([job])
                    await append_audit_log(
                        session,
                        AuditLogCreate(
                            job_id=uuid.UUID(str(job.id)),
                            submission_id=uuid.UUID(str(job.submission_id)),
                            provider=result.provider,
                            model=result.model,
                            prompt_template_version=PROMPT_TEMPLATE_VERSION,
                            schema_version="1.0",
                            duration_ms=result.duration_ms,
                            retry_count=result.attempt_count - 1,
                            raw_model_output=result.raw_text,
                            validation_status=None,
                            validation_failures=tuple(failure.code for failure in result.failures),
                            error_type="cancelled",
                            error_message=SAFE_AUDIT_ERROR_MESSAGES["cancelled"],
                            final_report_id=None,
                        ),
                    )
                return
            except EvaluationSubjectNotFound:
                raise
            except Exception as exc:  # noqa: BLE001 - terminal compensation is retried
                logger.warning(
                    "evaluation cancellation evidence persistence failed "
                    "job_id=%s attempt=%s error_type=%s",
                    job_id,
                    attempt + 1,
                    type(exc).__name__,
                )
                if attempt == 2:
                    raise EvaluationRecoveryRequired("evaluation recovery required") from None
                await asyncio.sleep(0)

    async def _write_failed_terminal(
        self,
        job_id: uuid.UUID,
        *,
        error_type: str,
        error_message: str,
        attempt_count: int,
        retry_count: int,
        duration_ms: int,
        raw_model_output: str | None,
        failures: tuple[RepairErrorCode, ...],
    ) -> None:
        async with self._session_factory() as session, session.begin():
            job = await session.scalar(
                select(EvaluationJob)
                .where(
                    EvaluationJob.id == job_id,
                    EvaluationJob.status == JobStatus.RUNNING,
                    EvaluationJob.execution_token == self._execution_token(),
                )
                .with_for_update()
            )
            if job is None:
                return
            job.status = JobStatus.FAILED
            job.attempt_count = attempt_count
            job.execution_token = None
            job.error_code = error_type
            job.error_message = error_message
            job.finished_at = datetime.now(UTC)
            await session.flush([job])
            await append_audit_log(
                session,
                AuditLogCreate(
                    job_id=uuid.UUID(str(job.id)),
                    submission_id=uuid.UUID(str(job.submission_id)),
                    provider=job.provider,
                    model=job.model,
                    prompt_template_version=PROMPT_TEMPLATE_VERSION,
                    schema_version="1.0",
                    duration_ms=duration_ms,
                    retry_count=retry_count,
                    raw_model_output=raw_model_output,
                    validation_status=None,
                    validation_failures=failures,
                    error_type=error_type,
                    error_message=error_message,
                    final_report_id=None,
                ),
            )

    async def _cancel_running_job(self, job_id: uuid.UUID) -> None:
        for attempt in range(3):
            try:
                async with self._session_factory() as session, session.begin():
                    await session.execute(
                        update(EvaluationJob)
                        .where(
                            EvaluationJob.id == job_id,
                            EvaluationJob.status == JobStatus.RUNNING,
                            EvaluationJob.execution_token == self._execution_token(),
                        )
                        .values(
                            status=JobStatus.CANCELLED,
                            execution_token=None,
                            finished_at=datetime.now(UTC),
                            error_code=None,
                            error_message=None,
                        )
                    )
                return
            except Exception as exc:  # noqa: BLE001 - transient compensation is retried
                logger.warning(
                    "evaluation cancellation persistence failed job_id=%s attempt=%s error_type=%s",
                    job_id,
                    attempt + 1,
                    type(exc).__name__,
                )
                if attempt == 2:
                    raise EvaluationRecoveryRequired("evaluation recovery required") from None
                await asyncio.sleep(0)
