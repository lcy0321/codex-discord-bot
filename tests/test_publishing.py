"""Check publication authorization, delivery, and private-source cleanup."""

import asyncio
import datetime
import json
from unittest import mock

import discord
import pytest

from codex_discord_bot import conversation, publishing, sessions
from tests import discord_helpers


def _private_response(
    *,
    guild_id: int | None = 1,
) -> tuple[mock.Mock, publishing.PublishView]:
    source = discord_helpers.interaction(acknowledged=True)
    source.guild_id = guild_id
    source.message.content = "> Owner: hello"
    source.message.embeds = [discord.Embed(description="Answer")]
    return source, publishing.PublishView(source=source, owner_id=42)


def _button_interaction(*, view: publishing.PublishView) -> mock.Mock:
    interaction = discord_helpers.interaction()
    interaction.id = 54321
    interaction.command = None
    interaction.type = discord.InteractionType.component
    interaction.guild_id = view._source.guild_id
    interaction.channel_id = view._source.channel_id
    message = view._source.message
    assert message is not None
    interaction.message = mock.Mock(
        spec=discord.Message,
        content=message.content,
        embeds=list(message.embeds),
    )
    interaction.data = {
        "custom_id": view._button.custom_id,
        "component_type": 2,
    }
    return interaction


async def _click(
    *,
    view: publishing.PublishView,
    interaction: mock.Mock,
) -> None:
    # Exercise discord.py's authorization and on_error routing together.
    await view._scheduled_task(item=view._button, interaction=interaction)


@pytest.mark.parametrize("guild_id", [1, None])
def test_publish_copies_response_before_deleting_private_source(
    capsys: pytest.CaptureFixture[str],
    guild_id: int | None,
) -> None:
    async def exercise() -> None:
        source, view = _private_response(guild_id=guild_id)
        click = _button_interaction(view=view)
        queued_click = _button_interaction(view=view)
        operations = mock.Mock()
        operations.attach_mock(mock=click.response.defer, attribute="acknowledge")
        operations.attach_mock(mock=click.followup.send, attribute="publish")
        operations.attach_mock(mock=source.delete_original_response, attribute="delete")
        with (
            mock.patch.object(conversation, "reply") as model,
            mock.patch.object(sessions, "ChannelSessions") as manager,
        ):
            await _click(view=view, interaction=click)
        model.assert_not_called()
        manager.assert_not_called()
        assert [call[0] for call in operations.mock_calls] == [
            "acknowledge",
            "publish",
            "delete",
        ]
        public = click.followup.send.call_args.kwargs
        assert public["ephemeral"] is False
        assert public["content"] == source.message.content
        assert [embed.to_dict() for embed in public["embeds"]] == [
            embed.to_dict() for embed in source.message.embeds
        ]
        assert "view" not in public and "files" not in public
        assert view.is_finished()

        # A click queued before deletion may still reach the handler.
        await _click(view=view, interaction=queued_click)
        notice = queued_click.response.send_message.call_args.kwargs
        assert notice["ephemeral"] is True
        assert (
            f"https://discord.com/channels/{guild_id or '@me'}/101/67890"
            in notice["content"]
        )
        queued_click.followup.send.assert_not_awaited()

    asyncio.run(exercise())
    record = next(
        record
        for record in map(json.loads, capsys.readouterr().out.splitlines())
        if record["event"] == "publish_sent"
    )
    assert (
        record["interaction_id"],
        record["source_interaction_id"],
        record["published_message_id"],
    ) == (54321, 12345, 67890)


def test_publish_uses_the_message_carried_by_the_button() -> None:
    async def exercise() -> None:
        _, view = _private_response()
        click = _button_interaction(view=view)
        click.message.content = "Updated private response"
        click.message.embeds = [discord.Embed(description="Updated answer")]
        await _click(view=view, interaction=click)
        public = click.followup.send.call_args.kwargs
        assert public["content"] == "Updated private response"
        assert public["embeds"][0].description == "Updated answer"

    asyncio.run(exercise())


def test_publish_requires_the_source_message() -> None:
    async def exercise() -> None:
        source, view = _private_response()
        click = _button_interaction(view=view)
        click.message = None
        await _click(view=view, interaction=click)
        feedback = click.response.send_message.call_args.kwargs
        assert feedback["ephemeral"] is True
        assert "private response is unavailable" in feedback["content"]
        assert view._button.disabled is False
        source.edit_original_response.assert_not_awaited()
        source.delete_original_response.assert_not_awaited()
        click.response.defer.assert_not_awaited()
        click.followup.send.assert_not_awaited()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mismatch",
    [
        "user",
        "channel",
        "guild",
        "user_install",
        "guild_install",
    ],
)
def test_publish_rechecks_authorization(mismatch: str) -> None:
    async def exercise() -> None:
        source, view = _private_response()
        click = _button_interaction(view=view)
        if mismatch == "user":
            click.user.id = 99
        elif mismatch == "channel":
            click.channel_id = 102
        elif mismatch == "guild":
            click.guild_id = 2
        elif mismatch == "user_install":
            click.is_user_integration.return_value = False
        else:
            click.is_guild_integration.return_value = True
        await _click(view=view, interaction=click)
        click.followup.send.assert_not_awaited()
        source.delete_original_response.assert_not_awaited()
        source.edit_original_response.assert_not_awaited()
        notice = click.response.send_message.call_args.kwargs
        assert notice["ephemeral"] is True
        assert "original owner" in notice["content"]

    asyncio.run(exercise())


def test_concurrent_publish_clicks_send_one_copy() -> None:
    async def exercise() -> None:
        source, view = _private_response()
        first = _button_interaction(view=view)
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(**kwargs: object) -> mock.Mock:
            started.set()
            await release.wait()
            return mock.Mock(spec=discord.WebhookMessage, id=67890)

        first.followup.send.side_effect = send
        task = asyncio.create_task(coro=_click(view=view, interaction=first))
        await started.wait()
        source.delete_original_response.assert_not_awaited()
        second = _button_interaction(view=view)
        await _click(view=view, interaction=second)
        feedback = second.response.send_message.call_args.kwargs
        assert feedback["ephemeral"] is True
        assert "unavailable" in feedback["content"]
        release.set()
        await task
        first.followup.send.assert_awaited_once()
        source.delete_original_response.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "error",
    [
        discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "denied"),
        OSError("connection lost"),
        discord.NotFound(
            mock.Mock(status=404, reason="Not Found"),
            {"code": 10015, "message": "Unknown Webhook"},
        ),
    ],
)
def test_published_response_is_not_resent_after_private_delete_failure(
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    async def exercise() -> None:
        source, view = _private_response()
        source.delete_original_response.side_effect = error
        click = _button_interaction(view=view)
        await _click(view=view, interaction=click)
        assert click.followup.send.call_args.kwargs["ephemeral"] is False
        source.delete_original_response.assert_awaited_once_with()
        source.edit_original_response.assert_awaited_once_with(view=view)
        assert any(
            isinstance(item, discord.ui.Button) and item.url == view._published_url
            for item in view.children
        )
        duplicate = _button_interaction(view=view)
        await _click(view=view, interaction=duplicate)
        assert duplicate.response.send_message.call_args.kwargs["ephemeral"] is True
        assert (
            view._published_url
            in duplicate.response.send_message.call_args.kwargs["content"]
        )
        click.followup.send.assert_awaited_once()
        duplicate.followup.send.assert_not_awaited()
        source.delete_original_response.assert_awaited_once()

    asyncio.run(exercise())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure = next(
        record for record in records if record["event"] == "publish_delete_failed"
    )
    assert failure["source_interaction_id"] == 12345
    assert failure["published_message_id"] == 67890


def test_already_deleted_private_source_finishes_publishing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def exercise() -> None:
        source, view = _private_response()
        source.delete_original_response.side_effect = discord.NotFound(
            mock.Mock(status=404, reason="Not Found"),
            {"code": 10008, "message": "Unknown Message"},
        )
        click = _button_interaction(view=view)
        await _click(view=view, interaction=click)
        click.followup.send.assert_awaited_once()
        source.delete_original_response.assert_awaited_once()
        source.edit_original_response.assert_not_awaited()
        assert view.is_finished()

    asyncio.run(exercise())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(record["level"] == "ERROR" for record in records)


def test_failed_publish_acknowledgment_preserves_private_source() -> None:
    async def exercise() -> None:
        source, view = _private_response()
        click = _button_interaction(view=view)
        click.response.defer.side_effect = discord.NotFound(
            mock.Mock(status=404, reason="Not Found"),
            {"code": 10062, "message": "Unknown interaction"},
        )
        await _click(view=view, interaction=click)
        click.followup.send.assert_not_awaited()
        source.delete_original_response.assert_not_awaited()
        assert click.response.send_message.call_args.kwargs["ephemeral"] is True
        assert view._button.disabled is False
        assert source.message.content == "> Owner: hello"

    asyncio.run(exercise())


def test_publish_can_retry_a_definite_rejection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def exercise() -> None:
        source, view = _private_response()
        click = _button_interaction(view=view)
        click.followup.send.side_effect = [
            discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "denied"),
            mock.DEFAULT,
        ]
        await _click(view=view, interaction=click)
        assert view._button.disabled is False
        assert source.message.content == "> Owner: hello"
        assert click.followup.send.call_args.kwargs["ephemeral"] is True
        source.edit_original_response.assert_not_awaited()
        source.delete_original_response.assert_not_awaited()
        retry = _button_interaction(view=view)
        await _click(view=view, interaction=retry)
        assert retry.followup.send.call_args.kwargs["ephemeral"] is False
        source.delete_original_response.assert_awaited_once()

    asyncio.run(exercise())
    failure = next(
        record
        for record in map(json.loads, capsys.readouterr().out.splitlines())
        if record["event"] == "publish_failed"
    )
    assert failure["interaction_id"] == 54321
    assert failure["source_interaction_id"] == 12345


@pytest.mark.parametrize(
    "error",
    [
        OSError("connection lost"),
        TimeoutError("unknown delivery"),
        discord.HTTPException(mock.Mock(status=503, reason="Unavailable"), "unknown"),
        discord.HTTPException(
            mock.Mock(status=400, reason="Bad Request"),
            {"code": 40060, "message": "Already acknowledged"},
        ),
    ],
)
def test_uncertain_publish_outcome_is_not_retried(error: Exception) -> None:
    async def exercise() -> None:
        source, view = _private_response()
        click = _button_interaction(view=view)
        acknowledgement_error = (
            isinstance(error, discord.HTTPException) and error.code == 40060
        )
        if acknowledgement_error:
            click.response.defer.side_effect = error
        else:
            click.followup.send.side_effect = [
                error,
                mock.DEFAULT,
            ]
        await _click(view=view, interaction=click)
        assert view._button.disabled is True
        assert source.message.content == "> Owner: hello"
        assert source.message.embeds[0].description == "Answer"
        source.edit_original_response.assert_awaited_once_with(view=view)
        notice = (
            click.response.send_message
            if acknowledgement_error
            else click.followup.send
        )
        assert notice.call_args.kwargs["ephemeral"] is True
        source.delete_original_response.assert_not_awaited()
        retry = _button_interaction(view=view)
        await _click(view=view, interaction=retry)
        assert retry.response.send_message.call_args.kwargs["ephemeral"] is True
        retry.followup.send.assert_not_awaited()
        retry.response.defer.assert_not_awaited()
        assert view._button.to_component_dict().get("disabled") is True

    asyncio.run(exercise())


def test_publish_expiry_is_absolute_and_preserves_private_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        source, view = _private_response()
        view.timeout = 10000
        monkeypatch.setattr(
            target=discord.utils,
            name="utcnow",
            value=lambda: view._expires_at + datetime.timedelta(seconds=1),
        )
        click = _button_interaction(view=view)
        await _click(view=view, interaction=click)
        feedback = click.response.send_message.call_args.kwargs
        assert feedback["ephemeral"] is True
        assert "expired" in feedback["content"]
        assert view.is_finished()
        assert view._button.disabled is True
        assert source.message.content == "> Owner: hello"
        assert source.message.embeds[0].description == "Answer"
        source.edit_original_response.assert_awaited_once_with(view=view)
        source.delete_original_response.assert_not_awaited()

    asyncio.run(exercise())


@pytest.mark.parametrize("timed_out", [False, True])
def test_publish_update_failure_logs_the_source_interaction(
    capsys: pytest.CaptureFixture[str],
    timed_out: bool,
) -> None:
    async def exercise() -> None:
        source, view = _private_response()
        source.edit_original_response.side_effect = discord.NotFound(
            mock.Mock(status=404, reason="Not Found"), "expired response"
        )
        if timed_out:
            await view.on_timeout()
        else:
            source.delete_original_response.side_effect = OSError("connection lost")
            await _click(view=view, interaction=_button_interaction(view=view))

    asyncio.run(exercise())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure = next(
        record for record in records if record["event"] == "publish_update_failed"
    )
    assert failure["interaction_id"] == 12345
    assert failure["source_interaction_id"] == 12345
