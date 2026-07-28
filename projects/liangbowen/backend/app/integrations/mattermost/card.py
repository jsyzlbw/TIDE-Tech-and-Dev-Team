from __future__ import annotations

import json
import unicodedata
import uuid
from typing import Annotated
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.integrations.mattermost.actions import MattermostAction, sign_action
from app.integrations.mattermost.ids import validate_mattermost_id
from app.integrations.mattermost.urls import (
    MAX_CARD_PROPS_BYTES,
    validate_required_card_urls,
)

MAX_MESSAGE_CHARACTERS = 16_383
MAX_PROPS_BYTES = MAX_CARD_PROPS_BYTES
_ACTION_LABELS: tuple[tuple[MattermostAction, str], ...] = (
    ("confirm", "确认评估"),
    ("reevaluate", "重新评估"),
    ("openreport", "打开报告"),
)


class ReportCardInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    outbox_id: uuid.UUID
    report_id: uuid.UUID
    recipient_user_id: Annotated[str, Field(min_length=1, max_length=128)]
    channel_id: Annotated[str, Field(min_length=1, max_length=128)]
    assignment_code: Annotated[str, Field(min_length=1, max_length=32)]
    assignment_title: Annotated[str, Field(min_length=1, max_length=500)]
    score: Annotated[int, Field(ge=0, le=100)]
    grade: Annotated[str, Field(pattern=r"^[ABCD]$")]
    completeness: str
    major_issues: tuple[str, ...]
    suggestions: tuple[str, ...]
    limitations: tuple[str, ...]
    action_url: Annotated[str, Field(min_length=1)]
    console_url: Annotated[str, Field(min_length=1)]
    action_secret: SecretStr
    allow_insecure_http: bool = False

    @field_validator("assignment_code", "assignment_title", "completeness")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("text contains a NUL byte")
        return value

    @field_validator("major_issues", "suggestions", "limitations")
    @classmethod
    def reject_nul_list(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any("\0" in value for value in values):
            raise ValueError("text contains a NUL byte")
        return values

    @field_validator("recipient_user_id", "channel_id")
    @classmethod
    def validate_mattermost_ids(cls, value: str) -> str:
        return validate_mattermost_id(value)


class MattermostPostCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)]
    props: dict[str, object]


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in "`*_{}[]()#+-.!|>":
        escaped = escaped.replace(character, "\\" + character)
    return escaped


def _is_cluster_extension(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.combining(character) != 0
        or codepoint in {0xFE0E, 0xFE0F}
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0100 <= codepoint <= 0xE01EF
    )


def _clusters(value: str) -> list[str]:
    clusters: list[str] = []
    current = ""
    join_next = False
    regional_count = 0
    for character in value:
        codepoint = ord(character)
        is_regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        if not current:
            current = character
            regional_count = 1 if is_regional else 0
        elif join_next or character == "\u200d" or _is_cluster_extension(character):
            current += character
            join_next = character == "\u200d"
        elif is_regional and regional_count == 1:
            current += character
            regional_count = 0
        else:
            clusters.append(current)
            current = character
            join_next = False
            regional_count = 1 if is_regional else 0
    if current:
        clusters.append(current.rstrip("\u200d"))
    return [cluster for cluster in clusters if cluster]


def _escape_and_truncate(value: str, byte_limit: int) -> str:
    output: list[str] = []
    used = 0
    truncated = False
    for cluster in _clusters(value):
        escaped = _escape_markdown(cluster)
        size = len(escaped.encode("utf-8"))
        if used + size > byte_limit - len("…".encode()):
            truncated = True
            break
        output.append(escaped)
        used += size
    if truncated:
        output.append("…")
    return "".join(output)


def _list_text(values: tuple[str, ...], *, empty: str, byte_limit: int = 4_000) -> str:
    if not values:
        return empty
    selected = values[:5]
    separators_size = len("；".encode()) * (len(selected) - 1)
    item_limit = max(4, (byte_limit - separators_size) // len(selected))
    return "；".join(_escape_and_truncate(str(value), item_limit) for value in selected)


def _report_url(console_url: str, report_id: uuid.UUID) -> str:
    return f"{console_url}/teacher/reports/{quote(str(report_id), safe='')}"


def render_report_card(value: ReportCardInput) -> MattermostPostCard:
    if not isinstance(value, ReportCardInput):
        raise TypeError("value must be a ReportCardInput")
    action_url, console_url = validate_required_card_urls(
        value.action_url,
        value.console_url,
        allow_insecure_http=value.allow_insecure_http,
    )
    if not action_url.endswith("/api/v1/integrations/mattermost/actions"):
        raise ValueError("action_url must target the Mattermost action endpoint")
    report_url = _report_url(console_url, value.report_id)

    title = (
        f"{_escape_and_truncate(value.assignment_code, 128)} · "
        f"{_escape_and_truncate(value.assignment_title, 1_024)}"
    )
    required_sections = [
        f"**分数 / 等级：** {value.score} / {_escape_markdown(value.grade)}",
        "**能力边界：** AI 基础评估，仅供教师审核。",
        f"[在系统中打开完整报告]({report_url})",
    ]
    optional_sections = [
        f"**完整性：** {_escape_and_truncate(value.completeness, 4_000)}",
        f"**主要问题：** {_list_text(value.major_issues, empty='未发现明显问题')}",
        f"**修改建议：** {_list_text(value.suggestions, empty='暂无')}",
        f"**评估限制：** {_list_text(value.limitations, empty='暂无')}",
    ]
    actions: list[dict[str, object]] = []
    for action, label in _ACTION_LABELS:
        actions.append(
            {
                "id": action,
                "name": label,
                "type": "button",
                "integration": {
                    "url": action_url,
                    "context": {
                        "action": action,
                        "report_id": str(value.report_id),
                        "delivery_id": str(value.outbox_id),
                        "expected_user_id": value.recipient_user_id,
                        "expected_channel_id": value.channel_id,
                        "signature": sign_action(
                            action,
                            value.report_id,
                            value.outbox_id,
                            value.recipient_user_id,
                            value.channel_id,
                            value.action_secret,
                        ),
                    },
                },
            }
        )
    props: dict[str, object] = {
        "a8_outbox_id": str(value.outbox_id),
        "attachments": [
            {
                "title": title,
                "text": "\n\n".join(
                    (*required_sections[:2], *optional_sections, required_sections[2])
                ),
                "actions": actions,
            }
        ],
    }
    while (
        len(json.dumps(props, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PROPS_BYTES
    ):
        if not optional_sections:
            raise ValueError("required report card content cannot fit Mattermost bounds")
        optional_sections.pop()
        attachment = props["attachments"][0]  # type: ignore[index]
        attachment["text"] = "\n\n".join(  # type: ignore[index]
            (*required_sections[:2], *optional_sections, required_sections[2])
        )
    return MattermostPostCard(message="AI 基础评估 · 待教师审核", props=props)
