from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_roles
from app.db.session import get_session
from app.db.types import Role
from app.users.model import User
from app.users.schemas import UserAccountPage, UserAccountRead, UserCreate
from app.users.service import (
    AccountRoleForbidden,
    UsernameAlreadyExists,
    create_user,
    list_users,
)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=UserAccountPage)
async def list_accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        User,
        Depends(require_roles(Role.ADMIN, Role.TEACHER)),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> UserAccountPage:
    try:
        return await list_users(session, current_user.id, limit=limit, offset=offset)
    except AccountRoleForbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient role for requested account",
        ) from None


@router.post("", response_model=UserAccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: UserCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        User,
        Depends(require_roles(Role.ADMIN, Role.TEACHER)),
    ],
) -> UserAccountRead:
    try:
        return await create_user(
            session,
            current_user.id,
            payload,
            str(request.state.request_id),
        )
    except AccountRoleForbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient role for requested account",
        ) from None
    except UsernameAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username already exists",
        ) from None
