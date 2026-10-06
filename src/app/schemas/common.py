import math
from typing import Any

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    message: str


class Page[T](BaseModel):
    items: list[T]
    page: int
    size: int
    total: int
    pages: int

    @classmethod
    def build(cls, items: list[T], *, page: int, size: int, total: int) -> "Page[T]":
        return cls(items=items, page=page, size=size, total=total, pages=math.ceil(total / size))


class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class ErrorDetail(BaseModel):
    code: str = Field(examples=["NOT_FOUND"])
    message: str
    details: list[Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# Reusable OpenAPI documentation for error responses.
def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    descriptions = {
        400: "Bad request",
        401: "Missing, invalid, expired or revoked token",
        403: "Authenticated but not allowed",
        404: "Resource not found",
        409: "Conflict with existing data",
        422: "Request validation failed",
        429: "Rate limit exceeded",
        503: "Dependency unavailable",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}
