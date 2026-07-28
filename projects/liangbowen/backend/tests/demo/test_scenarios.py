from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.demo.scenarios import DEMO_SCENARIOS, DemoScenario


def test_demo_scenarios_are_ordered_distinct_and_complete() -> None:
    assert tuple(scenario.fixture_key for scenario in DEMO_SCENARIOS) == (
        "complete",
        "partial",
        "incorrect",
    )
    assert tuple(scenario.student_username for scenario in DEMO_SCENARIOS) == (
        "student1",
        "student2",
        "student3",
    )
    assert tuple(
        (scenario.expected_score, scenario.expected_grade) for scenario in DEMO_SCENARIOS
    ) == (
        (94, "A"),
        (72, "C"),
        (35, "D"),
    )
    assert all(scenario.answer.strip() for scenario in DEMO_SCENARIOS)
    assert len({scenario.answer for scenario in DEMO_SCENARIOS}) == len(DEMO_SCENARIOS)
    assert "Dijkstra" in DEMO_SCENARIOS[0].answer
    assert "非负" in DEMO_SCENARIOS[0].answer
    assert "松弛" in DEMO_SCENARIOS[0].answer


def test_demo_scenario_is_frozen() -> None:
    scenario = DEMO_SCENARIOS[0]

    with pytest.raises(FrozenInstanceError):
        scenario.answer = "tampered"  # type: ignore[misc]

    assert isinstance(scenario, DemoScenario)
