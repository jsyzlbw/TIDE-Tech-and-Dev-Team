from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.mattermost.parser import CommandParseError, parse_command


def test_publish_preserves_quoted_unicode_and_converts_shanghai_due_to_utc() -> None:
    command = parse_command(
        'publish --title "最短路径" --due "2026-07-26 18:00" '
        '--question "解释 Dijkstra 算法" --notes "允许伪代码"'
    )

    assert command.name == "publish"
    assert command.positionals == ()
    assert command.arguments == {
        "title": "最短路径",
        "due_at": datetime(2026, 7, 26, 10, 0, tzinfo=UTC),
        "question": "解释 Dijkstra 算法",
        "notes": "允许伪代码",
    }


@pytest.mark.parametrize(
    ("raw", "name", "positionals", "arguments"),
    [
        ("help", "help", (), {}),
        ("list", "list", (), {}),
        ("show HW-0001", "show", ("HW-0001",), {}),
        (
            'submit HW-0001 --text "答案中有 空格 和 Unicode ✓"',
            "submit",
            ("HW-0001",),
            {"text": "答案中有 空格 和 Unicode ✓"},
        ),
        ("summary HW-9999999999", "summary", ("HW-9999999999",), {}),
        ("evaluate HW-0001", "evaluate", ("HW-0001",), {}),
    ],
)
def test_command_happy_paths(raw, name, positionals, arguments) -> None:
    command = parse_command(raw)

    assert command.name == name
    assert command.positionals == positionals
    assert command.arguments == arguments


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "command is required"),
        ("unknown", "unknown command"),
        ("help extra", "help accepts no arguments"),
        ("list --limit 2", "unknown flag"),
        ("show", "show requires one assignment code"),
        ("show HW-0001 extra", "show requires one assignment code"),
        ("show hw-0001", "assignment code must match"),
        ("show HW-001", "assignment code must match"),
        ("show HW-00000000000", "assignment code must match"),
        ("submit HW-0001", "--text is required"),
        ("submit HW-0001 --text", "--text requires a value"),
        ("submit HW-0001 --text a --text b", "--text may only be provided once"),
        ("submit HW-0001 --unknown a", "unknown flag"),
        ("submit HW-0001 unexpected --text a", "requires one assignment code"),
        (
            'publish --title "T" --due "2026-07-26 18:00"',
            "--question is required",
        ),
        (
            ('publish --title "T" --title "T2" --due "2026-07-26 18:00" --question "Q"'),
            "--title may only be provided once",
        ),
        (
            'publish --title "T" --due "2026-07-26 18:00+08:00" --question "Q"',
            "due must use YYYY-MM-DD HH:MM in Asia/Shanghai",
        ),
        (
            'publish --title "T" --due "2026-02-30 18:00" --question "Q"',
            "due must use YYYY-MM-DD HH:MM in Asia/Shanghai",
        ),
        (
            'publish --title "unterminated --due "2026-07-26 18:00" --question Q',
            "command quoting is invalid",
        ),
        ("show HW-0001\0", "command contains a NUL byte"),
    ],
)
def test_invalid_commands_have_actionable_bounded_errors(raw: str, message: str) -> None:
    with pytest.raises(CommandParseError, match=message) as raised:
        parse_command(raw)

    assert 1 <= len(raised.value.public_message) <= 500
    assert "/hw" in raised.value.public_message


@pytest.mark.parametrize(
    ("field", "size"),
    [
        ("title", 200),
        ("question", 50_000),
        ("notes", 10_000),
    ],
)
def test_publish_accepts_exact_text_limits(field: str, size: int) -> None:
    values = {
        "title": "T",
        "due": "2026-07-26 18:00",
        "question": "Q",
        "notes": "N",
    }
    values[field] = "A" * size
    raw = (
        f'publish --title "{values["title"]}" --due "{values["due"]}" '
        f'--question "{values["question"]}" --notes "{values["notes"]}"'
    )

    assert parse_command(raw).arguments[field if field != "due" else "due_at"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            f'publish --title "{"T" * 201}" --due "2026-07-26 18:00" --question Q',
            "title exceeds 200 characters",
        ),
        (
            f'publish --title T --due "2026-07-26 18:00" --question "{"Q" * 50_001}"',
            "question exceeds 50000 characters",
        ),
        (
            (f'publish --title T --due "2026-07-26 18:00" --question Q --notes "{"N" * 10_001}"'),
            "notes exceeds 10000 characters",
        ),
        (
            f'submit HW-0001 --text "{"A" * 50_001}"',
            "text exceeds 50000 characters",
        ),
        (
            f"help {'x' * (60 * 1024)}",
            "command text exceeds 61440 bytes",
        ),
        (
            'publish --title "\u0001" --due "2026-07-26 18:00" --question Q',
            "title must contain visible text",
        ),
    ],
)
def test_command_bounds_are_enforced(raw: str, message: str) -> None:
    with pytest.raises(CommandParseError, match=message):
        parse_command(raw)
