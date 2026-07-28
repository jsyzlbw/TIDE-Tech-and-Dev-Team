from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_roles
from app.db.session import get_session
from app.db.types import Role
from app.evaluations.api_schemas import EvaluationJobRead, EvaluationReportRead
from app.evaluations.router import evaluation_service_context
from app.evaluations.service import EvaluationSubjectNotFound
from app.reviews.schemas import (
    ConfirmRequest,
    RawReportOutputRead,
    ReevaluateRequest,
    ReportPatch,
    ReportWorkspaceRead,
    ReviewJobRead,
    ReviewReportRead,
    TimelineReportSummaryRead,
    TimelineReviewActionRead,
    WorkspaceAssignmentRead,
    WorkspaceReportAuthorRead,
    WorkspaceReportRead,
    WorkspaceReviewActionRead,
)
from app.reviews.service import (
    ReviewConflict,
    ReviewForbidden,
    ReviewNotFound,
    ReviewService,
    ReviewValidation,
)
from app.reviews.workspace import ReportTimelineItem, ReportTimelineSummary, ReviewWorkspaceService
from app.users.model import User

router = APIRouter(tags=["reviews"])


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _report_read(report: object, request: Request) -> ReviewReportRead:
    safe = EvaluationReportRead.model_validate(report).model_dump()
    return ReviewReportRead.model_validate({**safe, "request_id": _request_id(request)})


def _job_read(job: object, request: Request) -> ReviewJobRead:
    safe = EvaluationJobRead.model_validate(job).model_dump()
    source_report_id = getattr(job, "source_report_id", None)
    if source_report_id is None:
        raise RuntimeError("review job has no source report")
    return ReviewJobRead.model_validate(
        {
            **safe,
            "source_report_id": source_report_id,
            "request_id": _request_id(request),
        }
    )


def _translate_review_error(error: Exception) -> HTTPException:
    if isinstance(error, ReviewNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
    if isinstance(error, ReviewForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    if isinstance(error, ReviewConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, ReviewValidation):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))
    raise error


@router.post(
    "/reports/{report_id}/confirm",
    response_model=ReviewReportRead,
    status_code=status.HTTP_200_OK,
)
async def confirm_report(
    report_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
    body: Annotated[ConfirmRequest | None, Body()] = None,
) -> ReviewReportRead:
    try:
        report = await ReviewService(session).confirm(
            report_id,
            current_user.id,
            body or ConfirmRequest(),
        )
    except (ReviewNotFound, ReviewForbidden, ReviewConflict, ReviewValidation) as exc:
        raise _translate_review_error(exc) from None
    return _report_read(report, request)


@router.patch(
    "/reports/{report_id}",
    response_model=ReviewReportRead,
    status_code=status.HTTP_200_OK,
)
async def modify_report(
    report_id: uuid.UUID,
    request: Request,
    body: ReportPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> ReviewReportRead:
    try:
        report = await ReviewService(session).modify(report_id, current_user.id, body)
    except (ReviewNotFound, ReviewForbidden, ReviewConflict, ReviewValidation) as exc:
        raise _translate_review_error(exc) from None
    return _report_read(report, request)


@router.post(
    "/reports/{report_id}/reevaluate",
    response_model=ReviewJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reevaluate_report(
    report_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
    body: Annotated[ReevaluateRequest | None, Body()] = None,
) -> ReviewJobRead:
    try:
        async with evaluation_service_context(session, current_user.id) as evaluation:
            job = await ReviewService(session).reevaluate(
                report_id,
                current_user.id,
                body or ReevaluateRequest(),
                evaluation,
            )
    except (ReviewNotFound, ReviewForbidden, ReviewConflict) as exc:
        raise _translate_review_error(exc) from None
    except EvaluationSubjectNotFound:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="report cannot be re-evaluated",
        ) from None
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="evaluation provider unavailable",
        ) from None
    return _job_read(job, request)


def _workspace_report(item: ReportTimelineItem) -> WorkspaceReportRead:
    safe = EvaluationReportRead.model_validate(item.report).model_dump()
    if item.creator is None:
        author = WorkspaceReportAuthorRead(kind="agent", display_name="AI Agent")
    else:
        author = WorkspaceReportAuthorRead(
            kind="teacher",
            display_name=item.creator.display_name or item.creator.username,
        )
    action = None
    if item.action is not None and item.action_teacher is not None:
        action = WorkspaceReviewActionRead(
            action=item.action.action,
            teacher=item.action_teacher,
            comment=item.action.comment,
            acted_at=item.action.created_at,
        )
    return WorkspaceReportRead.model_validate(
        {**safe, "author": author, "latest_review_action": action}
    )


def _timeline_summary(item: ReportTimelineSummary) -> TimelineReportSummaryRead:
    if item.creator is None:
        author = WorkspaceReportAuthorRead(kind="agent", display_name="AI Agent")
    else:
        author = WorkspaceReportAuthorRead(
            kind="teacher",
            display_name=item.creator.display_name or item.creator.username,
        )
    action = None
    if item.action is not None and item.action_teacher is not None:
        action = TimelineReviewActionRead(
            action=item.action.action,
            teacher=item.action_teacher,
            acted_at=item.action.created_at,
        )
    return TimelineReportSummaryRead(
        id=item.id,
        version=item.version,
        origin=item.origin,
        review_status=item.review_status,
        score=item.score,
        grade=item.grade,
        author=author,
        created_at=item.created_at,
        latest_review_action=action,
    )


@router.get(
    "/reports/{report_id}/workspace",
    response_model=ReportWorkspaceRead,
    status_code=status.HTTP_200_OK,
)
async def get_report_workspace(
    report_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> ReportWorkspaceRead:
    try:
        snapshot = await ReviewWorkspaceService(session).workspace(report_id, current_user.id)
    except (ReviewNotFound, ReviewForbidden) as exc:
        raise _translate_review_error(exc) from None
    timeline = [_timeline_summary(item) for item in snapshot.timeline]
    selected = _workspace_report(snapshot.selected_item)
    assignment = WorkspaceAssignmentRead(
        id=snapshot.assignment.id,
        code=snapshot.assignment.code,
        title=snapshot.assignment.title,
        question=snapshot.assignment.question,
        notes=snapshot.assignment.notes,
        rubric=snapshot.rubric,
        due_at=snapshot.assignment.due_at,
        status=snapshot.assignment.status,
    )
    return ReportWorkspaceRead(
        requested_report_id=report_id,
        current_report_id=snapshot.current_report_id,
        assignment=assignment,
        submission=snapshot.submission,
        student=snapshot.student,
        selected_report=selected,
        timeline=timeline,
        timeline_total=snapshot.timeline_total,
        timeline_truncated=snapshot.timeline_total > len(timeline),
        reevaluation_job=snapshot.reevaluation_job,
        request_id=_request_id(request),
    )


@router.get(
    "/reports/{report_id}/raw-output",
    response_model=RawReportOutputRead,
    status_code=status.HTTP_200_OK,
)
async def get_report_raw_output(
    report_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> RawReportOutputRead:
    try:
        output = await ReviewWorkspaceService(session).raw_output(report_id, current_user.id)
    except (ReviewNotFound, ReviewForbidden) as exc:
        raise _translate_review_error(exc) from None
    return RawReportOutputRead(
        report_id=report_id,
        available=output is not None,
        raw_model_output=output,
        request_id=_request_id(request),
    )
