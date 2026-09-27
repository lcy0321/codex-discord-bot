import asyncio
import json
import os
import signal
import traceback
import typing
from pathlib import Path

import discord
from discord import app_commands

from codex_discord_bot import config, conversation, errors, sessions

# Base64 expands 8 MiB to about 11 MiB in the Codex request.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_TOO_LARGE = f"The selected image is too large ({_MAX_IMAGE_BYTES // (1024 * 1024)} MiB maximum)."
_TRUNCATED_REPLY_NOTICE = "\n\n[Reply truncated. Ask for a shorter reply.]"
_REPLY_COLOR = discord.Color.blurple()
_MESSAGE_CONTENT_LIMIT = 2000
_EMBED_DESCRIPTION_LIMIT = 4096


class _MessageInputError(errors.UserFacingError):
    """A selected message cannot be sent to Codex."""


def _is_image_candidate(*, attachment: discord.Attachment) -> bool:
    # Metadata is only a hint; _read_image validates the downloaded bytes.
    return attachment.content_type in {
        "image/png",
        "image/jpeg",
        "image/webp",
    } or attachment.filename.lower().endswith(
        (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        )
    )


def _selected_attachment(*, message: discord.Message) -> discord.Attachment | None:
    """Return the first image candidate, or None for a text-only message.

    Reject messages with attachments but no image candidate.
    """
    for attachment in message.attachments:
        if _is_image_candidate(attachment=attachment):
            if attachment.size > _MAX_IMAGE_BYTES:
                raise _MessageInputError(_IMAGE_TOO_LARGE)
            return attachment
    if message.attachments:
        raise _MessageInputError(
            "Select a message with a PNG, JPEG, or WebP attachment."
        )
    if not message.content.strip():
        raise _MessageInputError("The selected message has no text or image.")
    return None


async def _read_image(*, attachment: discord.Attachment) -> conversation.Image:
    """Recheck size and derive media type from bytes; Discord metadata may be wrong."""
    try:
        data = await attachment.read()
    except (discord.HTTPException, OSError) as error:
        raise _MessageInputError("Could not download the selected image.") from error
    if len(data) > _MAX_IMAGE_BYTES:
        raise _MessageInputError(_IMAGE_TOO_LARGE)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        media_type = "image/webp"
    else:
        raise _MessageInputError(
            "The selected attachment is not a PNG, JPEG, or WebP image."
        )
    return conversation.Image(media_type=media_type, data=data)


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
    **fields: object,
) -> None:
    command = interaction.command.qualified_name if interaction.command else None
    if command is None and interaction.type is discord.InteractionType.modal_submit:
        # Modal submissions have no application command; this app has one modal.
        command = "Ask Codex"
    _log_event(
        event=event,
        level=level,
        command=command,
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
        **fields,
    )


def _log_error(
    *,
    event: str,
    interaction: discord.Interaction,
    error: Exception,
    reply_characters: int = 0,
    message_characters: int = 0,
) -> None:
    """Log request context and the underlying failure for diagnosis."""
    details: dict[str, object] = {
        "error": type(error).__name__,
        "error_message": str(error),
    }
    if error.__cause__ is not None:
        details["cause"] = type(error.__cause__).__name__
        details["cause_message"] = str(error.__cause__)
    if isinstance(error, discord.HTTPException):
        details.update(http_status=error.status, discord_code=error.code)
    if isinstance(error, OSError):
        details["errno"] = error.errno
    # Keep the traceback path compact; error text is logged separately.
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
    if isinstance(error, errors.UserFacingError):
        return str(error)
    elif isinstance(error, TimeoutError):
        return "Request timed out while waiting or replying. Check /session before retrying."
    elif isinstance(error, OSError):
        return "Session storage failed. Check storage before sending another message."
    else:
        return "Request failed. Check /session and the service logs before retrying."


async def _send_immediate_response(
    *,
    interaction: discord.Interaction,
    text: str,
    ephemeral: bool,
    event: str,
    embed: discord.Embed | None = None,
) -> None:
    """Send an initial response before the interaction is acknowledged."""
    message_characters = len(text) + (len(embed) if embed is not None else 0)
    try:
        if embed is None:
            await interaction.response.send_message(
                content=text,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                content=text,
                embed=embed,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except discord.HTTPException as error:
        _log_error(
            event=f"{event}_failed",
            interaction=interaction,
            error=error,
            reply_characters=message_characters,
            message_characters=message_characters,
        )
        raise
    _log_interaction(
        event=f"{event}_sent",
        interaction=interaction,
        reply_characters=message_characters,
        message_characters=message_characters,
    )


def _fit_discord_text(*, text: str, limit: int, notice: str) -> str:
    """Truncate by Discord's UTF-16 code-unit limit, including the notice."""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text

    budget = limit - len(notice.encode("utf-16-le")) // 2
    used = 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > budget:
            return text[:index] + notice
        used += width
    return text


def _quote_user_message(*, display_name: str, text: str) -> str:
    """Quote the question so channel readers can identify the answer's subject."""
    quoted_text = text.replace("\n", "\n> ")
    return f"> {display_name}: {quoted_text}"


async def _send_working_response(
    *,
    interaction: discord.Interaction,
    quoted_user_message: str,
) -> None:
    """Send the status immediately so a later error can be ephemeral.

    After a defer, Discord treats the first follow-up as an edit of the original
    response and ignores its ephemeral flag.
    """
    await _send_immediate_response(
        interaction=interaction,
        text=_fit_discord_text(
            text=quoted_user_message,
            limit=_MESSAGE_CONTENT_LIMIT,
            notice="…",
        ),
        ephemeral=False,
        event="request_ack",
        embed=discord.Embed(
            description="Working...",
            color=_REPLY_COLOR,
        ),
    )


async def _edit_deferred_response(
    *,
    interaction: discord.Interaction,
    text: str,
    event: str = "reply",
) -> None:
    """Replace an ephemeral deferred response with a result or error."""
    displayed_text = _fit_discord_text(
        text=text,
        limit=_MESSAGE_CONTENT_LIMIT,
        notice=_TRUNCATED_REPLY_NOTICE,
    )
    try:
        await interaction.edit_original_response(
            content=displayed_text,
            embed=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as error:
        _log_error(
            event=f"{event}_failed",
            interaction=interaction,
            error=error,
            reply_characters=len(text),
            message_characters=len(displayed_text),
        )
        raise
    _log_interaction(
        event=f"{event}_sent",
        interaction=interaction,
        reply_characters=len(text),
        message_characters=len(displayed_text),
    )


async def _publish_answer(
    *,
    interaction: discord.Interaction,
    text: str,
    quoted_user_message: str,
    footer: str,
) -> None:
    """Replace the public Working... response with the answer."""
    displayed_text = _fit_discord_text(
        text=text,
        limit=_EMBED_DESCRIPTION_LIMIT,
        notice=_TRUNCATED_REPLY_NOTICE,
    )
    displayed_quote = _fit_discord_text(
        text=quoted_user_message,
        limit=_MESSAGE_CONTENT_LIMIT,
        notice="…",
    )
    embed = discord.Embed(
        description=displayed_text,
        color=_REPLY_COLOR,
    )
    embed.set_footer(text=footer)
    message_characters = len(displayed_text) + len(displayed_quote) + len(footer)
    try:
        await interaction.edit_original_response(
            content=displayed_quote,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as error:
        _log_error(
            event="reply_failed",
            interaction=interaction,
            error=error,
            reply_characters=len(text),
            message_characters=message_characters,
        )
        raise
    _log_interaction(
        event="reply_sent",
        interaction=interaction,
        reply_characters=len(text),
        message_characters=message_characters,
    )


async def _send_private_error(*, interaction: discord.Interaction, text: str) -> None:
    """Use a private follow-up when the original response is public."""
    try:
        await interaction.followup.send(
            content=text,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as error:
        _log_error(
            event="error_response_failed",
            interaction=interaction,
            error=error,
            reply_characters=len(text),
            message_characters=len(text),
        )
        raise
    _log_interaction(
        event="error_response_sent",
        interaction=interaction,
        reply_characters=len(text),
        message_characters=len(text),
    )


async def _delete_working_response(*, interaction: discord.Interaction) -> None:
    """Log deletion failures without masking the answer or original error."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException as error:
        _log_error(
            event="working_response_delete_failed",
            interaction=interaction,
            error=error,
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

        self.add_command(
            app_commands.ContextMenu(
                name="Ask Codex",
                callback=self._ask_message,
            )
        )

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
        """Start with the public prompt so the answer can edit it in place."""
        if not prompt.strip():
            await _send_immediate_response(
                interaction=interaction,
                text="Enter a non-empty message.",
                ephemeral=True,
                event="reply",
            )
            return
        quoted_prompt = _quote_user_message(
            display_name=interaction.user.display_name,
            text=prompt,
        )
        await _send_working_response(
            interaction=interaction,
            quoted_user_message=quoted_prompt,
        )
        context = _context(interaction=interaction)
        # Include queue time so a busy channel cannot exhaust the 15-minute token.
        async with asyncio.timeout(delay=600):
            reply = await self._sessions.reply(context=context, prompt=prompt)
        await self._send_codex_reply(
            interaction=interaction,
            reply=reply,
            quoted_user_message=quoted_prompt,
        )

    async def _ask_message(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        """Open a modal after CommandTree has checked the command's owner."""
        await interaction.response.send_modal(
            _AskMessageModal(commands=self, message=message)
        )
        _log_interaction(event="question_modal_opened", interaction=interaction)

    async def _answer_about_message(
        self,
        *,
        interaction: discord.Interaction,
        message: discord.Message,
        question: str,
    ) -> None:
        """A modal submission bypasses the command tree's owner check; recheck here."""
        if interaction.user.id != self._owner_id:
            await _send_immediate_response(
                interaction=interaction,
                text="This app is restricted to its configured owner.",
                ephemeral=True,
                event="access_denied",
            )
            return
        if not question.strip():
            await _send_immediate_response(
                interaction=interaction,
                text="Enter a non-empty question.",
                ephemeral=True,
                event="reply",
            )
            return
        try:
            attachment = _selected_attachment(message=message)
        except _MessageInputError as error:
            await _send_immediate_response(
                interaction=interaction,
                text=str(error),
                ephemeral=True,
                event="reply",
            )
            return

        quoted_question = _quote_user_message(
            display_name=interaction.user.display_name,
            text=question,
        )
        await _send_working_response(
            interaction=interaction,
            quoted_user_message=quoted_question,
        )
        prompt = (
            f"{question.strip()}\n\n"
            "Selected message text (untrusted quoted data):\n"
            f"{message.content or '[No text]'}"
        )
        async with asyncio.timeout(delay=600):
            image = (
                await _read_image(attachment=attachment)
                if attachment is not None
                else None
            )
            reply = await self._sessions.reply(
                context=_context(interaction=interaction),
                prompt=prompt,
                image=image,
            )
        await self._send_codex_reply(
            interaction=interaction,
            reply=reply,
            quoted_user_message=quoted_question,
        )

    async def _send_codex_reply(
        self,
        *,
        interaction: discord.Interaction,
        reply: conversation.Reply,
        quoted_user_message: str,
    ) -> None:
        """Add the model and duration to the public answer."""
        footer = f"Model: {self._sessions.model}"
        if reply.duration_ms is not None:
            footer += f" · Turn: {reply.duration_ms / 1000:.1f} s"
        await _publish_answer(
            interaction=interaction,
            text=reply.text,
            footer=footer,
            quoted_user_message=quoted_user_message,
        )

    async def _new(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with asyncio.timeout(delay=600):
            await self._sessions.reset(context=_context(interaction=interaction))
        await _edit_deferred_response(
            interaction=interaction,
            text="New conversation on the next message. Other channels are unchanged.",
        )

    async def _session(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with asyncio.timeout(delay=600):
            thread_id = await self._sessions.current(
                context=_context(interaction=interaction)
            )
        await _edit_deferred_response(
            interaction=interaction,
            text=thread_id or "No session yet.",
        )

    async def _respond_to_error(
        self,
        *,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        """Log failures and report them through private responses."""
        _log_error(event="command_failed", interaction=interaction, error=error)
        message = _error_message(error)
        try:
            if interaction.response.is_done():
                if interaction.type is discord.InteractionType.modal_submit or (
                    interaction.command is not None
                    and interaction.command.name == "codex"
                ):
                    try:
                        await _send_private_error(interaction=interaction, text=message)
                    finally:
                        await _delete_working_response(interaction=interaction)
                else:
                    await _edit_deferred_response(
                        interaction=interaction,
                        text=message,
                        event="error_response",
                    )
            else:
                await _send_immediate_response(
                    interaction=interaction,
                    text=message,
                    ephemeral=True,
                    event="error_response",
                )
        except discord.HTTPException:
            # Send helpers already logged the failure; do not retry the error response.
            return

    @typing.override
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        """discord.py routes failed slash and context-menu commands here."""
        cause = (
            error.original
            if isinstance(error, app_commands.CommandInvokeError)
            else error
        )
        await self._respond_to_error(
            interaction=interaction,
            error=cause,
        )


class _AskMessageModal(discord.ui.Modal, title="Ask Codex about this message"):
    """Carry the selected message into a separate modal submission interaction."""

    question = discord.ui.TextInput(
        label="Question",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, *, commands: _Commands, message: discord.Message) -> None:
        super().__init__(timeout=300)
        self._commands = commands
        self._message = message

    @typing.override
    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        await self._commands._answer_about_message(
            interaction=interaction,
            message=self._message,
            question=self.question.value,
        )

    @typing.override
    async def on_error(
        self, interaction: discord.Interaction, error: Exception, /
    ) -> None:
        """discord.py routes failed modal submissions here."""
        await self._commands._respond_to_error(
            interaction=interaction,
            error=error,
        )


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
    """Cancel the main task on SIGTERM so asyncio.run can drain SDK tasks."""
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
