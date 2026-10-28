"""Handle owner commands and message questions within channel conversations."""

import asyncio
import typing

import discord
from discord import app_commands

from codex_discord_bot import conversation, errors, event_log, publishing, sessions

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
    """Validate a message for Codex input and select an optional image attachment.

    Return the first image candidate, or None for valid text-only input.
    Messages with attachments but no image candidate are rejected, even with text.
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
    """Prepare a Discord attachment as model image input.

    Recheck downloaded size and derive media type from bytes; metadata may be wrong.
    """
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


def _context(*, interaction: discord.Interaction) -> sessions.DiscordContext:
    """Build the session lookup context from an interaction's guild and channel."""
    if interaction.channel_id is None:
        raise sessions.SessionError("This interaction has no channel ID.")
    return sessions.DiscordContext(
        channel_id=interaction.channel_id,
        guild_id=interaction.guild_id,
    )


def _error_message(error: Exception) -> str:
    """Translate failures into user messages; internal errors use generic text."""
    if isinstance(error, errors.UserFacingError):
        return str(error)
    elif isinstance(error, TimeoutError):
        return "Request timed out while waiting or replying. Check /session before retrying."
    elif isinstance(error, OSError):
        return "Session storage failed. Check storage before sending another message."
    else:
        return "Request failed. Check /session and the service logs before retrying."


async def _defer_response(*, interaction: discord.Interaction) -> None:
    """Acknowledge a request whose private response will be completed later.

    A follow-up sent directly after defer edits the original response and
    inherits its visibility.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    event_log.write(event="request_ack_sent", interaction=interaction)


async def _send_immediate_response(
    *,
    interaction: discord.Interaction,
    text: str,
    view: discord.ui.View | None = None,
) -> None:
    """Complete an unacknowledged interaction with a private text response."""
    displayed_text = _fit_discord_text(
        text=text,
        limit=_MESSAGE_CONTENT_LIMIT,
        notice=_TRUNCATED_REPLY_NOTICE,
    )
    await interaction.response.send_message(
        content=displayed_text,
        view=view if view is not None else discord.utils.MISSING,
        ephemeral=True,
    )
    event_log.write(event="reply_sent", interaction=interaction)


async def _edit_deferred_response(
    *,
    interaction: discord.Interaction,
    text: str,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Replace an interaction's original private response with a result or error.

    Omitting embed or view clears previous embeds and controls.
    """
    displayed_text = _fit_discord_text(
        text=text,
        limit=_MESSAGE_CONTENT_LIMIT,
        notice=_TRUNCATED_REPLY_NOTICE,
    )
    await interaction.edit_original_response(
        content=displayed_text,
        embed=embed,
        view=view,
    )
    event_log.write(event="reply_sent", interaction=interaction)


def _fit_discord_text(*, text: str, limit: int, notice: str) -> str:
    """Fit text and any truncation notice within Discord's UTF-16 code-unit limit."""
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


def _answer_embed(
    *,
    text: str,
    footer: str,
    source_url: str | None = None,
) -> discord.Embed:
    displayed_text = _fit_discord_text(
        text=text,
        limit=_EMBED_DESCRIPTION_LIMIT,
        notice=_TRUNCATED_REPLY_NOTICE,
    )
    embed = discord.Embed(
        description=displayed_text,
        color=_REPLY_COLOR,
    )
    embed.set_footer(text=footer)
    if source_url is not None:
        embed.title = "Selected message"
        embed.url = source_url
    return embed


class Commands(app_commands.CommandTree[discord.Client]):
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
        """Authorize command execution for the configured owner's User Install."""
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
            )
            event_log.write(
                event="access_denied",
                interaction=interaction,
            )
        except discord.HTTPException as error:
            event_log.write(
                event="access_denied_response_failed",
                interaction=interaction,
                error=error,
            )
        return False

    def _publish_view(
        self, *, interaction: discord.Interaction
    ) -> publishing.PublishView | None:
        if (
            interaction.user.id == self._owner_id
            and interaction.channel_id is not None
            and interaction.is_user_integration()
            and not interaction.is_guild_integration()
            and (interaction.expires_at - discord.utils.utcnow()).total_seconds() > 30
        ):
            return publishing.PublishView(source=interaction, owner_id=self._owner_id)
        return None

    async def _codex(self, interaction: discord.Interaction, prompt: str) -> None:
        if not prompt.strip():
            await _send_immediate_response(
                interaction=interaction,
                text="Enter a non-empty message.",
                view=self._publish_view(interaction=interaction),
            )
            return
        quoted_prompt = _quote_user_message(
            display_name=interaction.user.display_name,
            text=prompt,
        )
        await _defer_response(interaction=interaction)
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
        """Open the Ask Codex question modal after CommandTree's owner check."""
        await interaction.response.send_modal(
            _AskMessageModal(commands=self, message=message)
        )
        event_log.write(event="question_modal_opened", interaction=interaction)

    async def _answer_about_message(
        self,
        *,
        interaction: discord.Interaction,
        message: discord.Message,
        question: str,
    ) -> None:
        """Answer a modal question using the selected message and current conversation.

        Recheck authorization because modal submissions bypass CommandTree checks.
        """
        if not await self.interaction_check(interaction):
            return
        if not question.strip():
            await _send_immediate_response(
                interaction=interaction,
                text="Enter a non-empty question.",
                view=self._publish_view(interaction=interaction),
            )
            return
        try:
            attachment = _selected_attachment(message=message)
        except _MessageInputError as error:
            await _send_immediate_response(
                interaction=interaction,
                text=str(error),
                view=self._publish_view(interaction=interaction),
            )
            return

        quoted_question = _quote_user_message(
            display_name=interaction.user.display_name,
            text=question,
        )
        await _defer_response(interaction=interaction)
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
            source_url=message.jump_url,
        )

    async def _send_codex_reply(
        self,
        *,
        interaction: discord.Interaction,
        reply: conversation.Reply,
        quoted_user_message: str,
        source_url: str | None = None,
    ) -> None:
        """Format a Codex reply for the private response, including model and timing."""
        footer = f"Model: {self._sessions.model}"
        if reply.duration_ms is not None:
            footer += f" · Turn: {reply.duration_ms / 1000:.1f} s"
        await _edit_deferred_response(
            interaction=interaction,
            text=_fit_discord_text(
                text=quoted_user_message,
                limit=_MESSAGE_CONTENT_LIMIT,
                notice="…",
            ),
            embed=_answer_embed(
                text=reply.text,
                footer=footer,
                source_url=source_url,
            ),
            view=self._publish_view(interaction=interaction),
        )

    async def _new(self, interaction: discord.Interaction) -> None:
        await _defer_response(interaction=interaction)
        async with asyncio.timeout(delay=600):
            await self._sessions.reset(context=_context(interaction=interaction))
        await _edit_deferred_response(
            interaction=interaction,
            text="New conversation on the next message. Other channels are unchanged.",
            view=self._publish_view(interaction=interaction),
        )

    async def _session(self, interaction: discord.Interaction) -> None:
        await _defer_response(interaction=interaction)
        async with asyncio.timeout(delay=600):
            thread_id = await self._sessions.current(
                context=_context(interaction=interaction)
            )
        await _edit_deferred_response(
            interaction=interaction,
            text=thread_id or "No session yet.",
            view=self._publish_view(interaction=interaction),
        )

    async def _respond_to_error(
        self,
        *,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        """Report command or modal failures privately, retaining diagnostics in the log."""
        event_log.write(
            event="command_failed",
            interaction=interaction,
            error=error,
        )
        # Opening a modal acknowledges the interaction without creating a message.
        if interaction.response.type is discord.InteractionResponseType.modal:
            return
        message = _error_message(error)
        view = self._publish_view(interaction=interaction)
        try:
            if interaction.response.is_done():
                await _edit_deferred_response(
                    interaction=interaction,
                    text=message,
                    view=view,
                )
            else:
                await _send_immediate_response(
                    interaction=interaction,
                    text=message,
                    view=view,
                )
        except Exception as response_error:
            event_log.write(
                event="error_response_failed",
                interaction=interaction,
                error=response_error,
            )
            if not isinstance(response_error, discord.HTTPException):
                raise

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

    def __init__(self, *, commands: Commands, message: discord.Message) -> None:
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
