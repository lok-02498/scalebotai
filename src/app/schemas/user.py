import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

USERNAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"


def validate_password_strength(value: str) -> str:
    if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        raise ValueError("password must contain at least one letter and one digit")
    return value


Password = Annotated[
    str,
    Field(min_length=8, max_length=128, examples=["Str0ngPassw0rd"]),
    AfterValidator(validate_password_strength),
]


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=USERNAME_PATTERN, examples=["alice"])
    email: EmailStr = Field(examples=["alice@scalebotai.dev"])
    password: Password
    full_name: str | None = Field(default=None, max_length=100, examples=["Alice Sharma"])

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        return value.lower()


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    # Plain str on output: stored addresses were validated on input, and the
    # bootstrap admin may use an internal domain such as scalebotai.local.
    email: str
    full_name: str | None
    role: Literal["user", "admin"]
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class UserUpdateRequest(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=100)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str | None) -> str | None:
        return value.lower() if value else value

    @model_validator(mode="after")
    def _at_least_one(self) -> "UserUpdateRequest":
        if self.email is None and self.full_name is None:
            raise ValueError("provide at least one of: email, full_name")
        return self


class UserStatsResponse(BaseModel):
    total_conversations: int
    total_messages: int
    feedback_given: int
    first_chat_at: datetime | None
    last_chat_at: datetime | None
