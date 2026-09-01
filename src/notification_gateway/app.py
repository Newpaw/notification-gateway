from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import AnyHttpUrl, HttpUrl
from starlette.responses import JSONResponse, Response

from notification_gateway.auth import OidcTokenVerifier
from notification_gateway.config import Settings, get_settings
from notification_gateway.database import Database
from notification_gateway.models import NotificationRequest, NotificationResult, Priority
from notification_gateway.ntfy import NtfyClient
from notification_gateway.service import NotificationService, UnknownChannelError

SENT = Counter("notification_gateway_sent_total", "Notifications sent", ["source", "channel"])


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = Database(settings.database_path)
    ntfy = NtfyClient(settings)
    service = NotificationService(settings.channels, database, ntfy)

    mcp_http = None
    if settings.mcp_enabled:
        verifier = OidcTokenVerifier(settings)
        mcp = MCPServer(
            name="notification-gateway",
            title="Notification Gateway",
            description="Send push notifications to approved ntfy channels.",
            version="0.1.0",
            token_verifier=verifier,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(str(settings.oauth_issuer)),
                resource_server_url=AnyHttpUrl(f"{str(settings.public_base_url).rstrip('/')}/mcp"),
                required_scopes=settings.required_scopes,
            ),
        )

        @mcp.tool(
            name="send_notification",
            description="Send a push notification to one approved channel.",
            structured_output=True,
        )
        async def send_notification(
            channel: str,
            title: str,
            message: str,
            priority: Priority = Priority.DEFAULT,
            tags: list[str] | None = None,
            click_url: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object]:
            token = get_access_token()
            request = NotificationRequest(
                channel=channel,
                title=title,
                message=message,
                priority=priority,
                tags=tags or [],
                click_url=HttpUrl(click_url) if click_url else None,
                idempotency_key=idempotency_key,
            )
            result = await service.send(
                request,
                source="mcp",
                actor=token.subject if token and token.subject else "unknown",
            )
            SENT.labels(source="mcp", channel=channel).inc()
            return result.model_dump()

        mcp_http = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.initialize()
        if mcp_http is not None:
            async with mcp_http.router.lifespan_context(mcp_http):
                yield
        else:
            yield
        await ntfy.close()

    app = FastAPI(title="Notification Gateway", version="0.1.0", lifespan=lifespan)

    async def rest_actor(x_api_key: str = Header(default="")) -> str:
        for actor, expected in settings.rest_api_keys.items():
            if expected and hmac.compare_digest(x_api_key, expected):
                return actor
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, bool]:
        return {"healthy": True}

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> dict[str, bool]:
        return {"ready": True}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/v1/notifications", response_model=NotificationResult)
    async def send_rest(
        notification: NotificationRequest,
        actor: str = Depends(rest_actor),
    ) -> NotificationResult:
        try:
            result = await service.send(notification, source="rest", actor=actor)
        except UnknownChannelError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        SENT.labels(source="rest", channel=notification.channel).inc()
        return result

    if mcp_http is not None:
        app.mount("/", mcp_http)
    else:

        @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
        async def mcp_disabled(_: Request) -> JSONResponse:
            return JSONResponse(
                {"detail": "MCP is disabled until OAuth is configured"}, status_code=503
            )

    return app


app = create_app()
