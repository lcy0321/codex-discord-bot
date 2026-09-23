import asyncio
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest import mock

import pytest

from codex_discord_bot import conversation, sessions


def test_context_keys() -> None:
    assert sessions.DiscordContext(channel_id=2, guild_id=1).key == "guild:1:channel:2"
    assert sessions.DiscordContext(channel_id=3, guild_id=1).key == "guild:1:channel:3"
    assert sessions.DiscordContext(channel_id=2).key == "private:channel:2"


@pytest.mark.parametrize("value", [0, -1, 2**64, True])
def test_invalid_context_ids(value: int) -> None:
    with pytest.raises(ValueError):
        sessions.DiscordContext(channel_id=value)
    with pytest.raises(ValueError):
        sessions.DiscordContext(channel_id=1, guild_id=value)


@pytest.mark.parametrize(
    "content",
    [
        "broken = [",
        "",
        '[sessions]\n"shared" = "thread"',
        '[sessions]\n"private:channel:1" = 1',
        '[sessions]\n"private:channel:1" = " "',
        '[sessions]\n"private:channel:1" = "one"\n[extra]',
    ],
)
def test_corrupt_state_is_not_reset(tmp_path: Path, content: str) -> None:
    path = tmp_path / "sessions.toml"
    path.write_text(content)
    with pytest.raises(sessions.SessionError, match="Restore a valid backup"):
        sessions.ChannelSessions(mapping_path=path, model="test-model")
    assert path.read_text() == content


def test_isolation_reset_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "sessions.toml"
    home = tmp_path / "codex"
    home.mkdir()
    credential = home / "auth.json"
    credential.write_text("credential-canary")
    contexts = [
        sessions.DiscordContext(channel_id=2, guild_id=1),
        sessions.DiscordContext(channel_id=3, guild_id=1),
        sessions.DiscordContext(channel_id=2),
    ]

    async def exercise() -> None:
        manager = sessions.ChannelSessions(mapping_path=path, model="test-model")
        for index, context in enumerate(contexts):
            assert await manager.current(context=context) is None
            with mock.patch.object(
                conversation,
                "reply",
                return_value=conversation.Reply(thread_id=f"thread-{index}", text="OK"),
            ):
                await manager.reply(context=context, prompt="remember")
        await manager.reset(context=contexts[0])
        resumed = sessions.ChannelSessions(mapping_path=path, model="test-model")
        assert await resumed.current(context=contexts[0]) is None
        for index, context in enumerate(contexts[1:], start=1):
            with mock.patch.object(
                conversation,
                "reply",
                return_value=conversation.Reply(thread_id=f"thread-{index}", text="OK"),
            ) as reply:
                await resumed.reply(context=context, prompt="recall")
            reply.assert_awaited_once_with(
                prompt="recall", model="test-model", thread_id=f"thread-{index}"
            )

    asyncio.run(exercise())
    assert credential.read_text() == "credential-canary"
    assert path.stat().st_mode & 0o777 == 0o600
    # A fresh interpreter must recover the mapping without process-local state.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio, sys; from pathlib import Path; "
                "from codex_discord_bot import sessions; "
                "manager = sessions.ChannelSessions(mapping_path=Path(sys.argv[1]), model='test'); "
                "print(asyncio.run(manager.current(context=sessions.DiscordContext(channel_id=2))))"
            ),
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "thread-2"


def test_concurrent_contexts_and_reset(tmp_path: Path) -> None:
    async def exercise() -> None:
        manager = sessions.ChannelSessions(
            mapping_path=tmp_path / "sessions.toml", model="test"
        )
        first = sessions.DiscordContext(channel_id=1)
        second = sessions.DiscordContext(channel_id=2)
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str | None] = []

        async def reply(
            *, prompt: str, model: str, thread_id: str | None
        ) -> conversation.Reply:
            calls.append(thread_id)
            if prompt == "slow":
                started.set()
                await release.wait()
            return conversation.Reply(thread_id=thread_id or prompt, text=model)

        with mock.patch.object(conversation, "reply", side_effect=reply):
            pending = asyncio.create_task(manager.reply(context=first, prompt="slow"))
            await started.wait()
            following = asyncio.create_task(manager.reply(context=first, prompt="next"))
            reset = asyncio.create_task(manager.reset(context=first))
            # Another context can complete while the first is still using the model.
            await manager.reply(context=second, prompt="other")
            assert not following.done()
            assert not reset.done()
            release.set()
            await pending
            await following
            await reset
            assert calls == [None, None, "slow"]
            assert await manager.current(context=first) is None
            assert await manager.current(context=second) == "other"

    asyncio.run(exercise())


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "error", [conversation.ConversationError("failed"), asyncio.CancelledError()]
)
def test_failed_turn_preserves_mapping(
    tmp_path: Path, existing: bool, error: BaseException
) -> None:
    path = tmp_path / "sessions.toml"
    if existing:
        path.write_text('[sessions]\n"private:channel:1" = "old"\n')
    before = path.read_bytes() if existing else None

    async def exercise() -> None:
        manager = sessions.ChannelSessions(mapping_path=path, model="test")
        context = sessions.DiscordContext(channel_id=1)
        with mock.patch.object(conversation, "reply", side_effect=error) as reply:
            with pytest.raises(type(error)):
                await manager.reply(context=context, prompt="test")
            reply.assert_awaited_once_with(
                prompt="test", model="test", thread_id="old" if existing else None
            )
        # Cancellation must release the context lock, including for reset.
        assert await manager.current(context=context) == ("old" if existing else None)

    asyncio.run(exercise())
    assert (path.read_bytes() if path.exists() else None) == before


def test_failed_atomic_replace_preserves_previous_file(tmp_path: Path) -> None:
    path = tmp_path / "sessions.toml"
    original = '[sessions]\n"private:channel:1" = "old"\n'
    path.write_text(original)

    async def exercise() -> None:
        manager = sessions.ChannelSessions(mapping_path=path, model="test")
        with (
            mock.patch.object(os, "replace", side_effect=OSError("disk failure")),
            pytest.raises(OSError, match="disk failure"),
        ):
            await manager.reset(context=sessions.DiscordContext(channel_id=1))
        assert (
            await manager.current(context=sessions.DiscordContext(channel_id=1))
            == "old"
        )

    asyncio.run(exercise())
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]
    assert tomllib.loads(path.read_text())["sessions"] == {"private:channel:1": "old"}


def test_toml_string_round_trip(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "sessions.toml"
        manager = sessions.ChannelSessions(mapping_path=path, model="test")
        context = sessions.DiscordContext(channel_id=1)
        thread_id = 'quote"\\\n\x7f\U0001f642'
        with mock.patch.object(
            conversation,
            "reply",
            return_value=conversation.Reply(thread_id=thread_id, text="OK"),
        ):
            await manager.reply(context=context, prompt="test")
        assert await manager.current(context=context) == thread_id

    asyncio.run(exercise())
