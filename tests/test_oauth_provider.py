from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request

from notification_gateway.oauth_provider import SelfHostedOAuthProvider


@pytest.fixture
async def oauth_provider(tmp_path: Path) -> SelfHostedOAuthProvider:
    provider = SelfHostedOAuthProvider(
        database_path=str(tmp_path / "oauth.db"),
        issuer="https://notify.novopacky.com",
        resource="https://notify.novopacky.com/mcp",
        login_password="correct-horse-battery-staple",
        required_scopes=["notifications:write"],
    )
    await provider.initialize()
    assert provider.issuer == "https://notify.novopacky.com/"
    return provider


@pytest.fixture
def oauth_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="chatgpt-client",
        client_secret="secret",
        redirect_uris=[AnyUrl("https://chatgpt.com/connector_platform_oauth_redirect")],
        token_endpoint_auth_method="client_secret_post",
    )


def callback_uri(client: OAuthClientInformationFull) -> AnyUrl:
    assert client.redirect_uris
    return client.redirect_uris[0]


async def test_oauth_authorization_and_token_rotation(
    oauth_provider: SelfHostedOAuthProvider,
    oauth_client: OAuthClientInformationFull,
) -> None:
    await oauth_provider.register_client(oauth_client)
    assert await oauth_provider.get_client(oauth_client.client_id) == oauth_client

    authorize_url = await oauth_provider.authorize(
        oauth_client,
        AuthorizationParams(
            state="state-123",
            scopes=["notifications:write"],
            code_challenge="challenge",
            redirect_uri=callback_uri(oauth_client),
            redirect_uri_provided_explicitly=True,
            resource="https://notify.novopacky.com/mcp",
        ),
    )
    transaction = parse_qs(urlparse(authorize_url).query)["transaction"][0]

    with pytest.raises(PermissionError, match="Invalid password"):
        await oauth_provider.complete_authorization(transaction, "wrong")

    callback = await oauth_provider.complete_authorization(
        transaction, "correct-horse-battery-staple"
    )
    callback_query = parse_qs(urlparse(callback).query)
    assert callback_query["state"] == ["state-123"]
    assert callback_query["iss"] == ["https://notify.novopacky.com/"]

    code_value = callback_query["code"][0]
    code = await oauth_provider.load_authorization_code(oauth_client, code_value)
    assert code is not None
    tokens = await oauth_provider.exchange_authorization_code(oauth_client, code)
    assert await oauth_provider.load_access_token(tokens.access_token) is not None

    with pytest.raises(TokenError):
        await oauth_provider.exchange_authorization_code(oauth_client, code)

    assert tokens.refresh_token is not None
    refresh = await oauth_provider.load_refresh_token(oauth_client, tokens.refresh_token)
    assert refresh is not None
    rotated = await oauth_provider.exchange_refresh_token(
        oauth_client, refresh, ["notifications:write"]
    )
    assert await oauth_provider.load_access_token(tokens.access_token) is None
    rotated_access = await oauth_provider.load_access_token(rotated.access_token)
    assert rotated_access is not None

    await oauth_provider.revoke_token(rotated_access)
    assert await oauth_provider.load_access_token(rotated.access_token) is None


async def test_oauth_supports_standard_clients_and_rejects_unsafe_redirects(
    oauth_provider: SelfHostedOAuthProvider,
    oauth_client: OAuthClientInformationFull,
) -> None:
    for redirect_uri in (
        "https://mcp-client.example/callback",
        "http://localhost:6274/oauth/callback",
        "http://127.0.0.1:49152/oauth/callback",
        "http://[::1]:6276/oauth/callback",
    ):
        client = oauth_client.model_copy(
            update={
                "client_id": f"client-{redirect_uri}",
                "redirect_uris": [AnyUrl(redirect_uri)],
            }
        )
        await oauth_provider.register_client(client)
        assert await oauth_provider.get_client(client.client_id) == client

    for redirect_uri in (
        "http://mcp-client.example/callback",
        "https://mcp-client.example/callback#fragment",
        "https://user:password@mcp-client.example/callback",
        "custom-scheme://callback",
    ):
        invalid_client = oauth_client.model_copy(update={"redirect_uris": [AnyUrl(redirect_uri)]})
        with pytest.raises(RegistrationError):
            await oauth_provider.register_client(invalid_client)

    missing_redirect = oauth_client.model_copy(update={"redirect_uris": []})
    with pytest.raises(RegistrationError):
        await oauth_provider.register_client(missing_redirect)

    with pytest.raises(AuthorizeError):
        await oauth_provider.authorize(
            oauth_client,
            AuthorizationParams(
                state=None,
                scopes=["admin"],
                code_challenge="challenge",
                redirect_uri=callback_uri(oauth_client),
                redirect_uri_provided_explicitly=True,
            ),
        )


async def test_login_route_renders_and_rejects_expired_transaction(
    oauth_provider: SelfHostedOAuthProvider,
    oauth_client: OAuthClientInformationFull,
) -> None:
    authorize_url = await oauth_provider.authorize(
        oauth_client,
        AuthorizationParams(
            state=None,
            scopes=None,
            code_challenge="challenge",
            redirect_uri=callback_uri(oauth_client),
            redirect_uri_provided_explicitly=True,
        ),
    )
    transaction = parse_qs(urlparse(authorize_url).query)["transaction"][0]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/oauth/login",
        "query_string": f"transaction={transaction}".encode(),
        "headers": [],
    }
    response = await oauth_provider.login_route(Request(scope))
    assert response.status_code == 200
    assert b"Authorize MCP client" in response.body
    assert b"chatgpt-client" in response.body
    assert b"https://chatgpt.com/connector_platform_oauth_redirect" in response.body
    content_security_policy = response.headers["content-security-policy"]
    assert "form-action" not in content_security_policy
    assert "base-uri 'none'" in content_security_policy
    assert "frame-ancestors 'none'" in content_security_policy

    expired_scope = {**scope, "query_string": b"transaction=expired"}
    expired = await oauth_provider.login_route(Request(expired_scope))
    assert expired.status_code == 400
