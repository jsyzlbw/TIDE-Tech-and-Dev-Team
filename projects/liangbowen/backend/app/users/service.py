from __future__ import annotations

import uuid
from collections.abc import Iterable

from anyio import CapacityLimiter, to_thread
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.security import hash_password
from app.db.types import Role
from app.users.creation_event import AccountCreationEvent
from app.users.model import User
from app.users.schemas import (
    AccountCreatorRead,
    UserAccountPage,
    UserAccountRead,
    UserCreate,
)

password_hash_limiter = CapacityLimiter(4)

CREATABLE_ROLES: dict[Role, frozenset[Role]] = {
    Role.ADMIN: frozenset((Role.TEACHER, Role.STUDENT)),
    Role.TEACHER: frozenset((Role.STUDENT,)),
}
VISIBLE_ROLES: dict[Role, tuple[Role, ...]] = {
    Role.ADMIN: (Role.ADMIN, Role.TEACHER, Role.STUDENT),
    Role.TEACHER: (Role.STUDENT,),
}


class AccountRoleForbidden(PermissionError):
    """Raised when the current database actor cannot manage the requested account role."""


class UsernameAlreadyExists(ValueError):
    """Raised only for the users username unique constraint."""


def _related_errors(error: BaseException) -> Iterable[BaseException]:
    for attribute in ("orig", "__cause__", "__context__"):
        related = getattr(error, attribute, None)
        if isinstance(related, BaseException):
            yield related


def _is_username_unique_violation(error: IntegrityError) -> bool:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    has_unique_violation = False
    constraint_names: set[str] = set()

    while pending:
        candidate = pending.pop()
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        sqlstate = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        has_unique_violation = has_unique_violation or sqlstate == "23505"
        constraint_name = getattr(candidate, "constraint_name", None)
        if isinstance(constraint_name, str):
            constraint_names.add(constraint_name)
        pending.extend(_related_errors(candidate))

    return has_unique_violation and constraint_names == {"ix_users_username"}


async def _select_actor(
    session: AsyncSession,
    actor_id: uuid.UUID,
    *,
    lock: bool,
) -> User | None:
    statement = (
        select(User)
        .where(
            User.id == actor_id,
            User.is_active.is_(True),
            User.role.in_(tuple(CREATABLE_ROLES)),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


def _account_read(user: User, creator: User | None) -> UserAccountRead:
    return UserAccountRead(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        created_by=AccountCreatorRead.model_validate(creator) if creator is not None else None,
    )


async def create_user(
    session: AsyncSession,
    actor_id: uuid.UUID,
    payload: UserCreate,
    request_id: str,
) -> UserAccountRead:
    try:
        actor = await _select_actor(session, actor_id, lock=True)
        if actor is None or payload.role not in CREATABLE_ROLES[actor.role]:
            raise AccountRoleForbidden("actor cannot create the requested account role")

        password_hash = await to_thread.run_sync(
            hash_password,
            payload.password,
            limiter=password_hash_limiter,
        )
        user = User(
            id=uuid.uuid4(),
            username=payload.username,
            display_name=payload.display_name,
            role=payload.role,
            password_hash=password_hash,
            is_active=True,
        )
        session.add(user)
        await session.flush([user])
        session.add(
            AccountCreationEvent(
                actor_user_id=actor.id,
                target_user_id=user.id,
                target_role=user.role,
                request_id=request_id,
            )
        )
        await session.flush()
        result = _account_read(user, actor)
        await session.commit()
        return result
    except IntegrityError as exc:
        await session.rollback()
        if _is_username_unique_violation(exc):
            raise UsernameAlreadyExists(payload.username) from exc
        raise
    except Exception:
        await session.rollback()
        raise


async def list_users(
    session: AsyncSession,
    actor_id: uuid.UUID,
    limit: int,
    offset: int,
) -> UserAccountPage:
    actor = await _select_actor(session, actor_id, lock=False)
    if actor is None:
        raise AccountRoleForbidden("active admin or teacher account required")

    visible_roles = VISIBLE_ROLES[actor.role]
    total = await session.scalar(
        select(func.count()).select_from(User).where(User.role.in_(visible_roles))
    )

    creator = aliased(User)
    rows = (
        await session.execute(
            select(User, creator)
            .outerjoin(
                AccountCreationEvent,
                AccountCreationEvent.target_user_id == User.id,
            )
            .outerjoin(creator, creator.id == AccountCreationEvent.actor_user_id)
            .where(User.role.in_(visible_roles))
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return UserAccountPage(
        items=[_account_read(user, creator_user) for user, creator_user in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
