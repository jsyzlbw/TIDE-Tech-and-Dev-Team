import json
import math
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.db.types import (
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)

MAX_CONTENT_JSON_BYTES = 100 * 1024
MAX_CONTENT_JSON_DEPTH = 32
MAX_CONTENT_JSON_NODES = 10_000


def _validate_content_json(value: JsonValue) -> None:
    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    node_count = 0
    while stack:
        node, depth = stack.pop()
        node_count += 1
        if node_count > MAX_CONTENT_JSON_NODES:
            raise ValueError("content_json exceeds the node limit")
        if depth > MAX_CONTENT_JSON_DEPTH:
            raise ValueError("content_json exceeds the depth limit")
        if isinstance(node, float) and not math.isfinite(node):
            raise ValueError("content_json numbers must be finite")
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_CONTENT_JSON_BYTES:
        raise ValueError("content_json exceeds the byte limit")


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: SubmissionContentType
    content_text: Annotated[str, Field(strict=True, max_length=50_000)]
    content_json: JsonValue | None = None

    @model_validator(mode="after")
    def validate_content_semantics(self) -> "SubmissionCreate":
        if self.content_type is SubmissionContentType.STRUCTURED:
            if not isinstance(self.content_json, (dict, list)):
                raise ValueError("structured submissions require object or list content_json")
        else:
            if self.content_json is not None:
                raise ValueError("content_json is only allowed for structured submissions")
            if not self.content_text.strip():
                raise ValueError("non-structured submissions require a non-empty answer")

        if self.content_json is not None:
            _validate_content_json(self.content_json)
        return self


class SubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
