"""Shared assignment content contract used at authoring and evaluation boundaries."""

from __future__ import annotations

import json
import math
from types import MappingProxyType

MAX_ASSIGNMENT_TITLE_BYTES = 512
MAX_QUESTION_BYTES = 64 * 1024
MAX_RUBRIC_BYTES = 128 * 1024
MAX_RUBRIC_DEPTH = 10
MAX_RUBRIC_NODES = 10_000
MAX_RUBRIC_KEY_BYTES = 512


def validate_utf8_text(
    value: object,
    *,
    field: str,
    maximum: int,
    required: bool,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    if required and not value.strip():
        raise ValueError(f"{field} must not be empty")
    if size > maximum:
        raise ValueError(f"{field} exceeds the UTF-8 byte limit")
    return value


def freeze_rubric(
    value: object,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> object:
    if depth > MAX_RUBRIC_DEPTH:
        raise ValueError("rubric exceeds the nesting limit")
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_RUBRIC_NODES:
        raise ValueError("rubric exceeds the item limit")

    if type(value) is dict:
        frozen: dict[str, object] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError("rubric object keys must be strings")
            validate_utf8_text(
                key,
                field="rubric key",
                maximum=MAX_RUBRIC_KEY_BYTES,
                required=True,
            )
            frozen[key] = freeze_rubric(child, depth=depth + 1, counter=counter)
        return MappingProxyType(frozen)
    if type(value) is list:
        return tuple(freeze_rubric(child, depth=depth + 1, counter=counter) for child in value)
    if value is None or type(value) in {str, bool, int}:
        if type(value) is str:
            validate_utf8_text(
                value,
                field="rubric string",
                maximum=MAX_RUBRIC_BYTES,
                required=False,
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("rubric numbers must be finite")
        return value
    raise TypeError("rubric must contain only JSON-compatible values")


def thaw_json(value: object) -> object:
    if isinstance(value, MappingProxyType):
        return {key: thaw_json(child) for key, child in value.items()}
    if type(value) is tuple:
        return [thaw_json(child) for child in value]
    return value


def compact_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_assignment_content(
    *,
    title: object,
    question: object,
    rubric: object,
) -> None:
    validate_utf8_text(
        title,
        field="title",
        maximum=MAX_ASSIGNMENT_TITLE_BYTES,
        required=True,
    )
    validate_utf8_text(
        question,
        field="question",
        maximum=MAX_QUESTION_BYTES,
        required=True,
    )
    if type(rubric) is not dict:
        raise TypeError("rubric must be a JSON object")
    frozen = freeze_rubric(rubric)
    if len(compact_json_bytes(thaw_json(frozen))) > MAX_RUBRIC_BYTES:
        raise ValueError("rubric exceeds the JSON byte limit")
