"""Check command inputs, private responses, and channel conversation routing."""

import asyncio
import datetime
import json
from pathlib import Path
from unittest import mock

import discord
import pytest

from codex_discord_bot import bot, commands, conversation, publishing, sessions
from tests import discord_helpers


def _selected_message(
    *,
    content: str = "Read this",
    attachments: list[mock.Mock] | None = None,
) -> mock.Mock:
    message = mock.Mock(spec=discord.Message)
    message.content = content
    message.attachments = attachments or []
    message.author = mock.Mock(display_name="Author")
    message.jump_url = "https://discord.com/channels/1/101/777"
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
    *,
    client: bot._Client,
    message: mock.Mock,
    question: str = "Explain this",
) -> tuple[commands._AskMessageModal, mock.Mock]:
    menu_interaction = discord_helpers.interaction(command="Ask Codex")
    resolved = mock.Mock()
    resolved.get.return_value = message
    with mock.patch.object(
        discord.app_commands.Namespace,
        "_get_resolved_items",
        return_value=resolved,
    ):
        asyncio.run(client._commands._call(menu_interaction))
    modal = menu_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, commands._AskMessageModal)
    modal.question._value = question
    submit = discord_helpers.interaction(command="Ask Codex")
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
        content=content,
        attachments=[attachment] if attachment else None,
    )
    modal, submit = _submitted_modal(
        client=client,
        message=message,
        question="Explain\nthis",
    )

    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(
            thread_id="selected-thread",
            text="Answer",
        ),
    ) as reply:
        asyncio.run(modal.on_submit(submit))

    options = reply.call_args.kwargs
    assert options["thread_id"] is None
    assert "Explain\nthis" in options["prompt"]
    assert (content or "[No text]") in options["prompt"]
    assert options["image"] == (
        conversation.Image(
            media_type="image/png",
            data=attachment.read.return_value,
        )
        if attachment
        else None
    )
    submit.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    submit.response.send_message.assert_not_awaited()
    answer = submit.edit_original_response.call_args.kwargs
    assert answer["content"] == "> Owner: Explain\n> this"
    assert answer["embed"].description == "Answer"
    assert answer["embed"].url == message.jump_url
    assert isinstance(answer["view"], publishing.PublishView)
    submit.followup.send.assert_not_awaited()
    submit.delete_original_response.assert_not_awaited()

    slash = discord_helpers.interaction()
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(
            thread_id="selected-thread",
            text="Follow-up",
        ),
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
    ("result", "expected"),
    [
        (
            discord.NotFound(
                mock.Mock(status=404, reason="Not Found"), "missing image"
            ),
            "Could not download",
        ),
        (OSError("network down"), "Could not download"),
        (b"not an image", "not a PNG, JPEG, or WebP"),
    ],
)
def test_image_input_failure_is_private(
    client: bot._Client, result: bytes | Exception, expected: str
) -> None:
    attachment = _attachment()
    attachment.read.side_effect = [result]
    modal, submit = _submitted_modal(
        client=client, message=_selected_message(attachments=[attachment])
    )
    with mock.patch.object(conversation, "reply") as reply:

        async def submit_with_error_handler() -> None:
            try:
                await modal.on_submit(submit)
            except commands._MessageInputError as error:
                await modal.on_error(submit, error)

        asyncio.run(submit_with_error_handler())
    reply.assert_not_called()
    submit.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert expected in submit.edit_original_response.call_args.kwargs["content"]


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
    assert asyncio.run(
        commands._read_image(attachment=attachment)
    ) == conversation.Image(
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
    with pytest.raises(commands._MessageInputError, match="not a PNG, JPEG, or WebP"):
        asyncio.run(commands._read_image(attachment=attachment))

    attachment.read.return_value = b"x" * (8 * 1024 * 1024 + 1)
    with pytest.raises(commands._MessageInputError, match="too large"):
        asyncio.run(commands._read_image(attachment=attachment))


@pytest.mark.parametrize("command", ["codex", "new", "session", "Ask Codex"])
def test_non_owner_cannot_touch_sessions(
    client: bot._Client, tmp_path: Path, command: str
) -> None:
    path = tmp_path / "sessions.toml"
    original = '[sessions]\n"guild:1:channel:101" = "old"\n'
    path.write_text(original)
    interaction = discord_helpers.interaction(command=command, owner=False)
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
    interaction = discord_helpers.interaction(command=command)
    interaction.is_guild_integration.return_value = True
    with mock.patch.object(conversation, "reply") as reply:
        asyncio.run(client._commands._call(interaction))
    reply.assert_not_called()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.parametrize("guild_id", [1, None])
def test_codex_uses_interaction_context(
    client: bot._Client, guild_id: int | None
) -> None:
    interaction = discord_helpers.interaction()
    interaction.guild_id = guild_id
    interaction.data["options"][0]["value"] = "hello\n\n  there"
    with (
        mock.patch.object(client, "dispatch"),
        mock.patch.object(
            conversation,
            "reply",
            return_value=conversation.Reply(
                thread_id="saved",
                text="@everyone hello",
            ),
        ) as reply,
    ):
        asyncio.run(client._commands._call(interaction))
    reply.assert_awaited_once_with(
        prompt="hello\n\n  there", model="test-model", thread_id=None, image=None
    )
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.response.send_message.assert_not_awaited()
    kwargs = interaction.edit_original_response.call_args.kwargs
    assert kwargs["content"] == "> Owner: hello\n> \n>   there"
    assert kwargs["embed"].description == "@everyone hello"
    assert kwargs["embed"].footer.text == "Model: test-model"
    assert isinstance(kwargs["view"], publishing.PublishView)
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


def test_codex_shows_available_turn_duration(
    client: bot._Client,
) -> None:
    interaction = discord_helpers.interaction()
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
    assert embed.footer.text == "Model: test-model · Turn: 2.5 s"


@pytest.mark.parametrize("command", ["codex", "new", "session"])
def test_missing_channel_fails_without_model(client: bot._Client, command: str) -> None:
    interaction = discord_helpers.interaction(command=command)
    interaction.channel_id = None
    with mock.patch.object(conversation, "reply") as reply:
        asyncio.run(client._commands._call(interaction))
    reply.assert_not_called()
    assert (
        "no channel ID"
        in interaction.edit_original_response.call_args.kwargs["content"]
    )
    assert interaction.edit_original_response.call_args.kwargs["view"] is None
    interaction.followup.send.assert_not_awaited()
    interaction.delete_original_response.assert_not_awaited()


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
        query = discord_helpers.interaction(command="session")
        asyncio.run(client._commands._call(query))
        assert query.edit_original_response.call_args.kwargs["content"] == "old"
        reset = discord_helpers.interaction(command="new")
        asyncio.run(client._commands._call(reset))
        query = discord_helpers.interaction(command="session")
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
    interaction = discord_helpers.interaction(acknowledged=True)
    text = character * 5000
    asyncio.run(commands._edit_deferred_response(interaction=interaction, text=text))

    kwargs = interaction.edit_original_response.call_args.kwargs
    displayed = kwargs["content"]
    assert displayed.endswith("[Reply truncated. Ask for a shorter reply.]")
    assert len(displayed.encode("utf-16-le")) // 2 <= 2000


@pytest.mark.parametrize("character", ["x", "🙂"])
def test_long_answer_is_truncated(character: str) -> None:
    text = character * 5000
    embed = commands._answer_embed(
        text=text,
        footer="Model: test-model",
    )
    displayed = embed.description
    assert displayed is not None
    assert displayed.endswith("[Reply truncated. Ask for a shorter reply.]")
    assert len(displayed.encode("utf-16-le")) // 2 <= 4096


@pytest.mark.parametrize("text", ["x" * 4096, "🙂" * 2048])
def test_answer_fits_embed_description(text: str) -> None:
    embed = commands._answer_embed(
        text=text,
        footer="Model: test-model",
    )
    assert embed.description == text


def test_long_quoted_prompt_preserves_answer(client: bot._Client) -> None:
    interaction = discord_helpers.interaction()
    interaction.data["options"][0]["value"] = "🙂" * 1200
    answer = "x" * 4096
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="saved", text=answer),
    ):
        asyncio.run(client._commands._call(interaction))
    response = interaction.edit_original_response.call_args.kwargs
    assert response["content"].startswith("> Owner: 🙂")
    assert response["content"].endswith("…")
    assert len(response["content"].encode("utf-16-le")) // 2 <= 2000
    assert response["embed"].description == answer


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
    interaction = discord_helpers.interaction()
    with mock.patch.object(conversation, "reply", side_effect=error):
        asyncio.run(client._commands._call(interaction))
    message = interaction.edit_original_response.call_args.kwargs["content"]
    assert "secret-canary" not in message
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure = next(record for record in records if record["event"] == "command_failed")
    assert failure["error_message"] == str(error)
    assert interaction.edit_original_response.call_args.kwargs["embed"] is None
    assert isinstance(
        interaction.edit_original_response.call_args.kwargs["view"],
        publishing.PublishView,
    )
    interaction.followup.send.assert_not_awaited()
    interaction.delete_original_response.assert_not_awaited()


def test_delivery_failure_is_not_retried(client: bot._Client) -> None:
    interaction = discord_helpers.interaction()
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
    interaction.delete_original_response.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    assert interaction.edit_original_response.await_count == 2
    assert "failed" in interaction.edit_original_response.call_args.kwargs["content"]
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

        interaction = discord_helpers.interaction()
        timeout = asyncio.timeout

        def short_timeout(*, delay: float) -> asyncio.Timeout:
            assert delay == 600
            return timeout(0.01)

        with (
            mock.patch.object(client._commands._sessions, "reply", side_effect=wait),
            mock.patch.object(commands.asyncio, "timeout", side_effect=short_timeout),
        ):
            await client._commands._call(interaction)
        assert cancelled.is_set()
        assert (
            "timed out"
            in interaction.edit_original_response.call_args.kwargs["content"]
        )
        interaction.followup.send.assert_not_awaited()

    asyncio.run(exercise())


def test_empty_prompt_does_not_call_model(client: bot._Client) -> None:
    interaction = discord_helpers.interaction()
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
    assert isinstance(kwargs["view"], publishing.PublishView)


@pytest.mark.parametrize("phase", ["acknowledge", "send_reply"])
@pytest.mark.parametrize(
    "secondary",
    [
        discord.NotFound(mock.Mock(status=404, reason="Not Found"), "reporting failed"),
        PermissionError(13, "reporting failed"),
    ],
)
def test_response_failure_preserves_original_error(
    client: bot._Client,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    secondary: Exception,
) -> None:
    interaction = discord_helpers.interaction()
    original = discord.NotFound(
        mock.Mock(status=404, reason="Not Found"), "original failure"
    )
    if phase == "acknowledge":
        interaction.response.defer.side_effect = original
        interaction.response.send_message.side_effect = secondary
    else:
        interaction.edit_original_response.side_effect = [original, secondary]
    with mock.patch.object(
        conversation,
        "reply",
        return_value=conversation.Reply(thread_id="saved", text="private-response"),
    ) as reply:
        if isinstance(secondary, discord.HTTPException):
            asyncio.run(client._commands._call(interaction))
        else:
            with pytest.raises(type(secondary)) as caught:
                asyncio.run(client._commands._call(interaction))
            assert caught.value is secondary
    assert reply.await_count == (0 if phase == "acknowledge" else 1)
    output = capsys.readouterr().out
    assert "private-response" not in output
    failures = [
        record
        for record in map(json.loads, output.splitlines())
        if record["level"] == "ERROR"
    ]
    assert [(record["event"], record["interaction_id"]) for record in failures] == [
        ("command_failed", interaction.id),
        ("error_response_failed", interaction.id),
    ]
    assert [record["error_message"] for record in failures] == [
        str(original),
        str(secondary),
    ]


@pytest.mark.parametrize("command", ["new", "session"])
def test_management_error_before_defer_is_private(
    client: bot._Client,
    command: str,
) -> None:
    interaction = discord_helpers.interaction(command=command)
    interaction.response.defer.side_effect = RuntimeError("secret-canary")
    asyncio.run(client._commands._call(interaction))
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("failed", [False, True])
def test_denial_logs_request_identity_and_send_failure(
    client: bot._Client,
    capsys: pytest.CaptureFixture[str],
    failed: bool,
) -> None:
    interaction = discord_helpers.interaction(owner=False)
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


@pytest.mark.parametrize(
    "command",
    [
        "codex",
        "new",
        "session",
        "Ask Codex",
    ],
)
def test_pending_operations_only_show_private_loading(
    client: bot._Client,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    async def exercise() -> None:
        source = discord_helpers.interaction(command=command)
        started = asyncio.Event()
        release = asyncio.Event()

        async def work(**kwargs: object) -> conversation.Reply | str | None:
            started.set()
            await release.wait()
            if command == "new":
                return None
            if command == "session":
                return "saved"
            return conversation.Reply(thread_id="saved", text="Answer")

        method = {"new": "reset", "session": "current"}.get(command, "reply")
        with mock.patch.object(
            client._commands._sessions,
            method,
            side_effect=work,
        ):
            if command == "Ask Codex":
                source.command = None
                source.type = discord.InteractionType.modal_submit
                operation = client._commands._answer_about_message(
                    interaction=source,
                    message=_selected_message(),
                    question="Explain",
                )
            else:
                operation = client._commands._call(source)
            task = asyncio.create_task(coro=operation)
            await started.wait()
            source.response.defer.assert_awaited_once_with(
                ephemeral=True, thinking=True
            )
            source.response.send_message.assert_not_awaited()
            source.edit_original_response.assert_not_awaited()
            source.followup.send.assert_not_awaited()
            release.set()
            await task
        assert isinstance(
            source.edit_original_response.call_args.kwargs["view"],
            publishing.PublishView,
        )
        source.response.send_message.assert_not_awaited()
        source.followup.send.assert_not_awaited()
        source.delete_original_response.assert_not_awaited()

    asyncio.run(exercise())
    records = list(map(json.loads, capsys.readouterr().out.splitlines()))
    assert records
    assert all(
        record["interaction_id"] == 12345 and record["user_id"] == 42
        for record in records
    )


def test_nearly_expired_response_has_no_publish_button(client: bot._Client) -> None:
    async def exercise() -> None:
        source = discord_helpers.interaction(acknowledged=True)
        source.expires_at = discord.utils.utcnow() + datetime.timedelta(seconds=20)
        await client._commands._send_codex_reply(
            interaction=source,
            reply=conversation.Reply(thread_id="saved", text="Answer"),
            quoted_user_message="> Owner: hello",
        )
        assert source.edit_original_response.call_args.kwargs["view"] is None

    asyncio.run(exercise())


def test_error_after_opening_modal_does_not_edit_nonexistent_message(
    client: bot._Client,
) -> None:
    async def exercise() -> None:
        source = discord_helpers.interaction(command="Ask Codex")
        await client._commands._ask_message(
            interaction=source, message=_selected_message()
        )
        await client._commands._respond_to_error(
            interaction=source,
            error=RuntimeError("failed after opening modal"),
        )
        source.edit_original_response.assert_not_awaited()
        source.response.send_message.assert_not_awaited()
        source.followup.send.assert_not_awaited()

    asyncio.run(exercise())


def test_concurrent_commands_log_their_own_request(
    client: bot._Client,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def exercise() -> None:
        success = discord_helpers.interaction()
        failure = discord_helpers.interaction()
        failure.id = 54321
        failure.channel_id = 202
        entered = 0
        ready = asyncio.Event()

        async def reply(
            *, context: sessions.DiscordContext, prompt: str
        ) -> conversation.Reply:
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            if context.channel_id == 202:
                raise conversation.ConversationError("Expected failure.")
            return conversation.Reply(thread_id="saved", text="Answer")

        with mock.patch.object(
            client._commands._sessions,
            "reply",
            side_effect=reply,
        ):
            await asyncio.gather(
                client._commands._call(success), client._commands._call(failure)
            )

    asyncio.run(exercise())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    for record in records:
        assert record["channel_id"] == (
            101 if record["interaction_id"] == 12345 else 202
        )
    assert any(
        record["event"] == "reply_sent" and record["interaction_id"] == 12345
        for record in records
    )
    assert any(
        record["event"] == "command_failed" and record["interaction_id"] == 54321
        for record in records
    )
    assert any(
        record["event"] == "reply_sent" and record["interaction_id"] == 54321
        for record in records
    )
