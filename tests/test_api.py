from __future__ import annotations

import base64
import hashlib
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

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
    with TestClient(create_app(oauth_settings), base_url="https://notify.example.test") as client:
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


def test_complete_oauth_and_authenticated_mcp_flow(settings: Settings) -> None:
    oauth_settings = settings.model_copy(
        update={
            "mcp_enabled": True,
            "oauth_self_hosted": True,
            "oauth_login_password": "correct-horse-battery-staple",
        }
    )
    callback = "https://chatgpt.com/connector/oauth/test"
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    challenge_value = challenge.decode().rstrip("=")

    with TestClient(create_app(oauth_settings), base_url="https://notify.example.test") as client:
        registration = client.post(
            "/register",
            json={
                "redirect_uris": [callback],
                "client_name": "ChatGPT",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
                "scope": "notifications:write",
            },
        )
        registered = registration.json()
        authorization = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registered["client_id"],
                "redirect_uri": callback,
                "scope": "notifications:write",
                "code_challenge": challenge_value,
                "code_challenge_method": "S256",
                "resource": "https://notify.example.test/mcp",
                "state": "test-state",
            },
            follow_redirects=False,
        )
        login_path = authorization.headers["location"].removeprefix("https://notify.example.test")
        transaction = parse_qs(urlparse(login_path).query)["transaction"][0]
        login = client.post(
            login_path,
            data={
                "transaction": transaction,
                "password": "correct-horse-battery-staple",
            },
            follow_redirects=False,
        )
        code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]
        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": callback,
                "resource": "https://notify.example.test/mcp",
            },
        )
        mcp_headers = {
            "Authorization": f"Bearer {token.json()['access_token']}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        }
        initialize = client.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        tools = client.post(
            "/mcp",
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert registration.status_code == 201
    assert login.status_code == 303
    assert token.status_code == 200
    assert initialize.status_code == 200
    assert tools.status_code == 200
    assert "send_notification" in tools.text
