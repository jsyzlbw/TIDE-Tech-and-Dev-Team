import json
import math
from typing import Any

MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 50_000


class UnsafeJSONError(ValueError):
    """JSON exceeded a safety bound or could not be parsed safely."""


def _reject_json_constant(value: str) -> None:
    raise UnsafeJSONError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UnsafeJSONError("duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise UnsafeJSONError("non-finite JSON number")
    return parsed


def _scan_json_structure(text: str) -> None:
    depth = 0
    nodes = 1
    in_string = False
    escaped = False

    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
            nodes += 1
        elif character in "[{":
            depth += 1
            nodes += 1
            if depth > MAX_JSON_DEPTH:
                raise UnsafeJSONError("JSON nesting limit exceeded")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise UnsafeJSONError("JSON structure is invalid")
        elif character == ",":
            nodes += 1

        if nodes > MAX_JSON_NODES:
            raise UnsafeJSONError("JSON node limit exceeded")


def load_bounded_json(data: bytes, *, max_bytes: int) -> Any:
    if len(data) > max_bytes:
        raise UnsafeJSONError("JSON byte limit exceeded")
    try:
        text = data.decode("utf-8", errors="strict")
        _scan_json_structure(text)
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_float,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, UnsafeJSONError, ValueError):
        raise UnsafeJSONError("JSON document is invalid") from None
