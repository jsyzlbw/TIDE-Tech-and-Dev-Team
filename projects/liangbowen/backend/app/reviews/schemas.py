from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.db.types import (
    AssignmentStatus,
    Grade,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)
from app.evaluations.api_schemas import EvaluationJobRead, EvaluationReportRead
from app.evaluations.model import MAX_RAW_MODEL_OUTPUT_BYTES
from app.evaluations.schemas import (
    MAX_ISSUES,
    MAX_LIMITATIONS,
    MAX_SUGGESTIONS,
    AnswerCompleteness,
    Correctness,
    LimitationsText,
    MajorIssue,
    Suggestion,
)
from app.evaluations.types import JobReason, JobStatus, ReportOrigin, ReviewStatus
from app.reviews.types import ReviewActionType

MAX_REVIEW_COMMENT_CHARACTERS = 4_000
MAX_REVIEW_COMMENT_BYTES = 8_000
MAX_WORKSPACE_RUBRIC_POINTS = 20
MAX_WORKSPACE_RUBRIC_POINT_CHARACTERS = 500
MAX_WORKSPACE_GRADING_NOTES_CHARACTERS = 5_000


def _normalize_comment(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("comment must be a string")  # noqa: TRY004
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized:
        raise ValueError("comment contains a null character")
    if len(normalized.encode("utf-8")) > MAX_REVIEW_COMMENT_BYTES:
        raise ValueError("comment exceeds the UTF-8 byte limit")
    return normalized


ReviewComment = Annotated[
    str,
    BeforeValidator(_normalize_comment),
    StringConstraints(max_length=MAX_REVIEW_COMMENT_CHARACTERS),
]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    comment: ReviewComment = ""


class ConfirmRequest(ReviewRequest):
    pass


class ReevaluateRequest(ReviewRequest):
    pass


class ReportPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    completeness: AnswerCompleteness | None = None
    correctness: Correctness | None = None
    major_issues: Annotated[list[MajorIssue], Field(strict=True, max_length=MAX_ISSUES)] | None = (
        None
    )
    suggestions: (
        Annotated[list[Suggestion], Field(strict=True, max_length=MAX_SUGGESTIONS)] | None
    ) = None
    score: Annotated[StrictInt, Field(ge=0, le=100)] | None = None
    limitations: (
        Annotated[list[LimitationsText], Field(strict=True, max_length=MAX_LIMITATIONS)] | None
    ) = None
    comment: ReviewComment | None = None

    @field_validator(
        "completeness",
        "correctness",
        "major_issues",
        "suggestions",
        "score",
        "limitations",
        "comment",
    )
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("explicit null is not allowed")
        return value

    @model_validator(mode="after")
    def require_at_least_one_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one review field is required")
        return self


class ReviewReportRead(EvaluationReportRead):
    request_id: str


class ReviewJobRead(EvaluationJobRead):
    source_report_id: uuid.UUID
    request_id: str


WorkspaceRubricPoint = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_WORKSPACE_RUBRIC_POINT_CHARACTERS
    ),
]


class WorkspaceRubricRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_points: Annotated[
        list[WorkspaceRubricPoint], Field(max_length=MAX_WORKSPACE_RUBRIC_POINTS)
    ]
    grading_notes: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=MAX_WORKSPACE_GRADING_NOTES_CHARACTERS),
    ]


class WorkspaceAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    code: str
    title: str
    question: str
    notes: str
    rubric: WorkspaceRubricRead
    due_at: datetime
    status: AssignmentStatus


class WorkspaceSubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    assignment_id: uuid.UUID
    student_id: uuid.UUID
    version: int
    content_type: SubmissionContentType
    content_text: str
    content_json: JsonValue | None
    status: SubmissionStatus
    submitted_at: datetime
    source: SubmissionSource


class WorkspaceStudentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    username: str
    display_name: str


class WorkspaceReportAuthorRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent", "teacher"]
    display_name: str


class WorkspaceReviewActionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ReviewActionType
    teacher: WorkspaceStudentRead
    comment: ReviewComment
    acted_at: datetime


class TimelineReviewActionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ReviewActionType
    teacher: WorkspaceStudentRead
    acted_at: datetime


class WorkspaceReportRead(EvaluationReportRead):
    author: WorkspaceReportAuthorRead
    latest_review_action: WorkspaceReviewActionRead | None


class TimelineReportSummaryRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    version: int
    origin: ReportOrigin
    review_status: ReviewStatus
    score: int
    grade: Grade
    author: WorkspaceReportAuthorRead
    created_at: datetime
    latest_review_action: TimelineReviewActionRead | None


class WorkspaceJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    source_report_id: uuid.UUID
    reason: JobReason
    status: JobStatus
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ReportWorkspaceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_report_id: uuid.UUID
    current_report_id: uuid.UUID
    assignment: WorkspaceAssignmentRead
    submission: WorkspaceSubmissionRead
    student: WorkspaceStudentRead
    selected_report: WorkspaceReportRead
    timeline: Annotated[list[TimelineReportSummaryRead], Field(max_length=100)]
    timeline_total: Annotated[int, Field(ge=1)]
    timeline_truncated: bool
    reevaluation_job: WorkspaceJobRead | None
    request_id: str


class RawReportOutputRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: uuid.UUID
    available: bool
    raw_model_output: str | None
    request_id: str

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        if self.available != (self.raw_model_output is not None):
            raise ValueError("available must match raw_model_output presence")
        if (
            self.raw_model_output is not None
            and len(self.raw_model_output.encode("utf-8")) > MAX_RAW_MODEL_OUTPUT_BYTES
        ):
            raise ValueError("raw_model_output exceeds the byte limit")
        return self
