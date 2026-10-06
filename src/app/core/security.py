"""Password hashing (bcrypt) and JWT creation/validation."""

import base64
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings

TokenType = Literal["access", "refresh"]
ISSUER = "scalebotai"


def _prehash(password: str) -> bytes:
    # bcrypt only reads the first 72 bytes. Pre-hashing with SHA-256 lets the
    # whole password count while staying within that limit (same idea as
    # passlib's bcrypt_sha256). base64 avoids NUL bytes in the digest.
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=rounds)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except ValueError:
        return False


# bcrypt is CPU-bound (~250 ms at cost 12); run it off the event loop so one
# login cannot stall every other request handled by this process.
async def hash_password_async(password: str, rounds: int = 12) -> str:
    return await run_in_threadpool(hash_password, password, rounds)


async def verify_password_async(password: str, password_hash: str) -> bool:
    return await run_in_threadpool(verify_password, password, password_hash)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


def create_token(
    settings: Settings, *, subject: str, role: str, token_type: TokenType
) -> IssuedToken:
    now = datetime.now(UTC)
    if token_type == "access":
        expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    else:
        expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    jti = uuid.uuid4().hex
    payload = {
        "sub": subject,
        "role": role,
        "type": token_type,
        "jti": jti,
        "iss": ISSUER,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


class InvalidTokenError(Exception):
    pass


def decode_token(settings: Settings, token: str, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("Token has expired") from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid token") from exc
    if payload.get("type") != expected_type:
        raise InvalidTokenError(f"Expected a {expected_type} token")
    return payload


def seconds_until(timestamp: int | float) -> int:
    return max(int(timestamp - datetime.now(UTC).timestamp()), 1)
