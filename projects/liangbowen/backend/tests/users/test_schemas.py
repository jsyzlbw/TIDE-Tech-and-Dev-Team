from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.db.types import Role
from app.users.schemas import (
    AccountCreatorRead,
    UserAccountPage,
    UserAccountRead,
    UserCreate,
)


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "username": "alice.teacher",
        "display_name": "Alice Teacher",
        "role": Role.TEACHER,
        "password": "correct horse battery staple",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("username", ["abc", "a" * 64, "a0._-z"])
def test_user_create_accepts_username_boundaries(username: str) -> None:
    assert UserCreate.model_validate(valid_payload(username=username)).username == username


def test_user_create_trims_username_without_silently_lowercasing() -> None:
    assert UserCreate.model_validate(valid_payload(username="  alice_01\t")).username == "alice_01"

    with pytest.raises(ValidationError):
        UserCreate.model_validate(valid_payload(username="Alice_01"))


@pytest.mark.parametrize(
    "username",
    [
        "ab",
        "a" * 65,
        "_alice",
        ".alice",
        "alice+tag",
        "álîce",
        "alice name",
        "alice\nname",
        "Ａlice",
    ],
)
def test_user_create_rejects_invalid_usernames(username: str) -> None:
    with pytest.raises(ValidationError):
        UserCreate.model_validate(valid_payload(username=username))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  张老师  ", "张老师"),
        (" x ", "x"),
        ("😀" * 128, "😀" * 128),
    ],
)
def test_user_create_trims_and_accepts_unicode_display_name_boundaries(
    raw: str,
    expected: str,
) -> None:
    assert UserCreate.model_validate(valid_payload(display_name=raw)).display_name == expected


@pytest.mark.parametrize(
    "display_name",
    [
        "",
        "   \t\n",
        "x" * 129,
        "Alice\x00Teacher",
        "Alice\nTeacher",
        "Alice\u200bTeacher",
        "Alice\u202eTeacher",
        "\u2060",
    ],
)
def test_user_create_rejects_empty_oversized_or_category_c_display_names(
    display_name: str,
) -> None:
    with pytest.raises(ValidationError):
        UserCreate.model_validate(valid_payload(display_name=display_name))


@pytest.mark.parametrize("role", list(Role))
def test_user_create_parses_the_complete_role_enum(role: Role) -> None:
    parsed = UserCreate.model_validate(valid_payload(role=role.value))
    assert parsed.role is role


@pytest.mark.parametrize("password", ["x" * 12, "😀" * 128])
def test_user_create_accepts_password_code_point_boundaries(password: str) -> None:
    assert UserCreate.model_validate(valid_payload(password=password)).password == password


@pytest.mark.parametrize(
    "password",
    [
        "x" * 11,
        "x" * 129,
        " leading-password",
        "trailing-password ",
        "valid-pass\x00word",
        "valid-pass\nword",
        "valid-pass\u200bword",
        "valid-pass\u202eword",
        "alice.teacher",
    ],
)
def test_user_create_rejects_invalid_passwords(password: str) -> None:
    with pytest.raises(ValidationError):
        UserCreate.model_validate(valid_payload(password=password))


class CreatorRow:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.username = "admin.user"
        self.display_name = "Admin User"


class AccountRow:
    def __init__(self, creator: CreatorRow | None = None) -> None:
        self.id = uuid.uuid4()
        self.username = "student.user"
        self.display_name = "Student User"
        self.role = Role.STUDENT
        self.is_active = True
        self.created_at = datetime.now(UTC)
        self.password_hash = "must-never-leak"
        self.created_by = creator


def test_public_models_are_orm_compatible_and_password_free() -> None:
    creator_row = CreatorRow()
    creator = AccountCreatorRead.model_validate(creator_row)
    account = UserAccountRead.model_validate(AccountRow(creator_row))
    page = UserAccountPage(items=[account], total=1, limit=20, offset=0)

    assert creator.username == "admin.user"
    assert page.items[0].created_by == creator
    dumped = page.model_dump()
    assert "password" not in repr(dumped)
    assert "password_hash" not in repr(dumped)
    assert not ({"password", "password_hash"} & AccountCreatorRead.model_fields.keys())
    assert not ({"password", "password_hash"} & UserAccountRead.model_fields.keys())
    assert not ({"password", "password_hash"} & UserAccountPage.model_fields.keys())


def test_public_models_are_strict_and_forbid_unknown_input() -> None:
    row = AccountRow()
    base = {
        "id": row.id,
        "username": row.username,
        "display_name": row.display_name,
        "role": row.role,
        "is_active": row.is_active,
        "created_at": row.created_at,
        "created_by": None,
    }

    with pytest.raises(ValidationError):
        UserAccountRead.model_validate({**base, "is_active": 1})
    with pytest.raises(ValidationError):
        UserAccountRead.model_validate({**base, "password_hash": "leak"})


@pytest.mark.parametrize(
    "page_fields",
    [
        {"total": -1, "limit": 20, "offset": 0},
        {"total": 0, "limit": 0, "offset": 0},
        {"total": 0, "limit": 20, "offset": -1},
    ],
)
def test_user_account_page_rejects_invalid_pagination(page_fields: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        UserAccountPage(items=[], **page_fields)
