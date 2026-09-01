from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from notification_gateway.models import NotificationRequest, NotificationResult


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    channel TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    click_url TEXT,
                    source TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    ntfy_message_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def find_by_idempotency_key(self, key: str) -> NotificationResult | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, channel, topic, ntfy_message_id, created_at "
                "FROM notifications WHERE idempotency_key = ?",
                (key,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return NotificationResult(**dict(row), duplicate=True)

    async def save(
        self,
        *,
        notification_id: str,
        request: NotificationRequest,
        topic: str,
        source: str,
        actor: str,
        ntfy_message_id: str | None,
        created_at: str,
    ) -> NotificationResult:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO notifications (
                    id, idempotency_key, channel, topic, title, message, priority,
                    tags_json, click_url, source, actor, ntfy_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    request.idempotency_key,
                    request.channel,
                    topic,
                    request.title,
                    request.message,
                    request.priority.value,
                    json.dumps(request.tags),
                    str(request.click_url) if request.click_url else None,
                    source,
                    actor,
                    ntfy_message_id,
                    created_at,
                ),
            )
            await db.commit()
        return NotificationResult(
            id=notification_id,
            channel=request.channel,
            topic=topic,
            ntfy_message_id=ntfy_message_id,
            created_at=created_at,
        )
