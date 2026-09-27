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
    interaction.user.display_name = "Owner"
    interaction.channel_id = 101
    interaction.guild_id = 1
    interaction.type = discord.InteractionType.application_command
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
    interaction.response.is_done.return_value = False

    async def send_message(**kwargs: object) -> None:
        interaction.response.is_done.return_value = True

    interaction.response.send_message = mock.AsyncMock(side_effect=send_message)
    interaction.response.send_modal = mock.AsyncMock()

    async def defer(*, ephemeral: bool, thinking: bool) -> None:
        assert ephemeral is (command not in {"codex", "Ask Codex"}) and thinking is True
        interaction.response.is_done.return_value = True

    interaction.response.defer = mock.AsyncMock(side_effect=defer)
    interaction.edit_original_response = mock.AsyncMock()
    interaction.delete_original_response = mock.AsyncMock()
    interaction.followup = mock.Mock()
    interaction.followup.send = mock.AsyncMock()
    return interaction


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


def _selected_message(
    *, content: str = "Read this", attachments: list[mock.Mock] | None = None
) -> mock.Mock:
    message = mock.Mock(spec=discord.Message)
    message.content = content
    message.attachments = attachments or []
    message.author = mock.Mock(display_name="Author")
    return message


def _attachment(
    *,
    content_type: str | None = "image/png",
    data: bytes = b"\x89PNG\r\n\x1a\nimage",
    filename: str = "example.png",
) -> mock.Mock:
    attachment = mock.Mock(spec=discord.Attachment)
    attachment.content_type = content_type
    attachment.filename = filename
    attachment.size = len(data)
    attachment.read = mock.AsyncMock(return_value=data)
    return attachment


def _submitted_modal(
    *, client: bot._Client, message: mock.Mock, question: str = "Explain this"
) -> tuple[bot._AskMessageModal, mock.Mock]:
    menu_interaction = _interaction(command="Ask Codex")
    resolved = mock.Mock()
    resolved.get.return_value = message
    with mock.patch.object(
        discord.app_commands.Namespace,
        "_get_resolved_items",
        return_value=resolved,
    ):
        asyncio.run(client._commands._call(menu_interaction))
    modal = menu_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, bot._AskMessageModal)
    modal.question._value = question
    submit = _interaction(command="Ask Codex")
    submit.command = None
    submit.type = discord.InteractionType.modal_submit
    return modal, submit


@pytest.mark.parametrize(
    ("content", "has_image"),
    [
        ("Selected text", False),
        ("", True),
        ("Selected text", True),
    ],
)
def test_message_menu_reads_selected_text(
    client: bot._Client,
    content: str,
    has_image: bool,
) -> None:
    attachment = _attachment() if has_image else None
    message = _selected_message(
        content=content, attachments=[attachment] if attachment else None
    )
    modal, submit = _submitted_modal(
        client=client,
        message=message,
        question="Explain\nthis",
    )

    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="selected-thread", text="Answer"),
    ) as reply:
        asyncio.run(modal.on_submit(submit))

    options = reply.call_args.kwargs
    assert options["thread_id"] is None
    assert "Explain\nthis" in options["prompt"]
    assert (content or "[No text]") in options["prompt"]
    assert options["image"] == (
        conversation.Image(media_type="image/png", data=attachment.read.return_value)
        if attachment
        else None
    )
    initial = submit.response.send_message.call_args.kwargs
    assert initial["ephemeral"] is False
    assert initial["content"] == "> Owner: Explain\n> this"
    assert initial["embed"].description == "Working..."
    answer = submit.edit_original_response.call_args.kwargs
    assert answer["content"] == initial["content"]
    assert answer["embed"].description == "Answer"
    submit.followup.send.assert_not_awaited()
    submit.delete_original_response.assert_not_awaited()

    slash = _interaction()
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="selected-thread", text="Follow-up"),
    ) as continued:
        asyncio.run(client._commands._call(slash))
    assert continued.call_args.kwargs["thread_id"] == "selected-thread"


def test_message_menu_rejects_non_owner_before_reading(
    client: bot._Client,
) -> None:
    attachment = _attachment()
    modal, submit = _submitted_modal(
        client=client,
        message=_selected_message(attachments=[attachment]),
    )
    submit.user.id = 99
    with mock.patch.object(conversation, "reply") as reply:
        asyncio.run(modal.on_submit(submit))
    attachment.read.assert_not_awaited()
    reply.assert_not_called()
    assert submit.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_selected_message(content=""), "no text or image"),
        (
            _selected_message(
                attachments=[
                    _attachment(
                        content_type="image/gif",
                        data=b"GIF89a",
                        filename="example.gif",
                    ),
                ],
            ),
            "PNG, JPEG, or WebP",
        ),
        (
            _selected_message(
                attachments=[_attachment(data=b"x" * (8 * 1024 * 1024 + 1))]
            ),
            "too large",
        ),
    ],
)
def test_invalid_selected_message_is_private(
    client: bot._Client, message: mock.Mock, expected: str
) -> None:
    modal, submit = _submitted_modal(client=client, message=message)
    with mock.patch.object(conversation, "reply") as reply:
        asyncio.run(modal.on_submit(submit))
    reply.assert_not_called()
    submit.response.defer.assert_not_awaited()
    sent = submit.response.send_message.call_args.kwargs
    assert expected in sent["content"]
    assert sent["ephemeral"] is True


@pytest.mark.parametrize(
    ("download_error", "cause"),
    [
        (
            discord.NotFound(
                mock.Mock(status=404, reason="Not Found"), "missing image"
            ),
            "NotFound",
        ),
        (OSError("network down"), "OSError"),
    ],
)
def test_image_download_failure_preserves_session(
    client: bot._Client,
    capsys: pytest.CaptureFixture[str],
    download_error: Exception,
    cause: str,
) -> None:
    attachment = _attachment()
    attachment.read.side_effect = download_error
    modal, submit = _submitted_modal(
        client=client,
        message=_selected_message(attachments=[attachment]),
    )
    with mock.patch.object(conversation, "reply") as reply:

        async def submit_with_library_error_handler() -> None:
            try:
                await modal.on_submit(submit)
            except bot._MessageInputError as error:
                await modal.on_error(submit, error)

        asyncio.run(submit_with_library_error_handler())
    reply.assert_not_called()
    assert submit.response.send_message.call_args.kwargs["ephemeral"] is False
    assert "Could not download" in submit.followup.send.call_args.kwargs["content"]
    assert submit.followup.send.call_args.kwargs["ephemeral"] is True
    submit.edit_original_response.assert_not_awaited()
    submit.delete_original_response.assert_awaited_once()
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-2]["event"] == "command_failed"
    assert records[-2]["command"] == "Ask Codex"
    assert records[-2]["cause"] == cause
    assert records[-1]["event"] == "error_response_sent"


def test_invalid_image_response_is_private(client: bot._Client) -> None:
    attachment = _attachment(data=b"not an image")
    modal, submit = _submitted_modal(
        client=client,
        message=_selected_message(attachments=[attachment]),
    )
    with mock.patch.object(conversation, "reply") as reply:

        async def submit_with_library_error_handler() -> None:
            try:
                await modal.on_submit(submit)
            except bot._MessageInputError as error:
                await modal.on_error(submit, error)

        asyncio.run(submit_with_library_error_handler())

    reply.assert_not_called()
    assert submit.response.send_message.call_args.kwargs["ephemeral"] is False
    error_message = submit.followup.send.call_args.kwargs["content"]
    assert "not a PNG, JPEG, or WebP" in error_message
    assert submit.followup.send.call_args.kwargs["ephemeral"] is True
    submit.edit_original_response.assert_not_awaited()
    submit.delete_original_response.assert_awaited_once()


@pytest.mark.parametrize(
    ("content_type", "filename", "data", "detected_type"),
    [
        ("image/jpeg", "photo.jpg", b"\xff\xd8\xffjpeg", "image/jpeg"),
        (None, "photo.png", b"\x89PNG\r\n\x1a\nimage", "image/png"),
        ("image/webp", "photo.webp", b"RIFF\x04\x00\x00\x00WEBP", "image/webp"),
        ("image/png", "photo.png", b"RIFF\x04\x00\x00\x00WEBP", "image/webp"),
    ],
)
def test_selected_image_type(
    content_type: str | None,
    filename: str,
    data: bytes,
    detected_type: str,
) -> None:
    attachment = _attachment(content_type=content_type, data=data)
    attachment.filename = filename
    assert asyncio.run(bot._read_image(attachment=attachment)) == conversation.Image(
        media_type=detected_type,
        data=data,
    )


def test_message_menu_reads_png_with_generic_content_type(client: bot._Client) -> None:
    attachment = _attachment(content_type="application/octet-stream")
    modal, submit = _submitted_modal(
        client=client,
        message=_selected_message(attachments=[attachment]),
    )
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="saved", text="Answer"),
    ) as reply:
        asyncio.run(modal.on_submit(submit))

    assert reply.call_args.kwargs["image"] == conversation.Image(
        media_type="image/png",
        data=attachment.read.return_value,
    )


def test_image_size_and_format_are_checked_after_download() -> None:
    attachment = _attachment(data=b"invalid image")
    with pytest.raises(bot._MessageInputError, match="not a PNG, JPEG, or WebP"):
        asyncio.run(bot._read_image(attachment=attachment))

    attachment.read.return_value = b"x" * (8 * 1024 * 1024 + 1)
    with pytest.raises(bot._MessageInputError, match="too large"):
        asyncio.run(bot._read_image(attachment=attachment))


@pytest.mark.parametrize("command", ["codex", "new", "session", "Ask Codex"])
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


@pytest.mark.parametrize("command", ["codex", "new", "session", "Ask Codex"])
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
    interaction.data["options"][0]["value"] = "hello\n\n  there"
    with (
        mock.patch.object(client, "dispatch"),
        mock.patch.object(
            conversation,
            "reply",
            return_value=conversation.Reply(thread_id="saved", text="@everyone hello"),
        ) as reply,
    ):
        asyncio.run(client._commands._call(interaction))
    reply.assert_awaited_once_with(
        prompt="hello\n\n  there", model="test-model", thread_id=None, image=None
    )
    initial = interaction.response.send_message.call_args.kwargs
    assert initial["ephemeral"] is False
    assert initial["content"] == "> Owner: hello\n> \n>   there"
    assert initial["embed"].description == "Working..."
    assert initial["embed"].color == discord.Color.blurple()
    kwargs = interaction.edit_original_response.call_args.kwargs
    assert kwargs["content"] == "> Owner: hello\n> \n>   there"
    assert kwargs["embed"].description == "@everyone hello"
    assert kwargs["embed"].footer.text == "Model: test-model"
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    interaction.followup.send.assert_not_awaited()
    interaction.delete_original_response.assert_not_awaited()
    assert (
        asyncio.run(
            client._commands._sessions.current(
                context=sessions.DiscordContext(channel_id=101, guild_id=guild_id)
            )
        )
        == "saved"
    )


def test_codex_status_keeps_long_prompt_visible(
    client: bot._Client, capsys: pytest.CaptureFixture[str]
) -> None:
    interaction = _interaction()
    interaction.data["options"][0]["value"] = "🙂" * 1200
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="saved", text="Answer"),
    ):
        asyncio.run(client._commands._call(interaction))

    initial = interaction.response.send_message.call_args.kwargs
    assert initial["content"].startswith("> Owner: 🙂")
    assert initial["content"].endswith("…")
    assert len(initial["content"].encode("utf-16-le")) // 2 <= 2000
    assert initial["embed"].description == "Working..."
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    acknowledgement = next(
        record for record in records if record["event"] == "request_ack_sent"
    )
    assert acknowledgement["message_characters"] == len(initial["content"]) + len(
        initial["embed"]
    )


def test_codex_shows_available_turn_duration(
    client: bot._Client, capsys: pytest.CaptureFixture[str]
) -> None:
    interaction = _interaction()
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(
            thread_id="saved",
            text="Hello",
            duration_ms=2500,
        ),
    ):
        asyncio.run(client._commands._call(interaction))

    embed = interaction.edit_original_response.call_args.kwargs["embed"]
    quoted_message = interaction.edit_original_response.call_args.kwargs["content"]
    assert embed.footer.text == "Model: test-model · Turn: 2.5 s"
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    record = next(record for record in records if record["event"] == "reply_sent")
    assert record["message_characters"] == (
        len("Hello") + len(embed.footer.text) + len(quoted_message)
    )


@pytest.mark.parametrize("command", ["codex", "new", "session"])
def test_missing_channel_fails_without_model(client: bot._Client, command: str) -> None:
    interaction = _interaction(command=command)
    interaction.channel_id = None
    with mock.patch.object(conversation, "reply") as reply:
        asyncio.run(client._commands._call(interaction))
    reply.assert_not_called()
    if command == "codex":
        assert "no channel ID" in interaction.followup.send.call_args.kwargs["content"]
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        interaction.delete_original_response.assert_awaited_once()
    else:
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


@pytest.mark.parametrize("character", ["x", "🙂"])
def test_long_private_response_is_truncated(character: str) -> None:
    interaction = _interaction()
    text = character * 5000
    asyncio.run(bot._edit_deferred_response(interaction=interaction, text=text))

    kwargs = interaction.edit_original_response.call_args.kwargs
    displayed = kwargs["content"]
    assert displayed.endswith("[Reply truncated. Ask for a shorter reply.]")
    assert len(displayed.encode("utf-16-le")) // 2 <= 2000
    assert "attachments" not in kwargs


@pytest.mark.parametrize("character", ["x", "🙂"])
def test_long_public_answer_is_truncated(character: str) -> None:
    interaction = _interaction()
    text = character * 5000
    asyncio.run(
        bot._publish_answer(
            interaction=interaction,
            text=text,
            quoted_user_message="> Owner: question",
            footer="Model: test-model",
        ),
    )

    kwargs = interaction.edit_original_response.call_args.kwargs
    displayed = kwargs["embed"].description
    assert displayed.endswith("[Reply truncated. Ask for a shorter reply.]")
    assert len(displayed.encode("utf-16-le")) // 2 <= 4096
    assert "attachments" not in kwargs


@pytest.mark.parametrize("text", ["x" * 4096, "🙂" * 2048])
def test_public_answer_fits_embed_description(text: str) -> None:
    interaction = _interaction()

    asyncio.run(
        bot._publish_answer(
            interaction=interaction,
            text=text,
            quoted_user_message="> Owner: question",
            footer="Model: test-model",
        ),
    )

    kwargs = interaction.edit_original_response.call_args.kwargs
    assert kwargs["content"] == "> Owner: question"
    assert kwargs["embed"].description == text


def test_long_quoted_message_preserves_answer() -> None:
    interaction = _interaction()
    answer = "x" * 4096

    asyncio.run(
        bot._publish_answer(
            interaction=interaction,
            text=answer,
            quoted_user_message="> Owner: " + "🙂" * 1200,
            footer="Model: test-model",
        ),
    )

    kwargs = interaction.edit_original_response.call_args.kwargs
    displayed_quote = kwargs["content"]
    assert displayed_quote.startswith("> Owner: ")
    assert displayed_quote.endswith("…")
    assert len(displayed_quote.encode("utf-16-le")) // 2 <= 2000
    assert kwargs["embed"].description == answer


@pytest.mark.parametrize(
    "error",
    [
        conversation.ConversationError("Safe failure."),
        RuntimeError("secret-canary"),
        OSError("secret-canary"),
        TimeoutError("secret-canary"),
    ],
)
def test_error_responses_do_not_expose_diagnostics(
    client: bot._Client, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    interaction = _interaction()
    with mock.patch.object(conversation, "reply", side_effect=error):
        asyncio.run(client._commands._call(interaction))
    message = interaction.followup.send.call_args.kwargs["content"]
    assert "secret-canary" not in message
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is False
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure = next(record for record in records if record["event"] == "command_failed")
    assert failure["error_message"] == str(error)
    interaction.edit_original_response.assert_not_awaited()
    interaction.delete_original_response.assert_awaited_once()


def test_command_failure_logs_sdk_cause(
    client: bot._Client, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        raise RuntimeError("model unavailable")
    except RuntimeError as error:
        failure = conversation.ConversationError("Codex could not complete the reply.")
        failure.__cause__ = error

    interaction = _interaction()
    with mock.patch.object(conversation, "reply", side_effect=failure):
        asyncio.run(client._commands._call(interaction))

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure_record = next(
        record for record in records if record["event"] == "command_failed"
    )
    assert failure_record["error"] == "ConversationError"
    assert failure_record["cause"] == "RuntimeError"
    assert failure_record["cause_message"] == "model unavailable"
    message = interaction.followup.send.call_args.kwargs["content"]
    assert "model unavailable" not in message
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


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
    interaction.delete_original_response.assert_awaited_once()
    assert "failed" in interaction.followup.send.call_args.kwargs["content"]
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
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
        assert "timed out" in interaction.followup.send.call_args.kwargs["content"]
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True

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


@pytest.mark.parametrize("phase", ["acknowledge", "send_reply"])
def test_discord_failure_logs_identify_request_and_stage(
    client: bot._Client, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    interaction = _interaction()
    original = discord.NotFound(
        mock.Mock(status=404, reason="Not Found"),
        {
            "code": 10062 if phase == "acknowledge" else 10008,
            "message": "secret-canary",
        },
    )
    secondary = discord.NotFound(
        mock.Mock(status=404, reason="Not Found"),
        {"code": 10015, "message": "secret-canary"},
    )
    if phase == "acknowledge":
        interaction.response.send_message.side_effect = [original, secondary]
    else:
        interaction.edit_original_response.side_effect = original
        interaction.followup.send.side_effect = secondary
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="saved", text="private-response"),
    ) as reply:
        asyncio.run(client._commands._call(interaction))
    assert reply.await_count == (0 if phase == "acknowledge" else 1)
    output = capsys.readouterr().out
    assert "secret-canary" in output
    assert "private-response" not in output
    records = [json.loads(line) for line in output.splitlines()]
    failures = [record for record in records if record["level"] == "ERROR"]
    first, command_error, second = failures
    assert first["event"] == (
        "request_ack_failed" if phase == "acknowledge" else "reply_failed"
    )
    assert command_error["event"] == "command_failed"
    assert all("phase" not in record for record in records)
    assert first["command"] == "codex"
    assert first["interaction_id"] == second["interaction_id"] == 12345
    assert first["user_id"] == 42
    assert first["reply_characters"] == (
        len(interaction.response.send_message.call_args_list[0].kwargs["content"])
        + len(interaction.response.send_message.call_args_list[0].kwargs["embed"])
        if phase == "acknowledge"
        else len("private-response")
    )
    assert second["message_characters"] > 0
    assert first["guild_id"] == 1
    assert first["channel_id"] == 101
    assert first["http_status"] == 404
    assert first["discord_code"] == (10062 if phase == "acknowledge" else 10008)
    assert first["age_seconds"] >= 5
    assert first["acknowledged"] is (phase != "acknowledge")
    assert first["expired"] is False
    assert any("bot.py:" in frame for frame in first["frames"])
    assert second["event"] == "error_response_failed"
    assert second["discord_code"] == 10015
    if phase == "send_reply":
        interaction.delete_original_response.assert_awaited_once()


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
    ("text", "public"),
    [
        ("Hello🙂", False),
        ("x" * 2500, False),
        ("x" * 2500, True),
    ],
)
def test_response_log_sizes(
    capsys: pytest.CaptureFixture[str], text: str, public: bool
) -> None:
    interaction = _interaction()
    if public:
        asyncio.run(
            bot._publish_answer(
                interaction=interaction,
                text=text,
                quoted_user_message="> Owner: question",
                footer="Model: test-model",
            ),
        )
        kwargs = interaction.edit_original_response.call_args.kwargs
        displayed_characters = (
            len(kwargs["content"])
            + len(kwargs["embed"].description)
            + len(kwargs["embed"].footer.text)
        )
    else:
        asyncio.run(bot._edit_deferred_response(interaction=interaction, text=text))
        kwargs = interaction.edit_original_response.call_args.kwargs
        displayed_characters = len(kwargs["content"])
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "reply_sent"
    assert record["guild_id"] == 1
    assert record["channel_id"] == 101
    assert record["user_id"] == 42
    assert record["interaction_id"] == 12345
    assert record["reply_characters"] == len(text)
    assert record["message_characters"] == displayed_characters


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
