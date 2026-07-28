from __future__ import annotations

import json
import uuid

import pytest
from pydantic import SecretStr

from app.integrations.mattermost.actions import (
    ActionAuthenticationError,
    ActionTransportError,
    action_request_hash,
    parse_and_verify_action,
    sign_action,
)

REPORT_ID = uuid.UUID("00000000-0000-4000-8000-000000000123")
DELIVERY_ID = uuid.UUID("00000000-0000-4000-8000-000000000456")
SECRET = SecretStr("action-secret-value")
USER_ID = "user0000000000000000000001"
CHANNEL_ID = "chan0000000000000000000001"


def _payload(*, action: str = "confirm", signature: str | None = None) -> bytes:
    context = {
        "action": action,
        "report_id": str(REPORT_ID),
        "delivery_id": str(DELIVERY_ID),
        "expected_user_id": USER_ID,
        "expected_channel_id": CHANNEL_ID,
        "signature": signature
        or sign_action(
            action,
            REPORT_ID,
            DELIVERY_ID,
            USER_ID,
            CHANNEL_ID,
            SECRET,
        ),
    }
    return json.dumps(
        {
            "user_id": USER_ID,
            "post_id": "post0000000000000000000001",
            "channel_id": CHANNEL_ID,
            "team_id": "",
            "context": context,
        }
    ).encode()


def test_signature_is_deterministic_length_framed_and_tamper_evident() -> None:
    first = sign_action("confirm", REPORT_ID, DELIVERY_ID, USER_ID, CHANNEL_ID, SECRET)
    assert first == sign_action("confirm", REPORT_ID, DELIVERY_ID, USER_ID, CHANNEL_ID, SECRET)
    assert len(first) == 64
    assert first != sign_action("reevaluate", REPORT_ID, DELIVERY_ID, USER_ID, CHANNEL_ID, SECRET)
    assert first != sign_action("confirm", uuid.uuid4(), DELIVERY_ID, USER_ID, CHANNEL_ID, SECRET)
    assert first != sign_action("confirm", REPORT_ID, uuid.uuid4(), USER_ID, CHANNEL_ID, SECRET)
    assert first != sign_action("confirm", REPORT_ID, DELIVERY_ID, "other-user", CHANNEL_ID, SECRET)
    assert first != sign_action("confirm", REPORT_ID, DELIVERY_ID, USER_ID, "other-channel", SECRET)


def test_action_request_hash_excludes_signature_but_frames_identity() -> None:
    request = parse_and_verify_action(_payload(), "application/json; charset=utf-8", SECRET)
    changed = request.model_copy(
        update={"context": request.context.model_copy(update={"signature": "0" * 64})}
    )
    assert action_request_hash(request) == action_request_hash(changed)
    assert action_request_hash(request) != action_request_hash(
        request.model_copy(update={"post_id": request.post_id + "x"})
    )


def test_parse_action_accepts_only_the_closed_shape() -> None:
    request = parse_and_verify_action(_payload(), "application/json", SECRET)
    assert request.context.action == "confirm"
    assert request.context.report_id == REPORT_ID


def test_parse_action_accepts_empty_team_id_for_direct_messages() -> None:
    payload = json.loads(_payload())
    payload["team_id"] = ""
    request = parse_and_verify_action(json.dumps(payload).encode(), "application/json", SECRET)
    assert request.team_id == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", "other-user"),
        ("channel_id", "other-channel"),
        ("team_id", "nonempty-team"),
    ],
)
def test_signed_direct_message_envelope_rejects_tampering(field: str, value: str) -> None:
    payload = json.loads(_payload())
    payload[field] = value
    with pytest.raises((ActionAuthenticationError, ActionTransportError)):
        parse_and_verify_action(json.dumps(payload).encode(), "application/json", SECRET)


@pytest.mark.parametrize(
    "body",
    [
        b'{"user_id":"a","user_id":"b"}',
        b'{"user_id": NaN}',
        b"{} trailing",
        b"[]",
        b'{"unknown":1}',
        b"\xff",
        b"{" + b" " * (16 * 1024),
    ],
)
def test_parse_action_rejects_malformed_or_oversized_json(body: bytes) -> None:
    with pytest.raises(ActionTransportError):
        parse_and_verify_action(body, "application/json", SECRET)


def test_parse_action_rejects_wrong_media_type() -> None:
    with pytest.raises(ActionTransportError) as caught:
        parse_and_verify_action(_payload(), "text/plain", SECRET)
    assert caught.value.status_code == 415


def test_signature_failure_has_one_safe_error() -> None:
    with pytest.raises(ActionAuthenticationError) as caught:
        parse_and_verify_action(_payload(signature="0" * 64), "application/json", SECRET)
    assert caught.value.status_code == 401
    assert "secret" not in str(caught.value).lower()


def test_signature_is_checked_after_shape_validation() -> None:
    malformed = json.loads(_payload())
    malformed["context"]["extra"] = "not allowed"
    with pytest.raises(ActionTransportError) as caught:
        parse_and_verify_action(json.dumps(malformed).encode(), "application/json", SECRET)
    assert caught.value.status_code == 400
