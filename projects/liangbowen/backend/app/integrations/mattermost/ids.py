from __future__ import annotations

import re

MAX_MATTERMOST_ID_CHARACTERS = 128
MATTERMOST_ID_SQL = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
_MATTERMOST_ID = re.compile(rf"\A{MATTERMOST_ID_SQL[1:-1]}\Z")


def validate_mattermost_id(value: str, *, name: str = "Mattermost identifier") -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if _MATTERMOST_ID.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value
