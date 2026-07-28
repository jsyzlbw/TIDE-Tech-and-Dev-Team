import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.assignments.content import (
    MAX_ASSIGNMENT_TITLE_BYTES,
    MAX_QUESTION_BYTES,
    MAX_RUBRIC_BYTES,
    MAX_RUBRIC_DEPTH,
    MAX_RUBRIC_NODES,
    compact_json_bytes,
    freeze_rubric,
    thaw_json,
    validate_utf8_text,
)
from app.db.contracts.evaluation_v1 import is_safe_identifier

__all__ = ["MAX_RUBRIC_DEPTH", "MAX_RUBRIC_NODES"]

MAX_STUDENT_ANSWER_BYTES = 1024 * 1024
MAX_FIXTURE_KEY_BYTES = 128
MAX_REQUEST_BYTES = 1280 * 1024
MAX_PROVIDER_RESULT_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_DURATION_MS = 2_147_483_647
MAX_REPAIR_ATTEMPTS = 2


class RepairErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    SCHEMA = "schema"
    SEMANTIC = "semantic"


class ProviderErrorCode(StrEnum):
    CONFIGURATION = "configuration"
    TIMEOUT = "timeout"
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    UPSTREAM = "upstream"
    PROTOCOL = "protocol"
    RESPONSE_TOO_LARGE = "response_too_large"
    FIXTURE = "fixture"


class ProviderUnavailable(RuntimeError):
    """A sanitized provider failure safe to persist or display."""

    def __init__(
        self,
        message: str = "evaluation provider unavailable",
        *,
        code: ProviderErrorCode = ProviderErrorCode.UPSTREAM,
    ) -> None:
        self.code = code
        super().__init__(message)


_validate_text = validate_utf8_text


def validate_provider_identifier(value: object, *, field: str, maximum: int) -> str:
    """Validate a visible Unicode identifier that is safe for logs and persistence."""
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    if not is_safe_identifier(value, maximum):
        raise ValueError(f"{field} must be a safe visible identifier")
    return value


_freeze_json = freeze_rubric
_thaw_json = thaw_json
_json_bytes = compact_json_bytes


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    assignment_title: str
    question: str
    rubric: dict[str, object]
    student_answer: str
    fixture_key: str | None = None
    repair_error: RepairErrorCode | None = None
    repair_attempt: int | None = None

    def __post_init__(self) -> None:
        title = _validate_text(
            self.assignment_title,
            field="assignment_title",
            maximum=MAX_ASSIGNMENT_TITLE_BYTES,
            required=True,
        )
        question = _validate_text(
            self.question,
            field="question",
            maximum=MAX_QUESTION_BYTES,
            required=True,
        )
        answer = _validate_text(
            self.student_answer,
            field="student_answer",
            maximum=MAX_STUDENT_ANSWER_BYTES,
            required=True,
        )
        if type(self.rubric) is not dict:
            raise TypeError("rubric must be a JSON object")
        frozen_rubric = _freeze_json(self.rubric)
        if self.fixture_key is not None:
            fixture_key = _validate_text(
                self.fixture_key,
                field="fixture_key",
                maximum=MAX_FIXTURE_KEY_BYTES,
                required=False,
            )
        else:
            fixture_key = None
        if self.repair_error is None:
            if self.repair_attempt is not None:
                raise ValueError("repair_attempt requires repair_error")
            repair_error = None
            repair_attempt = None
        else:
            if type(self.repair_error) is not RepairErrorCode:
                raise TypeError("repair_error must be a RepairErrorCode")
            if (
                type(self.repair_attempt) is not int
                or not 1 <= self.repair_attempt <= MAX_REPAIR_ATTEMPTS
            ):
                raise ValueError("repair_attempt is outside the allowed range")
            repair_error = self.repair_error
            repair_attempt = self.repair_attempt

        rubric_size = len(_json_bytes(_thaw_json(frozen_rubric)))
        if rubric_size > MAX_RUBRIC_BYTES:
            raise ValueError("rubric exceeds the JSON byte limit")
        provider_data = {
            "assignment_title": title,
            "question": question,
            "rubric": _thaw_json(frozen_rubric),
            "student_answer": answer,
        }
        if len(_json_bytes(provider_data)) > MAX_REQUEST_BYTES:
            raise ValueError("evaluation request exceeds the JSON byte limit")

        object.__setattr__(self, "assignment_title", title)
        object.__setattr__(self, "question", question)
        object.__setattr__(self, "student_answer", answer)
        object.__setattr__(self, "rubric", frozen_rubric)
        object.__setattr__(self, "fixture_key", fixture_key)
        object.__setattr__(self, "repair_error", repair_error)
        object.__setattr__(self, "repair_attempt", repair_attempt)

    def provider_data(self) -> dict[str, Any]:
        """Return an isolated JSON-compatible copy of the model input."""
        return {
            "assignment_title": self.assignment_title,
            "question": self.question,
            "rubric": _thaw_json(self.rubric),
            "student_answer": self.student_answer,
        }

    def revalidated(self) -> "EvaluationRequest":
        """Rebuild a canonical, alias-free request at a provider trust boundary."""
        data = self.provider_data()
        return type(self)(
            assignment_title=data["assignment_title"],
            question=data["question"],
            rubric=data["rubric"],
            student_answer=data["student_answer"],
            fixture_key=self.fixture_key,
            repair_error=self.repair_error,
            repair_attempt=self.repair_attempt,
        )

    def without_repair(self) -> "EvaluationRequest":
        """Return the original untrusted input with caller-supplied repair metadata removed."""
        data = self.provider_data()
        return type(self)(
            assignment_title=data["assignment_title"],
            question=data["question"],
            rubric=data["rubric"],
            student_answer=data["student_answer"],
            fixture_key=self.fixture_key,
        )

    def with_repair(self, *, error: RepairErrorCode, attempt: int) -> "EvaluationRequest":
        """Attach fixed, trusted repair metadata without changing evaluation input data."""
        data = self.provider_data()
        return type(self)(
            assignment_title=data["assignment_title"],
            question=data["question"],
            rubric=data["rubric"],
            student_answer=data["student_answer"],
            fixture_key=self.fixture_key,
            repair_error=error,
            repair_attempt=attempt,
        )


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    model: str
    raw_text: str
    duration_ms: int

    def __post_init__(self) -> None:
        validate_provider_identifier(self.provider, field="provider", maximum=128)
        validate_provider_identifier(self.model, field="model", maximum=256)
        _validate_text(
            self.raw_text,
            field="raw_text",
            maximum=MAX_PROVIDER_RESULT_BYTES,
            required=True,
        )
        if (
            type(self.duration_ms) is not int
            or not 0 <= self.duration_ms <= MAX_PROVIDER_DURATION_MS
        ):
            raise ValueError("duration_ms must be a bounded non-negative integer")


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Stable provider identity persisted before an evaluation is dispatched."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        validate_provider_identifier(self.provider, field="provider", maximum=128)
        validate_provider_identifier(self.model, field="model", maximum=256)


def elapsed_duration_ms(started_ns: int, *, finished_ns: int | None = None) -> int:
    if finished_ns is None:
        finished_ns = time.monotonic_ns()
    elapsed = max(0, (finished_ns - started_ns) // 1_000_000)
    return min(elapsed, MAX_PROVIDER_DURATION_MS)


@runtime_checkable
class EvaluationProvider(Protocol):
    @property
    def identity(self) -> ProviderIdentity: ...

    async def evaluate(self, request: EvaluationRequest) -> ProviderResult: ...
