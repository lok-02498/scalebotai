from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.user import Password, UserResponse


class LoginRequest(BaseModel):
    username: str = Field(
        min_length=3, max_length=255, description="Username or email", examples=["alice"]
    )
    password: str = Field(min_length=1, max_length=128, examples=["Str0ngPassw0rd"])


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds")
    user: UserResponse


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None, description="Also revoke this refresh token (recommended)"
    )


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: Password
