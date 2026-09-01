from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "notification-gateway"
    log_level: str = "INFO"
    database_path: str = "/data/notifications.db"

    public_base_url: HttpUrl = HttpUrl("http://localhost:8000")
    ntfy_base_url: HttpUrl = HttpUrl("http://ntfy:80")
    ntfy_token: str = ""
    ntfy_username: str = ""
    ntfy_password: str = ""

    channel_topics_json: str = '{"personal":"personal","urgent":"urgent","system":"system"}'
    rest_api_keys_json: str = "{}"

    mcp_enabled: bool = False
    oauth_issuer: HttpUrl | None = None
    oauth_audience: str = "notification-gateway"
    oauth_jwks_url: HttpUrl | None = None
    oauth_required_scopes: str = "notifications:write"
    oauth_algorithms: str = "RS256"

    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_oauth(self) -> Settings:
        if self.mcp_enabled and not self.oauth_issuer:
            raise ValueError("OAUTH_ISSUER is required when MCP_ENABLED=true")
        return self

    @property
    def channels(self) -> dict[str, str]:
        value = json.loads(self.channel_topics_json)
        if not isinstance(value, dict) or not value:
            raise ValueError("CHANNEL_TOPICS_JSON must be a non-empty JSON object")
        return {str(k): str(v) for k, v in value.items()}

    @property
    def rest_api_keys(self) -> dict[str, str]:
        value = json.loads(self.rest_api_keys_json)
        if not isinstance(value, dict):
            raise ValueError("REST_API_KEYS_JSON must be a JSON object")
        return {str(k): str(v) for k, v in value.items()}

    @property
    def required_scopes(self) -> list[str]:
        return [scope for scope in self.oauth_required_scopes.split() if scope]

    @property
    def algorithms(self) -> list[str]:
        return [
            algorithm.strip() for algorithm in self.oauth_algorithms.split(",") if algorithm.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
