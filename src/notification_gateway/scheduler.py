from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from notification_gateway.database import Database
from notification_gateway.models import (
    NotificationRequest,
    ScheduledNotification,
    ScheduledStatus,
    ScheduleNotificationRequest,
)
from notification_gateway.service import NotificationService, UnknownChannelError

logger = logging.getLogger(__name__)


class ScheduleService:
    def __init__(self, database: Database, notifications: NotificationService) -> None:
        self.database = database
        self.notifications = notifications

    async def schedule(
        self,
        request: ScheduleNotificationRequest,
        *,
        actor: str,
        now: datetime | None = None,
    ) -> ScheduledNotification:
        if request.channel not in self.notifications.channels:
            allowed = ", ".join(sorted(self.notifications.channels))
            raise UnknownChannelError(f"unknown channel; allowed channels: {allowed}")
        created_at = now or datetime.now(UTC)
        if request.send_at <= created_at:
            raise ValueError("send_at must be in the future")
        return await self.database.create_schedule(
            schedule_id=str(uuid4()),
            request=request,
            actor=actor,
            created_at=created_at,
        )

    async def list(
        self,
        *,
        actor: str,
        status: ScheduledStatus | None = None,
        limit: int = 20,
    ) -> list[ScheduledNotification]:
        return await self.database.list_schedules(actor=actor, status=status, limit=limit)

    async def cancel(self, schedule_id: str, *, actor: str) -> bool:
        return await self.database.cancel_schedule(schedule_id, actor)


class NotificationScheduler:
    def __init__(
        self,
        database: Database,
        notifications: NotificationService,
        *,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 5,
    ) -> None:
        self.database = database
        self.notifications = notifications
        self.poll_interval_seconds = poll_interval_seconds
        self.max_attempts = max_attempts
        self._stop_event = asyncio.Event()

    async def run_once(self, now: datetime | None = None) -> bool:
        current_time = now or datetime.now(UTC)
        claimed = await self.database.claim_due(current_time)
        if claimed is None:
            return False
        scheduled, actor = claimed
        try:
            result = await self.notifications.send(
                NotificationRequest(
                    channel=scheduled.channel,
                    title=scheduled.title,
                    message=scheduled.message,
                    priority=scheduled.priority,
                    tags=scheduled.tags,
                    click_url=scheduled.click_url,
                    idempotency_key=f"scheduled:{scheduled.id}",
                ),
                source="scheduler",
                actor=actor,
            )
            await self.database.mark_schedule_sent(
                scheduled.id,
                result.ntfy_message_id,
                datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retry_at = None
            if scheduled.attempts < self.max_attempts:
                delay = min(30 * (2 ** (scheduled.attempts - 1)), 900)
                retry_at = current_time + timedelta(seconds=delay)
            await self.database.mark_schedule_failed(
                scheduled.id,
                error=str(exc),
                retry_at=retry_at,
            )
            logger.exception("Scheduled notification %s failed", scheduled.id)
        return True

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notification scheduler iteration failed")
                processed = False
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()
