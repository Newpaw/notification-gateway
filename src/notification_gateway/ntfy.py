from __future__ import annotations

from typing import Any

import httpx

from notification_gateway.config import Settings
from notification_gateway.models import NotificationRequest


class NtfyClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def publish(self, topic: str, request: NotificationRequest) -> str | None:
        headers: dict[str, str] = {}
        auth: Any = None
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
        elif self.settings.ntfy_username:
            auth = httpx.BasicAuth(self.settings.ntfy_username, self.settings.ntfy_password)

        payload: dict[str, object] = {
            "topic": topic,
            "title": request.title,
            "message": request.message,
            "priority": request.priority.value,
            "tags": request.tags,
        }
        if request.click_url:
            payload["click"] = str(request.click_url)

        response = await self.client.post(
            str(self.settings.ntfy_base_url).rstrip("/"),
            json=payload,
            headers=headers,
            auth=auth,
        )
        response.raise_for_status()
        data = response.json()
        return str(data["id"]) if data.get("id") else None
