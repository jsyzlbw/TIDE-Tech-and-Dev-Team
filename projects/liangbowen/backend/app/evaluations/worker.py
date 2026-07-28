import asyncio
import logging
import uuid

from celery import Celery
from celery.exceptions import Reject
from celery.result import EagerResult
from sqlalchemy import exc as sa_exc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.migration_gate import migration_gated_sessionmaker
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob
from app.evaluations.outbox import redrive_evaluation_outbox
from app.evaluations.providers.factory import create_evaluation_provider
from app.evaluations.service import (
    EvaluationInProgress,
    EvaluationJobFailed,
    EvaluationRecoveryRequired,
    EvaluationService,
)
from app.integrations.mattermost.delivery import deliver_report_notification

logger = logging.getLogger(__name__)
EVALUATION_QUEUE = "evaluations"
NOTIFICATION_QUEUE = "notifications"
MAINTENANCE_QUEUE = "maintenance"
_RECOVERABLE_WORKER_ERRORS = (
    OSError,
    sa_exc.DisconnectionError,
    sa_exc.InterfaceError,
    sa_exc.OperationalError,
    sa_exc.PendingRollbackError,
    sa_exc.TimeoutError,
)


def _is_recoverable_worker_error(error: Exception) -> bool:
    if isinstance(error, (PermissionError, sa_exc.IntegrityError, sa_exc.ProgrammingError)):
        return False
    if isinstance(error, sa_exc.DBAPIError) and error.connection_invalidated is True:
        return True
    return isinstance(error, _RECOVERABLE_WORKER_ERRORS)


settings = get_settings()
celery_app = Celery("ai-grading-worker", broker=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=True,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    broker_connection_retry_on_startup=True,
    task_always_eager=settings.celery_task_always_eager,
    task_store_eager_result=False,
    task_routes={
        "app.evaluations.worker.run_evaluation": {"queue": EVALUATION_QUEUE},
        "app.evaluations.worker.deliver_evaluation_notification": {"queue": NOTIFICATION_QUEUE},
        "app.evaluations.worker.redrive_evaluation_delivery": {"queue": MAINTENANCE_QUEUE},
    },
    beat_schedule={
        "redrive-evaluation-delivery": {
            "task": "app.evaluations.worker.redrive_evaluation_delivery",
            "schedule": 15.0,
        }
    },
)


def _check_eager_result(result: object) -> None:
    if isinstance(result, EagerResult) and result.failed():
        raise RuntimeError("eager task failed")


async def enqueue_evaluation_task(job_id: uuid.UUID) -> None:
    result = await asyncio.to_thread(
        run_evaluation.apply_async,
        args=[str(job_id)],
        queue=EVALUATION_QUEUE,
    )
    _check_eager_result(result)


async def enqueue_notification_task(job_id: uuid.UUID, report_id: uuid.UUID) -> None:
    result = await asyncio.to_thread(
        deliver_evaluation_notification.apply_async,
        args=[str(job_id), str(report_id)],
        queue=NOTIFICATION_QUEUE,
    )
    _check_eager_result(result)


async def _run_evaluation(job_id: uuid.UUID, *, redelivered: bool) -> str:
    runtime_settings = get_settings()
    database_engine = create_async_engine(runtime_settings.database_url, pool_pre_ping=True)
    provider = None
    service: EvaluationService | None = None
    try:
        session_factory = migration_gated_sessionmaker(database_engine, expire_on_commit=False)
        async with session_factory() as session:
            requested_by = await session.scalar(
                select(EvaluationJob.requested_by).where(EvaluationJob.id == job_id)
            )
            if requested_by is None:
                raise RuntimeError("evaluation job unavailable")
            provider = create_evaluation_provider(runtime_settings)
            service = EvaluationService.for_persisted_job(
                session,
                EvaluationEngine(provider),
                job_id=job_id,
                requested_by=uuid.UUID(str(requested_by)),
                redelivered=redelivered,
                notification=enqueue_notification_task,
                mock_fixture_key=runtime_settings.agent_mock_fixture,
            )
        try:
            report = await service.execute_job(job_id)
        except (EvaluationInProgress, EvaluationJobFailed):
            return str(job_id)
        except (SystemExit, MemoryError):
            raise RuntimeError("evaluation failed") from None
        return str(report.id)
    finally:
        try:
            if provider is not None:
                close = getattr(provider, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except (Exception, SystemExit, MemoryError) as exc:  # noqa: BLE001
                        logger.warning(
                            "evaluation provider close failed error_type=%s",
                            type(exc).__name__,
                        )
        finally:
            try:
                await database_engine.dispose()
            except Exception as exc:  # noqa: BLE001 - cleanup cannot alter terminal outcome
                logger.warning(
                    "evaluation database engine dispose failed error_type=%s",
                    type(exc).__name__,
                )


async def _redrive_delivery() -> str:
    runtime_settings = get_settings()
    database_engine = create_async_engine(runtime_settings.database_url, pool_pre_ping=True)
    try:

        async def notification(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
            return await deliver_report_notification(
                database_engine,
                job_id=job_id,
                report_id=report_id,
                settings=runtime_settings,
            )

        result = await redrive_evaluation_outbox(
            database_engine,
            dispatch=enqueue_evaluation_task,
            notification=notification,
            limit=100,
        )
        return f"{result.scanned}:{result.delivered}:{result.failed}"
    finally:
        await database_engine.dispose()


async def _deliver_notification(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
    runtime_settings = get_settings()
    database_engine = create_async_engine(runtime_settings.database_url, pool_pre_ping=True)
    try:

        async def notification(claimed_job_id: uuid.UUID, claimed_report_id: uuid.UUID) -> str:
            if (claimed_job_id, claimed_report_id) != (job_id, report_id):
                raise RuntimeError("notification claim does not match task")
            return await deliver_report_notification(
                database_engine,
                job_id=claimed_job_id,
                report_id=claimed_report_id,
                settings=runtime_settings,
            )

        result = await redrive_evaluation_outbox(
            database_engine,
            dispatch=None,
            notification=notification,
            job_ids=(job_id,),
            limit=2,
        )
        return f"{result.scanned}:{result.delivered}:{result.failed}"
    finally:
        await database_engine.dispose()


@celery_app.task(
    name="app.evaluations.worker.run_evaluation",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_evaluation(task, job_id: str) -> str:
    """Run one persisted job; worker-lost redelivery may reclaim it immediately."""
    if type(job_id) is not str:
        raise ValueError("job_id must be a UUID string")
    try:
        parsed_job_id = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        raise ValueError("job_id must be a UUID string") from None
    delivery_info = getattr(task.request, "delivery_info", None) or {}
    headers = getattr(task.request, "headers", None) or {}
    redelivered = delivery_info.get("redelivered") is True or headers.get("redelivered") is True
    try:
        return asyncio.run(_run_evaluation(parsed_job_id, redelivered=redelivered))
    except EvaluationRecoveryRequired:
        raise Reject("evaluation recovery required", requeue=True) from None
    except Exception as exc:
        if _is_recoverable_worker_error(exc):
            raise Reject("evaluation recovery required", requeue=True) from None
        raise


@celery_app.task(name="app.evaluations.worker.deliver_evaluation_notification")
def deliver_evaluation_notification(job_id: str, report_id: str) -> str:
    """Deliver one persisted notification outbox row through Mattermost."""
    try:
        parsed_job_id = uuid.UUID(job_id)
        parsed_report_id = uuid.UUID(report_id)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("notification identifiers must be UUID strings") from None
    return asyncio.run(_deliver_notification(parsed_job_id, parsed_report_id))


@celery_app.task(name="app.evaluations.worker.redrive_evaluation_delivery")
def redrive_evaluation_delivery() -> str:
    return asyncio.run(_redrive_delivery())
