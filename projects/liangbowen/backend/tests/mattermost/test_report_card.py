from __future__ import annotations

import json
import uuid

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.integrations.mattermost.card import ReportCardInput, render_report_card
from app.integrations.mattermost.urls import MAX_CARD_PROPS_BYTES, MAX_HTTP_URL_BYTES


def _input(**changes: object) -> ReportCardInput:
    values: dict[str, object] = {
        "outbox_id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
        "report_id": uuid.UUID("00000000-0000-4000-8000-000000000002"),
        "recipient_user_id": "user0000000000000000000001",
        "channel_id": "chan0000000000000000000001",
        "assignment_code": "HW-1001",
        "assignment_title": "图论 *基础* [练习]",
        "score": 82,
        "grade": "B",
        "completeness": "覆盖主要要求",
        "major_issues": ("遗漏 `复杂度` 说明",),
        "suggestions": ("补充边界条件",),
        "limitations": ("AI 可能误判",),
        "action_url": "https://api.example.test/api/v1/integrations/mattermost/actions",
        "console_url": "https://app.example.test",
        "action_secret": SecretStr("action-secret-value"),
    }
    values.update(changes)
    return ReportCardInput(**values)


def test_report_card_is_deterministic_safe_and_has_exactly_three_actions() -> None:
    first = render_report_card(_input())
    second = render_report_card(_input())
    assert first == second
    assert first.message == "AI 基础评估 · 待教师审核"
    assert first.props["a8_outbox_id"] == "00000000-0000-4000-8000-000000000001"
    attachment = first.props["attachments"][0]
    assert "\\*基础\\*" in attachment["title"]
    actions = attachment["actions"]
    assert [action["id"] for action in actions] == ["confirm", "reevaluate", "openreport"]
    assert len(actions) == 3
    for action in actions:
        context = action["integration"]["context"]
        assert set(context) == {
            "action",
            "report_id",
            "delivery_id",
            "expected_user_id",
            "expected_channel_id",
            "signature",
        }
        assert action["integration"]["url"].startswith("https://api.example.test/")


def test_report_link_cannot_escape_configured_console_origin() -> None:
    card = render_report_card(_input())
    assert "https://app.example.test/teacher/reports/" in card.props["attachments"][0]["text"]
    assert "javascript:" not in json.dumps(card.props)


@pytest.mark.parametrize("field", ["action_url", "console_url"])
@pytest.mark.parametrize("control", ["\x01", "\x7f", "\x85"])
def test_report_card_rejects_control_characters_in_urls(field: str, control: str) -> None:
    value = (
        f"https://api.example.test/{control}private/api/v1/integrations/mattermost/actions"
        if field == "action_url"
        else f"https://app.example.test/{control}private"
    )
    with pytest.raises(ValueError) as caught:
        render_report_card(_input(**{field: value}))
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("field", ["action_url", "console_url"])
@pytest.mark.parametrize("case", ["userinfo", "query", "fragment", "length"])
def test_report_card_rejects_every_unsafe_url_component_without_echo(
    field: str,
    case: str,
) -> None:
    suffix = "/api/v1/integrations/mattermost/actions" if field == "action_url" else "/console"
    values = {
        "userinfo": f"https://do-not-echo:password@example.test{suffix}",
        "query": f"https://example.test{suffix}?token=do-not-echo",
        "fragment": f"https://example.test{suffix}#do-not-echo",
        "length": f"https://example.test/do-not-echo{'x' * 2_048}{suffix}",
    }
    with pytest.raises((ValueError, ValidationError)) as caught:
        render_report_card(_input(**{field: values[case]}))
    assert "do-not-echo" not in str(caught.value)


def test_report_card_http_policy_must_be_explicitly_enabled() -> None:
    urls = {
        "action_url": "http://api.test/api/v1/integrations/mattermost/actions",
        "console_url": "http://console.test",
    }
    with pytest.raises(ValueError):
        render_report_card(_input(**urls))
    card = render_report_card(_input(**urls, allow_insecure_http=True))
    assert card.props["attachments"][0]["actions"][0]["integration"]["url"].startswith(
        "http://api.test/"
    )


def test_settings_max_ascii_urls_render_within_required_card_budget() -> None:
    prefix = "https://example.test/"
    action_suffix = "/api/v1/integrations/mattermost/actions"
    action_url = (
        prefix + "a" * (MAX_HTTP_URL_BYTES - len(prefix) - len(action_suffix)) + action_suffix
    )
    console_url = prefix + "c" * (MAX_HTTP_URL_BYTES - len(prefix))
    settings = Settings(
        _env_file=None,
        mattermost_action_url=action_url,
        web_console_url=console_url,
    )
    card = render_report_card(
        _input(
            recipient_user_id="u" * 128,
            channel_id="c" * 128,
            assignment_code="\\" * 32,
            assignment_title="🚀" * 500,
            completeness="🚀" * 10_000,
            major_issues=("🚀" * 10_000,),
            suggestions=("🚀" * 10_000,),
            limitations=("🚀" * 10_000,),
            action_url=settings.mattermost_action_url,
            console_url=settings.web_console_url,
        )
    )
    encoded = json.dumps(card.props, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= MAX_CARD_PROPS_BYTES
    assert len(card.props["attachments"][0]["actions"]) == 3


def test_card_bounds_large_unicode_without_splitting_joined_emoji() -> None:
    joined = "👩🏽‍💻"
    card = render_report_card(
        _input(
            completeness=joined * 10_000,
            major_issues=(joined * 10_000,),
            suggestions=(joined * 10_000,),
            limitations=(joined * 10_000,),
        )
    )
    encoded = json.dumps(card.props, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 32 * 1024
    assert len(card.message) <= 16_383
    rendered = card.props["attachments"][0]["text"]
    assert not rendered.endswith(("\u200d", "🏽"))


def test_card_never_contains_secret_or_submission_material() -> None:
    card = render_report_card(_input())
    serialized = json.dumps(card.props, ensure_ascii=False)
    assert "action-secret-value" not in serialized
    assert set(card.model_dump()) == {"message", "props"}


def test_card_budget_removes_whole_optional_sections_but_keeps_required_contract(
    monkeypatch,
) -> None:
    import app.integrations.mattermost.card as card_module

    monkeypatch.setattr(card_module, "MAX_PROPS_BYTES", 5_000)
    huge = tuple("\\[]*`" * 500 for _ in range(5))
    card = render_report_card(
        _input(
            completeness="\\[]*`" * 2_000,
            major_issues=huge,
            suggestions=huge,
            limitations=huge,
        )
    )
    attachment = card.props["attachments"][0]
    text = attachment["text"]
    assert "**分数 / 等级：** 82 / B" in text
    assert "**能力边界：** AI 基础评估，仅供教师审核。" in text
    assert "[在系统中打开完整报告](https://app.example.test/teacher/reports/" in text
    assert len(attachment["actions"]) == 3
    assert len(json.dumps(card.props, ensure_ascii=False, separators=(",", ":")).encode()) <= 5_000
    for heading in ("**完整性：**", "**主要问题：**", "**修改建议：**", "**评估限制：**"):
        assert text.count(heading) in {0, 1}


def test_card_escapes_single_large_codepoints_without_broken_markdown() -> None:
    card = render_report_card(
        _input(
            assignment_title="[]()\\*`" * 50 + "\U0010ffff" * 100,
            completeness="\U0010ffff" * 10_000,
        )
    )
    attachment = card.props["attachments"][0]
    assert attachment["text"].count("[在系统中打开完整报告](") == 1
    assert not attachment["title"].endswith("\\")
