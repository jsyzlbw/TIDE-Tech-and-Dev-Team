from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.evaluations.providers.base import MAX_RUBRIC_BYTES
from app.reviews.schemas import ReportPatch
from app.reviews.service import ReviewService
from tests.evaluations.test_jobs import _seed_subject
from tests.reviews.test_api import _report, review_client


@pytest.mark.asyncio
async def test_workspace_is_a_safe_versioned_teacher_snapshot(postgres_session) -> None:
    teacher, _, assignment, submission = await _seed_subject(postgres_session)
    teacher.display_name = "林老师"
    rubric = {
        "required_points": ["  算法步骤  ", "复杂度"],
        "grading_notes": "  注意边界  ",
        "secret_solution": "WORKSPACE_RUBRIC_SECRET" * 5_000,
    }
    rubric_bytes = json.dumps(
        rubric,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(rubric_bytes) <= MAX_RUBRIC_BYTES
    assignment.rubric = rubric
    await postgres_session.commit()
    source = await _report(postgres_session, teacher, submission.id)
    modified = await ReviewService(postgres_session).modify(
        source.id,
        teacher.id,
        ReportPatch(score=88, comment="补充证据后调整"),
    )

    async with review_client(teacher, postgres_session) as client:
        response = await client.get(f"/api/v1/reports/{source.id}/workspace")

    assert response.status_code == 200
    body = response.json()
    assert body["requested_report_id"] == str(source.id)
    assert body["current_report_id"] == str(modified.id)
    assert body["assignment"]["id"] == str(assignment.id)
    assert body["assignment"]["rubric"] == {
        "required_points": ["算法步骤", "复杂度"],
        "grading_notes": "注意边界",
    }
    assert body["submission"]["id"] == str(submission.id)
    assert set(body["student"]) == {"id", "username", "display_name"}
    assert body["selected_report"]["id"] == str(source.id)
    assert [item["version"] for item in body["timeline"]] == [2, 1]
    assert body["timeline"][0]["author"] == {
        "kind": "teacher",
        "display_name": "林老师",
    }
    assert body["timeline_total"] == 2
    assert body["timeline_truncated"] is False
    assert body["timeline"][1]["latest_review_action"]["action"] == "modify"
    assert "comment" not in body["timeline"][1]["latest_review_action"]
    assert body["timeline"][1]["latest_review_action"]["acted_at"] is not None
    assert body["selected_report"]["latest_review_action"]["comment"] == "补充证据后调整"
    assert set(body["timeline"][0]) == {
        "id",
        "version",
        "origin",
        "review_status",
        "score",
        "grade",
        "author",
        "created_at",
        "latest_review_action",
    }
    assert "completeness" in body["selected_report"]
    serialized = response.text
    for forbidden in (
        "raw_model_output",
        '"provider"',
        '"model"',
        '"changes"',
        "idempotency_key",
        "WORKSPACE_RUBRIC_SECRET",
    ):
        assert forbidden not in serialized


def test_workspace_timeline_limit_is_explicit_and_bounded() -> None:
    from app.reviews.workspace import TIMELINE_LIMIT

    assert TIMELINE_LIMIT == 100


def test_workspace_rubric_projection_whitelists_and_normalizes_legacy_data() -> None:
    import app.reviews.workspace as workspace_module

    project = getattr(workspace_module, "project_workspace_rubric", None)
    assert callable(project)
    secret = "绝密答案" * 100_000
    assert project(
        {
            "required_points": ["  算法步骤  ", "复杂度"],
            "grading_notes": "  注意边界  ",
            "secret_solution": secret,
            "nested": {"secret": secret},
        }
    ) == {
        "required_points": ["算法步骤", "复杂度"],
        "grading_notes": "注意边界",
    }
    assert project(
        {
            "required_points": ["有效", "点" * 501],
            "grading_notes": {"secret": secret},
        }
    ) == {"required_points": [], "grading_notes": ""}
    assert project(
        {
            "required_points": [f"要点-{index}" for index in range(21)],
            "grading_notes": "注" * 5_001,
        }
    ) == {"required_points": [], "grading_notes": ""}
    assert project("not-an-object") == {"required_points": [], "grading_notes": ""}


def test_maximum_legal_workspace_payload_requires_only_three_mib_wire_budget() -> None:
    from app.reviews.schemas import ReportWorkspaceRead

    now = datetime.now(UTC)
    student_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    report_id = uuid.uuid4()
    emoji = "🧠"
    assert len(emoji.encode("utf-8")) == 4
    full_action = {
        "action": "modify",
        "teacher": {
            "id": uuid.uuid4(),
            "username": "u" * 64,
            "display_name": emoji * 128,
        },
        "comment": emoji * 2_000,
        "acted_at": now,
    }
    assert len(full_action["comment"].encode("utf-8")) == 8_000
    compact_action = {key: value for key, value in full_action.items() if key != "comment"}

    def max_text(group: int, index: int, length: int) -> str:
        unique_emoji = chr(0x1F300 + group * 100 + index)
        assert len(unique_emoji.encode("utf-8")) == 4
        return emoji * (length - 1) + unique_emoji

    selected = {
        "id": report_id,
        "submission_id": submission_id,
        "job_id": None,
        "source_report_id": uuid.uuid4(),
        "origin": "teacher",
        "version": 1,
        "schema_version": "1.0",
        "completeness": {
            "level": "partial",
            "covered_points": [max_text(0, index, 1_000) for index in range(30)],
            "missing_points": [max_text(1, index, 1_000) for index in range(30)],
            "rationale": emoji * 4_000,
        },
        "correctness": {"judgment": "mostly_correct", "rationale": emoji * 4_000},
        "major_issues": [
            {
                "code": f"I{index:02d}" + "X" * 61,
                "title": emoji * 200,
                "evidence": emoji * 4_000,
                "impact": emoji * 2_000,
            }
            for index in range(20)
        ],
        "suggestions": [
            {"priority": "high", "action": emoji * 2_000, "example": emoji * 4_000}
            for _ in range(20)
        ],
        "score": 82,
        "grade": "B",
        "confidence": 0.5,
        "limitations": [emoji * 4_000 for _ in range(20)],
        "validation_status": "valid",
        "review_status": "modified",
        "created_at": now,
        "author": {"kind": "teacher", "display_name": emoji * 128},
        "latest_review_action": full_action,
    }
    timeline = [
        {
            "id": uuid.UUID(int=index + 1),
            "version": 100 - index,
            "origin": "teacher",
            "review_status": "superseded" if index else "proposed",
            "score": 82,
            "grade": "B",
            "author": {"kind": "teacher", "display_name": emoji * 128},
            "created_at": now - timedelta(minutes=index),
            "latest_review_action": compact_action,
        }
        for index in range(100)
    ]
    payload = ReportWorkspaceRead.model_validate(
        {
            "requested_report_id": report_id,
            "current_report_id": report_id,
            "assignment": {
                "id": uuid.uuid4(),
                "code": "HW-MAX",
                "title": emoji * 200,
                "question": emoji * 50_000,
                "notes": emoji * 10_000,
                "rubric": {
                    "required_points": [max_text(2, index, 500) for index in range(20)],
                    "grading_notes": emoji * 5_000,
                },
                "due_at": now,
                "status": "published",
            },
            "submission": {
                "id": submission_id,
                "assignment_id": uuid.uuid4(),
                "student_id": student_id,
                "version": 1,
                "content_type": "text",
                "content_text": emoji * 50_000,
                "content_json": None,
                "status": "submitted",
                "submitted_at": now,
                "source": "web",
            },
            "student": {
                "id": student_id,
                "username": "s" * 64,
                "display_name": emoji * 128,
            },
            "selected_report": selected,
            "timeline": timeline,
            "timeline_total": 100,
            "timeline_truncated": False,
            "reevaluation_job": None,
            "request_id": "request-id",
        }
    )
    wire_bytes = len(payload.model_dump_json().encode("utf-8"))
    assert 2 * 1024 * 1024 < wire_bytes <= 3 * 1024 * 1024


@pytest.mark.asyncio
async def test_raw_output_is_lazy_bounded_and_non_enumerating(postgres_session) -> None:
    teacher, student, _, submission = await _seed_subject(postgres_session)
    source = await _report(postgres_session, teacher, submission.id)

    async with review_client(teacher, postgres_session) as client:
        available = await client.get(f"/api/v1/reports/{source.id}/raw-output")
    assert available.status_code == 200
    assert available.json()["available"] is True
    assert available.json()["raw_model_output"]
    assert len(available.json()["raw_model_output"].encode("utf-8")) <= 2 * 1024 * 1024

    without_raw = await ReviewService(postgres_session).modify(
        source.id,
        teacher.id,
        ReportPatch(score=source.score, comment="保留评分"),
    )
    async with review_client(teacher, postgres_session) as client:
        absent = await client.get(f"/api/v1/reports/{without_raw.id}/raw-output")
        missing = await client.get(f"/api/v1/reports/{uuid.uuid4()}/raw-output")
    assert absent.status_code == 200
    assert absent.json()["available"] is False
    assert absent.json()["raw_model_output"] is None
    assert missing.status_code == 404

    async with review_client(student, postgres_session) as client:
        blocked = await client.get(f"/api/v1/reports/{source.id}/raw-output")
        blocked_missing = await client.get(f"/api/v1/reports/{uuid.uuid4()}/raw-output")
    assert blocked.status_code == blocked_missing.status_code == 403


@pytest.mark.asyncio
async def test_workspace_permissions_not_found_and_large_score_comment_rule(
    postgres_session,
) -> None:
    teacher, student, _, submission = await _seed_subject(postgres_session)
    source = await _report(postgres_session, teacher, submission.id)

    async with review_client(student, postgres_session) as client:
        blocked = await client.get(f"/api/v1/reports/{source.id}/workspace")
    assert blocked.status_code == 403

    async with review_client(teacher, postgres_session) as client:
        missing = await client.get(f"/api/v1/reports/{uuid.uuid4()}/workspace")
        invalid = await client.get("/api/v1/reports/not-a-uuid/workspace")
        rejected = await client.patch(f"/api/v1/reports/{source.id}", json={"score": 60})
    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "comment is required when score changes by 10 or more"


def test_workspace_openapi_contract_is_strict_and_read_only() -> None:
    from app.main import create_app

    schema = create_app().openapi()
    paths = schema["paths"]
    assert set(paths["/api/v1/reports/{report_id}/workspace"]) == {"get"}
    assert set(paths["/api/v1/reports/{report_id}/raw-output"]) == {"get"}
    workspace = schema["components"]["schemas"]["ReportWorkspaceRead"]
    raw = schema["components"]["schemas"]["RawReportOutputRead"]
    timeline = schema["components"]["schemas"]["TimelineReportSummaryRead"]
    timeline_action = schema["components"]["schemas"].get("TimelineReviewActionRead")
    selected_action = schema["components"]["schemas"]["WorkspaceReviewActionRead"]
    workspace_rubric = schema["components"]["schemas"].get("WorkspaceRubricRead")
    assert workspace["additionalProperties"] is False
    assert raw["additionalProperties"] is False
    assert timeline["additionalProperties"] is False
    assert "completeness" not in timeline["properties"]
    assert timeline_action is not None
    assert timeline_action["additionalProperties"] is False
    assert "comment" not in timeline_action["properties"]
    assert "comment" in selected_action["properties"]
    assert workspace_rubric is not None
    assert workspace_rubric["additionalProperties"] is False
    assert set(workspace_rubric["properties"]) == {"required_points", "grading_notes"}
    assert "raw_model_output" not in str(workspace)


def test_raw_output_wire_budget_has_margin_for_maximum_json_escaping() -> None:
    from app.reviews.schemas import RawReportOutputRead

    raw = "\x00" * (2 * 1024 * 1024)
    payload = RawReportOutputRead(
        report_id=uuid.uuid4(),
        available=True,
        raw_model_output=raw,
        request_id="request-id",
    )
    wire_bytes = len(payload.model_dump_json().encode("utf-8"))
    assert wire_bytes > 12 * 1024 * 1024
    assert wire_bytes < 13 * 1024 * 1024
