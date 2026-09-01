from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from notification_gateway.app import create_app


def test_health_and_disabled_mcp(settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {"healthy": True}
        assert client.post("/mcp").status_code == 503


def test_rest_requires_api_key(settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/notifications",
            json={"channel": "personal", "title": "Hello", "message": "World"},
        )
        assert response.status_code == 401


def test_rest_sends_notification(settings) -> None:
    with patch("notification_gateway.ntfy.NtfyClient.publish", new_callable=AsyncMock) as publish:
        publish.return_value = "ntfy-123"
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/api/v1/notifications",
                headers={"X-API-Key": "test-secret"},
                json={"channel": "personal", "title": "Hello", "message": "World"},
            )
        assert response.status_code == 200
        assert response.json()["ntfy_message_id"] == "ntfy-123"
