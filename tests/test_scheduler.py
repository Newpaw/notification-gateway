from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from notification_gateway.database import Database
from notification_gateway.models import (
    NotificationRequest,
    ScheduledStatus,
    ScheduleNotificationRequest,
)
from notification_gateway.ntfy import NtfyClient
from notification_gateway.scheduler import NotificationScheduler, ScheduleService
from notification_gateway.service import NotificationService


class FakeNtfy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, NotificationRequest]] = []

    async def publish(self, topic: str, request: NotificationRequest) -> str:
        self.calls.append((topic, request))
        return f"ntfy-{len(self.calls)}"


@pytest.mark.asyncio
async def test_scheduler_persists_and_sends_due_notification(settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    ntfy = FakeNtfy()
    notifications = NotificationService(
        settings.channels,
        database,
        cast(NtfyClient, ntfy),
    )
    schedules = ScheduleService(database, notifications)
    scheduler = NotificationScheduler(database, notifications)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    scheduled = await schedules.schedule(
        ScheduleNotificationRequest(
            channel="personal",
            title="Call Petr",
            message="Remember to call Petr.",
            send_at=now + timedelta(minutes=5),
        ),
        actor="newpaw",
        now=now,
    )

    assert await scheduler.run_once(now + timedelta(minutes=4)) is False
    assert await scheduler.run_once(now + timedelta(minutes=5)) is True
    listed = await schedules.list(actor="newpaw")

    assert scheduled.status is ScheduledStatus.PENDING
    assert listed[0].status is ScheduledStatus.SENT
    assert listed[0].ntfy_message_id == "ntfy-1"
    assert ntfy.calls[0][0] == "private-personal"
    assert ntfy.calls[0][1].idempotency_key == f"scheduled:{scheduled.id}"


@pytest.mark.asyncio
async def test_cancelled_schedule_is_not_sent(settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    ntfy = FakeNtfy()
    notifications = NotificationService(
        settings.channels,
        database,
        cast(NtfyClient, ntfy),
    )
    schedules = ScheduleService(database, notifications)
    scheduler = NotificationScheduler(database, notifications)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    scheduled = await schedules.schedule(
        ScheduleNotificationRequest(
            channel="urgent",
            title="Cancelled",
            message="This must not be sent.",
            send_at=now + timedelta(minutes=1),
        ),
        actor="newpaw",
        now=now,
    )

    assert await schedules.cancel(scheduled.id, actor="someone-else") is False
    assert await schedules.cancel(scheduled.id, actor="newpaw") is True
    assert await scheduler.run_once(now + timedelta(minutes=2)) is False
    assert ntfy.calls == []
    cancelled = await schedules.list(actor="newpaw", status=ScheduledStatus.CANCELLED)
    assert cancelled[0].id == scheduled.id


def test_schedule_requires_timezone() -> None:
    with pytest.raises(ValidationError, match="timezone offset"):
        ScheduleNotificationRequest(
            channel="personal",
            title="Naive time",
            message="No timezone.",
            send_at=datetime(2026, 9, 1, 12, 0),
        )
