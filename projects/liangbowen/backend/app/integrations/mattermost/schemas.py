from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.integrations.mattermost.ids import validate_mattermost_id

MattermostCommandName = Literal[
    "help",
    "publish",
    "list",
    "show",
    "submit",
    "summary",
    "evaluate",
]


class MattermostCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: MattermostCommandName
    positionals: tuple[str, ...]
    arguments: dict[str, str | datetime]


class MattermostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: Annotated[str, Field(min_length=1, max_length=128)]
    team_domain: Annotated[str, Field(max_length=128)]
    channel_id: Annotated[str, Field(min_length=1, max_length=128)]
    channel_name: Annotated[str, Field(max_length=128)]
    user_id: Annotated[str, Field(min_length=1, max_length=128)]
    user_name: Annotated[str, Field(max_length=128)]
    command: Annotated[str, Field(min_length=1, max_length=32)]
    text: Annotated[str, Field(max_length=60 * 1024)]
    trigger_id: Annotated[str, Field(min_length=1, max_length=256)]
    response_url: Annotated[str, Field(max_length=2_048)]

    @field_validator("*")
    @classmethod
    def reject_nul_and_oversized_utf8(cls, value: str, info) -> str:
        if "\0" in value:
            raise ValueError(f"{info.field_name} contains a NUL byte")
        if info.field_name == "text" and len(value.encode("utf-8")) > 60 * 1024:
            raise ValueError("text exceeds 61440 UTF-8 bytes")
        return value


class MattermostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    response_type: Literal["ephemeral", "in_channel"] = "ephemeral"
    text: Annotated[str, Field(min_length=1, max_length=60_000)]

    @field_validator("text", mode="before")
    @classmethod
    def bound_utf8_response(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        encoded = value.encode("utf-8")
        if len(encoded) <= 60_000:
            return value
        return encoded[:59_990].decode("utf-8", errors="ignore") + "…"


class DemoBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    local_username: Annotated[str, Field(min_length=1, max_length=64)]
    mattermost_user_id: Annotated[str, Field(min_length=1, max_length=128)]
    mattermost_username: Annotated[str, Field(min_length=1, max_length=128)]

    @field_validator("mattermost_user_id")
    @classmethod
    def validate_external_user_id(cls, value: str) -> str:
        return validate_mattermost_id(value, name="mattermost_user_id")

    @field_validator("*")
    @classmethod
    def reject_unsafe_identity_text(cls, value: str) -> str:
        if value != value.strip() or "\0" in value:
            raise ValueError("identity text is invalid")
        return value


class DemoBindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    user_id: uuid.UUID
    mattermost_user_id: str
    mattermost_username: str
    bound_at: datetime
