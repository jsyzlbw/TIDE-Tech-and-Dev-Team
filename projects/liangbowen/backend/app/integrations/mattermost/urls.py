from __future__ import annotations

import json
import unicodedata
from urllib.parse import urlsplit, urlunsplit

MAX_HTTP_URL_BYTES = 2_048
MAX_CARD_PROPS_BYTES = 32 * 1_024
REQUIRED_CARD_NON_URL_BUDGET_BYTES = 24 * 1_024
MAX_REQUIRED_CARD_URL_JSON_BYTES = MAX_CARD_PROPS_BYTES - REQUIRED_CARD_NON_URL_BUDGET_BYTES


def validate_http_url(
    value: str,
    *,
    name: str = "URL",
    allow_insecure_http: bool = False,
) -> str:
    """Validate and canonically trim an HTTP(S) URL without echoing its value."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if type(allow_insecure_http) is not bool:
        raise TypeError("allow_insecure_http must be a boolean")
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{name} is invalid") from None
    if (
        not value
        or encoded_size > MAX_HTTP_URL_BYTES
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ValueError(f"{name} is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} is invalid")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ValueError(f"{name} is invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def validate_required_card_urls(
    action_url: str,
    console_url: str,
    *,
    allow_insecure_http: bool = False,
) -> tuple[str, str]:
    action = validate_http_url(
        action_url,
        name="mattermost_action_url",
        allow_insecure_http=allow_insecure_http,
    )
    console = validate_http_url(
        console_url,
        name="web_console_url",
        allow_insecure_http=allow_insecure_http,
    )
    action_json_bytes = len(json.dumps(action, ensure_ascii=False).encode("utf-8")) - 2
    console_json_bytes = len(json.dumps(console, ensure_ascii=False).encode("utf-8")) - 2
    if 3 * action_json_bytes + console_json_bytes > MAX_REQUIRED_CARD_URL_JSON_BYTES:
        raise ValueError("Mattermost report card URLs are invalid")
    return action, console
