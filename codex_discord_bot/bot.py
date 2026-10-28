"""Configure and run the Discord client with per-channel Codex sessions."""

import asyncio
import os
import signal
import typing
from pathlib import Path

import discord

from codex_discord_bot import commands, config, event_log, sessions


class _Client(discord.Client):
    def __init__(
        self,
        *,
        settings: config.Settings,
        manager: sessions.ChannelSessions,
    ) -> None:
        super().__init__(
            intents=discord.Intents.none(),
            # Interaction replies, edits, and follow-ups inherit this default.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._commands = commands.Commands(
            client=self,
            owner_id=settings.discord_owner_id,
            manager=manager,
        )

    @typing.override
    async def setup_hook(self) -> None:
        # setup_hook runs once per login, not after every Gateway reconnect.
        await self._commands.sync()

    @typing.override
    async def close(self) -> None:
        """Remove global commands while the HTTP client is still available.

        discord.py calls this on context exit or a terminal connection failure.
        Bound command cleanup so API delays leave time for connection cleanup.
        """
        try:
            if self.application_id is None or self.is_closed():
                return

            async with asyncio.timeout(delay=5):
                self._commands.clear_commands(guild=None)
                await self._commands.sync(guild=None)
            event_log.write(event="commands_unregistered")
        except (discord.HTTPException, OSError) as error:
            event_log.write(event="commands_unregister_failed", error=error)
        finally:
            await super().close()

    async def on_ready(self) -> None:
        event_log.write(event="discord_ready")


async def run(*, settings: config.Settings) -> None:
    """Run the Discord client with per-channel Codex sessions."""
    manager = sessions.ChannelSessions(
        mapping_path=Path(os.environ["CODEX_HOME"]).parent / "sessions.toml",
        model=settings.codex_model,
    )
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    # Without a handler, SIGTERM bypasses async cleanup.
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with _Client(settings=settings, manager=manager) as client:
            await client.start(token=settings.discord_bot_token.get_secret_value())
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
