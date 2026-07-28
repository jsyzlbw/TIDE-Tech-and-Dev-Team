import logging
from typing import Annotated

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, invalid_authentication
from app.auth.schemas import CurrentUser, LoginRequest, TokenResponse
from app.auth.security import create_access_token, verify_password
from app.db.session import get_session
from app.users.model import User

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

DUMMY_PASSWORD_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.etuWbW/zszMhzI6q55iPxDOj9cJ3cT."
password_check_limiter = CapacityLimiter(4)


def _verify_login_password(password: str, password_hash: str) -> bool:
    try:
        return verify_password(password, password_hash)
    except ValueError:
        logger.error("stored password hash is invalid")
        verify_password(password, DUMMY_PASSWORD_HASH)
        return False


@router.post("/login", response_model=TokenResponse)
async def login(
    login_request: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenResponse:
    user = await session.scalar(select(User).where(User.username == login_request.username))
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_valid = await to_thread.run_sync(
        _verify_login_password,
        login_request.password,
        password_hash,
        limiter=password_check_limiter,
    )

    if user is None or not user.is_active or not password_valid:
        raise invalid_authentication()

    return TokenResponse(access_token=create_access_token(user.id, user.role))


@router.get("/me", response_model=CurrentUser)
async def me(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    return current_user
