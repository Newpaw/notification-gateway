from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from notification_gateway.database import Database
from notification_gateway.models import NotificationRequest, NotificationResult
from notification_gateway.ntfy import NtfyClient


class UnknownChannelError(ValueError):
    pass


class NotificationService:
    def __init__(self, channels: dict[str, str], database: Database, ntfy: NtfyClient) -> None:
        self.channels = channels
        self.database = database
        self.ntfy = ntfy
        self._publish_lock = asyncio.Lock()

    async def send(
        self, request: NotificationRequest, *, source: str, actor: str
    ) -> NotificationResult:
        topic = self.channels.get(request.channel)
        if topic is None:
            allowed = ", ".join(sorted(self.channels))
            raise UnknownChannelError(f"unknown channel; allowed channels: {allowed}")

        async with self._publish_lock:
            if request.idempotency_key:
                existing = await self.database.find_by_idempotency_key(request.idempotency_key)
                if existing:
                    return existing

            ntfy_id = await self.ntfy.publish(topic, request)
            return await self.database.save(
                notification_id=str(uuid4()),
                request=request,
                topic=topic,
                source=source,
                actor=actor,
                ntfy_message_id=ntfy_id,
                created_at=datetime.now(UTC).isoformat(),
            )
