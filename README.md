# Notification Gateway

A small, security-focused Python gateway that lets ChatGPT and automation tools send push
notifications through a self-hosted [ntfy](https://ntfy.sh/) server.

## Interfaces

- MCP Streamable HTTP at `/mcp` for ChatGPT, protected by OAuth 2.1/OIDC and the
  `notifications:write` scope.
- REST `POST /api/v1/notifications` for monitoring and automations, protected by `X-API-Key`.
- `/healthz`, `/readyz`, and Prometheus `/metrics` endpoints.
- SQLite audit trail and idempotency keys to prevent duplicate pushes.

MCP is deliberately disabled until a real identity provider is configured. The service does not
implement its own authorization server.

## Request

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

Channels are aliases. Their private ntfy topics live only in `CHANNEL_TOPICS_JSON`, so callers do
not get arbitrary publish access.

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
- `REST_API_KEYS_JSON`: caller-to-key map; use long random values.

The initial safe deployment uses `MCP_ENABLED=false`. REST and health checks work immediately.

## Enable ChatGPT MCP

Configure an OAuth 2.1/OIDC provider that supports the MCP client registration flow, then set:

```dotenv
PUBLIC_BASE_URL=https://notify.novopacky.com
MCP_ENABLED=true
OAUTH_ISSUER=https://issuer.example.com/
OAUTH_AUDIENCE=notification-gateway
OAUTH_JWKS_URL=https://issuer.example.com/.well-known/jwks.json
OAUTH_REQUIRED_SCOPES=notifications:write
```

The MCP SDK publishes protected-resource metadata, challenges unauthenticated clients, validates
JWT signature/issuer/audience/expiry, and enforces the required scope. In ChatGPT developer mode,
add the connector URL `https://notify.novopacky.com/mcp` and complete its OAuth login.

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
