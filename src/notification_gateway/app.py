from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import AnyHttpUrl, HttpUrl
from starlette.responses import JSONResponse, Response

from notification_gateway.auth import OidcTokenVerifier
from notification_gateway.config import Settings, get_settings
from notification_gateway.database import Database
from notification_gateway.models import (
    NotificationRequest,
    NotificationResult,
    Priority,
    ScheduledStatus,
    ScheduleNotificationRequest,
)
from notification_gateway.ntfy import NtfyClient
from notification_gateway.oauth_provider import SelfHostedOAuthProvider
from notification_gateway.scheduler import NotificationScheduler, ScheduleService
from notification_gateway.service import NotificationService, UnknownChannelError

SENT = Counter("notification_gateway_sent_total", "Notifications sent", ["source", "channel"])


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = Database(settings.database_path)
    ntfy = NtfyClient(settings)
    service = NotificationService(settings.channels, database, ntfy)
    schedules = ScheduleService(database, service)
    scheduler = NotificationScheduler(database, service)

    mcp_http = None
    oauth_provider = None
    if settings.mcp_enabled:
        resource_url = f"{str(settings.public_base_url).rstrip('/')}/mcp"
        if settings.oauth_self_hosted:
            oauth_provider = SelfHostedOAuthProvider(
                database_path=settings.database_path,
                issuer=str(settings.public_base_url).rstrip("/"),
                resource=resource_url,
                login_password=settings.oauth_login_password,
                required_scopes=settings.required_scopes,
            )
            verifier = None
            issuer_url = AnyHttpUrl(str(settings.public_base_url).rstrip("/"))
        else:
            verifier = OidcTokenVerifier(settings)
            issuer_url = AnyHttpUrl(str(settings.oauth_issuer))
        mcp = MCPServer(
            name="notification-gateway",
            title="Notification Gateway",
            description="Send push notifications to approved ntfy channels.",
            version="0.2.0",
            auth_server_provider=oauth_provider,
            token_verifier=verifier,
            auth=AuthSettings(
                issuer_url=issuer_url,
                resource_server_url=AnyHttpUrl(resource_url),
                required_scopes=settings.required_scopes,
                client_registration_options=ClientRegistrationOptions(
                    enabled=settings.oauth_self_hosted,
                    valid_scopes=settings.required_scopes,
                    default_scopes=settings.required_scopes,
                    client_secret_expiry_seconds=None,
                ),
                revocation_options=RevocationOptions(enabled=settings.oauth_self_hosted),
            ),
        )

        if oauth_provider is not None:
            mcp.custom_route("/oauth/login", ["GET", "POST"])(oauth_provider.login_route)

        @mcp.tool(
            name="send_notification",
            description="Send a push notification immediately to one approved channel.",
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

        @mcp.tool(
            name="schedule_notification",
            description=(
                "Persist a notification in Notification Gateway and send it at send_at, "
                "independently of ChatGPT. send_at must be a future ISO 8601 timestamp "
                "with a timezone offset."
            ),
            structured_output=True,
        )
        async def schedule_notification(
            channel: str,
            title: str,
            message: str,
            send_at: datetime,
            priority: Priority = Priority.DEFAULT,
            tags: list[str] | None = None,
            click_url: str | None = None,
        ) -> dict[str, object]:
            token = get_access_token()
            scheduled = await schedules.schedule(
                ScheduleNotificationRequest(
                    channel=channel,
                    title=title,
                    message=message,
                    send_at=send_at,
                    priority=priority,
                    tags=tags or [],
                    click_url=HttpUrl(click_url) if click_url else None,
                ),
                actor=token.subject if token and token.subject else "unknown",
            )
            return scheduled.model_dump(mode="json")

        @mcp.tool(
            name="list_scheduled_notifications",
            description="List notifications persisted in Notification Gateway for this user.",
            structured_output=True,
        )
        async def list_scheduled_notifications(
            status: ScheduledStatus | None = None,
            limit: int = 20,
        ) -> dict[str, object]:
            token = get_access_token()
            actor = token.subject if token and token.subject else "unknown"
            bounded_limit = max(1, min(limit, 100))
            scheduled = await schedules.list(
                actor=actor,
                status=status,
                limit=bounded_limit,
            )
            return {"notifications": [item.model_dump(mode="json") for item in scheduled]}

        @mcp.tool(
            name="cancel_scheduled_notification",
            description="Cancel one pending notification by its Notification Gateway ID.",
            structured_output=True,
        )
        async def cancel_scheduled_notification(notification_id: str) -> dict[str, object]:
            token = get_access_token()
            actor = token.subject if token and token.subject else "unknown"
            cancelled = await schedules.cancel(notification_id, actor=actor)
            return {"id": notification_id, "cancelled": cancelled}

        public_host = settings.public_base_url.host
        if public_host is None:
            raise ValueError("PUBLIC_BASE_URL must include a hostname")
        public_origin = str(settings.public_base_url).rstrip("/")
        mcp_http = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[public_host, f"{public_host}:*"],
                allowed_origins=[public_origin, "https://chatgpt.com"],
            ),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.initialize()
        if oauth_provider is not None:
            await oauth_provider.initialize()
        scheduler_task = asyncio.create_task(scheduler.run(), name="notification-scheduler")
        try:
            if mcp_http is not None:
                async with mcp_http.router.lifespan_context(mcp_http):
                    yield
            else:
                yield
        finally:
            scheduler.stop()
            await scheduler_task
            await ntfy.close()

    app = FastAPI(title="Notification Gateway", version="0.2.0", lifespan=lifespan)

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
