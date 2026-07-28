from enum import StrEnum


class CompletenessLevel(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"


class CorrectnessJudgment(StrEnum):
    CORRECT = "correct"
    MOSTLY_CORRECT = "mostly_correct"
    PARTIALLY_CORRECT = "partially_correct"
    INCORRECT = "incorrect"
    UNABLE_TO_DETERMINE = "unable_to_determine"


class IssuePriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobReason(StrEnum):
    INITIAL = "initial"
    MANUAL_RETRY = "manual_retry"
    PROVIDER_RETRY = "provider_retry"


class ReportOrigin(StrEnum):
    AGENT = "agent"
    TEACHER = "teacher"


class ReviewStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    MODIFIED = "modified"
    SUPERSEDED = "superseded"


class ValidationStatus(StrEnum):
    VALID = "valid"
    REPAIRED = "repaired"
