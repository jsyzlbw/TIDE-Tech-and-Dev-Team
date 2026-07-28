import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings
from app.db.types import Role

JWT_ALGORITHM = "HS256"
MIN_PASSWORD_LENGTH = 1
MAX_PASSWORD_LENGTH = 1024


def _password_material(password: str) -> bytes:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError("password must be between 1 and 1024 characters")
    return sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_material(password), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(_password_material(password), password_hash.encode())


def create_access_token(subject: uuid.UUID, role: Role) -> str:
    settings = get_settings()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.jwt_exp_minutes)
    claims = {
        "sub": str(subject),
        "role": role.value,
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    claims = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[JWT_ALGORITHM],
        options={"require": ["sub", "iat", "exp"], "verify_iat": True},
    )
    if not isinstance(claims["sub"], str):
        raise jwt.DecodeError("Subject claim (sub) must be a string")
    if isinstance(claims["iat"], bool) or not isinstance(claims["iat"], int):
        raise jwt.InvalidIssuedAtError("Issued At claim (iat) must be an integer")
    if isinstance(claims["exp"], bool) or not isinstance(claims["exp"], int):
        raise jwt.DecodeError("Expiration Time claim (exp) must be an integer")
    return claims
