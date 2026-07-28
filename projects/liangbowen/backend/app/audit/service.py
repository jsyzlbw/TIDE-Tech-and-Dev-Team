from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.model import AuditLog
from app.evaluations.providers.base import (
    MAX_PROVIDER_DURATION_MS,
    RepairErrorCode,
    validate_provider_identifier,
)
from app.evaluations.types import ValidationStatus

MAX_AUDIT_RETRIES = 2
MAX_RAW_OUTPUT_BYTES = 2 * 1024 * 1024
SAFE_AUDIT_ERROR_MESSAGES = MappingProxyType(
    {
        "configuration": "evaluation provider configuration is invalid",
        "timeout": "evaluation provider timed out",
        "network": "evaluation provider network unavailable",
        "authentication": "evaluation provider authentication failed",
        "rate_limited": "evaluation provider rate limited",
        "upstream": "evaluation provider unavailable",
        "protocol": "evaluation provider returned an invalid response",
        "response_too_large": "evaluation provider response too large",
        "fixture": "evaluation fixture is invalid",
        "validation_exhausted": "evaluation output validation exhausted",
        "internal_error": "evaluation failed",
        "cancelled": "evaluation cancelled after provider execution",
    }
)
PROVIDER_TERMINAL_ERROR_TYPES = frozenset(
    {
        "timeout",
        "network",
        "authentication",
        "rate_limited",
        "upstream",
        "protocol",
        "response_too_large",
        "fixture",
        "internal_error",
        "cancelled",
    }
)


def _bounded_text(
    value: object,
    *,
    field: str,
    maximum: int,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field} must be valid UTF-8") from None
    if not value or value != value.strip() or not 1 <= byte_length <= maximum:
        raise ValueError(f"{field} is outside its safe text contract")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"}
        for character in value
    ):
        raise ValueError(f"{field} contains control characters")
    return value


def _bounded_raw_output(value: object) -> str:
    if type(value) is not str:
        raise TypeError("raw_model_output must be a string")
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("raw_model_output must be valid UTF-8") from None
    if not 1 <= byte_length <= MAX_RAW_OUTPUT_BYTES or "\x00" in value:
        raise ValueError("raw_model_output is outside its safe text contract")
    return value


@dataclass(frozen=True, slots=True)
class AuditLogCreate:
    job_id: uuid.UUID
    submission_id: uuid.UUID
    provider: str
    model: str
    prompt_template_version: str
    schema_version: str
    duration_ms: int
    retry_count: int
    raw_model_output: str | None
    validation_status: ValidationStatus | None
    validation_failures: tuple[RepairErrorCode, ...]
    error_type: str | None
    error_message: str | None
    final_report_id: uuid.UUID | None

    def __post_init__(self) -> None:
        if type(self.job_id) is not uuid.UUID or type(self.submission_id) is not uuid.UUID:
            raise TypeError("audit identifiers must be UUID values")
        if self.final_report_id is not None and type(self.final_report_id) is not uuid.UUID:
            raise TypeError("final_report_id must be a UUID")
        validate_provider_identifier(self.provider, field="provider", maximum=128)
        validate_provider_identifier(self.model, field="model", maximum=256)
        prompt_version = _bounded_text(
            self.prompt_template_version,
            field="prompt_template_version",
            maximum=64,
            required=True,
        )
        if (
            prompt_version is None
            or not all(
                character.isascii() and (character.isalnum() or character in "_.-")
                for character in prompt_version
            )
            or not prompt_version[0].isalnum()
        ):
            raise ValueError("prompt_template_version is invalid")
        if self.schema_version != "1.0":
            raise ValueError("schema_version must be 1.0")
        if (
            type(self.duration_ms) is not int
            or not 0 <= self.duration_ms <= MAX_PROVIDER_DURATION_MS
        ):
            raise ValueError("duration_ms is outside the allowed range")
        if type(self.retry_count) is not int or not 0 <= self.retry_count <= MAX_AUDIT_RETRIES:
            raise ValueError("retry_count is outside the allowed range")
        if type(self.validation_failures) is not tuple or len(self.validation_failures) > 3:
            raise TypeError("validation_failures must be a bounded tuple")
        if any(type(item) is not RepairErrorCode for item in self.validation_failures):
            raise TypeError("validation_failures contains an invalid code")
        if self.raw_model_output is not None:
            _bounded_raw_output(self.raw_model_output)
        if self.error_type == "cancelled" and self.raw_model_output is None:
            raise ValueError("raw_model_output is required for cancelled provider evidence")
        if self.final_report_id is not None:
            if (
                self.raw_model_output is None
                or type(self.validation_status) is not ValidationStatus
            ):
                raise ValueError("successful audit records require output and validation status")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("successful audit records cannot contain error fields")
            expected_failures = (
                0 if self.validation_status is ValidationStatus.VALID else self.retry_count
            )
            if (
                len(self.validation_failures) != expected_failures
                or (self.validation_status is ValidationStatus.VALID and self.retry_count != 0)
                or (self.validation_status is ValidationStatus.REPAIRED and self.retry_count == 0)
            ):
                raise ValueError("retry_count does not match successful validation evidence")
        else:
            if self.validation_status is not None:
                raise ValueError("failed audit records cannot contain validation status")
            if type(self.error_type) is not str or self.error_type not in SAFE_AUDIT_ERROR_MESSAGES:
                raise ValueError("error_type is not an allowed safe category")
            if self.error_message != SAFE_AUDIT_ERROR_MESSAGES[self.error_type]:
                raise ValueError("error_message must match the fixed safe category")
            failure_count = len(self.validation_failures)
            valid_failure_evidence = (
                (
                    self.error_type == "configuration"
                    and self.retry_count == 0
                    and failure_count == 0
                )
                or (
                    self.error_type == "validation_exhausted"
                    and failure_count == self.retry_count + 1
                )
                or (
                    self.error_type in PROVIDER_TERMINAL_ERROR_TYPES
                    and failure_count == self.retry_count
                )
            )
            if not valid_failure_evidence:
                raise ValueError("error_type does not match terminal failure evidence")


async def append_audit_log(session: AsyncSession, data: AuditLogCreate) -> AuditLog:
    if type(data) is not AuditLogCreate:
        raise TypeError("data must be an AuditLogCreate")
    data = AuditLogCreate(
        job_id=data.job_id,
        submission_id=data.submission_id,
        provider=data.provider,
        model=data.model,
        prompt_template_version=data.prompt_template_version,
        schema_version=data.schema_version,
        duration_ms=data.duration_ms,
        retry_count=data.retry_count,
        raw_model_output=data.raw_model_output,
        validation_status=data.validation_status,
        validation_failures=data.validation_failures,
        error_type=data.error_type,
        error_message=data.error_message,
        final_report_id=data.final_report_id,
    )
    record = AuditLog(
        job_id=data.job_id,
        submission_id=data.submission_id,
        provider=data.provider,
        model=data.model,
        prompt_template_version=data.prompt_template_version,
        schema_version=data.schema_version,
        duration_ms=data.duration_ms,
        retry_count=data.retry_count,
        raw_model_output=data.raw_model_output,
        validation_status=data.validation_status,
        validation_failures=[failure.value for failure in data.validation_failures],
        error_type=data.error_type,
        error_message=data.error_message,
        final_report_id=data.final_report_id,
    )
    with session.no_autoflush:
        session.add(record)
        await session.flush([record])
    return record
