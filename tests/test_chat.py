import builtins
import sys
from pathlib import Path
from unittest import mock

import pytest

from codex_discord_bot import __main__, auth, conversation, sessions


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name="CODEX_HOME", value=str(tmp_path / "codex"))


def test_chat_continues_and_resets(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        mock.patch.object(
            builtins,
            "input",
            side_effect=["first", "second", "/new", "third", "/exit"],
        ),
        mock.patch.object(
            conversation,
            "reply",
            side_effect=[
                conversation.Reply(thread_id="one", text="First reply"),
                conversation.Reply(thread_id="one", text="Second reply"),
                conversation.Reply(thread_id="two", text="Third reply"),
            ],
        ) as reply,
    ):
        __main__._chat(
            model="configured-model", context=sessions.DiscordContext(channel_id=42)
        )

    assert reply.await_args_list == [
        mock.call(prompt="first", model="configured-model", thread_id=None),
        mock.call(prompt="second", model="configured-model", thread_id="one"),
        mock.call(prompt="third", model="configured-model", thread_id=None),
    ]
    output = capsys.readouterr().out
    assert "First reply" in output
    assert "Second reply" in output
    assert "Third reply" in output


@pytest.mark.parametrize(
    "error",
    [
        auth.AuthenticationError("Run login."),
        conversation.ConversationError("Reply timed out."),
    ],
)
def test_error_preserves_conversation(
    error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        mock.patch.object(
            builtins,
            "input",
            side_effect=["first", "failed", "retry", "/exit"],
        ),
        mock.patch.object(
            conversation,
            "reply",
            side_effect=[
                conversation.Reply(thread_id="one", text="Hello"),
                error,
                conversation.Reply(thread_id="one", text="Recovered"),
            ],
        ) as reply,
    ):
        __main__._chat(
            model="configured-model", context=sessions.DiscordContext(channel_id=42)
        )

    assert reply.await_count == 3
    assert reply.await_args == mock.call(
        prompt="retry", model="configured-model", thread_id="one"
    )
    assert str(error) in capsys.readouterr().err


@pytest.mark.parametrize("ending", ["/exit", EOFError()])
def test_empty_input_and_commands_do_not_call_model(ending: str | Exception) -> None:
    with (
        mock.patch.object(builtins, "input", side_effect=["  ", "/new", ending]),
        mock.patch.object(conversation, "reply") as reply,
    ):
        __main__._chat(
            model="configured-model", context=sessions.DiscordContext(channel_id=42)
        )

    reply.assert_not_called()


@pytest.mark.parametrize("interrupted", [False, True])
def test_chat_entrypoint(monkeypatch: pytest.MonkeyPatch, interrupted: bool) -> None:
    monkeypatch.setattr(
        target=sys,
        name="argv",
        value=["codex-discord-bot", "chat", "--channel-id", "42"],
    )
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value="secret-canary")
    monkeypatch.setenv(name="DISCORD_OWNER_ID", value="42")
    monkeypatch.setenv(name="CODEX_MODEL", value="configured-model")

    with mock.patch.object(
        __main__,
        "_chat",
        side_effect=KeyboardInterrupt() if interrupted else None,
    ) as chat:
        assert __main__._main() == (130 if interrupted else 0)

    chat.assert_called_once_with(
        model="configured-model", context=sessions.DiscordContext(channel_id=42)
    )


def test_invalid_chat_settings_fail_before_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        target=sys,
        name="argv",
        value=["codex-discord-bot", "chat", "--channel-id", "42"],
    )
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value="secret-canary")
    monkeypatch.setenv(name="DISCORD_OWNER_ID", value="invalid-canary")

    with mock.patch.object(__main__, "_chat") as chat:
        assert __main__._main() == 2

    chat.assert_not_called()
    output = capsys.readouterr().err
    assert "discord_owner_id" in output
    assert "canary" not in output


def test_chat_recovers_saved_context(capsys: pytest.CaptureFixture[str]) -> None:
    context = sessions.DiscordContext(channel_id=42, guild_id=7)
    with (
        mock.patch.object(builtins, "input", side_effect=["remember", "/exit"]),
        mock.patch.object(
            conversation,
            "reply",
            return_value=conversation.Reply(thread_id="saved", text="OK"),
        ),
    ):
        __main__._chat(model="configured-model", context=context)

    with (
        mock.patch.object(
            builtins,
            "input",
            side_effect=["/session", "recall", "/new", "/session", "/exit"],
        ),
        mock.patch.object(
            conversation,
            "reply",
            return_value=conversation.Reply(thread_id="saved", text="remembered"),
        ) as reply,
    ):
        __main__._chat(model="configured-model", context=context)

    reply.assert_awaited_once_with(
        prompt="recall", model="configured-model", thread_id="saved"
    )
    output = capsys.readouterr().out
    assert "saved" in output
    assert "No session yet." in output
