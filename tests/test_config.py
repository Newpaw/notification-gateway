import pytest
from pydantic import ValidationError

from notification_gateway.config import Settings


def test_mcp_requires_issuer() -> None:
    with pytest.raises(ValidationError, match="OAUTH_ISSUER"):
        Settings(mcp_enabled=True)


def test_json_settings_are_parsed(settings: Settings) -> None:
    assert settings.channels["personal"] == "private-personal"
    assert settings.rest_api_keys == {"tests": "test-secret"}
