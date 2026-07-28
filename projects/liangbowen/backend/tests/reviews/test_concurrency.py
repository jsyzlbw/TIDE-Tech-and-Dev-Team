from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationService
from app.evaluations.types import JobReason, JobStatus, ReviewStatus
from app.reviews.model import ReviewAction
from app.reviews.schemas import ConfirmRequest, ReevaluateRequest, ReportPatch
from app.reviews.service import ReviewConflict, ReviewService
from app.reviews.types import ReviewActionType
from tests.evaluations.test_jobs import _seed_subject


async def _agent_report(session: AsyncSession, teacher_id, submission_id):
    return await EvaluationService(
        session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher_id,
        dispatch=None,
    ).evaluate_now(submission_id, JobReason.INITIAL)


def _synchronize_teacher_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ReviewService._require_teacher
    barrier = asyncio.Barrier(2)

    async def synchronized(session, teacher_id):
        teacher = await original(session, teacher_id)
        await barrier.wait()
        return teacher

    monkeypatch.setattr(ReviewService, "_require_teacher", staticmethod(synchronized))


async def _set_short_lock_timeout(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '3s'"))
    await session.execute(text("SET LOCAL statement_timeout = '5s'"))


async def _insert_direct_reevaluation(
    session: AsyncSession,
    *,
    report: EvaluationReport,
    teacher_id: uuid.UUID,
    backend_pid: asyncio.Future[int],
    attempted: asyncio.Event,
    constraints_checked: asyncio.Event,
    release_commit: asyncio.Event,
) -> EvaluationJob:
    await _set_short_lock_timeout(session)
    backend_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
    job = EvaluationJob(
        submission_id=report.submission_id,
        requested_by=teacher_id,
        source_report_id=report.id,
        reason=JobReason.MANUAL_RETRY,
        status="queued",
        idempotency_key=uuid.uuid4().hex + uuid.uuid4().hex,
        attempt_count=0,
        provider="mock",
        model="fixture-v1",
    )
    session.add(job)
    attempted.set()
    await session.flush([job])
    source_status = await session.scalar(
        select(EvaluationReport.review_status).where(EvaluationReport.id == report.id)
    )
    assert source_status is not None
    session.add(
        ReviewAction(
            report_id=report.id,
            teacher_id=teacher_id,
            action=ReviewActionType.REEVALUATE,
            changes={
                "evaluation_job_id": {"before": None, "after": str(job.id)},
                "source_review_status": {
                    "before": source_status.value,
                    "after": source_status.value,
                },
            },
            comment="",
        )
    )
    await session.flush()
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    constraints_checked.set()
    await release_commit.wait()
    await session.commit()
    return job


async def _insert_direct_confirmation(
    session: AsyncSession,
    *,
    report: EvaluationReport,
    teacher_id: uuid.UUID,
    backend_pid: asyncio.Future[int],
    attempted: asyncio.Event,
    constraints_checked: asyncio.Event,
    release_commit: asyncio.Event,
) -> None:
    await _set_short_lock_timeout(session)
    backend_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
    attempted.set()
    await session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.CONFIRMED)
    )
    session.add(
        ReviewAction(
            report_id=report.id,
            teacher_id=teacher_id,
            action=ReviewActionType.CONFIRM,
            changes={"review_status": {"before": "proposed", "after": "confirmed"}},
            comment="",
        )
    )
    await session.flush()
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    constraints_checked.set()
    await release_commit.wait()
    await session.commit()


async def _wait_for_check_or_lock(
    factory: async_sessionmaker[AsyncSession],
    backend_pid: int,
    constraints_checked: asyncio.Event,
) -> bool:
    async def wait_for_lock() -> None:
        async with factory() as monitor:
            for _ in range(300):
                wait_event_type = await monitor.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": backend_pid},
                )
                if wait_event_type == "Lock":
                    return
                await asyncio.sleep(0.01)
        raise AssertionError("transaction neither checked constraints nor waited on a lock")

    checked_task = asyncio.create_task(constraints_checked.wait())
    lock_task = asyncio.create_task(wait_for_lock())
    done, pending = await asyncio.wait(
        {checked_task, lock_task},
        timeout=4,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if not done:
        raise AssertionError("transaction neither checked constraints nor waited on a lock")
    completed = done.pop()
    await completed
    return completed is checked_task


@pytest.mark.asyncio
async def test_database_serializes_confirm_then_reevaluate_across_transactions(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    attempted = asyncio.Event()
    retry_backend_pid = asyncio.get_running_loop().create_future()
    checked = asyncio.Event()
    release = asyncio.Event()

    async with factory() as confirm_session, factory() as retry_session:
        await _set_short_lock_timeout(confirm_session)
        await confirm_session.execute(
            update(EvaluationReport)
            .where(EvaluationReport.id == report.id)
            .values(review_status=ReviewStatus.CONFIRMED)
        )
        confirm_session.add(
            ReviewAction(
                report_id=report.id,
                teacher_id=teacher.id,
                action=ReviewActionType.CONFIRM,
                changes={"review_status": {"before": "proposed", "after": "confirmed"}},
                comment="",
            )
        )
        await confirm_session.flush()
        retry_task = asyncio.create_task(
            _insert_direct_reevaluation(
                retry_session,
                report=report,
                teacher_id=teacher.id,
                backend_pid=retry_backend_pid,
                attempted=attempted,
                constraints_checked=checked,
                release_commit=release,
            )
        )
        await attempted.wait()
        retry_checked_before_confirm = await _wait_for_check_or_lock(
            factory,
            await retry_backend_pid,
            checked,
        )
        await confirm_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        if retry_checked_before_confirm:
            release.set()
            await asyncio.gather(confirm_session.commit(), retry_task)
        else:
            await confirm_session.commit()
            await asyncio.wait_for(checked.wait(), timeout=3)
            release.set()
            await retry_task

    await postgres_session.rollback()
    retry_action = await postgres_session.scalar(
        select(ReviewAction).where(
            ReviewAction.report_id == report.id,
            ReviewAction.action == ReviewActionType.REEVALUATE,
        )
    )
    assert retry_action is not None
    assert retry_action.changes["source_review_status"] == {
        "before": "confirmed",
        "after": "confirmed",
    }
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.review_status).where(EvaluationReport.id == report.id)
        )
        is ReviewStatus.CONFIRMED
    )


@pytest.mark.asyncio
async def test_database_serializes_reevaluate_then_rejects_confirm(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    retry_attempted = asyncio.Event()
    retry_backend_pid = asyncio.get_running_loop().create_future()
    retry_checked = asyncio.Event()
    release_retry = asyncio.Event()
    confirm_attempted = asyncio.Event()
    confirm_backend_pid = asyncio.get_running_loop().create_future()
    confirm_checked = asyncio.Event()
    release_confirm = asyncio.Event()

    async with factory() as retry_session, factory() as confirm_session:
        retry_task = asyncio.create_task(
            _insert_direct_reevaluation(
                retry_session,
                report=report,
                teacher_id=teacher.id,
                backend_pid=retry_backend_pid,
                attempted=retry_attempted,
                constraints_checked=retry_checked,
                release_commit=release_retry,
            )
        )
        await retry_checked.wait()
        confirm_task = asyncio.create_task(
            _insert_direct_confirmation(
                confirm_session,
                report=report,
                teacher_id=teacher.id,
                backend_pid=confirm_backend_pid,
                attempted=confirm_attempted,
                constraints_checked=confirm_checked,
                release_commit=release_confirm,
            )
        )
        await confirm_attempted.wait()
        confirm_checked_before_retry = await _wait_for_check_or_lock(
            factory,
            await confirm_backend_pid,
            confirm_checked,
        )
        if confirm_checked_before_retry:
            release_confirm.set()
            release_retry.set()
            outcomes = await asyncio.gather(retry_task, confirm_task, return_exceptions=True)
        else:
            release_retry.set()
            retry_outcome = await retry_task
            release_confirm.set()
            confirm_outcome = (await asyncio.gather(confirm_task, return_exceptions=True))[0]
            outcomes = [retry_outcome, confirm_outcome]

    assert sum(isinstance(item, EvaluationJob) for item in outcomes) == 1
    assert sum(isinstance(item, DBAPIError) for item in outcomes) == 1
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.review_status).where(EvaluationReport.id == report.id)
        )
        is ReviewStatus.PROPOSED
    )


@pytest.mark.asyncio
async def test_concurrent_confirm_is_same_teacher_idempotent(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    _synchronize_teacher_checks(monkeypatch)

    async with factory() as first_session, factory() as second_session:
        first, second = await asyncio.gather(
            ReviewService(first_session).confirm(report.id, teacher.id, ConfirmRequest()),
            ReviewService(second_session).confirm(report.id, teacher.id, ConfirmRequest()),
        )

    assert first.id == second.id == report.id
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(
                ReviewAction.report_id == report.id,
                ReviewAction.action == ReviewActionType.CONFIRM,
            )
        )
        == 1
    )
    refreshed = await postgres_session.scalar(
        select(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed.review_status is ReviewStatus.CONFIRMED


@pytest.mark.asyncio
async def test_concurrent_modify_creates_one_contiguous_version(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    _synchronize_teacher_checks(monkeypatch)

    async with factory() as first_session, factory() as second_session:
        outcomes = await asyncio.gather(
            ReviewService(first_session).modify(report.id, teacher.id, ReportPatch(score=81)),
            ReviewService(second_session).modify(report.id, teacher.id, ReportPatch(score=91)),
            return_exceptions=True,
        )

    assert sum(isinstance(item, EvaluationReport) for item in outcomes) == 1
    assert sum(isinstance(item, ReviewConflict) for item in outcomes) == 1
    await postgres_session.rollback()
    versions = (
        (
            await postgres_session.execute(
                select(EvaluationReport.version)
                .where(EvaluationReport.submission_id == submission.id)
                .order_by(EvaluationReport.version)
            )
        )
        .scalars()
        .all()
    )
    assert versions == [1, 2]
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(
                ReviewAction.report_id == report.id,
                ReviewAction.action == ReviewActionType.MODIFY,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_concurrent_confirm_vs_modify_has_exactly_one_winner(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    _synchronize_teacher_checks(monkeypatch)

    async with factory() as confirm_session, factory() as modify_session:
        outcomes = await asyncio.gather(
            ReviewService(confirm_session).confirm(
                report.id,
                teacher.id,
                ConfirmRequest(comment="confirm"),
            ),
            ReviewService(modify_session).modify(
                report.id,
                teacher.id,
                ReportPatch(score=91, comment="modify"),
            ),
            return_exceptions=True,
        )

    assert sum(isinstance(item, EvaluationReport) for item in outcomes) == 1
    assert sum(isinstance(item, ReviewConflict) for item in outcomes) == 1
    await postgres_session.rollback()
    actions = await postgres_session.scalar(
        select(func.count())
        .select_from(ReviewAction)
        .where(
            ReviewAction.report_id == report.id,
            ReviewAction.action.in_((ReviewActionType.CONFIRM, ReviewActionType.MODIFY)),
        )
    )
    versions = (
        (
            await postgres_session.execute(
                select(EvaluationReport.version)
                .where(EvaluationReport.submission_id == submission.id)
                .order_by(EvaluationReport.version)
            )
        )
        .scalars()
        .all()
    )
    assert actions == 1
    assert versions in ([1], [1, 2])


@pytest.mark.asyncio
async def test_concurrent_reevaluate_creates_one_job_outbox_and_action(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    _synchronize_teacher_checks(monkeypatch)

    async with (
        factory() as first_session,
        factory() as second_session,
        factory() as first_evaluation_session,
        factory() as second_evaluation_session,
    ):
        first_evaluation = EvaluationService(
            first_evaluation_session,
            EvaluationEngine(MockEvaluationProvider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        second_evaluation = EvaluationService(
            second_evaluation_session,
            EvaluationEngine(MockEvaluationProvider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        first, second = await asyncio.gather(
            ReviewService(first_session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(),
                first_evaluation,
            ),
            ReviewService(second_session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(),
                second_evaluation,
            ),
        )

    assert first.id == second.id
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(EvaluationJob)
            .where(EvaluationJob.source_report_id == report.id)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(EvaluationOutbox)
            .where(
                EvaluationOutbox.job_id == first.id,
                EvaluationOutbox.kind == "dispatch",
            )
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(
                ReviewAction.report_id == report.id,
                ReviewAction.action == ReviewActionType.REEVALUATE,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_concurrent_retry_after_failed_reevaluation_creates_one_new_generation(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    initial_service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    failed = await ReviewService(postgres_session).reevaluate(
        report.id,
        teacher.id,
        ReevaluateRequest(),
        initial_service,
    )
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == failed.id)
        .values(
            status=JobStatus.FAILED,
            attempt_count=1,
            error_code="network",
            error_message="evaluation provider network unavailable",
            finished_at=func.now(),
        )
    )
    await postgres_session.commit()
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async with (
        factory() as first_session,
        factory() as second_session,
        factory() as first_evaluation_session,
        factory() as second_evaluation_session,
    ):
        first_evaluation = EvaluationService(
            first_evaluation_session,
            EvaluationEngine(MockEvaluationProvider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        second_evaluation = EvaluationService(
            second_evaluation_session,
            EvaluationEngine(MockEvaluationProvider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        first, second = await asyncio.gather(
            ReviewService(first_session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(),
                first_evaluation,
            ),
            ReviewService(second_session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(),
                second_evaluation,
            ),
        )

    assert first.id == second.id
    assert first.id != failed.id
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(func.count(EvaluationJob.id)).where(
                EvaluationJob.source_report_id == report.id,
                EvaluationJob.reason == JobReason.MANUAL_RETRY,
            )
        )
        == 2
    )
    assert (
        await postgres_session.scalar(
            select(func.count(ReviewAction.id)).where(
                ReviewAction.report_id == report.id,
                ReviewAction.action == ReviewActionType.REEVALUATE,
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_concurrent_reevaluate_vs_modify_never_mutates_a_pending_source(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    _synchronize_teacher_checks(monkeypatch)

    async with (
        factory() as review_session,
        factory() as modify_session,
        factory() as evaluation_session,
    ):
        evaluation = EvaluationService(
            evaluation_session,
            EvaluationEngine(MockEvaluationProvider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        outcomes = await asyncio.gather(
            ReviewService(review_session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(comment="retry"),
                evaluation,
            ),
            ReviewService(modify_session).modify(
                report.id,
                teacher.id,
                ReportPatch(score=91, comment="modify"),
            ),
            return_exceptions=True,
        )

    assert sum(isinstance(item, (EvaluationJob, EvaluationReport)) for item in outcomes) == 1
    assert sum(isinstance(item, ReviewConflict) for item in outcomes) == 1
    await postgres_session.rollback()
    retry_jobs = await postgres_session.scalar(
        select(func.count())
        .select_from(EvaluationJob)
        .where(EvaluationJob.source_report_id == report.id)
    )
    modified_reports = await postgres_session.scalar(
        select(func.count())
        .select_from(EvaluationReport)
        .where(EvaluationReport.source_report_id == report.id)
    )
    actions = await postgres_session.scalar(
        select(func.count())
        .select_from(ReviewAction)
        .where(
            ReviewAction.report_id == report.id,
            ReviewAction.action.in_((ReviewActionType.REEVALUATE, ReviewActionType.MODIFY)),
        )
    )
    assert (retry_jobs, modified_reports, actions) in {(1, 0, 1), (0, 1, 1)}


@pytest.mark.asyncio
async def test_worker_finalize_waits_for_review_lock_without_deadlock(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    class PausingProvider(MockEvaluationProvider):
        async def evaluate(self, request):
            provider_started.set()
            await release_provider.wait()
            return await super().evaluate(request)

    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(PausingProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await ReviewService(postgres_session).reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(comment="retry"),
        evaluation,
    )
    execution = asyncio.create_task(evaluation.execute_job(job.id), name="worker-finalize")
    await asyncio.wait_for(provider_started.wait(), timeout=3)

    original_lock_report = ReviewService._lock_report
    review_locked = asyncio.Event()
    release_review = asyncio.Event()

    async def pause_modify_after_lock(session, report_id):
        locked = await original_lock_report(session, report_id)
        if asyncio.current_task().get_name() == "review-modify":
            await _set_short_lock_timeout(session)
            review_locked.set()
            await release_review.wait()
        return locked

    monkeypatch.setattr(ReviewService, "_lock_report", staticmethod(pause_modify_after_lock))
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    async with factory() as modify_session:
        modify = asyncio.create_task(
            ReviewService(modify_session).modify(
                source.id,
                teacher.id,
                ReportPatch(score=92, comment="modify"),
            ),
            name="review-modify",
        )
        await asyncio.wait_for(review_locked.wait(), timeout=3)
        release_provider.set()
        await asyncio.sleep(0.2)
        assert not execution.done()
        release_review.set()
        worker_result, modify_result = await asyncio.wait_for(
            asyncio.gather(execution, modify, return_exceptions=True),
            timeout=5,
        )

    assert isinstance(worker_result, EvaluationReport)
    assert isinstance(modify_result, ReviewConflict)
    await postgres_session.rollback()
    persisted_job = await postgres_session.get(EvaluationJob, job.id)
    persisted_source = await postgres_session.get(EvaluationReport, source.id)
    assert persisted_job is not None
    assert persisted_job.status is JobStatus.SUCCEEDED
    assert persisted_source is not None
    assert persisted_source.review_status is ReviewStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_confirm_then_reevaluate_is_a_legal_serial_order_with_exact_evidence(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    original_lock_report = ReviewService._lock_report
    barrier = asyncio.Barrier(2)
    confirm_finished = asyncio.Event()

    async def synchronized_lock(session, report_id):
        await barrier.wait()
        if asyncio.current_task().get_name() == "reevaluate":
            await confirm_finished.wait()
        return await original_lock_report(session, report_id)

    monkeypatch.setattr(ReviewService, "_lock_report", staticmethod(synchronized_lock))

    async with (
        factory() as reevaluate_session,
        factory() as confirm_session,
        factory() as evaluation_session,
    ):
        evaluation = EvaluationService(
            evaluation_session,
            EvaluationEngine(MockEvaluationProvider()),
            requested_by=teacher.id,
            dispatch=None,
        )

        async def run_confirm():
            try:
                return await ReviewService(confirm_session).confirm(
                    report.id,
                    teacher.id,
                    ConfirmRequest(comment="confirm"),
                )
            finally:
                confirm_finished.set()

        reevaluate_task = asyncio.create_task(
            ReviewService(reevaluate_session).reevaluate(
                report.id,
                teacher.id,
                ReevaluateRequest(comment="retry"),
                evaluation,
            ),
            name="reevaluate",
        )
        confirm_task = asyncio.create_task(run_confirm(), name="confirm")
        outcomes = await asyncio.gather(
            reevaluate_task,
            confirm_task,
            return_exceptions=True,
        )

    assert sum(isinstance(item, (EvaluationJob, EvaluationReport)) for item in outcomes) == 2
    await postgres_session.rollback()
    job_count = await postgres_session.scalar(
        select(func.count())
        .select_from(EvaluationJob)
        .where(EvaluationJob.source_report_id == report.id)
    )
    action_types = frozenset(
        (
            await postgres_session.scalars(
                select(ReviewAction.action).where(ReviewAction.report_id == report.id)
            )
        ).all()
    )
    persisted = await postgres_session.get(EvaluationReport, report.id)
    assert persisted is not None
    assert (job_count, action_types, persisted.review_status) == (
        1,
        frozenset({ReviewActionType.CONFIRM, ReviewActionType.REEVALUATE}),
        ReviewStatus.CONFIRMED,
    )
    retry_action = await postgres_session.scalar(
        select(ReviewAction).where(
            ReviewAction.report_id == report.id,
            ReviewAction.action == ReviewActionType.REEVALUATE,
        )
    )
    assert retry_action is not None
    assert retry_action.changes["source_review_status"] == {
        "before": "confirmed",
        "after": "confirmed",
    }
