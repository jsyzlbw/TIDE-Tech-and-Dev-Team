import copy
import math
import unicodedata
from collections.abc import Callable
from typing import Annotated, Any, Literal, Self, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from app.db.types import Grade
from app.evaluations.types import CompletenessLevel, CorrectnessJudgment, IssuePriority

MAX_POINT_LENGTH = 1_000
MAX_POINTS = 30
MAX_RATIONALE_LENGTH = 4_000
MAX_ISSUES = 20
MAX_ISSUE_TITLE_LENGTH = 200
MAX_EVIDENCE_LENGTH = 4_000
MAX_IMPACT_LENGTH = 2_000
MAX_SUGGESTIONS = 20
MAX_ACTION_LENGTH = 2_000
MAX_EXAMPLE_LENGTH = 4_000
MAX_LIMITATIONS = 20
MAX_LIMITATIONS_LENGTH = 4_000
_ALLOWED_FORMAT_CONTROLS = frozenset({"\u200c", "\u200d"})
_ALLOWED_TEXT_CONTROLS = frozenset({"\n", "\t"})
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

T = TypeVar("T")


class FrozenTuple(tuple[T, ...]):
    """Internal immutable representation for JSON array fields."""

    __slots__ = ()


def _accept_json_list_or_frozen_tuple(value: Any) -> list[Any]:
    if type(value) is FrozenTuple:
        return list(value)
    if type(value) is not list:
        # Pydantic validators require ValueError to produce a ValidationError.
        raise ValueError("list input required")
    return value


def _freeze_tuple[T](values: list[T]) -> FrozenTuple[T]:
    return FrozenTuple(values)


def _normalize_safe_string(value: Any) -> str:
    if not isinstance(value, str):
        # Pydantic validators require ValueError to produce a ValidationError.
        raise ValueError("string input required")  # noqa: TRY004
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    for character in normalized:
        if character in _ALLOWED_TEXT_CONTROLS or character in _ALLOWED_FORMAT_CONTROLS:
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cn", "Co", "Cs"}:
            raise ValueError("text contains an unsafe Unicode character")
    return normalized


def _has_visible_character(value: str) -> bool:
    return any(
        not character.isspace() and not unicodedata.category(character).startswith(("C", "M", "Z"))
        for character in value
    )


def _require_visible_text(value: str) -> str:
    if not _has_visible_character(value):
        raise ValueError("text must contain a visible character")
    return value


def _allow_empty_or_require_visible_text(value: str) -> str:
    if value and not _has_visible_character(value):
        raise ValueError("text must contain a visible character")
    return value


def _require_finite_number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # Pydantic validators require ValueError to produce a ValidationError.
        raise ValueError("confidence must be a number")  # noqa: TRY004
    if not math.isfinite(value):
        raise ValueError("confidence must be finite")
    return value


def _normalized_text_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _is_default_ignorable(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _DEFAULT_IGNORABLE_RANGES)


def _normalized_point_key(value: str) -> str:
    value = "".join(character for character in value if not _is_default_ignorable(character))
    return _normalized_text_key(value)


def _deduplicate_text(values: list[str], key_for: Callable[[str], str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = key_for(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


PointText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_POINT_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_require_visible_text),
]
RationaleText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_RATIONALE_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_require_visible_text),
]
IssueTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ISSUE_TITLE_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_require_visible_text),
]
EvidenceText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_EVIDENCE_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_require_visible_text),
]
ImpactText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_IMPACT_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_require_visible_text),
]
ActionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ACTION_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_require_visible_text),
]
ExampleText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=MAX_EXAMPLE_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_allow_empty_or_require_visible_text),
]
LimitationsText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LIMITATIONS_LENGTH),
    BeforeValidator(_normalize_safe_string),
    AfterValidator(_require_visible_text),
]
IssueCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9_]{0,63}$"),
    BeforeValidator(_normalize_safe_string),
]
SchemaVersion = Annotated[Literal["1.0"], BeforeValidator(_normalize_safe_string)]
StrictCompletenessLevel = Annotated[CompletenessLevel, BeforeValidator(_normalize_safe_string)]
StrictCorrectnessJudgment = Annotated[CorrectnessJudgment, BeforeValidator(_normalize_safe_string)]
StrictIssuePriority = Annotated[IssuePriority, BeforeValidator(_normalize_safe_string)]
StrictGrade = Annotated[Grade, BeforeValidator(_normalize_safe_string)]
SerializedPointList = Annotated[list[PointText], Field(max_length=MAX_POINTS)]


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    def model_copy(
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        data = self.model_dump(round_trip=True)
        data.update(update)
        return type(self).model_validate(data)


class AnswerCompleteness(EvaluationModel):
    level: StrictCompletenessLevel
    covered_points: Annotated[
        list[PointText],
        Field(strict=True, max_length=MAX_POINTS),
        BeforeValidator(_accept_json_list_or_frozen_tuple),
    ]
    missing_points: Annotated[
        list[PointText],
        Field(strict=True, max_length=MAX_POINTS),
        BeforeValidator(_accept_json_list_or_frozen_tuple),
    ]
    rationale: RationaleText

    @field_validator("covered_points", "missing_points")
    @classmethod
    def deduplicate_points(cls, values: list[str]) -> FrozenTuple[str]:
        return _freeze_tuple(_deduplicate_text(values, _normalized_point_key))

    @field_serializer("covered_points", "missing_points", return_type=SerializedPointList)
    def serialize_points(self, values: FrozenTuple[str]) -> list[str]:
        return list(values)

    @model_validator(mode="after")
    def require_consistent_completeness(self) -> Self:
        covered_keys = {_normalized_point_key(point) for point in self.covered_points}
        missing_keys = {_normalized_point_key(point) for point in self.missing_points}
        if covered_keys & missing_keys:
            raise ValueError("covered_points and missing_points must not overlap")
        if self.level is CompletenessLevel.COMPLETE and self.missing_points:
            raise ValueError("complete answers cannot contain missing_points")
        if self.level is not CompletenessLevel.COMPLETE and not self.missing_points:
            raise ValueError("partial and incomplete answers require missing_points")
        return self


class Correctness(EvaluationModel):
    judgment: StrictCorrectnessJudgment
    rationale: RationaleText


class MajorIssue(EvaluationModel):
    code: IssueCode
    title: IssueTitle
    evidence: EvidenceText
    impact: ImpactText


class Suggestion(EvaluationModel):
    priority: StrictIssuePriority
    action: ActionText
    example: ExampleText = ""


SerializedMajorIssueList = Annotated[list[MajorIssue], Field(max_length=MAX_ISSUES)]
SerializedSuggestionList = Annotated[list[Suggestion], Field(max_length=MAX_SUGGESTIONS)]
SerializedLimitationsList = Annotated[list[LimitationsText], Field(max_length=MAX_LIMITATIONS)]


class EvaluationScore(EvaluationModel):
    value: Annotated[StrictInt, Field(ge=0, le=100)]
    grade: StrictGrade
    confidence: Annotated[
        float,
        Field(ge=0, le=1, allow_inf_nan=False),
        BeforeValidator(_require_finite_number),
    ]


Score = EvaluationScore


class EvaluationOutput(EvaluationModel):
    schema_version: SchemaVersion
    answer_completeness: AnswerCompleteness
    correctness: Correctness
    major_issues: Annotated[
        list[MajorIssue],
        Field(strict=True, max_length=MAX_ISSUES),
        BeforeValidator(_accept_json_list_or_frozen_tuple),
    ]
    suggestions: Annotated[
        list[Suggestion],
        Field(strict=True, max_length=MAX_SUGGESTIONS),
        BeforeValidator(_accept_json_list_or_frozen_tuple),
    ]
    score: EvaluationScore
    limitations: Annotated[
        list[LimitationsText],
        Field(strict=True, max_length=MAX_LIMITATIONS),
        BeforeValidator(_accept_json_list_or_frozen_tuple),
    ]
    requires_human_review: StrictBool

    @field_validator("major_issues")
    @classmethod
    def require_unique_issue_codes(cls, issues: list[MajorIssue]) -> FrozenTuple[MajorIssue]:
        codes = [issue.code for issue in issues]
        if len(codes) != len(set(codes)):
            raise ValueError("major issue codes must be unique")
        return _freeze_tuple(issues)

    @field_serializer("major_issues", return_type=SerializedMajorIssueList)
    def serialize_major_issues(self, values: FrozenTuple[MajorIssue]) -> list[MajorIssue]:
        return list(values)

    @field_serializer("suggestions", return_type=SerializedSuggestionList)
    def serialize_suggestions(self, values: FrozenTuple[Suggestion]) -> list[Suggestion]:
        return list(values)

    @field_serializer("limitations", return_type=SerializedLimitationsList)
    def serialize_limitations(self, values: FrozenTuple[str]) -> list[str]:
        return list(values)

    @field_validator("suggestions")
    @classmethod
    def freeze_suggestions(cls, suggestions: list[Suggestion]) -> FrozenTuple[Suggestion]:
        return _freeze_tuple(suggestions)

    @field_validator("limitations")
    @classmethod
    def deduplicate_limitations(cls, limitations: list[str]) -> FrozenTuple[str]:
        return _freeze_tuple(_deduplicate_text(limitations, _normalized_text_key))


_PROVIDER_UNSUPPORTED_KEYWORDS = {
    "default",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
    "pattern",
    "uniqueItems",
}


def _make_provider_schema_strict(node: object) -> None:
    if isinstance(node, list):
        for item in node:
            _make_provider_schema_strict(item)
        return
    if not isinstance(node, dict):
        return

    if "const" in node:
        node["enum"] = [node.pop("const")]
    for keyword in _PROVIDER_UNSUPPORTED_KEYWORDS:
        node.pop(keyword, None)

    for value in node.values():
        _make_provider_schema_strict(value)

    if node.get("type") == "object":
        properties = node.get("properties", {})
        node["required"] = list(properties)
        node["additionalProperties"] = False


def evaluation_output_provider_schema() -> dict[str, Any]:
    """Return an isolated OpenAI-compatible strict schema for provider requests."""
    schema = copy.deepcopy(EvaluationOutput.model_json_schema())
    _make_provider_schema_strict(schema)
    return schema
