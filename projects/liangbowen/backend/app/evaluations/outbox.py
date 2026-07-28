from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.migration_gate import migration_gated_sessionmaker
from app.evaluations.model import EvaluationJob, EvaluationOutbox
from app.evaluations.types import JobStatus
from app.integrations.mattermost.ids import validate_mattermost_id
from app.integrations.mattermost.model import IntegrationEvent, IntegrationEventStatus

logger = logging.getLogger(__name__)
Dispatch = Callable[[uuid.UUID], Awaitable[None] | None]
Notification = Callable[[uuid.UUID, uuid.UUID], Awaitable[str] | str]
OUTBOX_DISPATCH = "dispatch"
OUTBOX_NOTIFICATION = "notification"
MAX_OUTBOX_ATTEMPTS = 1_000
MAX_NOTIFICATION_ATTEMPTS = 3
MAX_REDRIVE_BATCH = 1_000
OUTBOX_CLAIM_TTL = timedelta(seconds=60)
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_SAFE_NOTIFICATION_ERROR_TYPES = frozenset(
    {
        "authentication",
        "configuration",
        "network",
        "protocol",
        "rate_limited",
        "recipient",
        "response_too_large",
        "upstream",
    }
)
_TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class OutboxDeliveryResult:
    scanned: int
    delivered: int
    failed: int


@dataclass(frozen=True, slots=True)
class _ClaimedOutboxEvent:
    id: uuid.UUID
    token: uuid.UUID
    kind: str
    job_id: uuid.UUID
    report_id: uuid.UUID | None
    attempt_count: int
    job_status: JobStatus
    actor_user_id: uuid.UUID


async def enqueue_evaluation_outbox(
    session: AsyncSession,
    *,
    kind: str,
    job_id: uuid.UUID,
    report_id: uuid.UUID | None = None,
) -> uuid.UUID:
    if kind not in {OUTBOX_DISPATCH, OUTBOX_NOTIFICATION}:
        raise ValueError("unsupported evaluation outbox kind")
    if not isinstance(job_id, uuid.UUID):
        raise TypeError("job_id must be a UUID")
    if (kind == OUTBOX_DISPATCH) is not (report_id is None):
        raise ValueError("outbox payload does not match its kind")
    if report_id is not None and not isinstance(report_id, uuid.UUID):
        raise TypeError("report_id must be a UUID")
    outbox_id = uuid.uuid4()
    persisted_id = await session.scalar(
        insert(EvaluationOutbox)
        .values(
            id=outbox_id,
            kind=kind,
            job_id=job_id,
            report_id=report_id,
            attempt_count=0,
            next_attempt_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=[EvaluationOutbox.kind, EvaluationOutbox.job_id])
        .returning(EvaluationOutbox.id)
    )
    if persisted_id is not None:
        return uuid.UUID(str(persisted_id))
    existing_id = await session.scalar(
        select(EvaluationOutbox.id).where(
            EvaluationOutbox.kind == kind,
            EvaluationOutbox.job_id == job_id,
        )
    )
    if existing_id is None:  # pragma: no cover - defensive corruption guard
        raise RuntimeError("evaluation outbox event disappeared")
    return uuid.UUID(str(existing_id))


async def enqueue_dispatch_outbox_batch(
    session: AsyncSession,
    job_ids: Sequence[uuid.UUID],
) -> None:
    if not job_ids:
        return
    if any(not isinstance(job_id, uuid.UUID) for job_id in job_ids):
        raise TypeError("job_ids must contain UUID values")
    now = datetime.now(UTC)
    await session.execute(
        insert(EvaluationOutbox)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "kind": OUTBOX_DISPATCH,
                    "job_id": job_id,
                    "report_id": None,
                    "attempt_count": 0,
                    "next_attempt_at": now,
                }
                for job_id in job_ids
            ]
        )
        .on_conflict_do_nothing(index_elements=[EvaluationOutbox.kind, EvaluationOutbox.job_id])
    )


def _safe_error_type(error: Exception) -> str:
    external_category = getattr(error, "error_type", None)
    if external_category in _SAFE_NOTIFICATION_ERROR_TYPES:
        return external_category
    candidate = type(error).__name__
    return candidate if _SAFE_ERROR_TYPE.fullmatch(candidate) else "Exception"


def _retry_delay(attempt_count: int) -> timedelta:
    return timedelta(seconds=min(300, 2 ** min(attempt_count, 8)))


async def _invoke(callback: Callable[..., object], *args: uuid.UUID) -> object:
    if inspect.iscoroutinefunction(callback):
        outcome = callback(*args)
    else:
        outcome = await asyncio.to_thread(callback, *args)
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


async def _renew_outbox_claim(
    engine: AsyncEngine,
    event: _ClaimedOutboxEvent,
) -> bool:
    session_factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        renewed_id = await session.scalar(
            update(EvaluationOutbox)
            .where(
                EvaluationOutbox.id == event.id,
                EvaluationOutbox.claim_token == event.token,
                EvaluationOutbox.delivered_at.is_(None),
                EvaluationOutbox.failed_at.is_(None),
            )
            .values(claim_expires_at=datetime.now(UTC) + OUTBOX_CLAIM_TTL)
            .returning(EvaluationOutbox.id)
        )
    return renewed_id is not None


async def _heartbeat_outbox_claim(
    engine: AsyncEngine,
    event: _ClaimedOutboxEvent,
    stop: asyncio.Event,
) -> None:
    interval = max(0.01, OUTBOX_CLAIM_TTL.total_seconds() / 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            try:
                if not await _renew_outbox_claim(engine, event):
                    return
            except Exception as exc:  # noqa: BLE001 - a lease outage remains at-least-once
                logger.warning(
                    "evaluation outbox lease renewal failed event_id=%s error_type=%s",
                    event.id,
                    _safe_error_type(exc),
                )
                return


async def _invoke_with_claim_heartbeat(
    engine: AsyncEngine,
    event: _ClaimedOutboxEvent,
    callback: Callable[..., object],
    args: tuple[uuid.UUID, ...],
) -> object:
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_outbox_claim(engine, event, stop))
    try:
        return await _invoke(callback, *args)
    finally:
        stop.set()
        await heartbeat


async def redrive_evaluation_outbox(
    engine: AsyncEngine,
    *,
    dispatch: Dispatch | None,
    notification: Notification | None,
    limit: int = 100,
    job_ids: Sequence[uuid.UUID] | None = None,
) -> OutboxDeliveryResult:
    if not isinstance(engine, AsyncEngine):
        raise TypeError("engine must be an AsyncEngine")
    if type(limit) is not int or not 1 <= limit <= MAX_REDRIVE_BATCH:
        raise ValueError("outbox redrive limit is outside the allowed range")
    if job_ids is not None and any(not isinstance(job_id, uuid.UUID) for job_id in job_ids):
        raise TypeError("job_ids must contain UUID values")
    available_kinds: list[str] = []
    if dispatch is not None:
        available_kinds.append(OUTBOX_DISPATCH)
    if notification is not None:
        available_kinds.append(OUTBOX_NOTIFICATION)
    if not available_kinds:
        return OutboxDeliveryResult(scanned=0, delivered=0, failed=0)
    scanned = 0
    delivered = 0
    failed = 0
    for _ in range(limit):
        claimed = await _claim_evaluation_outbox(
            engine,
            kinds=tuple(available_kinds),
            limit=1,
            job_ids=job_ids,
        )
        if not claimed:
            break
        event = claimed[0]
        scanned += 1
        callback: Callable[..., object] | None
        args: tuple[uuid.UUID, ...]
        if event.kind == OUTBOX_DISPATCH:
            if event.job_status in _TERMINAL_JOB_STATUSES:
                if await _finalize_outbox_claim(engine, event, error=None):
                    delivered += 1
                continue
            callback = dispatch
            args = (event.job_id,)
        else:
            callback = notification
            if event.report_id is None:  # pragma: no cover - database check constraint
                raise RuntimeError("notification outbox event has no report")
            args = (event.job_id, event.report_id)
        if callback is None:  # pragma: no cover - claim query filters unavailable kinds
            continue
        try:
            outcome = await _invoke_with_claim_heartbeat(engine, event, callback, args)
        except Exception as exc:  # noqa: BLE001 - broker details must remain out of storage
            if await _finalize_outbox_claim(engine, event, error=exc):
                failed += 1
            logger.warning(
                "evaluation outbox delivery failed event_id=%s kind=%s error_type=%s",
                event.id,
                event.kind,
                _safe_error_type(exc),
            )
        else:
            delivery_ref: str | None = None
            if event.kind == OUTBOX_NOTIFICATION:
                try:
                    delivery_ref = validate_mattermost_id(outcome, name="delivery_ref")
                except (TypeError, ValueError):
                    error = ValueError("notification callback returned an invalid post id")
                    if await _finalize_outbox_claim(engine, event, error=error):
                        failed += 1
                    continue
            if await _finalize_outbox_claim(
                engine,
                event,
                error=None,
                delivery_ref=delivery_ref,
            ):
                delivered += 1
    return OutboxDeliveryResult(scanned=scanned, delivered=delivered, failed=failed)


async def _claim_evaluation_outbox(
    engine: AsyncEngine,
    *,
    kinds: tuple[str, ...],
    limit: int,
    job_ids: Sequence[uuid.UUID] | None,
) -> tuple[_ClaimedOutboxEvent, ...]:
    session_factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    claimed: list[_ClaimedOutboxEvent] = []
    async with session_factory() as session, session.begin():
        statement = (
            select(EvaluationOutbox, EvaluationJob.status, EvaluationJob.requested_by)
            .join(EvaluationJob, EvaluationJob.id == EvaluationOutbox.job_id)
            .where(
                EvaluationOutbox.delivered_at.is_(None),
                EvaluationOutbox.failed_at.is_(None),
                EvaluationOutbox.next_attempt_at <= now,
                EvaluationOutbox.kind.in_(kinds),
                or_(
                    EvaluationOutbox.claim_token.is_(None),
                    EvaluationOutbox.claim_expires_at <= now,
                ),
            )
            .order_by(EvaluationOutbox.next_attempt_at, EvaluationOutbox.id)
            .limit(limit)
            .with_for_update(of=EvaluationOutbox, skip_locked=True)
        )
        if job_ids is not None:
            if not job_ids:
                return ()
            statement = statement.where(EvaluationOutbox.job_id.in_(tuple(job_ids)))
        rows = (await session.execute(statement)).all()
        for event, job_status, requested_by in rows:
            token = uuid.uuid4()
            event.claim_token = token
            event.claim_expires_at = now + OUTBOX_CLAIM_TTL
            claimed.append(
                _ClaimedOutboxEvent(
                    id=uuid.UUID(str(event.id)),
                    token=token,
                    kind=event.kind,
                    job_id=uuid.UUID(str(event.job_id)),
                    report_id=(
                        None if event.report_id is None else uuid.UUID(str(event.report_id))
                    ),
                    attempt_count=event.attempt_count,
                    job_status=JobStatus(job_status),
                    actor_user_id=uuid.UUID(str(requested_by)),
                )
            )
    return tuple(claimed)


async def _finalize_outbox_claim(
    engine: AsyncEngine,
    event: _ClaimedOutboxEvent,
    *,
    error: Exception | None,
    delivery_ref: str | None = None,
) -> bool:
    values: dict[str, object] = {
        "claim_token": None,
        "claim_expires_at": None,
    }
    now = datetime.now(UTC)
    terminal_event_type: str | None = None
    terminal_status: IntegrationEventStatus | None = None
    terminal_response: dict[str, object] | None = None
    if error is None:
        if event.kind == OUTBOX_NOTIFICATION and (delivery_ref is None):
            raise ValueError("notification delivery_ref is invalid")
        if delivery_ref is not None:
            validate_mattermost_id(delivery_ref, name="delivery_ref")
        values.update(
            delivered_at=now,
            delivery_ref=delivery_ref,
            last_error_type=None,
            last_error_summary=None,
        )
        if event.kind == OUTBOX_NOTIFICATION:
            terminal_event_type = "notification_delivered"
            terminal_status = IntegrationEventStatus.COMPLETED
            terminal_response = {"delivery_status": "delivered", "post_id": delivery_ref}
    else:
        max_attempts = (
            MAX_NOTIFICATION_ATTEMPTS if event.kind == OUTBOX_NOTIFICATION else MAX_OUTBOX_ATTEMPTS
        )
        attempt_count = min(max_attempts, event.attempt_count + 1)
        error_type = _safe_error_type(error)
        if event.kind == OUTBOX_NOTIFICATION:
            retryable = getattr(error, "retryable", True) is not False
            final_failure = not retryable or attempt_count >= MAX_NOTIFICATION_ATTEMPTS
            values.update(
                attempt_count=attempt_count,
                next_attempt_at=now,
                last_error_type=error_type,
                last_error_summary="Mattermost delivery failed",
            )
            if final_failure:
                values["failed_at"] = now
                terminal_event_type = "notification_failed"
                terminal_status = IntegrationEventStatus.DETERMINISTIC_ERROR
                terminal_response = {
                    "delivery_status": "failed",
                    "error_type": error_type,
                }
            else:
                retry_after = getattr(error, "retry_after_seconds", None)
                delay = (
                    timedelta(seconds=retry_after)
                    if type(retry_after) is int and 1 <= retry_after <= 300
                    else _retry_delay(attempt_count)
                )
                values["next_attempt_at"] = now + delay
        else:
            values.update(
                attempt_count=attempt_count,
                next_attempt_at=now + _retry_delay(attempt_count),
                last_error_type=error_type,
            )
    session_factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        finalized_id = await session.scalar(
            update(EvaluationOutbox)
            .where(
                EvaluationOutbox.id == event.id,
                EvaluationOutbox.claim_token == event.token,
                EvaluationOutbox.delivered_at.is_(None),
                EvaluationOutbox.failed_at.is_(None),
            )
            .values(**values)
            .returning(EvaluationOutbox.id)
        )
        if finalized_id is not None and terminal_event_type is not None:
            if event.report_id is None or terminal_status is None or terminal_response is None:
                raise RuntimeError("notification terminal evidence is incomplete")
            await session.execute(
                insert(IntegrationEvent)
                .values(
                    id=uuid.uuid4(),
                    request_hash=_notification_event_hash(event.id, terminal_event_type),
                    source="mattermost",
                    event_type=terminal_event_type,
                    status=terminal_status,
                    actor_user_id=event.actor_user_id,
                    response=terminal_response,
                    business_refs={
                        "outbox_id": str(event.id),
                        "job_id": str(event.job_id),
                        "report_id": str(event.report_id),
                        **(
                            {"post_id": delivery_ref}
                            if terminal_event_type == "notification_delivered"
                            else {}
                        ),
                    },
                    arrived_at=now,
                    completed_at=now,
                )
                .on_conflict_do_nothing(index_elements=[IntegrationEvent.request_hash])
            )
    return finalized_id is not None


def _notification_event_hash(outbox_id: uuid.UUID, event_type: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"mattermost-notification-evidence-v1")
    for value in (str(outbox_id), event_type):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()
