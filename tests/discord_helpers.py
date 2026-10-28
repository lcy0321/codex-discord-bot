"""Discord interaction mocks for command and publishing tests."""

import datetime
from unittest import mock

import discord


def interaction(
    *,
    command: str = "codex",
    owner: bool = True,
    acknowledged: bool = False,
) -> mock.Mock:
    """Create an interaction mock for testing response flows.

    Initial responses update acknowledgment state; edits update content and embeds.
    """
    interaction = mock.Mock(spec=discord.Interaction)
    interaction.id = 12345
    interaction.created_at = discord.utils.utcnow() - datetime.timedelta(seconds=5)
    interaction.expires_at = interaction.created_at + datetime.timedelta(minutes=15)
    interaction.is_expired.return_value = False
    interaction.command = mock.Mock(name=command, qualified_name=command)
    interaction.command.name = command
    interaction.user = mock.Mock(id=42 if owner else 99)
    interaction.user.display_name = "Owner"
    interaction.channel_id = 101
    interaction.guild_id = 1
    interaction.type = discord.InteractionType.application_command
    interaction.message = mock.Mock(
        spec=discord.Message,
        content="",
        embeds=[],
    )
    interaction.command_failed = False
    options: list[dict[str, str | int]] = []
    if command == "codex":
        options.append({"name": "prompt", "type": 3, "value": "hello"})
    interaction.data = {
        "name": command,
        "type": 3 if command == "Ask Codex" else 1,
        "options": options,
    }
    if command == "Ask Codex":
        interaction.data["target_id"] = "777"
    interaction.is_user_integration.return_value = True
    interaction.is_guild_integration.return_value = False
    interaction.response = mock.Mock()
    interaction.response.is_done.return_value = acknowledged
    interaction.response.type = (
        discord.InteractionResponseType.deferred_channel_message
        if acknowledged
        else None
    )

    async def send_message(**kwargs: object) -> mock.Mock:
        interaction.response.is_done.return_value = True
        interaction.response.type = discord.InteractionResponseType.channel_message
        if interaction.type is not discord.InteractionType.component:
            interaction.message.content = kwargs.get("content", "")
        return mock.Mock(spec=discord.InteractionCallbackResponse, message_id=67890)

    interaction.response.send_message = mock.AsyncMock(side_effect=send_message)

    async def send_modal(modal: discord.ui.Modal) -> None:
        interaction.response.is_done.return_value = True
        interaction.response.type = discord.InteractionResponseType.modal

    interaction.response.send_modal = mock.AsyncMock(side_effect=send_modal)

    async def defer(*, ephemeral: bool = False, thinking: bool = False) -> None:
        interaction.response.is_done.return_value = True
        interaction.response.type = (
            discord.InteractionResponseType.deferred_message_update
            if interaction.type is discord.InteractionType.component and not thinking
            else discord.InteractionResponseType.deferred_channel_message
        )

    interaction.response.defer = mock.AsyncMock(side_effect=defer)

    async def edit_original_response(**kwargs: object) -> mock.Mock:
        if "content" in kwargs:
            interaction.message.content = kwargs["content"]
        if "embed" in kwargs:
            embed = kwargs["embed"]
            interaction.message.embeds = [embed] if embed is not None else []
        return interaction.message

    interaction.edit_original_response = mock.AsyncMock(
        side_effect=edit_original_response
    )
    interaction.delete_original_response = mock.AsyncMock()
    interaction.followup = mock.Mock()
    interaction.followup.send = mock.AsyncMock(
        return_value=mock.Mock(spec=discord.WebhookMessage, id=67890)
    )
    return interaction
