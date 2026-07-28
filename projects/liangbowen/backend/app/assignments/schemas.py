import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.assignments.content import validate_assignment_content
from app.db.types import AssignmentStatus


class AssignmentCreate(BaseModel):
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ]
    question: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=50_000),
    ]
    notes: Annotated[str, Field(max_length=10_000)] = ""
    rubric: dict[str, Any]
    due_at: datetime

    @field_validator("title", "question")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        if not any(
            not character.isspace() and not unicodedata.category(character).startswith("C")
            for character in value
        ):
            raise ValueError("text must contain a visible character")
        return value

    @field_validator("due_at")
    @classmethod
    def require_timezone_aware_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("due_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_evaluable_content(self) -> "AssignmentCreate":
        validate_assignment_content(
            title=self.title,
            question=self.question,
            rubric=self.rubric,
        )
        return self


class AssignmentListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    due_at: datetime
    status: AssignmentStatus
    created_at: datetime
    published_at: datetime | None


class AssignmentStudentRead(AssignmentListItem):
    question: str
    notes: str


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    question: str
    notes: str
    rubric: dict[str, Any]
    due_at: datetime
    status: AssignmentStatus
    mattermost_channel_id: str | None
    created_by: uuid.UUID
    created_at: datetime
    published_at: datetime | None
