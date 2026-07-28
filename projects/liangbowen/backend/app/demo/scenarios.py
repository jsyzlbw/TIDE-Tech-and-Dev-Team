from __future__ import annotations

from dataclasses import dataclass

from app.db.types import Grade


@dataclass(frozen=True, slots=True)
class DemoScenario:
    fixture_key: str
    student_username: str
    answer: str
    expected_score: int
    expected_grade: Grade


DEMO_SCENARIOS = (
    DemoScenario(
        fixture_key="complete",
        student_username="student1",
        answer=(
            "Dijkstra 算法适用于边权非负的图。先把源点距离设为 0，其余顶点设为无穷大；"
            "每轮从尚未确定的顶点中选择当前距离最小者，将其最短距离固定，再遍历它的出边，"
            "用 dist[v] = min(dist[v], dist[u] + w(u,v)) 进行松弛。重复直到所有可达顶点均已确定。"
            "使用邻接表和二叉堆时，时间复杂度为 O((V+E)log V)，空间复杂度为 O(V+E)。"
        ),
        expected_score=94,
        expected_grade=Grade.A,
    ),
    DemoScenario(
        fixture_key="partial",
        student_username="student2",
        answer=(
            "从起点开始，反复选择当前距离最短且没有访问过的顶点，并更新它相邻顶点的距离。"
            "所有顶点访问后就得到最短路。我认为使用数组实现大约需要 O(V²)。"
        ),
        expected_score=72,
        expected_grade=Grade.C,
    ),
    DemoScenario(
        fixture_key="incorrect",
        student_username="student3",
        answer=(
            "Dijkstra 每一步选择边权最大的边加入路径，因此既能处理负权边，也能直接处理负环。"
            "只需扫描一次所有边，所以时间复杂度是 O(E)。"
        ),
        expected_score=35,
        expected_grade=Grade.D,
    ),
)
