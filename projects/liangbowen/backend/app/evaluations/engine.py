import time
from dataclasses import dataclass, field
from typing import NoReturn

from pydantic import ValidationError

from app.evaluations.providers.base import (
    MAX_PROVIDER_DURATION_MS,
    MAX_PROVIDER_RESULT_BYTES,
    EvaluationProvider,
    EvaluationRequest,
    ProviderErrorCode,
    ProviderIdentity,
    ProviderResult,
    ProviderUnavailable,
    RepairErrorCode,
    elapsed_duration_ms,
    validate_provider_identifier,
)
from app.evaluations.providers.json_safety import UnsafeJSONError, load_bounded_json
from app.evaluations.schemas import EvaluationOutput
from app.evaluations.types import ValidationStatus
from app.evaluations.validation import normalize_evaluation

ValidationFailureCode = RepairErrorCode


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    attempt: int
    code: ValidationFailureCode

    def __post_init__(self) -> None:
        if type(self.attempt) is not int or not 1 <= self.attempt <= 3:
            raise ValueError("attempt is outside the allowed range")
        if type(self.code) is not ValidationFailureCode:
            raise TypeError("code must be a ValidationFailureCode")


@dataclass(frozen=True, slots=True)
class EngineResult:
    output: EvaluationOutput
    raw_text: str
    attempt_count: int
    validation_status: ValidationStatus
    failures: tuple[ValidationFailure, ...]
    provider: str
    model: str
    duration_ms: int

    def __post_init__(self) -> None:
        if type(self.output) is not EvaluationOutput:
            raise TypeError("output must be an EvaluationOutput")
        normalized_output = normalize_evaluation(self.output.model_dump(mode="python"))
        if type(self.attempt_count) is not int or not 1 <= self.attempt_count <= 3:
            raise ValueError("attempt_count is outside the allowed range")
        if type(self.validation_status) is not ValidationStatus:
            raise TypeError("validation_status must be a ValidationStatus")
        if type(self.failures) is not tuple:
            raise TypeError("failures must be a tuple")
        normalized_failures = tuple(
            ValidationFailure(attempt=failure.attempt, code=failure.code)
            for failure in self.failures
            if type(failure) is ValidationFailure
        )
        if len(normalized_failures) != len(self.failures):
            raise TypeError("failures must contain ValidationFailure values")
        if len(normalized_failures) != self.attempt_count - 1:
            raise ValueError("failures do not match attempt_count")
        expected_status = (
            ValidationStatus.VALID if self.attempt_count == 1 else ValidationStatus.REPAIRED
        )
        if self.validation_status is not expected_status:
            raise ValueError("validation_status does not match attempt_count")
        validated_result = ProviderResult(
            provider=self.provider,
            model=self.model,
            raw_text=self.raw_text,
            duration_ms=self.duration_ms,
        )
        object.__setattr__(self, "output", normalized_output)
        object.__setattr__(self, "failures", normalized_failures)
        object.__setattr__(self, "provider", validated_result.provider)
        object.__setattr__(self, "model", validated_result.model)
        object.__setattr__(self, "raw_text", validated_result.raw_text)
        object.__setattr__(self, "duration_ms", validated_result.duration_ms)


@dataclass(slots=True)
class _ExecutionTracker:
    attempt_count: int = 0
    failures: tuple[ValidationFailure, ...] = ()
    duration_ms: int = 0
    last_raw_text: str | None = None


@dataclass(frozen=True, slots=True)
class EngineFailureEvidence:
    """Non-exception worker evidence; raw output is hidden and deliberately unpicklable."""

    error_type: str
    code: ProviderErrorCode | None
    attempt_count: int
    retry_count: int
    failures: tuple[ValidationFailure, ...]
    duration_ms: int
    last_raw_text: str | None = field(repr=False)
    provider: str
    model: str

    def __post_init__(self) -> None:
        validate_provider_identifier(self.provider, field="provider", maximum=128)
        validate_provider_identifier(self.model, field="model", maximum=256)
        if type(self.attempt_count) is not int or type(self.retry_count) is not int:
            raise TypeError("failure attempt counters must be integers")
        if (
            type(self.duration_ms) is not int
            or not 0 <= self.duration_ms <= MAX_PROVIDER_DURATION_MS
        ):
            raise ValueError("failure duration is outside the allowed range")
        if type(self.failures) is not tuple or any(
            type(failure) is not ValidationFailure for failure in self.failures
        ):
            raise TypeError("failure evidence must contain a tuple of validation failures")
        if tuple(failure.attempt for failure in self.failures) != tuple(
            range(1, len(self.failures) + 1)
        ):
            raise ValueError("validation failure attempts are inconsistent")

        if self.error_type == "validation_exhausted":
            valid_terminal = (
                self.code is None
                and 1 <= self.attempt_count <= 3
                and self.retry_count == self.attempt_count - 1
                and len(self.failures) == self.attempt_count
            )
        elif type(self.code) is ProviderErrorCode and self.error_type == self.code.value:
            if self.code is ProviderErrorCode.CONFIGURATION:
                valid_terminal = self.attempt_count == self.retry_count == 0 and not self.failures
            else:
                valid_terminal = (
                    1 <= self.attempt_count <= 3
                    and self.retry_count == self.attempt_count - 1
                    and len(self.failures) == self.retry_count
                )
        else:
            valid_terminal = False
        if not valid_terminal:
            raise ValueError("terminal failure evidence is inconsistent")
        if self.last_raw_text is not None:
            ProviderResult(
                provider=self.provider,
                model=self.model,
                raw_text=self.last_raw_text,
                duration_ms=self.duration_ms,
            )

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del protocol
        raise TypeError("engine failure evidence cannot be pickled")


def _failure_evidence(
    *,
    error_type: str,
    code: ProviderErrorCode | None,
    tracker: _ExecutionTracker,
    identity: ProviderIdentity,
) -> EngineFailureEvidence:
    return EngineFailureEvidence(
        error_type=error_type,
        code=code,
        attempt_count=tracker.attempt_count,
        retry_count=max(tracker.attempt_count - 1, 0),
        failures=tracker.failures,
        duration_ms=tracker.duration_ms,
        last_raw_text=tracker.last_raw_text,
        provider=identity.provider,
        model=identity.model,
    )


class EvaluationExhausted(RuntimeError):
    """All bounded validation attempts failed; contains sanitized metadata only."""

    __slots__ = ("_failures", "_locked")
    _PROTECTED_ATTRIBUTES = frozenset(
        {"_failures", "_locked", "_PROTECTED_ATTRIBUTES", "args", "failures", "attempt_count"}
    )

    def __init__(self, failures: tuple[ValidationFailure, ...]) -> None:
        object.__setattr__(self, "_locked", False)
        object.__setattr__(self, "_failures", _normalize_exhausted_failures(failures))
        super().__init__("evaluation output validation exhausted")
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False) and name in EvaluationExhausted._PROTECTED_ATTRIBUTES:
            raise AttributeError(f"{name} is read-only")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_locked", False) and name in EvaluationExhausted._PROTECTED_ATTRIBUTES:
            raise AttributeError(f"{name} is read-only")
        super().__delattr__(name)

    @property
    def failures(self) -> tuple[ValidationFailure, ...]:
        return self._failures

    @property
    def attempt_count(self) -> int:
        return len(self._failures)

    def __reduce__(self) -> tuple[type["EvaluationExhausted"], tuple[object, ...]]:
        return type(self), (self._failures,)


def _normalize_exhausted_failures(
    failures: tuple[ValidationFailure, ...],
) -> tuple[ValidationFailure, ...]:
    if type(failures) is not tuple:
        raise TypeError("exhausted failures must be a tuple")
    if any(type(failure) is not ValidationFailure for failure in failures):
        raise TypeError("exhausted failures must contain ValidationFailure values")
    try:
        normalized = tuple(
            ValidationFailure(attempt=failure.attempt, code=failure.code) for failure in failures
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("exhausted failures are invalid") from None
    if not 1 <= len(normalized) <= 3:
        raise ValueError("exhausted evaluation must contain one to three failures")
    if tuple(failure.attempt for failure in normalized) != tuple(range(1, len(normalized) + 1)):
        raise ValueError("exhausted failure attempts are invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class _ExhaustedOutcome:
    failures: tuple[ValidationFailure, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", _normalize_exhausted_failures(self.failures))


def _raise_exhausted(failures: tuple[ValidationFailure, ...]) -> NoReturn:
    raise EvaluationExhausted(failures) from None


def _schema_failure_code(error: ValidationError) -> ValidationFailureCode:
    semantic_error_types = {"value_error", "assertion_error"}
    if any(item["type"] in semantic_error_types for item in error.errors(include_input=False)):
        return ValidationFailureCode.SEMANTIC
    return ValidationFailureCode.SCHEMA


def _validated_provider_result(value: object) -> ProviderResult:
    if type(value) is not ProviderResult:
        raise ProviderUnavailable(
            "evaluation provider returned an invalid response",
            code=ProviderErrorCode.PROTOCOL,
        )
    try:
        return ProviderResult(
            provider=value.provider,
            model=value.model,
            raw_text=value.raw_text,
            duration_ms=value.duration_ms,
        )
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise ProviderUnavailable(
            "evaluation provider returned an invalid response",
            code=ProviderErrorCode.PROTOCOL,
        ) from None


def _canonical_request(value: object) -> EvaluationRequest:
    if type(value) is not EvaluationRequest:
        raise TypeError("request must be an EvaluationRequest")
    try:
        return value.without_repair()
    except (AttributeError, TypeError, ValueError, RecursionError):
        raise ProviderUnavailable(
            "evaluation request is invalid",
            code=ProviderErrorCode.CONFIGURATION,
        ) from None


class EvaluationEngine:
    def __init__(self, provider: EvaluationProvider, *, max_repairs: int = 2) -> None:
        if type(max_repairs) is not int or not 0 <= max_repairs <= 2:
            raise ValueError("max_repairs must be an integer from 0 to 2")
        evaluate = getattr(provider, "evaluate", None)
        if not callable(evaluate):
            raise TypeError("provider must implement evaluate")
        self._provider = provider
        self._max_repairs = max_repairs

    @property
    def provider_identity(self) -> ProviderIdentity:
        identity = getattr(self._provider, "identity", None)
        if type(identity) is not ProviderIdentity:
            raise ValueError("provider identity is unavailable")
        return ProviderIdentity(provider=identity.provider, model=identity.model)

    async def evaluate(self, request: EvaluationRequest) -> EngineResult:
        outcome = await _evaluate_outcome(self._provider, self._max_repairs, request)
        if type(outcome) is _ExhaustedOutcome:
            failures = outcome.failures
            del request, self, outcome
            _raise_exhausted(failures)
        return outcome

    async def execute_with_evidence(
        self,
        request: EvaluationRequest,
    ) -> EngineResult | EngineFailureEvidence:
        """Run the unchanged engine semantics while sealing worker-safe failure evidence."""
        tracker = _ExecutionTracker()
        identity = self.provider_identity
        try:
            outcome = await _evaluate_outcome(
                self._provider,
                self._max_repairs,
                request,
                tracker=tracker,
            )
        except ProviderUnavailable as error:
            if error.code is ProviderErrorCode.CONFIGURATION:
                if tracker.failures:
                    error_type = ProviderErrorCode.PROTOCOL.value
                    code = ProviderErrorCode.PROTOCOL
                else:
                    tracker.attempt_count = 0
                    tracker.duration_ms = 0
                    tracker.last_raw_text = None
                    error_type = error.code.value
                    code = error.code
            else:
                error_type = error.code.value
                code = error.code
            return _failure_evidence(
                error_type=error_type,
                code=code,
                tracker=tracker,
                identity=identity,
            )
        if type(outcome) is _ExhaustedOutcome:
            tracker.failures = outcome.failures
            return _failure_evidence(
                error_type="validation_exhausted",
                code=None,
                tracker=tracker,
                identity=identity,
            )
        return outcome


async def _evaluate_outcome(
    provider: EvaluationProvider,
    max_repairs: int,
    request: EvaluationRequest,
    *,
    tracker: _ExecutionTracker | None = None,
) -> EngineResult | _ExhaustedOutcome:
    original_request = _canonical_request(request)
    failures: list[ValidationFailure] = []
    total_duration = 0
    expected_identity: tuple[str, str] | None = None
    current_request = original_request

    for attempt in range(1, max_repairs + 2):
        # Rebuild at every provider boundary so no mutable or constructed input can cross it.
        try:
            boundary_request = current_request.revalidated()
        except (AttributeError, TypeError, ValueError, RecursionError):
            raise ProviderUnavailable(
                "evaluation request is invalid",
                code=ProviderErrorCode.CONFIGURATION,
            ) from None
        if tracker is not None:
            tracker.attempt_count = attempt
            tracker.failures = tuple(failures)
        provider_started_ns = time.monotonic_ns()
        try:
            provider_result = _validated_provider_result(await provider.evaluate(boundary_request))
        except ProviderUnavailable:
            if tracker is not None:
                tracker.duration_ms = min(
                    MAX_PROVIDER_DURATION_MS,
                    tracker.duration_ms + elapsed_duration_ms(provider_started_ns),
                )
            raise
        identity = (provider_result.provider, provider_result.model)
        if expected_identity is None:
            expected_identity = identity
        elif identity != expected_identity:
            raise ProviderUnavailable(
                "evaluation provider returned inconsistent metadata",
                code=ProviderErrorCode.PROTOCOL,
            )
        total_duration = min(
            MAX_PROVIDER_DURATION_MS,
            total_duration + provider_result.duration_ms,
        )
        if tracker is not None:
            tracker.duration_ms = total_duration
            tracker.last_raw_text = provider_result.raw_text

        try:
            payload = load_bounded_json(
                provider_result.raw_text.encode("utf-8"),
                max_bytes=MAX_PROVIDER_RESULT_BYTES,
            )
        except UnsafeJSONError:
            code = ValidationFailureCode.INVALID_JSON
        else:
            if type(payload) is not dict:
                code = ValidationFailureCode.INVALID_SHAPE
            else:
                try:
                    output = normalize_evaluation(payload)
                except ValidationError as error:
                    code = _schema_failure_code(error)
                else:
                    return EngineResult(
                        output=output,
                        raw_text=provider_result.raw_text,
                        attempt_count=attempt,
                        validation_status=(
                            ValidationStatus.VALID if attempt == 1 else ValidationStatus.REPAIRED
                        ),
                        failures=tuple(failures),
                        provider=provider_result.provider,
                        model=provider_result.model,
                        duration_ms=total_duration,
                    )

        failures.append(ValidationFailure(attempt=attempt, code=code))
        if tracker is not None:
            tracker.failures = tuple(failures)
        if attempt <= max_repairs:
            current_request = original_request.with_repair(error=code, attempt=attempt)

    return _ExhaustedOutcome(tuple(failures))
