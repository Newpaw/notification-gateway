from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

from notification_gateway.config import Settings


class OidcTokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        issuer = str(settings.oauth_issuer).rstrip("/")
        jwks_url = (
            str(settings.oauth_jwks_url)
            if settings.oauth_jwks_url
            else f"{issuer}/.well-known/jwks.json"
        )
        self.jwks = PyJWKClient(jwks_url, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=self.settings.algorithms,
                audience=self.settings.oauth_audience,
                issuer=str(self.settings.oauth_issuer).rstrip("/"),
            )
            scopes = str(claims.get("scope", "")).split()
            if not set(self.settings.required_scopes).issubset(scopes):
                return None
            return AccessToken(
                token=token,
                client_id=str(claims.get("client_id") or claims.get("azp") or "unknown"),
                subject=str(claims.get("sub") or "unknown"),
                scopes=scopes,
                expires_at=int(claims.get("exp", time.time() + 60)),
                resource=self.settings.oauth_audience,
                claims=claims,
            )
        except (jwt.PyJWTError, ValueError, OSError):
            return None
