import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.types import Grade
from app.evaluations.types import (
    JobReason,
    JobStatus,
    ReportOrigin,
    ReviewStatus,
    ValidationStatus,
)


class EvaluationJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    submission_id: uuid.UUID
    requested_by: uuid.UUID
    reason: JobReason
    status: JobStatus
    attempt_count: int
    provider: str
    model: str
    error_code: str | None
    error_message: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AssignmentEvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["initial"] = "initial"


class SubmissionEvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["initial", "provider_retry"] = "initial"


class EvaluationBatchRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    queued: int = Field(ge=0)
    skipped: int = Field(ge=0)
    job_ids: list[uuid.UUID]


class EvaluationReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    submission_id: uuid.UUID
    job_id: uuid.UUID | None
    source_report_id: uuid.UUID | None
    origin: ReportOrigin
    version: int
    schema_version: str
    completeness: dict[str, object]
    correctness: dict[str, object]
    major_issues: list[object]
    suggestions: list[object]
    score: int
    grade: Grade
    confidence: float
    limitations: list[object]
    validation_status: ValidationStatus
    review_status: ReviewStatus
    created_at: datetime
