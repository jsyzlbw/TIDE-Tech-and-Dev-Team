from __future__ import annotations

import re
import shlex
import unicodedata
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.integrations.mattermost.schemas import MattermostCommand, MattermostCommandName

MAX_COMMAND_BYTES = 60 * 1024
ASSIGNMENT_CODE_PATTERN = re.compile(r"HW-[0-9]{4,10}\Z")
SHANGHAI = ZoneInfo("Asia/Shanghai")
COMMAND_NAMES = frozenset({"help", "publish", "list", "show", "submit", "summary", "evaluate"})
USAGE = {
    "help": "/hw help",
    "publish": (
        '/hw publish --title "标题" --due "YYYY-MM-DD HH:MM" --question "题目" [--notes "说明"]'
    ),
    "list": "/hw list",
    "show": "/hw show HW-0001",
    "submit": '/hw submit HW-0001 --text "答案"',
    "summary": "/hw summary HW-0001",
    "evaluate": "/hw evaluate HW-0001",
}
FLAG_LIMITS = {
    "title": 200,
    "due": 64,
    "question": 50_000,
    "notes": 10_000,
    "text": 50_000,
}
FLAGS_BY_COMMAND = {
    "publish": frozenset({"title", "due", "question", "notes"}),
    "submit": frozenset({"text"}),
}


class CommandParseError(ValueError):
    def __init__(self, reason: str, *, command: str | None = None) -> None:
        usage = USAGE.get(command or "", "/hw help")
        self.public_message = f"{reason}. 用法：{usage}"
        super().__init__(self.public_message)


def _contains_visible_text(value: str) -> bool:
    return any(
        not character.isspace() and not unicodedata.category(character).startswith("C")
        for character in value
    )


def _parse_tokens(text: str) -> list[str]:
    if not isinstance(text, str):
        raise TypeError("command text must be a string")
    if "\0" in text:
        raise CommandParseError("command contains a NUL byte")
    if len(text.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise CommandParseError(f"command text exceeds {MAX_COMMAND_BYTES} bytes")
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        raise CommandParseError("command quoting is invalid") from None


def _parse_parts(tokens: list[str], command: str) -> tuple[tuple[str, ...], dict[str, str]]:
    allowed_flags = FLAGS_BY_COMMAND.get(command, frozenset())
    positionals: list[str] = []
    arguments: dict[str, str] = {}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            positionals.append(token)
            index += 1
            continue
        flag = token[2:]
        if flag not in allowed_flags:
            raise CommandParseError(f"unknown flag --{flag}", command=command)
        if flag in arguments:
            raise CommandParseError(f"--{flag} may only be provided once", command=command)
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            raise CommandParseError(f"--{flag} requires a value", command=command)
        value = tokens[index + 1]
        limit = FLAG_LIMITS[flag]
        if len(value) > limit:
            raise CommandParseError(
                f"{flag} exceeds {limit} characters",
                command=command,
            )
        arguments[flag] = value
        index += 2
    return tuple(positionals), arguments


def _require_assignment_code(positionals: tuple[str, ...], command: str) -> None:
    if len(positionals) != 1:
        raise CommandParseError(
            f"{command} requires one assignment code",
            command=command,
        )
    if ASSIGNMENT_CODE_PATTERN.fullmatch(positionals[0]) is None:
        raise CommandParseError("assignment code must match HW-0001", command=command)


def _parse_publish(arguments: dict[str, str]) -> dict[str, str | datetime]:
    for required in ("title", "due", "question"):
        if required not in arguments:
            raise CommandParseError(f"--{required} is required", command="publish")
    for required in ("title", "question"):
        if not _contains_visible_text(arguments[required]):
            raise CommandParseError(
                f"{required} must contain visible text",
                command="publish",
            )
    try:
        due_at = datetime.strptime(arguments["due"], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
    except ValueError:
        raise CommandParseError(
            "due must use YYYY-MM-DD HH:MM in Asia/Shanghai",
            command="publish",
        ) from None
    return {
        "title": arguments["title"],
        "due_at": due_at.astimezone(UTC),
        "question": arguments["question"],
        "notes": arguments.get("notes", ""),
    }


def parse_command(text: str) -> MattermostCommand:
    tokens = _parse_tokens(text)
    if not tokens:
        raise CommandParseError("command is required")
    raw_name = tokens[0]
    if raw_name not in COMMAND_NAMES:
        raise CommandParseError("unknown command")
    name: MattermostCommandName = raw_name  # type: ignore[assignment]
    positionals, raw_arguments = _parse_parts(tokens, name)

    if name in {"help", "list"}:
        if positionals or raw_arguments:
            raise CommandParseError(f"{name} accepts no arguments", command=name)
        arguments: dict[str, str | datetime] = {}
    elif name == "publish":
        if positionals:
            raise CommandParseError("publish accepts no positionals", command=name)
        arguments = _parse_publish(raw_arguments)
    elif name == "submit":
        _require_assignment_code(positionals, name)
        if "text" not in raw_arguments:
            raise CommandParseError("--text is required", command=name)
        if not _contains_visible_text(raw_arguments["text"]):
            raise CommandParseError("text must contain visible text", command=name)
        arguments = {"text": raw_arguments["text"]}
    else:
        _require_assignment_code(positionals, name)
        arguments = {}

    return MattermostCommand(
        name=name,
        positionals=positionals,
        arguments=arguments,
    )
