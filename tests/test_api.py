from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from notification_gateway.app import create_app
from notification_gateway.config import Settings


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


def test_self_hosted_oauth_discovery(settings: Settings) -> None:
    oauth_settings = settings.model_copy(
        update={
            "mcp_enabled": True,
            "oauth_self_hosted": True,
            "oauth_login_password": "correct-horse-battery-staple",
        }
    )
    with TestClient(create_app(oauth_settings)) as client:
        protected = client.get("/.well-known/oauth-protected-resource/mcp")
        authorization = client.get("/.well-known/oauth-authorization-server")
        mcp = client.get("/mcp")

    assert protected.status_code == 200
    assert protected.json()["resource"] == "https://notify.example.test/mcp"
    assert authorization.status_code == 200
    assert authorization.json()["registration_endpoint"].endswith("/register")
    assert authorization.json()["code_challenge_methods_supported"] == ["S256"]
    assert mcp.status_code == 401
    assert "resource_metadata=" in mcp.headers["www-authenticate"]
