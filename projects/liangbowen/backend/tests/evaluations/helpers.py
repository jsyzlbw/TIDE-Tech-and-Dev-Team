from typing import Any

from sqlalchemy.engine import URL


def render_test_database_url(url: URL) -> str:
    """Render credentials only for isolated test database connections and environments."""
    return url.render_as_string(hide_password=False)


def database_credentials_are_hidden(message: str, url: URL) -> bool:
    """Return whether a message omits both the password and complete test database URL."""
    password = url.password
    complete_url = render_test_database_url(url)
    return (not password or password not in message) and complete_url not in message


def valid_payload(*, score: int = 82, grade: str = "B") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "answer_completeness": {
            "level": "partial",
            "covered_points": ["松弛"],
            "missing_points": ["复杂度"],
            "rationale": "覆盖核心步骤但未分析复杂度。",
        },
        "correctness": {
            "judgment": "mostly_correct",
            "rationale": "算法方向正确。",
        },
        "major_issues": [],
        "suggestions": [
            {
                "priority": "high",
                "action": "补充复杂度。",
                "example": "O((V+E)logV)",
            }
        ],
        "score": {"value": score, "grade": grade, "confidence": 0.86},
        "limitations": ["未运行代码。"],
        "requires_human_review": True,
    }


REQUEST_DATA = {
    "assignment_title": "图的最短路径",
    "question": "说明 Dijkstra 算法、复杂度与适用条件。",
    "rubric": {"required_points": ["松弛", "复杂度", "非负权"]},
    "student_answer": "使用优先队列进行松弛。",
}
