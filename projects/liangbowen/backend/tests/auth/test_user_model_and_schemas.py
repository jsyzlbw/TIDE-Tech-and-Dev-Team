import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import Boolean, Enum, String
from sqlalchemy.sql.elements import True_

from app.auth.schemas import CurrentUser, LoginRequest, TokenResponse
from app.db.base import Base
from app.db.types import Role
from app.users.model import User


def test_user_model_uses_shared_metadata_and_required_columns() -> None:
    table = Base.metadata.tables["users"]

    assert table is User.__table__
    assert set(table.c) == {
        table.c.id,
        table.c.created_at,
        table.c.username,
        table.c.display_name,
        table.c.role,
        table.c.password_hash,
        table.c.is_active,
    }
    assert isinstance(table.c.username.type, String)
    assert table.c.username.type.length == 64
    assert table.c.username.unique is True
    assert table.c.username.index is True
    assert isinstance(table.c.display_name.type, String)
    assert table.c.display_name.type.length == 128
    assert isinstance(table.c.password_hash.type, String)
    assert table.c.password_hash.type.length == 128
    assert isinstance(table.c.is_active.type, Boolean)
    assert table.c.is_active.default is not None
    assert table.c.is_active.default.arg is True
    assert table.c.is_active.server_default is not None
    assert isinstance(table.c.is_active.server_default.arg, True_)


def test_user_role_metadata_persists_enum_values() -> None:
    role_column = User.__table__.c.role

    assert isinstance(role_column.type, Enum)
    assert role_column.type.name == "role"
    assert role_column.type.enums == ["teacher", "student", "admin"]
    assert role_column.index is True


def test_auth_schema_contracts_and_defaults() -> None:
    user_id = uuid.uuid4()

    login = LoginRequest(username="ada", password="secret")
    token = TokenResponse(access_token="encoded-token")
    current = CurrentUser(
        id=user_id,
        username="ada",
        display_name="Ada Lovelace",
        role=Role.TEACHER,
    )

    assert login.model_dump() == {"username": "ada", "password": "secret"}
    assert token.model_dump() == {
        "access_token": "encoded-token",
        "token_type": "bearer",
    }
    assert current.model_dump() == {
        "id": user_id,
        "username": "ada",
        "display_name": "Ada Lovelace",
        "role": Role.TEACHER,
    }


def test_login_request_strips_and_bounds_username() -> None:
    login = LoginRequest(username="  ada  ", password="secret")

    assert login.username == "ada"

    for username in ["", "   ", "a" * 65]:
        with pytest.raises(ValidationError):
            LoginRequest(username=username, password="secret")


@pytest.mark.parametrize("password", ["", "x" * 1025])
def test_login_request_bounds_password(password: str) -> None:
    with pytest.raises(ValidationError):
        LoginRequest(username="ada", password=password)
