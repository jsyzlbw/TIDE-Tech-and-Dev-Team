from enum import StrEnum


class Role(StrEnum):
    TEACHER = "teacher"
    STUDENT = "student"
    ADMIN = "admin"


class AssignmentStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CLOSED = "closed"
    ARCHIVED = "archived"


class SubmissionContentType(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    CODE = "code"
    STRUCTURED = "structured"


class SubmissionSource(StrEnum):
    WEB = "web"
    MATTERMOST = "mattermost"


class SubmissionStatus(StrEnum):
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"


class Grade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


def grade_for_score(score: int) -> Grade:
    if isinstance(score, bool) or not isinstance(score, int):
        raise TypeError("score must be an integer")
    if not 0 <= score <= 100:
        raise ValueError("score must be between 0 and 100")
    if score >= 90:
        return Grade.A
    if score >= 75:
        return Grade.B
    if score >= 60:
        return Grade.C
    return Grade.D
