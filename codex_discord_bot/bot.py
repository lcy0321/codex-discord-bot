import asyncio
import contextlib
import io
import json
import os
import signal
import traceback
import typing
from pathlib import Path

import discord
from discord import app_commands

from codex_discord_bot import auth, config, conversation, sessions

# 800 code points use at most 1,600 UTF-16 units, leaving room for the notice.
_PREVIEW_CHARACTERS = 800


def _log_event(*, event: str, level: str = "INFO", **fields: object) -> None:
    """Service events use JSON; interactive CLI output remains plain text."""
    print(
        json.dumps(
            {
                "time": discord.utils.utcnow().isoformat(),
                "level": level,
                "event": event,
                **fields,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _log_interaction(
    *,
    event: str,
    interaction: discord.Interaction,
    level: str = "INFO",
    reply_characters: int = 0,
    message_characters: int = 0,
    attachment_characters: int = 0,
    **fields: object,
) -> None:
    _log_event(
        event=event,
        level=level,
        command=interaction.command.qualified_name if interaction.command else None,
        interaction_id=interaction.id,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        user_id=interaction.user.id,
        age_seconds=round(
            (discord.utils.utcnow() - interaction.created_at).total_seconds(), 3
        ),
        acknowledged=interaction.response.is_done(),
        expired=interaction.is_expired(),
        reply_characters=reply_characters,
        message_characters=message_characters,
        attachment_characters=attachment_characters,
        **fields,
    )


def _log_error(
    *,
    event: str,
    interaction: discord.Interaction,
    error: Exception,
    reply_characters: int = 0,
    message_characters: int = 0,
    attachment_characters: int = 0,
) -> None:
    """Log failure locations and protocol metadata without exception payloads."""
    details: dict[str, object] = {"error": type(error).__name__}
    if isinstance(error, discord.HTTPException):
        details.update(http_status=error.status, discord_code=error.code)
    if isinstance(error, OSError):
        details["errno"] = error.errno
    # Traceback text and source lines can contain credentials or conversation data.
    details["frames"] = [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    _log_interaction(
        event=event,
        interaction=interaction,
        level="ERROR",
        reply_characters=reply_characters,
        message_characters=message_characters,
        attachment_characters=attachment_characters,
        **details,
    )


def _context(*, interaction: discord.Interaction) -> sessions.DiscordContext:
    if interaction.channel_id is None:
        raise sessions.SessionError("This interaction has no channel ID.")
    return sessions.DiscordContext(
        channel_id=interaction.channel_id,
        guild_id=interaction.guild_id,
    )


def _error_message(error: Exception) -> str:
    if isinstance(
        error,
        (
            auth.AuthenticationError,
            conversation.ConversationError,
            sessions.SessionError,
        ),
    ):
        return str(error)
    elif isinstance(error, TimeoutError):
        return "Request timed out while waiting or replying. Check /session before retrying."
    elif isinstance(error, OSError):
        return "Session storage failed. Check storage before sending another message."
    else:
        return "Request failed. Check /session and the service logs before retrying."


async def _send_reply(
    *,
    interaction: discord.Interaction,
    text: str,
    ephemeral: bool,
    event: str,
) -> None:
    try:
        await interaction.response.send_message(
            content=text,
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as error:
        _log_error(
            event=f"{event}_failed",
            interaction=interaction,
            error=error,
            reply_characters=len(text),
            message_characters=len(text),
        )
        raise
    _log_interaction(
        event=f"{event}_sent",
        interaction=interaction,
        reply_characters=len(text),
        message_characters=len(text),
    )


async def _update_deferred_response(
    *,
    interaction: discord.Interaction,
    text: str,
    content: str,
    event: str,
    attachment: discord.File | None = None,
) -> None:
    """Update the deferred Discord response with prepared content and an optional file.

    `text` is the full reply for character counts; `content` is the displayed body.
    Record delivery success or failure, propagating HTTP errors to the caller.
    """
    reply_characters = len(text)
    message_characters = len(content)
    attachment_characters = len(text) if attachment is not None else 0
    try:
        await interaction.edit_original_response(
            content=content,
            attachments=[attachment] if attachment is not None else [],
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as error:
        _log_error(
            event=f"{event}_failed",
            interaction=interaction,
            error=error,
            reply_characters=reply_characters,
            message_characters=message_characters,
            attachment_characters=attachment_characters,
        )
        raise
    _log_interaction(
        event=f"{event}_sent",
        interaction=interaction,
        reply_characters=reply_characters,
        message_characters=message_characters,
        attachment_characters=attachment_characters,
    )


async def _reply(
    *,
    interaction: discord.Interaction,
    text: str,
    event: str = "reply",
) -> None:
    """Edit the deferred response; long output uses one UTF-8 attachment."""
    # Budget conservatively for Discord's 2,000-character cap:
    # UTF-16 counts astral emoji twice, whereas Python len() counts them once.
    if len(text.encode("utf-16-le")) // 2 <= 2000:
        await _update_deferred_response(
            interaction=interaction,
            text=text,
            content=text,
            event=event,
        )
        return

    data = text.encode("utf-8")
    preview = text[:_PREVIEW_CHARACTERS]
    if (
        not interaction.app_permissions.attach_files
        or len(data) > interaction.filesize_limit
    ):
        content = (
            preview
            + "\n\n[Reply truncated: attachment unavailable. Ask for a shorter reply.]"
        )
        await _update_deferred_response(
            interaction=interaction, text=text, content=content, event=event
        )
        return

    # File needs a readable, seekable stream; BytesIO avoids writing private text to disk.
    with (
        io.BytesIO(data) as buffer,
        contextlib.closing(discord.File(fp=buffer, filename="reply.txt")) as attachment,
    ):
        await _update_deferred_response(
            interaction=interaction,
            text=text,
            content=preview + "\n\n[Full reply attached.]",
            event=event,
            attachment=attachment,
        )


class _Commands(app_commands.CommandTree[discord.Client]):
    def __init__(
        self,
        *,
        client: discord.Client,
        owner_id: int,
        manager: sessions.ChannelSessions,
    ) -> None:
        super().__init__(
            client=client,
            allowed_installs=app_commands.AppInstallationType(
                guild=False,
                user=True,
            ),
            allowed_contexts=app_commands.AppCommandContext(
                guild=True,
                dm_channel=True,
                private_channel=True,
            ),
        )
        self._owner_id = owner_id
        self._sessions = manager

        self.command(
            name="codex",
            description="Send a message to Codex",
        )(self._codex)

        self.command(
            name="new",
            description="Start a fresh conversation here",
        )(self._new)

        self.command(
            name="session",
            description="Show this channel's current session",
        )(self._session)

    @typing.override
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        # A tree-wide gate protects every command before session access.
        if interaction.user.id != self._owner_id:
            message = "This app is restricted to its configured owner."
        elif (
            not interaction.is_user_integration() or interaction.is_guild_integration()
        ):
            message = "Use this app through User Install."
        else:
            return True

        try:
            await interaction.response.send_message(
                content=message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            _log_interaction(
                event="access_denied",
                interaction=interaction,
                reply_characters=len(message),
                message_characters=len(message),
            )
        except discord.HTTPException as error:
            _log_error(
                event="access_denied_response_failed",
                interaction=interaction,
                error=error,
                reply_characters=len(message),
                message_characters=len(message),
            )
        return False

    async def _codex(self, interaction: discord.Interaction, prompt: str) -> None:
        if not prompt.strip():
            # Visibility is fixed by the first response, including defer.
            await _send_reply(
                interaction=interaction,
                text="Enter a non-empty message.",
                ephemeral=True,
                event="reply",
            )
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        context = _context(interaction=interaction)
        # Include queue time so a busy channel cannot exhaust the 15-minute token.
        async with asyncio.timeout(delay=600):
            reply = await self._sessions.reply(context=context, prompt=prompt)
        await _reply(interaction=interaction, text=reply.text)

    async def _new(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with asyncio.timeout(delay=600):
            await self._sessions.reset(context=_context(interaction=interaction))
        await _reply(
            interaction=interaction,
            text="New conversation on the next message. Other channels are unchanged.",
        )

    async def _session(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with asyncio.timeout(delay=600):
            thread_id = await self._sessions.current(
                context=_context(interaction=interaction)
            )
        await _reply(interaction=interaction, text=thread_id or "No session yet.")

    @typing.override
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        cause = (
            error.original
            if isinstance(error, app_commands.CommandInvokeError)
            else error
        )
        _log_error(event="command_failed", interaction=interaction, error=cause)
        message = _error_message(cause)
        try:
            if interaction.response.is_done():
                await _reply(
                    interaction=interaction, text=message, event="error_response"
                )
            else:
                await _send_reply(
                    interaction=interaction,
                    text=message,
                    ephemeral=interaction.command is None
                    or interaction.command.name != "codex",
                    event="error_response",
                )
        except discord.HTTPException:
            # Send helpers already logged the failure; do not retry the error response.
            return


class _Client(discord.Client):
    def __init__(
        self, *, settings: config.Settings, manager: sessions.ChannelSessions
    ) -> None:
        super().__init__(
            intents=discord.Intents.none(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._commands = _Commands(
            client=self,
            owner_id=settings.discord_owner_id,
            manager=manager,
        )

    @typing.override
    async def setup_hook(self) -> None:
        # setup_hook runs once per login, not after every Gateway reconnect.
        await self._commands.sync()

    async def on_ready(self) -> None:
        _log_event(event="discord_ready")


async def run(*, settings: config.Settings) -> None:
    """Serve until cancelled; the CLI runner drains pending SDK tasks on exit."""
    manager = sessions.ChannelSessions(
        mapping_path=Path(os.environ["CODEX_HOME"]).parent / "sessions.toml",
        model=settings.codex_model,
    )
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    # SIGTERM would otherwise terminate Python without running async cleanup.
    # Cancel the main task so asyncio.run also cancels and drains command tasks.
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with _Client(settings=settings, manager=manager) as client:
            await client.start(token=settings.discord_bot_token.get_secret_value())
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
