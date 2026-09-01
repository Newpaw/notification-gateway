from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import aiosqlite
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


class StoredAccessToken(AccessToken):
    family_id: str


class StoredRefreshToken(RefreshToken):
    family_id: str


class SelfHostedOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, StoredRefreshToken, StoredAccessToken]
):
    def __init__(
        self,
        *,
        database_path: str,
        issuer: str,
        resource: str,
        login_password: str,
        required_scopes: list[str],
        access_token_ttl: int = 900,
        refresh_token_ttl: int = 2_592_000,
    ) -> None:
        self.database_path = database_path
        self.issuer = issuer.rstrip("/")
        self.resource = resource
        self.login_password = login_password
        self.required_scopes = required_scopes
        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    async def initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_transactions (
                    id_hash TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    data TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    family_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS oauth_tokens_family_idx
                    ON oauth_tokens(family_id);
                """
            )
            now = int(time.time())
            await db.execute("DELETE FROM oauth_transactions WHERE expires_at < ?", (now,))
            await db.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            await db.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
            await db.commit()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with aiosqlite.connect(self.database_path) as db:
            row = await (
                await db.execute("SELECT data FROM oauth_clients WHERE client_id = ?", (client_id,))
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirect_uris = client_info.redirect_uris or []
        if not redirect_uris or any(
            urlparse(str(uri)).scheme != "https" or urlparse(str(uri)).hostname != "chatgpt.com"
            for uri in redirect_uris
        ):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Only HTTPS redirect URIs on chatgpt.com are allowed",
            )
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO oauth_clients(client_id, data, created_at) "
                "VALUES (?, ?, ?)",
                (client_info.client_id, client_info.model_dump_json(), int(time.time())),
            )
            await db.commit()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        scopes = params.scopes or self.required_scopes
        if not set(scopes).issubset(self.required_scopes):
            raise AuthorizeError(error="invalid_scope", error_description="Unsupported scope")
        if params.resource and params.resource != self.resource:
            raise AuthorizeError(error="invalid_target", error_description="Invalid resource")

        transaction = secrets.token_urlsafe(32)
        data = {
            "client_id": client.client_id,
            "state": params.state,
            "scopes": scopes,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource or self.resource,
        }
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "INSERT INTO oauth_transactions(id_hash, data, expires_at) VALUES (?, ?, ?)",
                (self._hash(transaction), json.dumps(data), int(time.time()) + 600),
            )
            await db.commit()
        return f"{self.issuer}/oauth/login?transaction={transaction}"

    async def _load_transaction(self, transaction: str) -> tuple[dict[str, object], int] | None:
        async with aiosqlite.connect(self.database_path) as db:
            row = await (
                await db.execute(
                    "SELECT data, attempts, expires_at FROM oauth_transactions WHERE id_hash = ?",
                    (self._hash(transaction),),
                )
            ).fetchone()
        if not row or int(row[2]) < time.time():
            return None
        return json.loads(row[0]), int(row[1])

    async def complete_authorization(self, transaction: str, password: str) -> str:
        loaded = await self._load_transaction(transaction)
        if loaded is None:
            raise ValueError("Authorization request expired")
        data, attempts = loaded
        if attempts >= 5:
            raise ValueError("Too many failed attempts")
        if not hmac.compare_digest(password, self.login_password):
            async with aiosqlite.connect(self.database_path) as db:
                await db.execute(
                    "UPDATE oauth_transactions SET attempts = attempts + 1 WHERE id_hash = ?",
                    (self._hash(transaction),),
                )
                await db.commit()
            raise PermissionError("Invalid password")

        code = secrets.token_urlsafe(32)
        raw_scopes = data["scopes"]
        scopes = [str(scope) for scope in raw_scopes] if isinstance(raw_scopes, list) else []
        auth_code = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + 300,
            client_id=str(data["client_id"]),
            code_challenge=str(data["code_challenge"]),
            redirect_uri=AnyUrl(str(data["redirect_uri"])),
            redirect_uri_provided_explicitly=bool(data["redirect_uri_provided_explicitly"]),
            resource=str(data["resource"]),
            subject="newpaw",
        )
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "DELETE FROM oauth_transactions WHERE id_hash = ?",
                (self._hash(transaction),),
            )
            await db.execute(
                "INSERT INTO oauth_codes(code_hash, data, expires_at) VALUES (?, ?, ?)",
                (self._hash(code), auth_code.model_dump_json(), int(auth_code.expires_at)),
            )
            await db.commit()
        state = data.get("state")
        return construct_redirect_uri(
            str(data["redirect_uri"]),
            code=code,
            state=str(state) if state is not None else None,
            iss=self.issuer,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with aiosqlite.connect(self.database_path) as db:
            row = await (
                await db.execute(
                    "SELECT data, expires_at FROM oauth_codes WHERE code_hash = ?",
                    (self._hash(authorization_code),),
                )
            ).fetchone()
        if not row or int(row[1]) < time.time():
            return None
        result = AuthorizationCode.model_validate_json(row[0])
        return result if result.client_id == client.client_id else None

    async def _issue_tokens(
        self, *, client_id: str, scopes: list[str], resource: str, family_id: str
    ) -> OAuthToken:
        now = int(time.time())
        access_value = secrets.token_urlsafe(32)
        refresh_value = secrets.token_urlsafe(48)
        access = StoredAccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + self.access_token_ttl,
            resource=resource,
            subject="newpaw",
            family_id=family_id,
        )
        refresh = StoredRefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + self.refresh_token_ttl,
            subject="newpaw",
            family_id=family_id,
        )
        async with aiosqlite.connect(self.database_path) as db:
            await db.executemany(
                "INSERT INTO oauth_tokens(token_hash, kind, data, expires_at, family_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        self._hash(access_value),
                        "access",
                        access.model_dump_json(),
                        access.expires_at,
                        family_id,
                    ),
                    (
                        self._hash(refresh_value),
                        "refresh",
                        refresh.model_dump_json(),
                        refresh.expires_at,
                        family_id,
                    ),
                ],
            )
            await db.commit()
        return OAuthToken(
            access_token=access_value,
            expires_in=self.access_token_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                "DELETE FROM oauth_codes WHERE code_hash = ?",
                (self._hash(authorization_code.code),),
            )
            await db.commit()
        if cursor.rowcount != 1:
            raise TokenError(error="invalid_grant", error_description="Code already used")
        return await self._issue_tokens(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource or self.resource,
            family_id=secrets.token_urlsafe(16),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        token = await self._load_token(refresh_token, "refresh")
        if isinstance(token, StoredRefreshToken) and token.client_id == client.client_id:
            return token
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "DELETE FROM oauth_tokens WHERE family_id = ?",
                (refresh_token.family_id,),
            )
            await db.commit()
        return await self._issue_tokens(
            client_id=client.client_id,
            scopes=scopes,
            resource=self.resource,
            family_id=refresh_token.family_id,
        )

    async def _load_token(
        self, value: str, kind: str
    ) -> StoredAccessToken | StoredRefreshToken | None:
        async with aiosqlite.connect(self.database_path) as db:
            row = await (
                await db.execute(
                    "SELECT data, expires_at FROM oauth_tokens WHERE token_hash = ? AND kind = ?",
                    (self._hash(value), kind),
                )
            ).fetchone()
        if not row or int(row[1]) < time.time():
            return None
        model = StoredAccessToken if kind == "access" else StoredRefreshToken
        return model.model_validate_json(row[0])

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        loaded = await self._load_token(token, "access")
        return loaded if isinstance(loaded, StoredAccessToken) else None

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("DELETE FROM oauth_tokens WHERE family_id = ?", (token.family_id,))
            await db.commit()

    async def login_route(self, request: Request) -> Response:
        transaction = request.query_params.get("transaction", "")
        error = ""
        if request.method == "POST":
            form = await request.form()
            transaction = str(form.get("transaction", ""))
            try:
                redirect_url = await self.complete_authorization(
                    transaction, str(form.get("password", ""))
                )
                return RedirectResponse(
                    redirect_url,
                    status_code=302,
                    headers={"Cache-Control": "no-store"},
                )
            except (PermissionError, ValueError) as exc:
                error = str(exc)

        if not transaction or await self._load_transaction(transaction) is None:
            return HTMLResponse("Authorization request expired", status_code=400)
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        page = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Notification Gateway</title>
<style>
body{{font-family:system-ui;background:#10131a;color:#f5f7fb;display:grid;
place-items:center;min-height:100vh;margin:0}}
main{{width:min(420px,calc(100% - 40px));background:#1b202b;padding:28px;
border-radius:16px}}
input,button{{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;
border-radius:9px;border:1px solid #394254}}
button{{background:#5b8cff;color:white;font-weight:700;cursor:pointer}}
.error{{color:#ff8f8f}}
</style></head><body><main><h1>Notification Gateway</h1>
<p>Authorize ChatGPT to send notifications.</p>{error_html}
<form method="post"><input type="hidden" name="transaction"
value="{html.escape(transaction)}"><label>Password
<input name="password" type="password" required autofocus
autocomplete="current-password"></label>
<button type="submit">Authorize</button></form></main></body></html>"""
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
                ),
                "X-Frame-Options": "DENY",
            },
        )
