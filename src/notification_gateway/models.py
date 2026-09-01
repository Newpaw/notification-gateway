from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Priority(StrEnum):
    MIN = "min"
    LOW = "low"
    DEFAULT = "default"
    HIGH = "high"
    MAX = "max"


class NotificationRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4096)
    priority: Priority = Priority.DEFAULT
    tags: list[str] = Field(default_factory=list, max_length=8)
    click_url: HttpUrl | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        if any(not tag or len(tag) > 64 for tag in tags):
            raise ValueError("tags must contain 1-64 character values")
        return tags


class NotificationResult(BaseModel):
    id: str
    channel: str
    topic: str
    ntfy_message_id: str | None = None
    duplicate: bool = False
    created_at: str
