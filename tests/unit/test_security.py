from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Settings
from app.core.security import (
    InvalidTokenError,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)

SETTINGS = Settings(_env_file=None, jwt_secret_key="unit-test-secret-key-with-32-plus-chars")


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("Secr3tPassword", rounds=4)
    assert hashed.startswith("$2b$04$")
    assert verify_password("Secr3tPassword", hashed)
    assert not verify_password("wrong-password1", hashed)


def test_long_passwords_are_fully_significant() -> None:
    # Plain bcrypt ignores everything after 72 bytes; the SHA-256 pre-hash must not.
    base = "a1" * 40  # 80 bytes
    hashed = hash_password(base + "X", rounds=4)
    assert not verify_password(base + "Y", hashed)


def test_verify_rejects_malformed_hash() -> None:
    assert verify_password("anything1", "not-a-bcrypt-hash") is False


def test_token_roundtrip_and_type_check() -> None:
    issued = create_token(SETTINGS, subject="user-1", role="user", token_type="access")
    payload = decode_token(SETTINGS, issued.token, "access")
    assert payload["sub"] == "user-1"
    assert payload["jti"] == issued.jti
    with pytest.raises(InvalidTokenError):
        decode_token(SETTINGS, issued.token, "refresh")


def test_expired_and_tampered_tokens_are_rejected() -> None:
    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": "u",
            "type": "access",
            "jti": "x",
            "iss": "scalebotai",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        SETTINGS.jwt_secret_key,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError, match="expired"):
        decode_token(SETTINGS, expired, "access")

    forged = create_token(
        Settings(_env_file=None, jwt_secret_key="another-secret-key-of-sufficient-len"),
        subject="u",
        role="admin",
        token_type="access",
    )
    with pytest.raises(InvalidTokenError):
        decode_token(SETTINGS, forged.token, "access")


def test_settings_build_dsns_with_escaped_passwords() -> None:
    settings = Settings(
        _env_file=None, postgres_password="p@ss:w/rd", redis_password="r@d!s", postgres_host="db"
    )
    assert (
        settings.sqlalchemy_database_url
        == "postgresql+asyncpg://scalebot:p%40ss%3Aw%2Frd@db:5432/scalebotai"
    )
    assert settings.redis_dsn == "redis://:r%40d%21s@redis:6379/0"
