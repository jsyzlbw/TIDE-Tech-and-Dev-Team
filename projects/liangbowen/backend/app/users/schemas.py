from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.db.types import Role

Username = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$",
    ),
]


def _contains_category_c(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Username
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    role: Role
    password: Annotated[str, Field(min_length=12, max_length=128)]

    @field_validator("username", "display_name", mode="before")
    @classmethod
    def trim_text_fields(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        if _contains_category_c(value):
            raise ValueError("display name must not contain control or format characters")
        if not any(not character.isspace() for character in value):
            raise ValueError("display name must contain a visible non-whitespace character")
        return value

    @field_validator("password")
    @classmethod
    def validate_password_characters(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("password must not have leading or trailing whitespace")
        if _contains_category_c(value):
            raise ValueError("password must not contain control or format characters")
        return value

    @model_validator(mode="after")
    def password_must_differ_from_username(self) -> Self:
        if self.password == self.username:
            raise ValueError("password must not equal username")
        return self


class PublicModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", strict=True)


class AccountCreatorRead(PublicModel):
    id: uuid.UUID
    username: str
    display_name: str


class UserAccountRead(PublicModel):
    id: uuid.UUID
    username: str
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime
    created_by: AccountCreatorRead | None


class UserAccountPage(PublicModel):
    items: list[UserAccountRead]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1)]
    offset: Annotated[int, Field(ge=0)]
