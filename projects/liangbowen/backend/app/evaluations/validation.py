from collections.abc import Mapping
from typing import Any

from app.db.types import grade_for_score
from app.evaluations.schemas import EvaluationOutput

DEFAULT_LIMITATIONS = "本报告是文本初评，必须由教师审核。"


def normalize_evaluation(payload: Mapping[str, Any]) -> EvaluationOutput:
    """Validate and normalize an untrusted structured evaluation report."""
    validated = EvaluationOutput.model_validate(payload)
    normalized_score = validated.score.model_copy(
        update={"grade": grade_for_score(validated.score.value)}
    )
    normalized = validated.model_copy(
        update={
            "score": normalized_score,
            "limitations": validated.limitations or [DEFAULT_LIMITATIONS],
            "requires_human_review": True,
        },
        deep=True,
    )
    return EvaluationOutput.model_validate(normalized.model_dump())
