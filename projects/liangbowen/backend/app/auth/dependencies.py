import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token
from app.db.session import get_session
from app.db.types import Role
from app.users.model import User

bearer_scheme = HTTPBearer(auto_error=False)


def invalid_authentication() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid authentication",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if credentials is None:
        raise invalid_authentication()

    try:
        claims = decode_access_token(credentials.credentials)
        subject = claims["sub"]
        if not isinstance(subject, str):
            raise KeyError("sub")
        user_id = uuid.UUID(subject)
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise invalid_authentication() from None

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise invalid_authentication()
    return user


def require_roles(*roles: Role) -> Callable[..., Awaitable[User]]:
    async def role_guard(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role",
            )
        return current_user

    return role_guard
