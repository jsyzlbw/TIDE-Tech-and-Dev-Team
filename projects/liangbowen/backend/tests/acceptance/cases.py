from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequiredCase:
    """One deterministic assignment-acceptance case and its observable contract."""

    fixture_key: str
    answer: str
    expected_score: int
    expected_grade: str
    expected_completeness: str
    expected_correctness: str
    maximum_confidence: float = 1.0


REQUIRED_CASES = (
    RequiredCase(
        fixture_key="complete",
        answer=(
            "Dijkstra 用最小堆反复取出暂定距离最小的顶点并松弛出边；"
            "邻接表与二叉堆实现为 O((V+E)logV)，且只适用于非负权边。"
        ),
        expected_score=94,
        expected_grade="A",
        expected_completeness="complete",
        expected_correctness="correct",
    ),
    RequiredCase(
        fixture_key="partial",
        answer="使用优先队列选点并松弛相邻顶点，但没有说明复杂度与边权限制。",
        expected_score=72,
        expected_grade="C",
        expected_completeness="partial",
        expected_correctness="mostly_correct",
    ),
    RequiredCase(
        fixture_key="incorrect",
        answer="每轮选择整个图中权值最小的边，就一定能得到单源最短路径。",
        expected_score=35,
        expected_grade="D",
        expected_completeness="incomplete",
        expected_correctness="incorrect",
    ),
    RequiredCase(
        fixture_key="ambiguous",
        answer="大概用贪心，也可能是动态规划；步骤和适用条件记不清了。",
        expected_score=45,
        expected_grade="D",
        expected_completeness="incomplete",
        expected_correctness="unable_to_determine",
        maximum_confidence=0.5,
    ),
)
