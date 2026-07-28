from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assignments.model import Assignment
from app.core.config import Settings
from app.db.migration_gate import migration_gated_sessionmaker
from app.db.types import Role
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.integrations.mattermost.card import ReportCardInput, render_report_card
from app.integrations.mattermost.client import MattermostClient, MattermostPermanentError
from app.integrations.mattermost.model import MattermostIdentity
from app.submissions.model import Submission
from app.users.model import User

logger = logging.getLogger(__name__)
ClientFactory = Callable[..., MattermostClient]


def _summary(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _summary_items(values: list[object]) -> tuple[str, ...]:
    return tuple(_summary(value) for value in values)


async def deliver_report_notification(
    engine: AsyncEngine,
    *,
    job_id: uuid.UUID,
    report_id: uuid.UUID,
    settings: Settings,
    client_factory: ClientFactory = MattermostClient,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    if not isinstance(engine, AsyncEngine):
        raise TypeError("engine must be an AsyncEngine")
    required = (
        settings.mattermost_url,
        settings.mattermost_bot_token,
        settings.mattermost_bot_user_id,
        settings.mattermost_action_secret,
        settings.mattermost_action_url,
        settings.web_console_url,
    )
    if any(value is None for value in required):
        raise MattermostPermanentError(error_type="configuration")

    session_factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    EvaluationOutbox.id,
                    EvaluationReport,
                    Assignment.code,
                    Assignment.title,
                    MattermostIdentity.mattermost_user_id,
                )
                .join(EvaluationJob, EvaluationJob.id == EvaluationOutbox.job_id)
                .join(EvaluationReport, EvaluationReport.id == EvaluationOutbox.report_id)
                .join(Submission, Submission.id == EvaluationReport.submission_id)
                .join(Assignment, Assignment.id == Submission.assignment_id)
                .join(User, User.id == EvaluationJob.requested_by)
                .join(MattermostIdentity, MattermostIdentity.user_id == User.id)
                .where(
                    EvaluationOutbox.kind == "notification",
                    EvaluationOutbox.job_id == job_id,
                    EvaluationOutbox.report_id == report_id,
                    EvaluationReport.job_id == job_id,
                    User.role == Role.TEACHER,
                    User.is_active.is_(True),
                )
            )
        ).one_or_none()
    if row is None:
        raise MattermostPermanentError(error_type="recipient")

    outbox_id, report, assignment_code, assignment_title, recipient_id = row
    client = client_factory(
        base_url=settings.mattermost_url,
        token=settings.mattermost_bot_token,
        bot_user_id=settings.mattermost_bot_user_id,
        allow_insecure_http=settings.app_env in {"development", "test"},
        transport=transport,
    )
    try:
        channel_id = await client.create_direct_channel(recipient_id)
        card = render_report_card(
            ReportCardInput(
                outbox_id=uuid.UUID(str(outbox_id)),
                report_id=uuid.UUID(str(report.id)),
                recipient_user_id=recipient_id,
                channel_id=channel_id,
                assignment_code=assignment_code,
                assignment_title=assignment_title,
                score=report.score,
                grade=report.grade.value,
                completeness=_summary(report.completeness),
                major_issues=_summary_items(report.major_issues),
                suggestions=_summary_items(report.suggestions),
                limitations=_summary_items(report.limitations),
                action_url=settings.mattermost_action_url,
                console_url=settings.web_console_url,
                action_secret=settings.mattermost_action_secret,
                allow_insecure_http=settings.app_env in {"development", "test"},
            )
        )
        return await client.create_post(
            channel_id=channel_id,
            message=card.message,
            props=card.props,
        )
    finally:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - cleanup cannot mask the delivery result
            logger.warning("Mattermost client close failed error_type=%s", type(exc).__name__)
