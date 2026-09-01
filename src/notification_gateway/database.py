from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from notification_gateway.models import (
    NotificationRequest,
    NotificationResult,
    ScheduledNotification,
    ScheduledStatus,
    ScheduleNotificationRequest,
)


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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_notifications (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    click_url TEXT,
                    send_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    ntfy_message_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    locked_at TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS scheduled_notifications_due_idx
                ON scheduled_notifications(status, next_attempt_at)
                """
            )
            await db.execute(
                """
                UPDATE scheduled_notifications
                SET status = ?, locked_at = NULL, next_attempt_at = ?
                WHERE status = ?
                """,
                (
                    ScheduledStatus.PENDING.value,
                    datetime.now(UTC).isoformat(),
                    ScheduledStatus.PROCESSING.value,
                ),
            )
            await db.commit()

    @staticmethod
    def _scheduled_from_row(row: aiosqlite.Row) -> ScheduledNotification:
        return ScheduledNotification(
            id=row["id"],
            channel=row["channel"],
            title=row["title"],
            message=row["message"],
            priority=row["priority"],
            tags=json.loads(row["tags_json"]),
            click_url=row["click_url"],
            send_at=row["send_at"],
            status=row["status"],
            attempts=row["attempts"],
            ntfy_message_id=row["ntfy_message_id"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            sent_at=row["sent_at"],
        )

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

    async def create_schedule(
        self,
        *,
        schedule_id: str,
        request: ScheduleNotificationRequest,
        actor: str,
        created_at: datetime,
    ) -> ScheduledNotification:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO scheduled_notifications (
                    id, actor, channel, title, message, priority, tags_json, click_url,
                    send_at, next_attempt_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    actor,
                    request.channel,
                    request.title,
                    request.message,
                    request.priority.value,
                    json.dumps(request.tags),
                    str(request.click_url) if request.click_url else None,
                    request.send_at.isoformat(),
                    request.send_at.isoformat(),
                    ScheduledStatus.PENDING.value,
                    created_at.isoformat(),
                ),
            )
            await db.commit()
        return ScheduledNotification(
            id=schedule_id,
            channel=request.channel,
            title=request.title,
            message=request.message,
            priority=request.priority,
            tags=request.tags,
            click_url=request.click_url,
            send_at=request.send_at,
            status=ScheduledStatus.PENDING,
            created_at=created_at,
        )

    async def claim_due(self, now: datetime) -> tuple[ScheduledNotification, str] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                UPDATE scheduled_notifications
                SET status = ?, attempts = attempts + 1, locked_at = ?
                WHERE id = (
                    SELECT id FROM scheduled_notifications
                    WHERE status = ? AND next_attempt_at <= ?
                    ORDER BY next_attempt_at, created_at
                    LIMIT 1
                )
                RETURNING *
                """,
                (
                    ScheduledStatus.PROCESSING.value,
                    now.isoformat(),
                    ScheduledStatus.PENDING.value,
                    now.isoformat(),
                ),
            )
            row = await cursor.fetchone()
            await db.commit()
        if row is None:
            return None
        return self._scheduled_from_row(row), str(row["actor"])

    async def mark_schedule_sent(
        self, schedule_id: str, ntfy_message_id: str | None, sent_at: datetime
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE scheduled_notifications
                SET status = ?, ntfy_message_id = ?, sent_at = ?, locked_at = NULL,
                    last_error = NULL
                WHERE id = ?
                """,
                (
                    ScheduledStatus.SENT.value,
                    ntfy_message_id,
                    sent_at.isoformat(),
                    schedule_id,
                ),
            )
            await db.commit()

    async def mark_schedule_failed(
        self,
        schedule_id: str,
        *,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        status = ScheduledStatus.PENDING if retry_at else ScheduledStatus.FAILED
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE scheduled_notifications
                SET status = ?, next_attempt_at = COALESCE(?, next_attempt_at),
                    last_error = ?, locked_at = NULL
                WHERE id = ?
                """,
                (
                    status.value,
                    retry_at.isoformat() if retry_at else None,
                    error[:1000],
                    schedule_id,
                ),
            )
            await db.commit()

    async def list_schedules(
        self,
        *,
        actor: str,
        status: ScheduledStatus | None = None,
        limit: int = 20,
    ) -> list[ScheduledNotification]:
        query = "SELECT * FROM scheduled_notifications WHERE actor = ?"
        parameters: list[object] = [actor]
        if status is not None:
            query += " AND status = ?"
            parameters.append(status.value)
        query += " ORDER BY send_at DESC LIMIT ?"
        parameters.append(limit)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(query, parameters)).fetchall()
        return [self._scheduled_from_row(row) for row in rows]

    async def cancel_schedule(self, schedule_id: str, actor: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE scheduled_notifications
                SET status = ?, locked_at = NULL
                WHERE id = ? AND actor = ? AND status = ?
                """,
                (
                    ScheduledStatus.CANCELLED.value,
                    schedule_id,
                    actor,
                    ScheduledStatus.PENDING.value,
                ),
            )
            await db.commit()
        return cursor.rowcount == 1
