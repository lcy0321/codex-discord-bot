"""Provide a client backed by temporary channel-session storage."""

from pathlib import Path

import pytest

from codex_discord_bot import bot, config, sessions


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bot._Client:
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value="secret-canary")
    monkeypatch.setenv(name="DISCORD_OWNER_ID", value="42")
    return bot._Client(
        settings=config.Settings(),
        manager=sessions.ChannelSessions(
            mapping_path=tmp_path / "sessions.toml", model="test-model"
        ),
    )
