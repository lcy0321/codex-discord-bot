import asyncio
import datetime
import json
import sys
from pathlib import Path
from unittest import mock

import discord
import pytest

from codex_discord_bot import __main__, bot, config, conversation, sessions


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


def _interaction(*, command: str = "codex", owner: bool = True) -> mock.Mock:
    interaction = mock.Mock(spec=discord.Interaction)
    interaction.id = 12345
    del interaction.extras
    interaction.created_at = discord.utils.utcnow() - datetime.timedelta(seconds=5)
    interaction.is_expired.return_value = False
    interaction.command = mock.Mock(name=command, qualified_name=command)
    interaction.command.name = command
    interaction.user = mock.Mock(id=42 if owner else 99)
    interaction.channel_id = 101
    interaction.guild_id = 1
    interaction.type = discord.InteractionType.application_command
    interaction.command_failed = False
    options: list[dict[str, str | int]] = []
    if command == "codex":
        options.append({"name": "prompt", "type": 3, "value": "hello"})
    interaction.data = {
        "name": command,
        "type": 1,
        "options": options,
    }
    interaction.is_user_integration.return_value = True
    interaction.is_guild_integration.return_value = False
    interaction.response = mock.Mock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = mock.AsyncMock()

    async def defer(*, ephemeral: bool, thinking: bool) -> None:
        assert ephemeral is (command != "codex") and thinking is True
        interaction.response.is_done.return_value = True

    interaction.response.defer = mock.AsyncMock(side_effect=defer)
    interaction.edit_original_response = mock.AsyncMock()
    interaction.app_permissions = discord.Permissions(attach_files=True)
    interaction.filesize_limit = 10_000_000
    return interaction


def test_command_registration(client: bot._Client) -> None:
    commands = client._commands.get_commands()
    assert {command.name for command in commands} == {"codex", "new", "session"}
    for command in commands:
        payload = command.to_dict(client._commands)
        assert payload["integration_types"] == [1]
        assert payload["contexts"] == [0, 1, 2]
    assert client.intents.value == 0


@pytest.mark.parametrize("command", ["codex", "new", "session"])
def test_non_owner_cannot_touch_sessions(
    client: bot._Client, tmp_path: Path, command: str
) -> None:
    path = tmp_path / "sessions.toml"
    original = '[sessions]\n"guild:1:channel:101" = "old"\n'
    path.write_text(original)
    interaction = _interaction(command=command, owner=False)
    with (
        mock.patch.object(conversation, "reply") as reply,
        mock.patch.object(client._commands._sessions, "_read") as read,
        mock.patch.object(client._commands._sessions, "_write") as write,
    ):
        asyncio.run(client._commands._call(interaction))
    reply.assert_not_called()
    read.assert_not_called()
    write.assert_not_called()
    interaction.response.defer.assert_not_called()
    interaction.response.send_message.assert_awaited_once()
    assert path.read_text() == original


@pytest.mark.parametrize("command", ["codex", "new", "session"])
def test_guild_install_rejected(client: bot._Client, command: str) -> None:
    interaction = _interaction(command=command)
    interaction.is_guild_integration.return_value = True
    with mock.patch.object(conversation, "reply") as reply:
        asyncio.run(client._commands._call(interaction))
    reply.assert_not_called()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.parametrize("guild_id", [1, None])
def test_codex_uses_interaction_context(
    client: bot._Client, guild_id: int | None
) -> None:
    interaction = _interaction()
    interaction.guild_id = guild_id
    with (
        mock.patch.object(client, "dispatch"),
        mock.patch.object(
            conversation,
            "reply",
            return_value=conversation.Reply(thread_id="saved", text="@everyone hello"),
        ) as reply,
    ):
        asyncio.run(client._commands._call(interaction))
    reply.assert_awaited_once_with(prompt="hello", model="test-model", thread_id=None)
    interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
    kwargs = interaction.edit_original_response.call_args.kwargs
    assert kwargs["content"] == "@everyone hello"
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert (
        asyncio.run(
            client._commands._sessions.current(
                context=sessions.DiscordContext(channel_id=101, guild_id=guild_id)
            )
        )
        == "saved"
    )


@pytest.mark.parametrize("command", ["codex", "new", "session"])
def test_missing_channel_fails_without_model(client: bot._Client, command: str) -> None:
    interaction = _interaction(command=command)
    interaction.channel_id = None
    with mock.patch.object(conversation, "reply") as reply:
        asyncio.run(client._commands._call(interaction))
    reply.assert_not_called()
    assert (
        "no channel ID"
        in interaction.edit_original_response.call_args.kwargs["content"]
    )


def test_query_and_reset_preserve_other_context(
    client: bot._Client, tmp_path: Path
) -> None:
    path = tmp_path / "sessions.toml"
    path.write_text(
        '[sessions]\n"guild:1:channel:101" = "old"\n"guild:1:channel:102" = "other"\n'
    )
    client._commands._sessions = sessions.ChannelSessions(
        mapping_path=path, model="test-model"
    )
    with (
        mock.patch.object(client, "dispatch"),
        mock.patch.object(conversation, "reply") as reply,
    ):
        query = _interaction(command="session")
        asyncio.run(client._commands._call(query))
        assert query.edit_original_response.call_args.kwargs["content"] == "old"
        reset = _interaction(command="new")
        asyncio.run(client._commands._call(reset))
        query = _interaction(command="session")
        asyncio.run(client._commands._call(query))
        assert (
            query.edit_original_response.call_args.kwargs["content"]
            == "No session yet."
        )
    reply.assert_not_called()
    assert (
        asyncio.run(
            client._commands._sessions.current(
                context=sessions.DiscordContext(channel_id=102, guild_id=1)
            )
        )
        == "other"
    )


@pytest.mark.parametrize("text", ["x" * 2001, "🙂" * 1001])
def test_long_reply_attachment(text: str) -> None:
    interaction = _interaction()
    received: list[bytes] = []

    async def capture(**kwargs: object) -> None:
        attachments = kwargs["attachments"]
        assert isinstance(attachments, list)
        attachment = attachments[0]
        assert isinstance(attachment, discord.File)
        received.append(attachment.fp.read())

    interaction.edit_original_response.side_effect = capture
    asyncio.run(bot._reply(interaction=interaction, text=text))
    assert received == [text.encode("utf-8")]
    assert (
        len(
            interaction.edit_original_response.call_args.kwargs["content"].encode(
                "utf-16-le"
            )
        )
        // 2
        <= 2000
    )
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.parametrize("permission", [False, True])
def test_attachment_unavailable_is_explicit(permission: bool) -> None:
    interaction = _interaction()
    interaction.app_permissions = discord.Permissions(attach_files=permission)
    interaction.filesize_limit = 10
    asyncio.run(bot._reply(interaction=interaction, text="x" * 3000))
    kwargs = interaction.edit_original_response.call_args.kwargs
    assert "truncated" in kwargs["content"]
    assert kwargs["attachments"] == []


@pytest.mark.parametrize(
    "error",
    [
        conversation.ConversationError("Safe failure."),
        RuntimeError("secret-canary"),
        OSError("secret-canary"),
        TimeoutError("secret-canary"),
    ],
)
def test_errors_are_safe(
    client: bot._Client, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    interaction = _interaction()
    with mock.patch.object(conversation, "reply", side_effect=error):
        asyncio.run(client._commands._call(interaction))
    message = interaction.edit_original_response.call_args.kwargs["content"]
    assert "secret-canary" not in message
    output = capsys.readouterr()
    assert "secret-canary" not in output.out + output.err
    interaction.edit_original_response.assert_awaited_once()


def test_setup_syncs_commands(client: bot._Client) -> None:
    with mock.patch.object(client._commands, "sync") as sync:
        asyncio.run(client.setup_hook())
    sync.assert_awaited_once()


def test_delivery_failure_is_not_retried(client: bot._Client) -> None:
    interaction = _interaction()
    error = discord.HTTPException(
        mock.Mock(status=403, reason="Forbidden"), "secret-canary"
    )
    interaction.edit_original_response.side_effect = error
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="saved", text="Hello"),
    ) as reply:
        asyncio.run(client._commands._call(interaction))
    reply.assert_awaited_once()
    assert (
        asyncio.run(
            client._commands._sessions.current(
                context=sessions.DiscordContext(channel_id=101, guild_id=1)
            )
        )
        == "saved"
    )


def test_queue_timeout_cancels_pending_work(client: bot._Client) -> None:
    async def exercise() -> None:
        cancelled = asyncio.Event()

        async def wait(**kwargs: object) -> conversation.Reply:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

        interaction = _interaction()
        timeout = asyncio.timeout

        def short_timeout(*, delay: float) -> asyncio.Timeout:
            assert delay == 600
            return timeout(0.01)

        with (
            mock.patch.object(client._commands._sessions, "reply", side_effect=wait),
            mock.patch.object(bot.asyncio, "timeout", side_effect=short_timeout),
        ):
            await client._commands._call(interaction)
        assert cancelled.is_set()
        assert (
            "timed out"
            in interaction.edit_original_response.call_args.kwargs["content"]
        )

    asyncio.run(exercise())


def test_empty_prompt_does_not_call_model(client: bot._Client) -> None:
    interaction = _interaction()
    interaction.data["options"][0]["value"] = "  "
    with (
        mock.patch.object(conversation, "reply") as reply,
        mock.patch.object(client, "dispatch"),
    ):
        asyncio.run(client._commands._call(interaction))
    reply.assert_not_called()
    interaction.response.defer.assert_not_awaited()
    interaction.edit_original_response.assert_not_awaited()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert "non-empty" in kwargs["content"]
    assert kwargs["ephemeral"] is True


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


@pytest.mark.parametrize("phase", ["defer", "send_reply"])
def test_discord_failure_logs_identify_request_and_stage(
    client: bot._Client, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    interaction = _interaction()
    original = discord.NotFound(
        mock.Mock(status=404, reason="Not Found"),
        {"code": 10062 if phase == "defer" else 10008, "message": "secret-canary"},
    )
    secondary = discord.NotFound(
        mock.Mock(status=404, reason="Not Found"),
        {"code": 10015, "message": "secret-canary"},
    )
    if phase == "defer":
        interaction.response.defer.side_effect = original
        interaction.response.send_message.side_effect = secondary
    else:
        interaction.edit_original_response.side_effect = [original, secondary]
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="saved", text="private-response"),
    ) as reply:
        asyncio.run(client._commands._call(interaction))
    assert reply.await_count == (0 if phase == "defer" else 1)
    output = capsys.readouterr().out
    assert "secret-canary" not in output
    assert "private-response" not in output
    records = [json.loads(line) for line in output.splitlines()]
    if phase == "defer":
        first, second = records
        assert first["event"] == "command_failed"
    else:
        first, command_error, second = records
        assert first["event"] == "reply_failed"
        assert command_error["event"] == "command_failed"
    assert all("phase" not in record for record in records)
    assert first["command"] == "codex"
    assert first["interaction_id"] == second["interaction_id"] == 12345
    assert first["user_id"] == 42
    assert first["reply_characters"] == (
        0 if phase == "defer" else len("private-response")
    )
    assert second["message_characters"] > 0
    assert first["guild_id"] == 1
    assert first["channel_id"] == 101
    assert first["http_status"] == 404
    assert first["discord_code"] == (10062 if phase == "defer" else 10008)
    assert first["age_seconds"] >= 5
    assert first["acknowledged"] is (phase != "defer")
    assert first["expired"] is False
    assert any("bot.py:" in frame for frame in first["frames"])
    assert second["event"] == "error_response_failed"
    assert second["discord_code"] == 10015


@pytest.mark.parametrize("command", ["new", "session"])
def test_management_error_before_defer_is_private(
    client: bot._Client, command: str
) -> None:
    interaction = _interaction(command=command)
    interaction.response.defer.side_effect = RuntimeError("secret-canary")
    asyncio.run(client._commands._call(interaction))
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


def test_ready_uses_service_log_format(
    client: bot._Client, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(client.on_ready())
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "discord_ready"
    assert record["level"] == "INFO"
    assert "time" in record


@pytest.mark.parametrize(
    ("text", "attach"),
    [("Hello🙂", True), ("x" * 2500, True), ("x" * 2500, False)],
)
def test_reply_log_sizes(
    capsys: pytest.CaptureFixture[str], text: str, attach: bool
) -> None:
    interaction = _interaction()
    interaction.app_permissions = discord.Permissions(attach_files=attach)
    asyncio.run(bot._reply(interaction=interaction, text=text))
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "reply_sent"
    assert record["guild_id"] == 1
    assert record["channel_id"] == 101
    assert record["user_id"] == 42
    assert record["interaction_id"] == 12345
    assert record["reply_characters"] == len(text)
    content = interaction.edit_original_response.call_args.kwargs["content"]
    assert record["message_characters"] == len(content)
    assert record["attachment_characters"] == (
        len(text) if attach and len(text) > 2000 else 0
    )


@pytest.mark.parametrize("failed", [False, True])
def test_denial_log_sizes(
    client: bot._Client, capsys: pytest.CaptureFixture[str], failed: bool
) -> None:
    interaction = _interaction(owner=False)
    if failed:
        interaction.response.send_message.side_effect = discord.NotFound(
            mock.Mock(status=404, reason="Not Found"),
            {"code": 10062, "message": "Unknown interaction"},
        )
    asyncio.run(client._commands._call(interaction))
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == (
        "access_denied_response_failed" if failed else "access_denied"
    )
    assert record["user_id"] == 99
    attempted = interaction.response.send_message.call_args.kwargs["content"]
    assert record["reply_characters"] == record["message_characters"] == len(attempted)
    assert record["attachment_characters"] == 0
