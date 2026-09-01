from __future__ import annotations

import httpx
import pytest

from notification_gateway.config import Settings
from notification_gateway.models import NotificationRequest, Priority
from notification_gateway.ntfy import NtfyClient


@pytest.mark.asyncio
async def test_publish_uses_token_and_json(settings: Settings) -> None:
    settings.ntfy_token = "secret-token"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        assert b'"topic":"private-personal"' in request.content
        return httpx.Response(200, json={"id": "ntfy-id"})

    client = NtfyClient(settings, transport=httpx.MockTransport(handler))
    result = await client.publish(
        "private-personal",
        NotificationRequest(
            channel="personal",
            title="Hello",
            message="World",
            priority=Priority.HIGH,
        ),
    )
    await client.close()
    assert result == "ntfy-id"
