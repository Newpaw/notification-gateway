# Notification Gateway

A small, security-focused Python gateway that lets ChatGPT and automation tools send push
notifications through a self-hosted [ntfy](https://ntfy.sh/) server.

## Interfaces

- MCP Streamable HTTP at `/mcp` for ChatGPT, protected by OAuth 2.1 and the
  `notifications:write` scope.
- REST `POST /api/v1/notifications` for monitoring and automations, protected by `X-API-Key`.
- `/healthz`, `/readyz`, and Prometheus `/metrics` endpoints.
- Persistent SQLite scheduler, audit trail, retries, and idempotency keys to prevent duplicate
  pushes.

The included self-hosted authorization server supports authorization code flow with PKCE S256,
dynamic client registration, short-lived access tokens, rotating refresh tokens, and revocation.
An external OIDC provider remains supported for multi-user installations.

## REST request

```json
{
  "channel": "personal",
  "title": "Backup finished",
  "message": "The nightly backup completed successfully.",
  "priority": "default",
  "tags": ["white_check_mark"],
  "click_url": "https://example.com/jobs/123",
  "idempotency_key": "backup-2026-09-01"
}
```

REST channels are aliases. Their ntfy topics live only in `CHANNEL_TOPICS_JSON`, so callers do not
get arbitrary publish access.

## MCP tools

- `schedule_notification`: persist a future notification in the gateway. The tool requires no
  channel parameter; every request is routed server-side through `DEFAULT_CHANNEL` to the `jan-personal`
  ntfy topic. `send_at` must be an ISO 8601 timestamp with a timezone offset, for example
  `2026-09-02T07:30:00+02:00`. A legacy optional `channel` argument is accepted for connectors
  with a cached pre-0.3.0 schema, but its value is always ignored.
- `list_scheduled_notifications`: list pending, sent, failed, or cancelled notifications.
- `cancel_scheduled_notification`: cancel a pending notification by ID.

Scheduled notifications are owned by Notification Gateway, not by ChatGPT Scheduled Tasks. The
background worker polls the persistent SQLite queue and sends due notifications even when ChatGPT
is closed. ChatGPT cannot choose or override the destination channel. Failed deliveries use
exponential backoff and stop after five attempts. A restart safely recovers work that was claimed
but not completed, and the notification ID is used as an idempotency key on delivery.

Example ChatGPT request:

> Tomorrow at 7:30 Europe/Prague, schedule a personal notification reminding me to call Petr.

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn notification_gateway.app:app --reload
```

Run checks with:

```bash
ruff check .
ruff format --check .
mypy
pytest
docker build -t notification-gateway .
```

## Deploy with Komodo

The supplied `compose.yaml` uses `ghcr.io/newpaw/notification-gateway:latest`, persists `/data`,
and joins the existing external Docker network `cloudflare`. Point the Cloudflare Tunnel origin at
`http://notification-gateway:8000`.

Set these secrets in Komodo, never in the compose file:

- `NTFY_TOKEN`: a dedicated ntfy access token. `NTFY_USERNAME`/`NTFY_PASSWORD` are supported as a
  migration fallback, but a revocable token is preferred.
- `CHANNEL_TOPICS_JSON`: channel-to-private-topic map.
- `DEFAULT_CHANNEL`: the only alias used by the MCP scheduler. For the personal deployment use
  `personal`; configure `CHANNEL_TOPICS_JSON={"personal":"jan-personal"}`.
- `REST_API_KEYS_JSON`: caller-to-key map; use long random values.

The initial safe deployment uses `MCP_ENABLED=false`. REST and health checks work immediately.

## Enable ChatGPT MCP

For a personal ChatGPT connector, enable the bundled authorization server:

```dotenv
PUBLIC_BASE_URL=https://notify.novopacky.com
MCP_ENABLED=true
OAUTH_SELF_HOSTED=true
OAUTH_LOGIN_PASSWORD=replace-with-at-least-20-random-characters
OAUTH_REQUIRED_SCOPES=notifications:write
```

Then create a custom connector in ChatGPT developer mode:

- Name: `Notification Gateway`
- Description: `Send push notifications to my phone through ntfy`
- MCP server URL: `https://notify.novopacky.com/mcp`
- Authentication: `OAuth`

ChatGPT discovers the authorization and registration endpoints automatically. During connection,
the gateway displays its own login page; enter `OAUTH_LOGIN_PASSWORD` there. The password is only
used to approve new OAuth sessions. Tokens and authorization codes are stored in SQLite as hashes,
access tokens expire after 15 minutes, refresh tokens rotate, and five failed login attempts lock
that authorization request.

Dynamic client registration is intentionally restricted to HTTPS callback URLs on `chatgpt.com`.
The MCP scheduler always publishes through `DEFAULT_CHANNEL`; the caller cannot supply a channel.
The production personal mapping is `personal` to `jan-personal`.

For a multi-user deployment, disable the bundled server and configure an external OIDC provider:

```dotenv
MCP_ENABLED=true
OAUTH_SELF_HOSTED=false
OAUTH_ISSUER=https://issuer.example.com/
OAUTH_AUDIENCE=notification-gateway
OAUTH_JWKS_URL=https://issuer.example.com/.well-known/jwks.json
OAUTH_REQUIRED_SCOPES=notifications:write
```

In either mode, the MCP SDK publishes protected-resource metadata, challenges unauthenticated
clients, and enforces the required scope.

## REST example

```bash
curl https://notify.novopacky.com/api/v1/notifications \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: replace-me' \
  -d '{"channel":"personal","title":"Hello","message":"Sent through the gateway"}'
```

## Releases

GitHub Actions runs Ruff, mypy, pytest, and a Docker build for every change. Pushes to `main` and
version tags publish signed, provenance-attested images to GHCR. Dependabot tracks Python, Docker,
and Actions updates.
