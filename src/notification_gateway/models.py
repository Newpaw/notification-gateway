from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Priority(StrEnum):
    MIN = "min"
    LOW = "low"
    DEFAULT = "default"
    HIGH = "high"
    MAX = "max"


class ScheduledStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


class ScheduleNotificationRequest(NotificationRequest):
    send_at: datetime

    @field_validator("send_at")
    @classmethod
    def validate_send_at(cls, send_at: datetime) -> datetime:
        if send_at.tzinfo is None or send_at.utcoffset() is None:
            raise ValueError("send_at must include a timezone offset")
        return send_at.astimezone(UTC)


class NotificationResult(BaseModel):
    id: str
    channel: str
    topic: str
    ntfy_message_id: str | None = None
    duplicate: bool = False
    created_at: str


class ScheduledNotification(BaseModel):
    id: str
    channel: str
    title: str
    message: str
    priority: Priority
    tags: list[str] = Field(default_factory=list)
    click_url: HttpUrl | None = None
    send_at: datetime
    status: ScheduledStatus
    attempts: int = 0
    ntfy_message_id: str | None = None
    last_error: str | None = None
    created_at: datetime
    sent_at: datetime | None = None
