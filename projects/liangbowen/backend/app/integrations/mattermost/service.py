from __future__ import annotations

import copy
import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.assignments.model import Assignment
from app.assignments.schemas import AssignmentCreate
from app.assignments.service import (
    InvalidAssignmentTransition,
    create_published_assignment_in_transaction,
)
from app.assignments.summary import build_assignment_summary
from app.core.config import Settings
from app.db.types import (
    AssignmentStatus,
    Role,
    SubmissionContentType,
    SubmissionSource,
)
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox
from app.evaluations.providers.factory import create_evaluation_provider
from app.evaluations.service import (
    EvaluationBatchTooLarge,
    EvaluationService,
    EvaluationSubjectNotFound,
)
from app.evaluations.types import JobReason
from app.integrations.mattermost.actions import MattermostActionRequest, action_request_hash
from app.integrations.mattermost.model import (
    IntegrationEvent,
    IntegrationEventStatus,
    MattermostIdentity,
)
from app.integrations.mattermost.parser import USAGE, CommandParseError, parse_command
from app.integrations.mattermost.schemas import (
    DemoBindingRequest,
    MattermostCommand,
    MattermostRequest,
    MattermostResponse,
)
from app.integrations.mattermost.security import request_hash
from app.reviews.schemas import ConfirmRequest, ReevaluateRequest
from app.reviews.service import (
    ReviewConflict,
    ReviewForbidden,
    ReviewNotFound,
    ReviewService,
)
from app.submissions.schemas import SubmissionCreate
from app.submissions.service import (
    SubmissionAssignmentNotFound,
    SubmissionClosed,
    SubmissionInvalid,
    SubmissionTooLong,
    create_submission,
)
from app.users.model import User

SUPPORTED_MATTERMOST_ROLES = (Role.TEACHER, Role.STUDENT)
SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


class MattermostIdentityRejected(PermissionError):
    """The external identity is not bound to an active supported local user."""


class DemoBindingRejected(ValueError):
    """The requested local account is not eligible for demo binding."""


class DemoBindingConflict(RuntimeError):
    """Either side of a requested identity binding is already authoritative."""


class DemoBindingPersistenceError(RuntimeError):
    """Persistence failed for a reason other than a competing binding."""


class CommandDomainError(ValueError):
    """A deterministic, safe-to-cache command rejection."""


class ActionDomainError(ValueError):
    """An authenticated, deterministic interactive-action rejection."""


async def resolve_mattermost_actor(
    session: AsyncSession,
    mattermost_user_id: str,
    *,
    supported_roles_only: bool = True,
) -> User:
    criteria = [
        MattermostIdentity.mattermost_user_id == mattermost_user_id,
        User.is_active.is_(True),
    ]
    if supported_roles_only:
        criteria.append(User.role.in_(SUPPORTED_MATTERMOST_ROLES))
    actor = await session.scalar(
        select(User)
        .join(MattermostIdentity, MattermostIdentity.user_id == User.id)
        .where(*criteria)
    )
    if actor is None:
        raise MattermostIdentityRejected("Mattermost identity is not available")
    return actor


async def _binding_axes(
    session: AsyncSession,
    *,
    user_id,
    mattermost_user_id: str,
) -> tuple[MattermostIdentity | None, MattermostIdentity | None]:
    local_binding = await session.get(MattermostIdentity, user_id)
    external_binding = await session.scalar(
        select(MattermostIdentity).where(
            MattermostIdentity.mattermost_user_id == mattermost_user_id
        )
    )
    return local_binding, external_binding


def _same_binding(
    local_binding: MattermostIdentity | None,
    external_binding: MattermostIdentity | None,
    *,
    user_id,
    mattermost_user_id: str,
) -> MattermostIdentity | None:
    candidates = [item for item in (local_binding, external_binding) if item is not None]
    if not candidates:
        return None
    if all(
        item.user_id == user_id and item.mattermost_user_id == mattermost_user_id
        for item in candidates
    ):
        return candidates[0]
    raise DemoBindingConflict("identity binding cannot be reassigned")


def _is_unique_violation(error: IntegrityError) -> bool:
    pending: list[object] = [error.orig]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop()
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if (
            getattr(candidate, "sqlstate", None) == "23505"
            or getattr(candidate, "pgcode", None) == "23505"
        ):
            return True
        pending.extend(
            item
            for item in (
                getattr(candidate, "orig", None),
                getattr(candidate, "__cause__", None),
                getattr(candidate, "__context__", None),
            )
            if item is not None
        )
    return False


async def bind_demo_identity(
    session: AsyncSession,
    request: DemoBindingRequest,
) -> tuple[MattermostIdentity, bool]:
    actor = await session.scalar(
        select(User).where(
            User.username == request.local_username,
            User.is_active.is_(True),
            User.role.in_(SUPPORTED_MATTERMOST_ROLES),
        )
    )
    if actor is None:
        raise DemoBindingRejected("local account is not eligible")

    local_binding, external_binding = await _binding_axes(
        session,
        user_id=actor.id,
        mattermost_user_id=request.mattermost_user_id,
    )
    existing = _same_binding(
        local_binding,
        external_binding,
        user_id=actor.id,
        mattermost_user_id=request.mattermost_user_id,
    )
    if existing is not None:
        existing.mattermost_username = request.mattermost_username
        await session.flush([existing])
        return existing, False

    identity = MattermostIdentity(
        user_id=actor.id,
        mattermost_user_id=request.mattermost_user_id,
        mattermost_username=request.mattermost_username,
    )
    try:
        async with session.begin_nested():
            session.add(identity)
            await session.flush([identity])
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise DemoBindingPersistenceError("identity binding persistence failed") from exc
        local_binding, external_binding = await _binding_axes(
            session,
            user_id=actor.id,
            mattermost_user_id=request.mattermost_user_id,
        )
        existing = _same_binding(
            local_binding,
            external_binding,
            user_id=actor.id,
            mattermost_user_id=request.mattermost_user_id,
        )
        if existing is None:  # pragma: no cover - unique conflict must expose one axis
            raise DemoBindingPersistenceError("identity conflict disappeared") from exc
        existing.mattermost_username = request.mattermost_username
        await session.flush([existing])
        return existing, False
    return identity, True


async def handle_mattermost_command(
    session: AsyncSession,
    request: MattermostRequest,
    settings: Settings,
) -> MattermostResponse:
    digest = request_hash(request)
    actor = await resolve_mattermost_actor(session, request.user_id)
    inserted_id = await session.scalar(
        insert(IntegrationEvent)
        .values(
            id=uuid.uuid4(),
            request_hash=digest,
            actor_user_id=actor.id,
            status=IntegrationEventStatus.PROCESSING,
            business_refs={},
        )
        .on_conflict_do_nothing(index_elements=[IntegrationEvent.request_hash])
        .returning(IntegrationEvent.id)
    )
    if inserted_id is None:
        existing = await session.scalar(
            select(IntegrationEvent).where(IntegrationEvent.request_hash == digest)
        )
        if (
            existing is None
            or existing.status is IntegrationEventStatus.PROCESSING
            or existing.response is None
        ):
            raise RuntimeError("integration event replay is not terminal")
        return MattermostResponse.model_validate(existing.response).model_copy(deep=True)

    event = await session.get(IntegrationEvent, inserted_id)
    if event is None:  # pragma: no cover - INSERT RETURNING invariant
        raise RuntimeError("claimed integration event disappeared")
    try:
        if request.command != "/hw":
            raise CommandDomainError("仅支持 /hw 命令。")
        command = parse_command(request.text)
        response, refs = await _dispatch_command(session, actor, request, command, settings)
    except (
        CommandParseError,
        CommandDomainError,
        InvalidAssignmentTransition,
        SubmissionAssignmentNotFound,
        SubmissionClosed,
        SubmissionInvalid,
        SubmissionTooLong,
        EvaluationSubjectNotFound,
        EvaluationBatchTooLarge,
        ValidationError,
    ) as exc:
        public_message = getattr(exc, "public_message", None) or _deterministic_message(exc)
        response = MattermostResponse(text=public_message)
        refs = {}
        terminal_status = IntegrationEventStatus.DETERMINISTIC_ERROR
    else:
        terminal_status = IntegrationEventStatus.COMPLETED

    event.status = terminal_status
    event.response = response.model_dump(mode="json")
    event.business_refs = refs
    event.completed_at = datetime.now(UTC)
    await session.flush([event])
    return response


async def handle_mattermost_action(
    session: AsyncSession,
    request: MattermostActionRequest,
    settings: Settings,
) -> dict[str, object]:
    actor = await resolve_mattermost_actor(
        session,
        request.user_id,
        supported_roles_only=False,
    )
    if actor.role is not Role.TEACHER:
        public_message = "当前账号没有教师审核权限。"
        return {
            "error": {"message": public_message},
            "ephemeral_text": public_message,
        }
    delivery_id = await session.scalar(
        select(EvaluationOutbox.id)
        .join(EvaluationJob, EvaluationJob.id == EvaluationOutbox.job_id)
        .where(
            EvaluationOutbox.id == request.context.delivery_id,
            EvaluationOutbox.kind == "notification",
            EvaluationOutbox.report_id == request.context.report_id,
            EvaluationOutbox.delivered_at.is_not(None),
            EvaluationOutbox.failed_at.is_(None),
            EvaluationOutbox.delivery_ref == request.post_id,
            EvaluationJob.requested_by == actor.id,
        )
        .with_for_update(of=EvaluationOutbox)
    )
    if delivery_id is None:
        raise MattermostIdentityRejected("Mattermost delivery is not authoritative")

    try:
        await ReviewService(session).require_current_report_in_transaction(
            session,
            request.context.report_id,
            actor.id,
        )
    except (ReviewConflict, ReviewForbidden, ReviewNotFound) as exc:
        public_message = _action_error_message(exc)
        return {
            "error": {"message": public_message},
            "ephemeral_text": public_message,
        }

    digest = action_request_hash(request)
    inserted_id = await session.scalar(
        insert(IntegrationEvent)
        .values(
            id=uuid.uuid4(),
            request_hash=digest,
            event_type="interactive_action",
            actor_user_id=actor.id,
            status=IntegrationEventStatus.PROCESSING,
            business_refs={},
        )
        .on_conflict_do_nothing(index_elements=[IntegrationEvent.request_hash])
        .returning(IntegrationEvent.id)
    )
    if inserted_id is None:
        existing = await session.scalar(
            select(IntegrationEvent).where(IntegrationEvent.request_hash == digest)
        )
        if (
            existing is None
            or existing.status is IntegrationEventStatus.PROCESSING
            or existing.response is None
        ):
            raise RuntimeError("integration event replay is not terminal")
        return copy.deepcopy(existing.response)

    event = await session.get(IntegrationEvent, inserted_id)
    if event is None:  # pragma: no cover - INSERT RETURNING invariant
        raise RuntimeError("claimed action event disappeared")
    try:
        response, refs = await _dispatch_action(session, actor, request, settings)
    except (ActionDomainError, ReviewConflict, ReviewForbidden, ReviewNotFound) as exc:
        public_message = _action_error_message(exc)
        response = {
            "error": {"message": public_message},
            "ephemeral_text": public_message,
        }
        refs = {"report_id": str(request.context.report_id)}
        terminal_status = IntegrationEventStatus.DETERMINISTIC_ERROR
    else:
        terminal_status = IntegrationEventStatus.COMPLETED

    event.status = terminal_status
    event.response = response
    event.business_refs = refs
    event.completed_at = datetime.now(UTC)
    await session.flush([event])
    return copy.deepcopy(response)


def _action_error_message(error: Exception) -> str:
    if isinstance(error, ActionDomainError):
        return str(error)
    if isinstance(error, ReviewConflict):
        return "该评估已变更，请打开最新报告。"
    if isinstance(error, ReviewForbidden):
        return "当前账号没有教师审核权限。"
    return "未找到可审核的当前报告。"


async def _dispatch_action(
    session: AsyncSession,
    actor: User,
    request: MattermostActionRequest,
    settings: Settings,
) -> tuple[dict[str, object], dict[str, object]]:
    review = ReviewService(session)
    report_id = request.context.report_id
    refs: dict[str, object] = {"report_id": str(report_id)}
    if request.context.action == "confirm":
        await review.confirm_in_transaction(
            session,
            report_id,
            actor.id,
            ConfirmRequest(comment="Mattermost 交互确认"),
        )
        return (
            {
                "update": {
                    "message": "AI 基础评估 · 教师已确认",
                    "props": {"attachments": []},
                },
                "ephemeral_text": "已确认评估结果。",
            },
            refs,
        )

    if request.context.action == "reevaluate":
        provider = create_evaluation_provider(settings)
        try:
            evaluation = EvaluationService(
                session,
                EvaluationEngine(provider),
                requested_by=actor.id,
                dispatch=None,
                notification=None,
                mock_fixture_key=settings.agent_mock_fixture,
            )
            job = await review.reevaluate_in_transaction(
                session,
                report_id,
                actor.id,
                ReevaluateRequest(comment="Mattermost 交互触发重新评估"),
                evaluation,
            )
        finally:
            close = getattr(provider, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 - cleanup cannot alter action semantics
                    logger.warning(
                        "action provider close failed error_type=%s",
                        type(exc).__name__,
                    )
        refs["evaluation_job_id"] = str(job.id)
        return (
            {
                "update": {
                    "message": "AI 基础评估 · 已请求重新评估",
                    "props": {"attachments": []},
                },
                "ephemeral_text": "重新评估已进入队列。",
            },
            refs,
        )

    if request.context.action == "openreport":
        await review.require_current_report_in_transaction(session, report_id, actor.id)
        if settings.web_console_url is None:
            raise RuntimeError("web console URL is unavailable")
        report_url = (
            settings.web_console_url.rstrip("/")
            + "/teacher/reports/"
            + quote(str(report_id), safe="")
        )
        return {"ephemeral_text": f"打开报告：{report_url}"}, refs

    raise RuntimeError("validated action has no dispatcher")


def _deterministic_message(error: Exception) -> str:
    if isinstance(error, CommandDomainError):
        return str(error)
    if isinstance(error, InvalidAssignmentTransition):
        return "作业截止时间或状态不允许此操作。"
    if isinstance(error, (SubmissionClosed, SubmissionAssignmentNotFound)):
        return "该作业当前不接受提交。"
    if isinstance(error, (SubmissionInvalid, SubmissionTooLong, ValidationError)):
        return "提交内容无效。"
    if isinstance(error, EvaluationBatchTooLarge):
        return "待评估作业数量超过单批上限。"
    if isinstance(error, EvaluationSubjectNotFound):
        return "未找到可评估的作业。"
    return "命令无法执行。"


def _require_role(actor: User, role: Role) -> None:
    if actor.role is not role:
        raise CommandDomainError("当前账号没有执行此命令的权限。")


async def _assignment_by_code(
    session: AsyncSession,
    code: str,
    *,
    student_visible: bool = False,
) -> Assignment:
    conditions = [Assignment.code == code]
    if student_visible:
        conditions.append(
            Assignment.status.in_((AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED))
        )
    assignment = await session.scalar(select(Assignment).where(*conditions))
    if assignment is None:
        raise CommandDomainError("未找到该作业。")
    return assignment


def _deadline(value: datetime) -> str:
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M %Z")


async def _dispatch_command(
    session: AsyncSession,
    actor: User,
    request: MattermostRequest,
    command: MattermostCommand,
    settings: Settings,
) -> tuple[MattermostResponse, dict[str, object]]:
    if command.name == "help":
        role_notes = "教师：publish/list/show/summary/evaluate；学生：list/show/submit。"
        return (
            MattermostResponse(text="\n".join(USAGE.values()) + "\n" + role_notes),
            {},
        )

    if command.name == "publish":
        _require_role(actor, Role.TEACHER)
        assignment = await create_published_assignment_in_transaction(
            session,
            AssignmentCreate(
                title=command.arguments["title"],
                question=command.arguments["question"],
                notes=command.arguments.get("notes", ""),
                rubric={},
                due_at=command.arguments["due_at"],
            ),
            actor.id,
            request.channel_id,
        )
        return (
            MattermostResponse(
                response_type="in_channel",
                text=(
                    f"已发布 {assignment.code}｜{assignment.title}｜截止 {_deadline(assignment.due_at)}"
                ),
            ),
            {"assignment_id": str(assignment.id)},
        )

    if command.name == "list":
        conditions = []
        if actor.role is Role.STUDENT:
            conditions.append(
                Assignment.status.in_((AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED))
            )
        assignments = (
            await session.scalars(
                select(Assignment)
                .where(*conditions)
                .order_by(Assignment.created_at.desc(), Assignment.id.desc())
                .limit(20)
            )
        ).all()
        lines = [
            f"{item.code}｜{item.title}｜{item.status.value}｜{_deadline(item.due_at)}"
            for item in assignments
        ]
        return MattermostResponse(text="\n".join(lines) if lines else "暂无可见作业。"), {}

    code = command.positionals[0]
    if command.name == "show":
        assignment = await _assignment_by_code(
            session,
            code,
            student_visible=actor.role is Role.STUDENT,
        )
        text = (
            f"{assignment.code}｜{assignment.title}｜{assignment.status.value}｜"
            f"截止 {_deadline(assignment.due_at)}\n题目：{assignment.question}"
        )
        if assignment.notes:
            text += f"\n说明：{assignment.notes}"
        return MattermostResponse(text=text), {"assignment_id": str(assignment.id)}

    if command.name == "submit":
        _require_role(actor, Role.STUDENT)
        assignment = await _assignment_by_code(session, code, student_visible=True)
        submission = await create_submission(
            session,
            assignment,
            actor.id,
            SubmissionCreate(
                content_type=SubmissionContentType.TEXT,
                content_text=command.arguments["text"],
                content_json=None,
            ),
            SubmissionSource.MATTERMOST,
        )
        return (
            MattermostResponse(text=f"已提交 {assignment.code}，版本 {submission.version}。"),
            {
                "assignment_id": str(assignment.id),
                "submission_id": str(submission.id),
            },
        )

    if command.name == "summary":
        _require_role(actor, Role.TEACHER)
        assignment = await _assignment_by_code(session, code)
        summary = await build_assignment_summary(
            session,
            assignment.id,
            limit=1,
            offset=0,
        )
        if summary is None:
            raise CommandDomainError("未找到该作业。")
        text = (
            f"{assignment.code} summary: total={summary.total_students}, "
            f"submitted={summary.submitted_students}, missing={summary.missing_students}, "
            f"queued={summary.pending_evaluation}, evaluating={summary.evaluating}, "
            f"pending_review={summary.pending_review}, reviewed={summary.reviewed}, "
            f"failed={summary.failed}"
        )
        return MattermostResponse(text=text), {"assignment_id": str(assignment.id)}

    if command.name == "evaluate":
        _require_role(actor, Role.TEACHER)
        assignment = await _assignment_by_code(session, code)
        provider = create_evaluation_provider(settings)
        try:
            batch = await EvaluationService(
                session,
                EvaluationEngine(provider),
                requested_by=actor.id,
                dispatch=None,
                mock_fixture_key=settings.agent_mock_fixture,
            ).enqueue_assignment_in_transaction(
                session,
                assignment.id,
                JobReason.INITIAL,
            )
        finally:
            close = getattr(provider, "aclose", None)
            if callable(close):
                await close()
        return (
            MattermostResponse(
                text=f"评估批次 {batch.batch_id}: queued={batch.queued}, skipped={batch.skipped}"
            ),
            {
                "assignment_id": str(assignment.id),
                "batch_id": str(batch.batch_id),
            },
        )

    raise RuntimeError("parsed command has no dispatcher")
