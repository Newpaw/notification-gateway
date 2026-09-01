from __future__ import annotations

from typing import cast

import pytest

from notification_gateway.database import Database
from notification_gateway.models import NotificationRequest
from notification_gateway.ntfy import NtfyClient
from notification_gateway.service import NotificationService, UnknownChannelError


class FakeNtfy:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, topic: str, request: NotificationRequest) -> str:
        self.calls += 1
        return f"ntfy-{self.calls}"


@pytest.mark.asyncio
async def test_idempotency_prevents_duplicate_publish(settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    ntfy = FakeNtfy()
    service = NotificationService(settings.channels, database, cast(NtfyClient, ntfy))
    request = NotificationRequest(
        channel="personal", title="Test", message="Message", idempotency_key="job-1"
    )

    first = await service.send(request, source="test", actor="pytest")
    second = await service.send(request, source="test", actor="pytest")

    assert first.id == second.id
    assert second.duplicate is True
    assert ntfy.calls == 1


@pytest.mark.asyncio
async def test_unknown_channel(settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    service = NotificationService(settings.channels, database, cast(NtfyClient, FakeNtfy()))
    with pytest.raises(UnknownChannelError):
        await service.send(
            NotificationRequest(channel="missing", title="Test", message="Message"),
            source="test",
            actor="pytest",
        )
