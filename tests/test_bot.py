"""Check client configuration, Discord payloads, and the run entry point."""

import asyncio
import json
import sys
import typing
from unittest import mock

import discord
import pytest
from discord.webhook import async_ as webhook

from codex_discord_bot import __main__, bot, commands, publishing

if typing.TYPE_CHECKING:
    from discord.types import interactions as interaction_types


def test_command_registration(client: bot._Client) -> None:
    commands = client._commands.get_commands()
    assert {command.name for command in commands} == {
        "codex",
        "new",
        "session",
        "Ask Codex",
    }
    for command in commands:
        payload = command.to_dict(client._commands)
        assert payload["integration_types"] == [1]
        assert payload["contexts"] == [0, 1, 2]
    assert client.intents.value == 0


@pytest.mark.parametrize(
    "operation",
    [
        "initial",
        "public",
        "edit",
        "followup",
    ],
)
def test_client_mention_policy_reaches_discord_payloads(
    client: bot._Client, operation: str
) -> None:
    async def exercise() -> None:
        text = "@everyone @here <@123> <@&456>"
        user = {
            "id": "42",
            "username": "Owner",
            "discriminator": "0",
            "avatar": None,
            "global_name": None,
        }
        message = {
            "id": "67890",
            "channel_id": "101",
            "type": 0,
            "content": text,
            "author": user,
        }
        data = {
            "id": str(discord.utils.time_snowflake(discord.utils.utcnow())),
            "application_id": "222",
            "type": 2,
            "data": {
                "id": "333",
                "type": 1,
                "name": "session",
            },
            "token": "test-token",
            "version": 1,
            "channel": {
                "id": "101",
                "type": 1,
                "recipients": [user],
            },
            "user": user,
            "authorizing_integration_owners": {"1": "42"},
            "attachment_size_limit": 8 * 1024 * 1024,
        }
        source = discord.Interaction(
            data=typing.cast("interaction_types.Interaction", data),
            state=client._connection,
        )
        adapter = mock.Mock(spec=webhook.AsyncWebhookAdapter)
        # These SDK methods return awaitables without being async functions.
        adapter.create_interaction_response = mock.AsyncMock(
            return_value={
                "interaction": {"id": data["id"], "response_message_id": "67890"}
            }
        )
        adapter.edit_original_interaction_response = mock.AsyncMock(
            return_value=message
        )
        adapter.execute_webhook = mock.AsyncMock(return_value=message)
        adapter.delete_original_interaction_response = mock.AsyncMock()

        with webhook.async_context.set(adapter):
            if operation == "initial":
                await commands._send_immediate_response(interaction=source, text=text)
            elif operation == "edit":
                await source.response.defer(ephemeral=True, thinking=True)
                await commands._edit_deferred_response(interaction=source, text=text)
            elif operation == "followup":
                await source.response.defer(ephemeral=True, thinking=True)
                view = publishing.PublishView(source=source, owner_id=42)
                await view._send_private_notice(interaction=source, text=text)
            else:
                view = publishing.PublishView(source=source, owner_id=42)
                await source.response.send_message(
                    content=text,
                    ephemeral=True,
                    view=view,
                )
                click_data = {
                    **data,
                    "id": str(source.id + 1),
                    "token": "click-token",
                    "type": 3,
                    "data": {
                        "component_type": 2,
                        "custom_id": view._button.custom_id,
                    },
                    "message": message,
                }
                click = discord.Interaction(
                    data=typing.cast("interaction_types.Interaction", click_data),
                    state=client._connection,
                )
                await view._scheduled_task(item=view._button, interaction=click)
                acknowledgement = adapter.create_interaction_response.call_args.kwargs[
                    "params"
                ].payload
                assert acknowledgement["type"] == 6
                assert "data" not in acknowledgement
                assert adapter.delete_original_interaction_response.await_count == 1
                assert adapter.delete_original_interaction_response.call_args.args[
                    :2
                ] == (source.application_id, source.token)
                assert view.is_finished()

        if operation == "edit":
            payload = adapter.edit_original_interaction_response.call_args.kwargs[
                "payload"
            ]
        elif operation in {"public", "followup"}:
            payload = adapter.execute_webhook.call_args.kwargs["payload"]
        else:
            payload = adapter.create_interaction_response.call_args.kwargs[
                "params"
            ].payload["data"]
        if operation != "edit":
            assert payload.get("flags", 0) == (0 if operation == "public" else 64)
        assert payload["content"] == text
        assert payload["allowed_mentions"] == {"parse": []}

    asyncio.run(exercise())


def test_shutdown_unregisters_commands_before_closing_transport(
    client: bot._Client,
) -> None:
    async def exercise() -> None:
        client._connection.application_id = 222
        with (
            mock.patch.object(
                client.http,
                "bulk_upsert_global_commands",
                new_callable=mock.AsyncMock,
                return_value=[],
            ) as sync,
            mock.patch.object(client.http, "close") as close,
        ):
            operations = mock.Mock()
            operations.attach_mock(mock=sync, attribute="commands")
            operations.attach_mock(mock=close, attribute="transport")
            with pytest.raises(asyncio.CancelledError):
                async with client:
                    await client.setup_hook()
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel()
                    await asyncio.sleep(delay=0)
            await client.close()
            assert [call[0] for call in operations.mock_calls] == [
                "commands",
                "commands",
                "transport",
            ]
            registered, removed = sync.call_args_list
            assert registered.kwargs["payload"]
            assert removed.kwargs["payload"] == []
            assert client.is_closed()

    asyncio.run(exercise())


def test_failed_unregistration_still_closes_transport(
    client: bot._Client, capsys: pytest.CaptureFixture[str]
) -> None:
    async def exercise() -> None:
        client._connection.application_id = 222
        error = discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "denied")
        with (
            mock.patch.object(
                client.http,
                "bulk_upsert_global_commands",
                new_callable=mock.AsyncMock,
                side_effect=error,
            ),
            mock.patch.object(client.http, "close") as close,
        ):
            async with client:
                pass
            close.assert_awaited_once()
            assert client.is_closed()

    asyncio.run(exercise())
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "commands_unregister_failed"
    assert record["error"] == "Forbidden"


def test_unregistration_timeout_still_closes_transport(
    client: bot._Client, capsys: pytest.CaptureFixture[str]
) -> None:
    async def exercise() -> None:
        client._connection.application_id = 222
        timeout = asyncio.timeout

        def short_timeout(*, delay: float) -> asyncio.Timeout:
            return timeout(delay=min(delay, 0.01))

        async def stalled_request(*args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

        with (
            mock.patch.object(
                client.http,
                "bulk_upsert_global_commands",
                new_callable=mock.AsyncMock,
                side_effect=stalled_request,
            ),
            mock.patch.object(client.http, "close") as close,
            mock.patch.object(bot.asyncio, "timeout", side_effect=short_timeout),
        ):
            async with client:
                pass
            close.assert_awaited_once()
            assert client.is_closed()

    asyncio.run(asyncio.wait_for(fut=exercise(), timeout=1))
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "commands_unregister_failed"
    assert record["error"] == "TimeoutError"


def test_login_failure_does_not_attempt_unregistration(client: bot._Client) -> None:
    async def exercise() -> None:
        error = discord.LoginFailure("invalid token")
        with (
            mock.patch.object(
                client.http,
                "bulk_upsert_global_commands",
                new_callable=mock.AsyncMock,
            ) as sync,
            mock.patch.object(client.http, "close") as close,
            pytest.raises(discord.LoginFailure) as caught,
        ):
            async with client:
                raise error
        assert caught.value is error
        sync.assert_not_awaited()
        close.assert_awaited_once()

    asyncio.run(exercise())


def test_run_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(target=sys, name="argv", value=["codex-discord-bot", "run"])
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value="secret-canary")
    monkeypatch.setenv(name="DISCORD_OWNER_ID", value="42")
    with mock.patch.object(bot, "run") as run:
        assert __main__._main() == 0
    run.assert_awaited_once()
    assert run.call_args.kwargs["settings"].discord_owner_id == 42


@pytest.mark.parametrize(
    "error",
    [
        discord.LoginFailure("original detail"),
        discord.HTTPException(
            mock.Mock(status=503, reason="Unavailable"), "original detail"
        ),
    ],
)
def test_run_discord_error_propagates(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(target=sys, name="argv", value=["codex-discord-bot", "run"])
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value="test-token")
    monkeypatch.setenv(name="DISCORD_OWNER_ID", value="42")
    with (
        mock.patch.object(bot, "run", side_effect=error),
        pytest.raises(type(error)) as caught,
    ):
        __main__._main()
    assert caught.value is error
