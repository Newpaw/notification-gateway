from __future__ import annotations

from pathlib import Path

import pytest

from notification_gateway.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "notifications.db"),
        ntfy_base_url="https://ntfy.example.test",
        public_base_url="https://notify.example.test",
        channel_topics_json='{"personal":"private-personal","urgent":"private-urgent"}',
        rest_api_keys_json='{"tests":"test-secret"}',
    )
