"""A Discord View for publishing private responses in their original channel."""

import datetime
import typing

import discord

from codex_discord_bot import event_log


class PublishView(discord.ui.View):
    """A Discord View providing a Publish button on a private response.

    `source` is the command or modal interaction that created the private response.
    Its token remains necessary for deleting or updating the private response.
    Each button click has a separate interaction for sending the public copy.
    The private response is removed only after confirmed public delivery.

    Publish buttons stop working after a restart because their state is in memory.
    """

    def __init__(
        self,
        *,
        source: discord.Interaction,
        owner_id: int,
    ) -> None:
        # The source token's 15 minutes include time spent generating the answer.
        # Reserve 30 seconds for deletion or control updates before it expires.
        self._expires_at = source.expires_at - datetime.timedelta(seconds=30)
        # View.timeout measures idle time from when the controls are sent or the
        # last accepted click. Keep it aligned with the fixed publishing deadline.
        super().__init__(
            timeout=max(0, (self._expires_at - discord.utils.utcnow()).total_seconds())
        )
        self._source = source
        self._owner_id = owner_id
        self._published_message_id: int | None = None
        self._button.label = f"Publish (until {self._expires_at:%H:%M} UTC)"

    @property
    def _published_url(self) -> str | None:
        if self._published_message_id is None:
            return None
        return (
            f"https://discord.com/channels/{self._source.guild_id or '@me'}/"
            f"{self._source.channel_id}/{self._published_message_id}"
        )

    @typing.override
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Authorize button clicks before discord.py calls the button handler.

        Button clicks bypass command authorization, so repeat the checks here.
        Returning False prevents discord.py from running the button callback.
        """
        if (
            interaction.user.id != self._owner_id
            or interaction.user.id != self._source.user.id
            or interaction.channel_id != self._source.channel_id
            or interaction.guild_id != self._source.guild_id
            or not interaction.is_user_integration()
            or interaction.is_guild_integration()
        ):
            await self._send_private_notice(
                interaction=interaction,
                text="Only the original owner can publish this response here.",
            )
            return False
        # A delayed timeout task must not allow publication after the cleanup
        # margin starts; reject late clicks before sending a public copy.
        remaining = (self._expires_at - discord.utils.utcnow()).total_seconds()
        if remaining <= 0:
            await self.on_timeout()
            await self._send_private_notice(
                interaction=interaction, text="Publishing has expired."
            )
            return False
        # discord.py restarts the idle timer after an accepted click. Reset its
        # duration to the time left so clicks cannot extend the fixed deadline.
        self.timeout = remaining
        return True

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.secondary)
    async def _button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PublishView],
    ) -> None:
        """Handle an authorized Publish click without another Codex turn."""
        if button.disabled:
            await self._send_private_notice(
                interaction=interaction,
                text=f"Published: {self._published_url}"
                if self._published_url
                else "Publishing is unavailable. Check this channel before retrying.",
            )
            return
        message = interaction.message
        if message is None:
            await self._send_private_notice(
                interaction=interaction,
                text="The private response is unavailable.",
            )
            return

        # Disable before the network call: more clicks can arrive before Discord
        # shows the disabled button, and must not send another copy.
        button.disabled = True
        try:
            # Meet the click's 3-second response deadline without a loading reply,
            # so the follow-up creates a separate public message.
            await interaction.response.defer(thinking=False)
            published = await interaction.followup.send(
                content=message.content,
                embeds=message.embeds,
                ephemeral=False,
                wait=True,
            )
        except discord.HTTPException as error:
            # Allow retry only after a definite rejection. Code 40060 means this
            # click was already answered, so delivery is uncertain.
            button.disabled = not (400 <= error.status < 500 and error.code != 40060)
            raise
        self._published_message_id = published.id
        button.label = "Published"
        if self._published_url is not None:
            self.add_item(
                item=discord.ui.Button(label="View message", url=self._published_url)
            )
        event_log.write(
            event="publish_sent",
            interaction=interaction,
            source_interaction_id=self._source.id,
            published_message_id=self._published_message_id,
        )
        await self._delete_source()

    async def _delete_source(self) -> None:
        """Delete the private source after confirmed public delivery."""
        try:
            await self._source.delete_original_response()
        except (discord.HTTPException, OSError) as error:
            # Unknown Message means the private copy is already gone.
            if not (isinstance(error, discord.NotFound) and error.code == 10008):
                event_log.write(
                    event="publish_delete_failed",
                    interaction=self._source,
                    error=error,
                    source_interaction_id=self._source.id,
                    published_message_id=self._published_message_id,
                )
                await self._update_source()
                return
        self.stop()

    async def _update_source(self) -> None:
        """Refresh private controls when the source must remain visible.

        The public copy is a separate follow-up; updates here use the saved
        request that created the private response.
        """
        try:
            await self._source.edit_original_response(view=self)
        except (discord.HTTPException, OSError) as error:
            event_log.write(
                event="publish_update_failed",
                interaction=self._source,
                error=error,
                source_interaction_id=self._source.id,
                published_message_id=self._published_message_id,
            )

    async def _send_private_notice(
        self,
        *,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        """Send a private publishing status or error to the person who clicked.

        Discord allows one initial response per click. After acknowledgment,
        followup sends an additional message, which is explicitly kept private.
        """
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content=text,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    content=text,
                    ephemeral=True,
                )
        except (discord.HTTPException, OSError) as error:
            event_log.write(
                event="publish_feedback_failed",
                interaction=interaction,
                error=error,
                source_interaction_id=self._source.id,
            )

    @typing.override
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[PublishView],
        /,
    ) -> None:
        """Report errors from button interactions privately.

        discord.py calls this if a component check or button callback raises.
        Returning False from a check rejects the click without invoking this hook.
        """
        event_log.write(
            event="publish_failed",
            interaction=interaction,
            error=error,
            source_interaction_id=self._source.id,
            published_message_id=self._published_message_id,
        )
        if self._published_url is not None:
            text = f"Published: {self._published_url}"
        elif self._button.disabled:
            self._button.label = "Check channel"
            await self._update_source()
            text = "Delivery could not be confirmed. Check this channel; this button will not resend."
        else:
            text = (
                "Could not publish here. Check channel permissions before trying again."
            )
        await self._send_private_notice(interaction=interaction, text=text)

    @typing.override
    async def on_timeout(self) -> None:
        """Disable the visible Publish controls when publishing expires.

        discord.py calls this on idle timeout; stop() does not invoke it.
        interaction_check also calls it for clicks past the fixed deadline.
        View expiry does not edit the message, so disable its controls explicitly.
        The timer supplies no interaction; edits use the saved source token.
        """
        self.stop()
        if not self._button.disabled:
            self._button.label = "Expired"
        self._button.disabled = True
        await self._update_source()
