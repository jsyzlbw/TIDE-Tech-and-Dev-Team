import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.core.config import get_settings
from app.db.types import Role


@pytest.fixture(autouse=True)
def use_secure_test_jwt_secret(
    isolate_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()


def test_password_hash_round_trip() -> None:
    password = "correct horse battery staple"

    password_hash = hash_password(password)

    assert password_hash != password
    assert verify_password(password, password_hash) is True


def test_password_verification_rejects_wrong_password() -> None:
    password_hash = hash_password("the-right-password")

    assert verify_password("the-wrong-password", password_hash) is False


@pytest.mark.parametrize(
    "password",
    [
        "a" * 73,
        "密碼" * 100,
    ],
)
def test_long_password_hash_round_trip_without_bcrypt_truncation(password: str) -> None:
    password_hash = hash_password(password)

    assert verify_password(password, password_hash) is True
    assert verify_password(f"{password[:-1]}x", password_hash) is False


@pytest.mark.parametrize("password", ["", "x" * 1025])
def test_password_hash_rejects_passwords_outside_policy(password: str) -> None:
    with pytest.raises(ValueError, match="password must be between 1 and 1024 characters"):
        hash_password(password)


@pytest.mark.parametrize("password", ["", "x" * 1025])
def test_password_verification_rejects_passwords_outside_policy(password: str) -> None:
    password_hash = hash_password("valid-password")

    with pytest.raises(ValueError, match="password must be between 1 and 1024 characters"):
        verify_password(password, password_hash)


def test_jwt_round_trip_preserves_authentication_claims() -> None:
    user_id = uuid.uuid4()
    issued_after = int(datetime.now(UTC).timestamp())

    token = create_access_token(user_id, Role.TEACHER)
    claims = decode_access_token(token)

    issued_before = int(datetime.now(UTC).timestamp())
    assert claims["sub"] == str(user_id)
    assert claims["role"] == Role.TEACHER.value
    assert issued_after <= claims["iat"] <= issued_before
    assert claims["exp"] - claims["iat"] == 60 * 60


def test_jwt_decode_rejects_tampered_token() -> None:
    token = create_access_token(uuid.uuid4(), Role.STUDENT)
    header, payload, signature = token.split(".")
    replacement = "a" if signature[0] != "a" else "b"
    tampered_signature = f"{replacement}{signature[1:]}"

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(f"{header}.{payload}.{tampered_signature}")


def test_jwt_decode_rejects_expired_token() -> None:
    now = datetime.now(UTC)
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": Role.ADMIN.value,
            "iat": now - timedelta(minutes=10),
            "exp": now - timedelta(minutes=5),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


@pytest.mark.parametrize("missing_claim", ["sub", "iat", "exp"])
def test_jwt_decode_requires_authentication_claims(missing_claim: str) -> None:
    now = datetime.now(UTC)
    claims = {
        "sub": str(uuid.uuid4()),
        "role": Role.STUDENT.value,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    del claims[missing_claim]
    settings = get_settings()
    token = jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(token)


@pytest.mark.parametrize(
    ("claim", "invalid_value"),
    [
        ("sub", 123),
        ("iat", "123"),
        ("exp", "9999999999"),
    ],
)
def test_jwt_decode_rejects_malformed_claim_types(claim: str, invalid_value: object) -> None:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "role": Role.STUDENT.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    claims[claim] = invalid_value
    settings = get_settings()
    token = jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)


def test_jwt_decode_rejects_future_issued_at() -> None:
    now = datetime.now(UTC)
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": Role.STUDENT.value,
            "iat": now + timedelta(minutes=5),
            "exp": now + timedelta(minutes=10),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(jwt.ImmatureSignatureError):
        decode_access_token(token)
