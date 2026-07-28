import copy
import importlib
import json
import math
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.types import Grade

Payload = dict[str, Any]


def valid_payload(*, score: object = 82, grade: object = "B") -> Payload:
    return {
        "schema_version": "1.0",
        "answer_completeness": {
            "level": "partial",
            "covered_points": ["解释了核心概念", "给出了示例"],
            "missing_points": ["没有讨论边界条件"],
            "rationale": "回答覆盖主要内容，但缺少边界分析。",
        },
        "correctness": {
            "judgment": "mostly_correct",
            "rationale": "主体推理正确，结论需要补充限定。",
        },
        "major_issues": [
            {
                "code": "MISSING_EDGE_CASE",
                "title": "未覆盖边界条件",
                "evidence": "答案只讨论了普通输入。",
                "impact": "结论无法推广到全部允许输入。",
            }
        ],
        "suggestions": [
            {
                "priority": "high",
                "action": "补充零值和最大值的讨论。",
                "example": "例如分别验证 n=0 与 n=100。",
            }
        ],
        "score": {"value": score, "grade": grade, "confidence": 0.84},
        "limitations": ["只依据提交文本判断，未执行其中的代码。"],
        "requires_human_review": True,
    }


def set_path(payload: Payload, path: tuple[str, ...], value: object) -> None:
    target: dict[str, Any] = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def test_evaluation_enum_values_are_exact() -> None:
    types = importlib.import_module("app.evaluations.types")

    assert [member.value for member in types.CompletenessLevel] == [
        "complete",
        "partial",
        "incomplete",
    ]
    assert [member.value for member in types.CorrectnessJudgment] == [
        "correct",
        "mostly_correct",
        "partially_correct",
        "incorrect",
        "unable_to_determine",
    ]
    assert [member.value for member in types.IssuePriority] == ["high", "medium", "low"]
    assert [member.value for member in types.JobStatus] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert [member.value for member in types.JobReason] == [
        "initial",
        "manual_retry",
        "provider_retry",
    ]
    assert [member.value for member in types.ReportOrigin] == ["agent", "teacher"]
    assert [member.value for member in types.ReviewStatus] == [
        "proposed",
        "confirmed",
        "modified",
        "superseded",
    ]
    assert [member.value for member in types.ValidationStatus] == ["valid", "repaired"]


def test_normalize_accepts_valid_report_and_preserves_computed_grade() -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload(score=82, grade="B"))

    assert report.score.value == 82
    assert report.score.grade is Grade.B
    assert report.requires_human_review is True


def test_normalize_overrides_a_valid_but_inconsistent_model_grade() -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload(score=94, grade="D"))

    assert report.score.grade is Grade.A


@pytest.mark.parametrize(
    ("score", "expected_grade"),
    [
        (0, Grade.D),
        (59, Grade.D),
        (60, Grade.C),
        (69, Grade.C),
        (70, Grade.C),
        (79, Grade.B),
        (80, Grade.B),
        (89, Grade.B),
        (90, Grade.A),
        (100, Grade.A),
    ],
)
def test_normalize_uses_the_shared_score_boundaries(score: int, expected_grade: Grade) -> None:
    from app.evaluations.validation import normalize_evaluation

    assert normalize_evaluation(valid_payload(score=score)).score.grade is expected_grade


@pytest.mark.parametrize("score", [-1, 101])
def test_score_rejects_values_outside_zero_to_one_hundred(score: int) -> None:
    from app.evaluations.validation import normalize_evaluation

    with pytest.raises(ValidationError):
        normalize_evaluation(valid_payload(score=score))


@pytest.mark.parametrize("score", [True, False, 82.0, "82", None])
def test_score_rejects_non_strict_integers(score: object) -> None:
    from app.evaluations.validation import normalize_evaluation

    with pytest.raises(ValidationError):
        normalize_evaluation(valid_payload(score=score))


@pytest.mark.parametrize("confidence", [math.nan, math.inf, -math.inf, True, "0.8", None])
def test_confidence_rejects_non_finite_or_non_numeric_values(confidence: object) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["score"]["confidence"] = confidence

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


@pytest.mark.parametrize("confidence", [0, 0.5, 1, 0.0, 1.0])
def test_confidence_accepts_finite_numeric_boundaries(confidence: float) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["score"]["confidence"] = confidence

    assert normalize_evaluation(payload).score.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_rejects_values_outside_zero_to_one(confidence: float) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["score"]["confidence"] = confidence

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        (("schema_version",), "1.1"),
        (("answer_completeness", "level"), "mostly_complete"),
        (("correctness", "judgment"), "probably_correct"),
        (("suggestions",), [{"priority": "urgent", "action": "修复", "example": "示例"}]),
        (("score", "grade"), "E"),
    ],
)
def test_version_and_enum_fields_reject_unknown_values(
    path: tuple[str, ...], invalid_value: object
) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    set_path(payload, path, invalid_value)

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


def test_schema_version_is_required() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    del payload["schema_version"]

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": "value"}),
        lambda payload: payload["answer_completeness"].update({"unexpected": "value"}),
        lambda payload: payload["correctness"].update({"unexpected": "value"}),
        lambda payload: payload["major_issues"][0].update({"unexpected": "value"}),
        lambda payload: payload["suggestions"][0].update({"unexpected": "value"}),
        lambda payload: payload["score"].update({"unexpected": "value"}),
    ],
)
def test_every_model_rejects_extra_fields(mutate: Callable[[Payload], None]) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("answer_completeness", "covered_points"),
        ("answer_completeness", "missing_points"),
        ("major_issues",),
        ("suggestions",),
        ("limitations",),
    ],
)
def test_list_fields_reject_tuple_coercion(path: tuple[str, ...]) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    current: object = payload
    for key in path:
        current = current[key]
    set_path(payload, path, tuple(current))

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("answer_completeness", "rationale"),
        ("correctness", "rationale"),
        ("major_issues", "title"),
        ("major_issues", "evidence"),
        ("major_issues", "impact"),
        ("suggestions", "action"),
    ],
)
@pytest.mark.parametrize(
    "invalid_text",
    ["   \n\t ", "\u200b\u200c\ufeff", "\x00\x01", "\u034f\ufe0f"],
)
def test_required_natural_language_rejects_invisible_only_text(
    path: tuple[str, ...], invalid_text: str
) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    section, field = path
    target = payload[section][0] if isinstance(payload[section], list) else payload[section]
    target[field] = invalid_text

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


def test_natural_language_is_trimmed_without_restricting_unicode_or_code_examples() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["level"] = "complete"
    payload["answer_completeness"]["covered_points"] = ["  中文要点：复杂度 O(n²)  "]
    payload["answer_completeness"]["missing_points"] = []
    payload["answer_completeness"]["rationale"] = "  推导包含 Σ 与 λ。  "
    payload["correctness"]["rationale"] = "  中文论证成立。  "
    payload["major_issues"][0]["title"] = "  边界问题  "
    payload["major_issues"][0]["evidence"] = "  `if x <= 0: return None`  "
    payload["major_issues"][0]["impact"] = "  影响中文用户。  "
    payload["suggestions"][0]["action"] = "  保留 Unicode。  "
    payload["suggestions"][0]["example"] = "  例如：值为「甲」。  "
    payload["limitations"] = ["  仅文本初评。  "]

    report = normalize_evaluation(payload)

    assert report.answer_completeness.covered_points == ("中文要点：复杂度 O(n²)",)
    assert report.answer_completeness.rationale == "推导包含 Σ 与 λ。"
    assert report.correctness.rationale == "中文论证成立。"
    assert report.major_issues[0].title == "边界问题"
    assert report.major_issues[0].evidence == "`if x <= 0: return None`"
    assert report.major_issues[0].impact == "影响中文用户。"
    assert report.suggestions[0].action == "保留 Unicode。"
    assert report.suggestions[0].example == "例如：值为「甲」。"
    assert report.limitations == ("仅文本初评。",)


def test_limitations_are_trimmed_and_deduplicated_in_stable_order() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["limitations"] = [
        "  仅文本   初评。  ",
        "仅文本 初评。",
        "未运行代码。",
    ]

    report = normalize_evaluation(payload)

    assert report.limitations == ("仅文本   初评。", "未运行代码。")


@pytest.mark.parametrize("invalid_text", ["   \n\t ", "\u200b\ufeff", "\x00", "\u034f\ufe0f"])
def test_limitations_reject_invisible_only_items(invalid_text: str) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["limitations"] = [invalid_text]

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


def test_limitations_reject_a_scalar_string() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["limitations"] = "不是列表"

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


def test_suggestion_example_may_be_omitted_or_explicitly_empty() -> None:
    from app.evaluations.validation import normalize_evaluation

    omitted = valid_payload()
    del omitted["suggestions"][0]["example"]
    explicit_empty = valid_payload()
    explicit_empty["suggestions"][0]["example"] = ""

    assert normalize_evaluation(omitted).suggestions[0].example == ""
    assert normalize_evaluation(explicit_empty).suggestions[0].example == ""


def test_suggestion_example_normalizes_whitespace_only_to_empty() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["suggestions"][0]["example"] = "  \n\t  "

    assert normalize_evaluation(payload).suggestions[0].example == ""


@pytest.mark.parametrize("invalid_text", ["\u200b\ufeff", "\x00", "\u034f\ufe0f"])
def test_nonempty_suggestion_example_rejects_invisible_only_text(invalid_text: str) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["suggestions"][0]["example"] = invalid_text

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


def test_points_are_deduplicated_after_unicode_whitespace_and_case_normalization() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["level"] = "complete"
    payload["answer_completeness"]["covered_points"] = [
        "  核心   概念  ",
        "核心 概念",
        "ＣODE",
        "code",
    ]
    payload["answer_completeness"]["missing_points"] = []

    report = normalize_evaluation(payload)

    assert report.answer_completeness.covered_points == ("核心   概念", "ＣODE")


def test_covered_and_missing_points_cannot_overlap_after_normalization() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["covered_points"] = ["步骤 Ａ"]
    payload["answer_completeness"]["missing_points"] = ["  步骤   a  "]

    with pytest.raises(ValidationError, match="covered_points.*missing_points"):
        normalize_evaluation(payload)


def test_complete_answer_cannot_claim_missing_points() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["level"] = "complete"

    with pytest.raises(ValidationError, match="complete.*missing_points"):
        normalize_evaluation(payload)


@pytest.mark.parametrize("level", ["partial", "incomplete"])
def test_non_complete_answers_must_identify_at_least_one_missing_point(level: str) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["level"] = level
    payload["answer_completeness"]["missing_points"] = []

    with pytest.raises(ValidationError, match="partial.*incomplete.*missing_points"):
        normalize_evaluation(payload)


def test_complete_answer_accepts_an_empty_missing_points_list() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["level"] = "complete"
    payload["answer_completeness"]["missing_points"] = []

    assert normalize_evaluation(payload).answer_completeness.missing_points == ()


def test_major_issue_codes_must_be_stable_and_unique() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    issue = payload["major_issues"][0]
    payload["major_issues"] = [issue, copy.deepcopy(issue)]

    with pytest.raises(ValidationError, match="major issue codes.*unique"):
        normalize_evaluation(payload)


@pytest.mark.parametrize("code", ["lowercase", "1_LEADING_DIGIT", "HAS-DASH", "A" * 65])
def test_major_issue_code_format_is_restricted(code: str) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["major_issues"][0]["code"] = code

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


def test_empty_limitations_gets_the_required_default_and_human_review_is_forced() -> None:
    from app.evaluations.validation import DEFAULT_LIMITATIONS, normalize_evaluation

    payload = valid_payload()
    payload["limitations"] = []
    payload["requires_human_review"] = False

    report = normalize_evaluation(payload)

    assert report.limitations == (DEFAULT_LIMITATIONS,)
    assert report.limitations == ("本报告是文本初评，必须由教师审核。",)
    assert report.requires_human_review is True


def test_unable_to_determine_still_requires_human_review() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["correctness"]["judgment"] = "unable_to_determine"
    payload["requires_human_review"] = False

    assert normalize_evaluation(payload).requires_human_review is True


def test_normalization_does_not_modify_the_input_dictionary() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload(score=94, grade="D")
    payload["limitations"] = []
    payload["requires_human_review"] = False
    before = copy.deepcopy(payload)

    normalize_evaluation(payload)

    assert payload == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload, value: payload.update({"schema_version": value}),
        lambda payload, value: payload["answer_completeness"].update({"level": value}),
        lambda payload, value: payload["correctness"].update({"judgment": value}),
        lambda payload, value: payload["suggestions"][0].update({"priority": value}),
        lambda payload, value: payload["score"].update({"grade": value}),
        lambda payload, value: payload["answer_completeness"].update({"rationale": value}),
        lambda payload, value: payload["major_issues"][0].update({"code": value}),
        lambda payload, value: payload["suggestions"][0].update({"example": value}),
        lambda payload, value: payload.update({"limitations": [value]}),
    ],
)
@pytest.mark.parametrize("invalid_string", [b"partial", bytearray(b"partial")])
def test_all_string_fields_reject_bytes_and_bytearray(
    mutate: Callable[[Payload, object], None], invalid_string: bytes | bytearray
) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    mutate(payload, invalid_string)

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload, value: payload["answer_completeness"]["covered_points"].__setitem__(
            0, value
        ),
        lambda payload, value: payload["answer_completeness"]["missing_points"].__setitem__(
            0, value
        ),
        lambda payload, value: payload["answer_completeness"].update({"rationale": value}),
        lambda payload, value: payload["correctness"].update({"rationale": value}),
        lambda payload, value: payload["major_issues"][0].update({"title": value}),
        lambda payload, value: payload["major_issues"][0].update({"evidence": value}),
        lambda payload, value: payload["major_issues"][0].update({"impact": value}),
        lambda payload, value: payload["suggestions"][0].update({"action": value}),
        lambda payload, value: payload["suggestions"][0].update({"example": value}),
        lambda payload, value: payload.update({"limitations": [value]}),
    ],
)
@pytest.mark.parametrize(
    "dangerous_character",
    [
        "\x00",
        "\x01",
        "\x7f",
        "\u200b",
        "\ufeff",
        "\u202e",
        "\ud800",
        "\u0378",
        "\ue000",
    ],
)
def test_embedded_unsafe_unicode_is_rejected_from_every_natural_text_field(
    mutate: Callable[[Payload, str], None], dangerous_character: str
) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    mutate(payload, f"安全{dangerous_character}文本")

    with pytest.raises(ValidationError) as raised:
        normalize_evaluation(payload)

    messages = [error["msg"] for error in raised.value.errors(include_input=False)]
    assert all(dangerous_character not in message for message in messages)


def test_text_normalizes_crlf_and_allows_newline_tab_and_join_controls() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["rationale"] = "第一行\r\n第二行\r第三行\t缩进"
    payload["suggestions"][0]["example"] = "क्\u200dष 与 ک\u200cی"

    report = normalize_evaluation(payload)

    assert report.answer_completeness.rationale == "第一行\n第二行\n第三行\t缩进"
    assert report.suggestions[0].example == "क्\u200dष 与 ک\u200cی"


@pytest.mark.parametrize("join_control", ["\u200c", "\u200d"])
def test_join_controls_cannot_bypass_point_overlap_detection(join_control: str) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["covered_points"] = [f"POINT{join_control}"]
    payload["answer_completeness"]["missing_points"] = ["point"]

    with pytest.raises(ValidationError, match="covered_points.*missing_points"):
        normalize_evaluation(payload)


@pytest.mark.parametrize(
    "ignorable",
    [
        "\u034f",
        "\ufe00",
        "\ufe0f",
        "\U000e0100",
        "\u115f",
        "\u3164",
        "\uffa0",
    ],
)
def test_default_ignorable_characters_cannot_bypass_point_overlap(ignorable: str) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["covered_points"] = [f"POINT{ignorable}"]
    payload["answer_completeness"]["missing_points"] = ["point"]

    with pytest.raises(ValidationError, match="covered_points.*missing_points"):
        normalize_evaluation(payload)


@pytest.mark.parametrize("ignorable", ["\u034f", "\ufe00", "\ufe0f", "\U000e0100"])
def test_default_ignorable_characters_are_removed_only_for_point_deduplication(
    ignorable: str,
) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["covered_points"] = ["POINT", f"point{ignorable}"]

    report = normalize_evaluation(payload)

    assert report.answer_completeness.covered_points == ("POINT",)


def test_visible_variation_selectors_are_preserved_in_original_text() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["answer_completeness"]["covered_points"] = ["☕️", "字\U000e0100"]

    report = normalize_evaluation(payload)

    assert report.answer_completeness.covered_points == ("☕️", "字\U000e0100")


def test_limitations_use_non_aggressive_deduplication_that_preserves_ignorables() -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    payload["limitations"] = ["A", "A\u200d", "☕", "☕️"]

    report = normalize_evaluation(payload)

    assert report.limitations == ("A", "A\u200d", "☕", "☕️")


async def test_safe_report_roundtrips_through_real_postgres_jsonb_and_unsafe_text_is_rejected(
    postgres_session: AsyncSession,
) -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload())
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    roundtripped = await postgres_session.scalar(
        text("SELECT CAST(:payload AS jsonb)::text"),
        {"payload": serialized},
    )
    assert json.loads(roundtripped) == report.model_dump(mode="json")

    unsafe = valid_payload()
    unsafe["limitations"] = ["危险\ud800文本"]
    with pytest.raises(ValidationError):
        normalize_evaluation(unsafe)


def test_normalized_report_is_frozen_and_json_serializable() -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload())

    with pytest.raises(ValidationError):
        report.requires_human_review = False
    with pytest.raises(ValidationError):
        report.score.value = 10
    json.dumps(report.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda values: values.append("新增限制"),
        lambda values: values.extend(["新增限制"]),
        lambda values: values.insert(0, "新增限制"),
        lambda values: values.pop(),
        lambda values: values.remove(values[0]),
        lambda values: values.clear(),
        lambda values: values.reverse(),
        lambda values: values.sort(),
        lambda values: values.__setitem__(0, "篡改限制"),
        lambda values: values.__setitem__(slice(None), ["篡改限制"]),
        lambda values: values.__delitem__(0),
        lambda values: values.__iadd__(["新增限制"]),
        lambda values: values.__imul__(2),
    ],
)
def test_internal_collections_expose_no_mutation_api_without_partial_changes(
    mutation: Callable[[Any], object],
) -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload())
    before = report.model_dump(mode="json")

    with pytest.raises((AttributeError, TypeError)):
        mutation(report.limitations)

    assert report.model_dump(mode="json") == before


def test_all_nested_report_collections_are_deeply_immutable_tuples() -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload())
    collections = [
        report.answer_completeness.covered_points,
        report.answer_completeness.missing_points,
        report.major_issues,
        report.suggestions,
        report.limitations,
    ]

    for values in collections:
        assert isinstance(values, tuple)
        assert not isinstance(values, list)
        with pytest.raises(AttributeError):
            values.clear()

    dumped = report.model_dump(mode="json")
    assert isinstance(dumped["answer_completeness"]["covered_points"], list)
    assert isinstance(dumped["answer_completeness"]["missing_points"], list)
    assert isinstance(dumped["major_issues"], list)
    assert isinstance(dumped["suggestions"], list)
    assert isinstance(dumped["limitations"], list)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda values: list.append(values, "绕过"),
        lambda values: list.__setitem__(values, 0, "绕过"),
        lambda values: list.__init__(values, ["绕过"]),
    ],
)
def test_list_base_descriptors_cannot_bypass_internal_collection_immutability(
    mutation: Callable[[Any], object],
) -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload())
    before = report.model_dump(mode="json")

    with pytest.raises(TypeError):
        mutation(report.limitations)

    assert report.model_dump(mode="json") == before


def test_model_copy_revalidates_updates_and_preserves_frozen_collections() -> None:
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload())

    with pytest.raises(ValidationError):
        report.score.model_copy(update={"value": True})
    with pytest.raises(ValidationError):
        report.score.model_copy(update={"confidence": math.nan})
    with pytest.raises(ValidationError, match="covered_points.*missing_points"):
        report.answer_completeness.model_copy(
            update={"missing_points": [report.answer_completeness.covered_points[0]]}
        )

    copied = report.model_copy()
    deep_copied = report.model_copy(deep=True)
    assert copied == report
    assert deep_copied == report
    with pytest.raises(AttributeError):
        deep_copied.major_issues.clear()


def test_revalidation_accepts_only_internal_tuple_collections_not_plain_tuples() -> None:
    from app.evaluations.schemas import EvaluationOutput
    from app.evaluations.validation import normalize_evaluation

    report = normalize_evaluation(valid_payload())
    internal_payload = valid_payload()
    internal_payload["limitations"] = report.limitations
    plain_tuple_payload = valid_payload()
    plain_tuple_payload["limitations"] = tuple(report.limitations)

    revalidated = EvaluationOutput.model_validate(internal_payload)
    assert revalidated.limitations == report.limitations
    assert EvaluationOutput.model_validate(report) == report
    with pytest.raises(ValidationError):
        EvaluationOutput.model_validate(plain_tuple_payload)


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"value": True, "grade": Grade.B, "confidence": 0.5},
        {"value": 82, "grade": Grade.B, "confidence": math.nan},
    ],
)
def test_constructed_invalid_score_is_revalidated_at_every_public_boundary(
    invalid_values: dict[str, object],
) -> None:
    from app.evaluations.schemas import EvaluationOutput, EvaluationScore
    from app.evaluations.validation import normalize_evaluation

    bad_score = EvaluationScore.model_construct(**invalid_values)
    payload = valid_payload()
    payload["score"] = bad_score
    report = normalize_evaluation(valid_payload())

    with pytest.raises(ValidationError):
        EvaluationOutput.model_validate(payload)
    with pytest.raises(ValidationError):
        normalize_evaluation(payload)
    with pytest.raises(ValidationError):
        report.model_copy(update={"score": bad_score})


def test_constructed_invalid_completeness_and_issue_models_are_revalidated() -> None:
    from app.evaluations.schemas import AnswerCompleteness, EvaluationOutput, MajorIssue

    bad_completeness = AnswerCompleteness.model_construct(
        level="partial",
        covered_points=[],
        missing_points=[],
        rationale="形式上可见但语义无效。",
    )
    bad_issue = MajorIssue.model_construct(
        code="lowercase",
        title="问题",
        evidence="证据",
        impact="影响",
    )

    completeness_payload = valid_payload()
    completeness_payload["answer_completeness"] = bad_completeness
    issue_payload = valid_payload()
    issue_payload["major_issues"] = [bad_issue]

    with pytest.raises(ValidationError):
        EvaluationOutput.model_validate(completeness_payload)
    with pytest.raises(ValidationError):
        EvaluationOutput.model_validate(issue_payload)


@pytest.mark.parametrize(
    "path",
    [
        ("answer_completeness", "covered_points"),
        ("answer_completeness", "missing_points"),
        ("limitations",),
    ],
)
def test_string_list_fields_reject_set_coercion(path: tuple[str, ...]) -> None:
    from app.evaluations.validation import normalize_evaluation

    payload = valid_payload()
    current: object = payload
    for key in path:
        current = current[key]
    set_path(payload, path, set(current))

    with pytest.raises(ValidationError):
        normalize_evaluation(payload)


def test_json_schema_is_closed_required_and_contains_provider_limits() -> None:
    from app.evaluations.schemas import EvaluationOutput

    schema = EvaluationOutput.model_json_schema()
    json.dumps(schema, allow_nan=False)

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "answer_completeness",
        "correctness",
        "major_issues",
        "suggestions",
        "score",
        "limitations",
        "requires_human_review",
    }
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert schema["properties"]["major_issues"]["maxItems"] > 0
    assert schema["properties"]["suggestions"]["maxItems"] > 0
    assert schema["properties"]["limitations"]["type"] == "array"
    assert schema["properties"]["limitations"]["maxItems"] > 0
    assert schema["properties"]["limitations"]["items"]["type"] == "string"

    object_schemas = [schema, *schema["$defs"].values()]
    for object_schema in object_schemas:
        if object_schema.get("type") == "object":
            assert object_schema["additionalProperties"] is False
            expected_required = set(object_schema["properties"])
            if object_schema.get("title") == "Suggestion":
                expected_required.remove("example")
            assert set(object_schema["required"]) == expected_required

    score_schema = schema["$defs"]["EvaluationScore"]
    assert score_schema["properties"]["value"]["minimum"] == 0
    assert score_schema["properties"]["value"]["maximum"] == 100
    assert score_schema["properties"]["confidence"]["minimum"] == 0
    assert score_schema["properties"]["confidence"]["maximum"] == 1

    suggestion_schema = schema["$defs"]["Suggestion"]
    assert suggestion_schema["properties"]["example"]["default"] == ""
    assert "example" not in suggestion_schema["required"]


def test_serialization_schema_preserves_every_constrained_collection_shape() -> None:
    from app.evaluations.schemas import EvaluationOutput

    validation_schema = EvaluationOutput.model_json_schema(mode="validation")
    serialization_schema = EvaluationOutput.model_json_schema(mode="serialization")
    validation_answer = validation_schema["$defs"]["AnswerCompleteness"]["properties"]
    serialization_answer = serialization_schema["$defs"]["AnswerCompleteness"]["properties"]

    for field_name in ("covered_points", "missing_points"):
        assert serialization_answer[field_name] == validation_answer[field_name]
    for field_name in ("major_issues", "suggestions", "limitations"):
        assert (
            serialization_schema["properties"][field_name]
            == validation_schema["properties"][field_name]
        )
    for definition_name in ("MajorIssue", "Suggestion"):
        assert (
            serialization_schema["$defs"][definition_name]
            == validation_schema["$defs"][definition_name]
        )


def test_provider_schema_is_strict_recursive_and_uses_only_supported_keywords() -> None:
    from app.evaluations.schemas import evaluation_output_provider_schema

    unsupported = {
        "const",
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
    schema = evaluation_output_provider_schema()

    def assert_strict(node: object) -> None:
        if isinstance(node, dict):
            assert unsupported.isdisjoint(node)
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                assert_strict(value)
        elif isinstance(node, list):
            for value in node:
                assert_strict(value)

    assert_strict(schema)
    assert schema["properties"]["schema_version"]["enum"] == ["1.0"]
    assert "example" in schema["$defs"]["Suggestion"]["required"]
    assert schema["properties"]["limitations"]["type"] == "array"
    json.dumps(schema, allow_nan=False)


def test_provider_schema_is_a_fresh_deep_copy_and_does_not_pollute_application_schema() -> None:
    from app.evaluations.schemas import EvaluationOutput, evaluation_output_provider_schema

    application_schema = EvaluationOutput.model_json_schema()
    first_provider_schema = evaluation_output_provider_schema()
    second_provider_schema = evaluation_output_provider_schema()

    assert EvaluationOutput.model_json_schema() == application_schema
    assert first_provider_schema == second_provider_schema
    first_provider_schema["$defs"]["Suggestion"]["required"].clear()
    assert evaluation_output_provider_schema() == second_provider_schema
    assert "example" not in application_schema["$defs"]["Suggestion"]["required"]
    assert application_schema["$defs"]["Suggestion"]["properties"]["example"]["default"] == ""


def test_declared_string_and_list_limits_accept_boundaries_and_reject_overflow() -> None:
    from app.evaluations.schemas import (
        MAX_LIMITATIONS,
        MAX_LIMITATIONS_LENGTH,
        MAX_POINT_LENGTH,
        MAX_POINTS,
    )
    from app.evaluations.validation import normalize_evaluation

    boundary_payload = valid_payload()
    boundary_payload["answer_completeness"]["level"] = "complete"
    boundary_payload["answer_completeness"]["covered_points"] = [
        "界" * MAX_POINT_LENGTH
    ] * MAX_POINTS
    boundary_payload["answer_completeness"]["missing_points"] = []
    boundary_report = normalize_evaluation(boundary_payload)
    assert len(boundary_report.answer_completeness.covered_points[0]) == MAX_POINT_LENGTH

    oversized_text = valid_payload()
    oversized_text["answer_completeness"]["covered_points"] = ["界" * (MAX_POINT_LENGTH + 1)]
    with pytest.raises(ValidationError):
        normalize_evaluation(oversized_text)

    list_bomb = valid_payload()
    list_bomb["answer_completeness"]["covered_points"] = [
        f"不同要点 {index}" for index in range(MAX_POINTS + 1)
    ]
    with pytest.raises(ValidationError):
        normalize_evaluation(list_bomb)

    limitations_boundary = valid_payload()
    limitations_boundary["limitations"] = [
        f"{index}:" + "界" * (MAX_LIMITATIONS_LENGTH - len(f"{index}:"))
        for index in range(MAX_LIMITATIONS)
    ]
    assert len(normalize_evaluation(limitations_boundary).limitations) == MAX_LIMITATIONS

    limitations_bomb = valid_payload()
    limitations_bomb["limitations"] = [f"不同限制 {index}" for index in range(MAX_LIMITATIONS + 1)]
    with pytest.raises(ValidationError):
        normalize_evaluation(limitations_bomb)
