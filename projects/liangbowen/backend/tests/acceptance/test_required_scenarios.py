import json
from importlib.resources import files

import pytest
from sqlalchemy import select

from app.audit.model import AuditLog
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob
from app.evaluations.providers.base import (
    EvaluationRequest,
    ProviderIdentity,
    ProviderResult,
)
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationJobFailed, EvaluationService
from app.evaluations.types import JobReason, JobStatus, ReviewStatus, ValidationStatus

from .cases import REQUIRED_CASES, RequiredCase
from .conftest import SubjectFactory


def _fixture_payload(key: str) -> dict[str, object]:
    path = files("fixtures.agent_outputs").joinpath(f"{key}.json")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "case",
    REQUIRED_CASES,
    ids=(
        "normal-complete-answer",
        "partially-correct-answer",
        "obviously-incorrect-answer",
        "incomplete-ambiguous-answer",
    ),
)
@pytest.mark.asyncio
async def test_required_deterministic_answer_scenarios_are_proposed_for_teacher_review(
    postgres_session,
    acceptance_subject_factory: SubjectFactory,
    case: RequiredCase,
) -> None:
    subject = await acceptance_subject_factory(case.answer)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=subject.teacher.id,
        dispatch=None,
        mock_fixture_key=case.fixture_key,
    )

    report = await service.evaluate_now(subject.submission.id, JobReason.INITIAL)
    await postgres_session.rollback()
    job = await postgres_session.scalar(
        select(EvaluationJob).where(EvaluationJob.submission_id == subject.submission.id)
    )
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == report.job_id))

    assert job is not None and audit is not None
    assert report.submission_id == subject.submission.id
    assert report.score == case.expected_score
    assert report.grade.value == case.expected_grade
    assert report.completeness["level"] == case.expected_completeness
    assert report.correctness["judgment"] == case.expected_correctness
    assert float(report.confidence) <= case.maximum_confidence
    assert report.limitations
    assert report.review_status is ReviewStatus.PROPOSED
    assert job.status is JobStatus.SUCCEEDED
    assert job.attempt_count == 1
    assert audit.final_report_id == report.id
    assert audit.validation_status is ValidationStatus.VALID
    assert audit.retry_count == 0
    assert audit.validation_failures == []


@pytest.mark.asyncio
async def test_required_agent_unstable_output_is_repaired_on_the_third_attempt(
    postgres_session,
    acceptance_subject_factory: SubjectFactory,
) -> None:
    subject = await acceptance_subject_factory(REQUIRED_CASES[0].answer)
    semantic_invalid = _fixture_payload("complete")
    semantic_invalid["answer_completeness"]["level"] = "complete"
    semantic_invalid["answer_completeness"]["missing_points"] = ["非负权限制"]
    valid = _fixture_payload("complete")

    class RepairingProvider:
        identity = ProviderIdentity(provider="scripted", model="repair-v1")

        def __init__(self) -> None:
            self.requests: list[EvaluationRequest] = []

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            self.requests.append(request)
            responses = ("not-json", json.dumps(semantic_invalid), json.dumps(valid))
            return ProviderResult(
                provider="scripted",
                model="repair-v1",
                raw_text=responses[len(self.requests) - 1],
                duration_ms=3,
            )

    provider = RepairingProvider()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(provider),
        requested_by=subject.teacher.id,
        dispatch=None,
    )

    report = await service.evaluate_now(subject.submission.id, JobReason.INITIAL)
    await postgres_session.rollback()
    job = await postgres_session.scalar(
        select(EvaluationJob).where(EvaluationJob.id == report.job_id)
    )
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == report.job_id))

    assert job is not None and audit is not None
    assert [
        request.repair_error.value if request.repair_error else None
        for request in provider.requests
    ] == [
        None,
        "invalid_json",
        "semantic",
    ]
    assert report.validation_status is ValidationStatus.REPAIRED
    assert report.review_status is ReviewStatus.PROPOSED
    assert job.status is JobStatus.SUCCEEDED
    assert job.attempt_count == 3
    assert audit.retry_count == 2
    assert audit.validation_failures == ["invalid_json", "semantic"]


@pytest.mark.asyncio
async def test_required_agent_always_invalid_output_fails_safely_after_three_attempts(
    postgres_session,
    acceptance_subject_factory: SubjectFactory,
) -> None:
    subject = await acceptance_subject_factory(REQUIRED_CASES[1].answer)

    class InvalidProvider:
        identity = ProviderIdentity(provider="scripted", model="invalid-v1")

        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            self.calls += 1
            return ProviderResult(
                provider="scripted",
                model="invalid-v1",
                raw_text=json.dumps({"bad": "private-invalid-output"}),
                duration_ms=2,
            )

    provider = InvalidProvider()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(provider),
        requested_by=subject.teacher.id,
        dispatch=None,
    )

    with pytest.raises(EvaluationJobFailed, match="^evaluation failed$") as raised:
        await service.evaluate_now(subject.submission.id, JobReason.INITIAL)

    await postgres_session.rollback()
    job = await postgres_session.scalar(
        select(EvaluationJob).where(EvaluationJob.submission_id == subject.submission.id)
    )
    assert job is not None
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert audit is not None

    assert provider.calls == 3
    assert job.status is JobStatus.FAILED
    assert job.attempt_count == 3
    assert job.error_code == "validation_exhausted"
    assert job.error_message == "evaluation output validation exhausted"
    assert audit.retry_count == 2
    assert audit.validation_failures == ["schema", "schema", "schema"]
    assert audit.error_type == "validation_exhausted"
    assert audit.error_message == "evaluation output validation exhausted"
    assert "private-invalid-output" not in str(raised.value)
    assert "private-invalid-output" not in job.error_message
