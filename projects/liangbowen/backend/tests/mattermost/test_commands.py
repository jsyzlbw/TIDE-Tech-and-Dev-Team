from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.assignments.model import Assignment
from app.core.config import Settings
from app.db.session import get_session
from app.db.types import (
    AssignmentStatus,
    Role,
    SubmissionContentType,
    SubmissionSource,
)
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.integrations.mattermost.model import (
    IntegrationEvent,
    IntegrationEventStatus,
    MattermostIdentity,
)
from app.main import create_app
from app.submissions.model import Submission
from app.users.model import User

PATH = "/api/v1/integrations/mattermost/commands"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _settings() -> Settings:
    return Settings(
        app_env="test",
        jwt_secret="test-only",
        mattermost_command_token=SecretStr("slash-secret"),
        mattermost_demo_setup_key=None,
        evaluation_dispatch_enabled=False,
    )


def _form(user_id: str, username: str, text: str, trigger: str) -> bytes:
    return urlencode(
        {
            "token": "slash-secret",
            "team_id": "team-1",
            "team_domain": "course",
            "channel_id": "channel-1",
            "channel_name": "homework",
            "user_id": user_id,
            "user_name": username,
            "command": "/hw",
            "text": text,
            "trigger_id": trigger,
            "response_url": "https://mattermost.invalid/hooks/response",
        }
    ).encode()


async def _bound_users(session) -> tuple[User, User]:
    teacher = User(
        username=f"command-teacher-{uuid.uuid4().hex}",
        display_name="Teacher",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=True,
    )
    student = User(
        username=f"command-student-{uuid.uuid4().hex}",
        display_name="Student",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=True,
    )
    session.add_all([teacher, student])
    await session.flush()
    session.add_all(
        [
            MattermostIdentity(
                user_id=teacher.id,
                mattermost_user_id="mm-teacher",
                mattermost_username="teacher-mm",
            ),
            MattermostIdentity(
                user_id=student.id,
                mattermost_user_id="mm-student",
                mattermost_username="student-mm",
            ),
        ]
    )
    await session.commit()
    return teacher, student


async def _command_fixture(session) -> tuple[User, User, Assignment]:
    teacher, student = await _bound_users(session)
    now = datetime.now(SHANGHAI)
    assignment = Assignment(
        code="HW-4321",
        title="Role fixture",
        question="Explain roles",
        notes="",
        rubric={"private": "RUBRIC_SECRET"},
        due_at=now + timedelta(days=2),
        status=AssignmentStatus.PUBLISHED,
        mattermost_channel_id="channel-1",
        created_by=teacher.id,
        published_at=now,
    )
    session.add(assignment)
    await session.flush()
    session.add(
        Submission(
            assignment_id=assignment.id,
            student_id=student.id,
            version=1,
            content_type=SubmissionContentType.TEXT,
            content_text="ANSWER_SECRET",
            content_json=None,
            source=SubmissionSource.MATTERMOST,
        )
    )
    await session.commit()
    return teacher, student, assignment


@pytest.mark.asyncio
async def test_seven_command_workflow_is_role_safe_and_async(
    postgres_session,
    monkeypatch,
) -> None:
    teacher, student = await _bound_users(postgres_session)
    teacher_id, student_id = teacher.id, student.id

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session

    async def provider_must_not_run(*args, **kwargs):
        del args, kwargs
        await __import__("asyncio").Event().wait()

    monkeypatch.setattr(
        "app.evaluations.providers.mock.MockEvaluationProvider.evaluate",
        provider_must_not_run,
    )
    due = (datetime.now(SHANGHAI) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    headers = {"content-type": "application/x-www-form-urlencoded"}
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        help_response = await client.post(
            PATH,
            content=_form("mm-student", student.username, "help", "t-help"),
            headers=headers,
        )
        denied = await client.post(
            PATH,
            content=_form(
                "mm-student",
                student.username,
                f'publish --title "Bad" --due "{due}" --question "No"',
                "t-denied",
            ),
            headers=headers,
        )
        published = await client.post(
            PATH,
            content=_form(
                "mm-teacher",
                teacher.username,
                f'publish --title "图算法" --due "{due}" --question "解释 Dijkstra"',
                "t-publish",
            ),
            headers=headers,
        )
        code = re.search(r"HW-[0-9]{4,10}", published.json()["text"]).group(0)  # type: ignore[union-attr]
        listed = await client.post(
            PATH,
            content=_form("mm-student", student.username, "list", "t-list"),
            headers=headers,
        )
        shown = await client.post(
            PATH,
            content=_form("mm-student", student.username, f"show {code}", "t-show"),
            headers=headers,
        )
        first_submit = await client.post(
            PATH,
            content=_form(
                "mm-student",
                student.username,
                f'submit {code} --text "first secret answer"',
                "t-submit-1",
            ),
            headers=headers,
        )
        second_submit = await client.post(
            PATH,
            content=_form(
                "mm-student",
                student.username,
                f'submit {code} --text "second secret answer"',
                "t-submit-2",
            ),
            headers=headers,
        )
        summary = await client.post(
            PATH,
            content=_form("mm-teacher", teacher.username, f"summary {code}", "t-summary"),
            headers=headers,
        )
        evaluated = await client.post(
            PATH,
            content=_form("mm-teacher", teacher.username, f"evaluate {code}", "t-evaluate"),
            headers=headers,
        )

    assert all(
        response.status_code == 200
        for response in (
            help_response,
            denied,
            published,
            listed,
            shown,
            first_submit,
            second_submit,
            summary,
            evaluated,
        )
    )
    assert "/hw publish" in help_response.json()["text"]
    assert "权限" in denied.json()["text"]
    assert code in listed.json()["text"] and "rubric" not in listed.text
    assert "解释 Dijkstra" in shown.json()["text"] and "secret answer" not in shown.text
    assert "版本 1" in first_submit.json()["text"]
    assert "版本 2" in second_submit.json()["text"]
    assert "submitted=1" in summary.json()["text"]
    assert "queued=1" in evaluated.json()["text"]
    assert "job" not in evaluated.json()["text"].casefold()

    assignment = await postgres_session.scalar(select(Assignment).where(Assignment.code == code))
    assert assignment is not None
    assert assignment.created_by == teacher_id
    submissions = (
        await postgres_session.scalars(
            select(Submission)
            .where(Submission.assignment_id == assignment.id)
            .order_by(Submission.version)
        )
    ).all()
    assert [item.version for item in submissions] == [1, 2]
    assert all(item.student_id == student_id for item in submissions)
    assert all(item.source is SubmissionSource.MATTERMOST for item in submissions)
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 0


@pytest.mark.asyncio
async def test_deterministic_parse_error_is_terminal_and_replayed(postgres_session) -> None:
    teacher, _ = await _bound_users(postgres_session)

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    body = _form("mm-teacher", teacher.username, "publish --title nope", "same-trigger")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        first = await client.post(
            PATH,
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        second = await client.post(
            PATH,
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    events = (await postgres_session.scalars(select(IntegrationEvent))).all()
    assert len(events) == 1
    assert events[0].status is IntegrationEventStatus.DETERMINISTIC_ERROR
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 0


@pytest.mark.asyncio
async def test_teacher_only_commands_do_not_reveal_assignment_existence(postgres_session) -> None:
    teacher, student = await _bound_users(postgres_session)
    assignment = Assignment(
        code="HW-9876",
        title="Hidden draft",
        question="Private",
        notes="",
        rubric={},
        due_at=datetime.now(SHANGHAI) + timedelta(days=1),
        status=AssignmentStatus.DRAFT,
        created_by=teacher.id,
    )
    postgres_session.add(assignment)
    await postgres_session.commit()
    student_username = student.username

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    headers = {"content-type": "application/x-www-form-urlencoded"}
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        existing = await client.post(
            PATH,
            content=_form("mm-student", student_username, "summary HW-9876", "role-existing"),
            headers=headers,
        )
        missing = await client.post(
            PATH,
            content=_form("mm-student", student_username, "summary HW-9999", "role-missing"),
            headers=headers,
        )
    assert existing.status_code == missing.status_code == 200
    assert existing.json()["text"] == missing.json()["text"]
    assert "权限" in existing.json()["text"]


ROLE_CASES = [
    ("help", Role.TEACHER, True),
    ("help", Role.STUDENT, True),
    ("publish", Role.TEACHER, True),
    ("publish", Role.STUDENT, False),
    ("list", Role.TEACHER, True),
    ("list", Role.STUDENT, True),
    ("show", Role.TEACHER, True),
    ("show", Role.STUDENT, True),
    ("submit", Role.TEACHER, False),
    ("submit", Role.STUDENT, True),
    ("summary", Role.TEACHER, True),
    ("summary", Role.STUDENT, False),
    ("evaluate", Role.TEACHER, True),
    ("evaluate", Role.STUDENT, False),
]


@pytest.mark.parametrize(
    ("command_name", "role", "allowed"),
    ROLE_CASES,
    ids=[
        f"{name}-{role.value}-{'allow' if allowed else 'deny'}"
        for name, role, allowed in ROLE_CASES
    ],
)
@pytest.mark.asyncio
async def test_each_command_has_an_explicit_teacher_student_role_contract(
    postgres_session,
    command_name: str,
    role: Role,
    allowed: bool,
) -> None:
    teacher, student, _ = await _command_fixture(postgres_session)
    due = (datetime.now(SHANGHAI) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    commands = {
        "help": "help",
        "publish": f'publish --title "Role" --due "{due}" --question "Allowed?"',
        "list": "list",
        "show": "show HW-4321",
        "submit": 'submit HW-4321 --text "new answer"',
        "summary": "summary HW-4321",
        "evaluate": "evaluate HW-4321",
    }
    actor = teacher if role is Role.TEACHER else student
    mattermost_user_id = "mm-teacher" if role is Role.TEACHER else "mm-student"

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            PATH,
            content=_form(
                mattermost_user_id,
                actor.username,
                commands[command_name],
                f"role-{command_name}-{role.value}",
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200
    if allowed:
        assert "权限" not in response.json()["text"]
    else:
        assert response.json()["response_type"] == "ephemeral"
        assert "权限" in response.json()["text"]


@pytest.mark.asyncio
async def test_unknown_and_inactive_identities_share_one_safe_response(postgres_session) -> None:
    inactive = User(
        username=f"inactive-{uuid.uuid4().hex}",
        display_name="Inactive",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=False,
    )
    postgres_session.add(inactive)
    await postgres_session.flush()
    postgres_session.add(
        MattermostIdentity(
            user_id=inactive.id,
            mattermost_user_id="mm-inactive",
            mattermost_username="inactive-mm",
        )
    )
    await postgres_session.commit()

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    headers = {"content-type": "application/x-www-form-urlencoded"}
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        unknown = await client.post(
            PATH,
            content=_form("mm-unknown", "unknown", "help", "identity-unknown"),
            headers=headers,
        )
        inactive_response = await client.post(
            PATH,
            content=_form("mm-inactive", "inactive-mm", "help", "identity-inactive"),
            headers=headers,
        )

    assert unknown.status_code == inactive_response.status_code == 401
    assert unknown.content == inactive_response.content
    assert unknown.json() == {
        "response_type": "ephemeral",
        "text": "Mattermost 身份未绑定或不可用。",
    }
    assert await postgres_session.scalar(select(func.count()).select_from(IntegrationEvent)) == 0


ERROR_CASES = [
    ("help", Role.TEACHER, "help extra"),
    ("publish", Role.TEACHER, "publish --title missing"),
    ("list", Role.STUDENT, "list extra"),
    ("show", Role.STUDENT, "show HW-9999"),
    ("submit", Role.STUDENT, 'submit HW-9999 --text "answer"'),
    ("summary", Role.TEACHER, "summary HW-9999"),
    ("evaluate", Role.TEACHER, "evaluate HW-9999"),
]


@pytest.mark.parametrize(
    ("command_name", "role", "command_text"),
    ERROR_CASES,
    ids=[f"{name}-{role.value}" for name, role, _ in ERROR_CASES],
)
@pytest.mark.asyncio
async def test_each_command_caches_and_replays_a_representative_deterministic_error(
    postgres_session,
    command_name: str,
    role: Role,
    command_text: str,
) -> None:
    teacher, student, _ = await _command_fixture(postgres_session)
    actor = teacher if role is Role.TEACHER else student
    mattermost_user_id = "mm-teacher" if role is Role.TEACHER else "mm-student"

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    body = _form(
        mattermost_user_id,
        actor.username,
        command_text,
        f"error-{command_name}",
    )
    headers = {"content-type": "application/x-www-form-urlencoded"}
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        first = await client.post(PATH, content=body, headers=headers)
        replay = await client.post(PATH, content=body, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert first.content == replay.content
    assert first.json()["response_type"] == "ephemeral"
    assert 0 < len(first.json()["text"]) < 500
    events = (await postgres_session.scalars(select(IntegrationEvent))).all()
    assert len(events) == 1
    assert events[0].status is IntegrationEventStatus.DETERMINISTIC_ERROR


@pytest.mark.asyncio
async def test_list_and_show_minimize_role_specific_fields(postgres_session) -> None:
    teacher, student, _ = await _command_fixture(postgres_session)
    draft = Assignment(
        code="HW-9876",
        title="Teacher-only draft",
        question="Private draft",
        notes="DRAFT_NOTES",
        rubric={"private": "DRAFT_RUBRIC_SECRET"},
        due_at=datetime.now(SHANGHAI) + timedelta(days=2),
        status=AssignmentStatus.DRAFT,
        created_by=teacher.id,
    )
    postgres_session.add(draft)
    await postgres_session.commit()

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    headers = {"content-type": "application/x-www-form-urlencoded"}
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        teacher_list = await client.post(
            PATH,
            content=_form("mm-teacher", teacher.username, "list", "fields-teacher-list"),
            headers=headers,
        )
        student_list = await client.post(
            PATH,
            content=_form("mm-student", student.username, "list", "fields-student-list"),
            headers=headers,
        )
        teacher_show = await client.post(
            PATH,
            content=_form("mm-teacher", teacher.username, "show HW-4321", "fields-teacher-show"),
            headers=headers,
        )
        student_show = await client.post(
            PATH,
            content=_form("mm-student", student.username, "show HW-4321", "fields-student-show"),
            headers=headers,
        )
        student_draft = await client.post(
            PATH,
            content=_form("mm-student", student.username, "show HW-9876", "fields-student-draft"),
            headers=headers,
        )

    assert "HW-9876" in teacher_list.json()["text"]
    assert "HW-9876" not in student_list.json()["text"]
    assert "未找到" in student_draft.json()["text"]
    for response in (teacher_list, student_list, teacher_show, student_show):
        text_value = response.json()["text"]
        assert "RUBRIC_SECRET" not in text_value
        assert "ANSWER_SECRET" not in text_value
        assert "report" not in text_value.casefold()
        assert "job" not in text_value.casefold()
