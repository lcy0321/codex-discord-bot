import asyncio
from collections.abc import Iterator
from unittest import mock

import openai_codex.types
import pytest

from codex_discord_bot import auth, conversation


@pytest.fixture
def client() -> Iterator[mock.AsyncMock]:
    client = mock.AsyncMock()
    client.account.return_value.account.root.type = "chatgpt"
    thread = client.thread_start.return_value
    thread.id = "thread-1"
    client.thread_resume.return_value = thread
    result = thread.turn.return_value.run.return_value
    result.status = openai_codex.types.TurnStatus.completed
    result.final_response = "Hello"
    result.items = [mock.Mock(root=mock.Mock(type="agentMessage"))]

    with (
        mock.patch.object(auth, "prepare_runtime"),
        mock.patch.object(openai_codex, "AsyncCodex", return_value=client),
    ):
        yield client


@pytest.mark.parametrize("thread_id", [None, "thread-1"])
def test_new_and_resumed_reply(client: mock.AsyncMock, thread_id: str | None) -> None:
    result = asyncio.run(
        conversation.reply(
            prompt="Hello",
            model="configured-model",
            thread_id=thread_id,
        )
    )

    assert result == conversation.Reply(thread_id="thread-1", text="Hello")
    method = client.thread_start if thread_id is None else client.thread_resume
    options = method.call_args.kwargs
    assert options["model"] == "configured-model"
    assert options["sandbox"] == openai_codex.Sandbox.read_only
    assert options["approval_mode"] == openai_codex.ApprovalMode.deny_all
    assert "search the web" in options["base_instructions"]
    assert "Do not use other tools" in options["base_instructions"]
    if thread_id is None:
        assert options["ephemeral"] is False
        client.thread_resume.assert_not_awaited()
    else:
        assert options["thread_id"] == thread_id
        client.thread_start.assert_not_awaited()
    client.close.assert_awaited_once_with()


@pytest.mark.parametrize("kind", [None, "apiKey"])
def test_auth_failure_never_starts_a_turn(
    client: mock.AsyncMock, kind: str | None
) -> None:
    client.account.return_value.account = (
        None if kind is None else mock.Mock(root=mock.Mock(type=kind))
    )

    with pytest.raises(auth.AuthenticationError):
        asyncio.run(conversation.reply(prompt="Hello", model="configured-model"))

    client.thread_start.assert_not_awaited()
    client.thread_resume.assert_not_awaited()
    client.close.assert_awaited_once_with()


def test_resume_failure_does_not_reset(client: mock.AsyncMock) -> None:
    error = openai_codex.CodexError("secret-canary")
    client.thread_resume.side_effect = error

    with pytest.raises(conversation.ConversationError, match="not reset") as caught:
        asyncio.run(
            conversation.reply(
                prompt="Hello",
                model="configured-model",
                thread_id="missing-thread",
            )
        )

    assert "secret-canary" not in str(caught.value)
    assert caught.value.__cause__ is error
    client.thread_start.assert_not_awaited()
    client.close.assert_awaited_once_with()


@pytest.mark.parametrize(
    "error",
    [RuntimeError("secret-canary"), openai_codex.CodexError("secret-canary")],
)
def test_turn_errors_are_safe(client: mock.AsyncMock, error: Exception) -> None:
    client.thread_start.return_value.turn.return_value.run.side_effect = error

    with pytest.raises(conversation.ConversationError) as caught:
        asyncio.run(conversation.reply(prompt="Hello", model="configured-model"))

    assert "secret-canary" not in str(caught.value)
    assert caught.value.__cause__ is error
    client.close.assert_awaited_once_with()


@pytest.mark.parametrize("text", [None, "", "  "])
def test_empty_reply(client: mock.AsyncMock, text: str | None) -> None:
    client.thread_start.return_value.turn.return_value.run.return_value.final_response = text
    with pytest.raises(conversation.ConversationError, match="no text"):
        asyncio.run(conversation.reply(prompt="Hello", model="configured-model"))


@pytest.mark.parametrize(
    "status",
    [openai_codex.types.TurnStatus.interrupted, openai_codex.types.TurnStatus.failed],
)
def test_partial_reply_is_not_success(
    client: mock.AsyncMock, status: openai_codex.types.TurnStatus
) -> None:
    client.thread_start.return_value.turn.return_value.run.return_value.status = status
    with pytest.raises(conversation.ConversationError):
        asyncio.run(conversation.reply(prompt="Hello", model="configured-model"))


def test_tool_result_is_not_forwarded(client: mock.AsyncMock) -> None:
    client.thread_start.return_value.turn.return_value.run.return_value.items = [
        mock.Mock(root=mock.Mock(type="fileChange")),
    ]
    with pytest.raises(conversation.ConversationError, match="non-text"):
        asyncio.run(conversation.reply(prompt="Hello", model="configured-model"))


def test_web_search_result_keeps_text_reply(client: mock.AsyncMock) -> None:
    client.thread_start.return_value.turn.return_value.run.return_value.items = [
        mock.Mock(root=mock.Mock(type="webSearch")),
        mock.Mock(root=mock.Mock(type="agentMessage")),
    ]

    result = asyncio.run(conversation.reply(prompt="Current news?", model="test-model"))

    assert result.text == "Hello"


def test_web_citation_marker_is_removed(client: mock.AsyncMock) -> None:
    client.thread_start.return_value.turn.return_value.run.return_value.final_response = "Source: https://www.python.org/about/ \ue200cite\ue202turn0search1\ue201"

    result = asyncio.run(conversation.reply(prompt="About Python?", model="test-model"))

    assert result.text == "Source: https://www.python.org/about/"


@pytest.mark.parametrize("stage", ["account", "thread_start", "turn", "run"])
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_close_process(
    client: mock.AsyncMock, stage: str, cancel: bool
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def block(**kwargs: object) -> None:
            entered.set()
            await asyncio.Future()

        thread = client.thread_start.return_value
        turn = thread.turn.return_value
        methods = {
            "account": client.account,
            "thread_start": client.thread_start,
            "turn": thread.turn,
            "run": turn.run,
        }
        methods[stage].side_effect = block
        task = asyncio.create_task(
            conversation.reply(
                prompt="Hello",
                model="configured-model",
                timeout=60 if cancel else 0.02,
            )
        )
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(conversation.ConversationError, match="timed out"):
                await task

        if stage == "run":
            turn.interrupt.assert_awaited_once_with()
        else:
            turn.interrupt.assert_not_awaited()
        client.close.assert_awaited_once_with()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), openai_codex.CodexError("secret-canary")],
)
def test_failed_interrupt_still_closes(
    client: mock.AsyncMock, error: Exception
) -> None:
    turn = client.thread_start.return_value.turn.return_value
    turn.run.side_effect = TimeoutError()
    turn.interrupt.side_effect = error

    with pytest.raises(conversation.ConversationError, match="timed out"):
        asyncio.run(conversation.reply(prompt="Hello", model="configured-model"))

    client.close.assert_awaited_once_with()


@pytest.mark.parametrize("fail", [False, True])
def test_cancel_during_startup_waits_before_close(
    client: mock.AsyncMock, fail: bool
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def initialize() -> None:
            entered.set()
            await finish.wait()
            if fail:
                raise openai_codex.CodexError("secret-canary")

        client.__aenter__.side_effect = initialize
        task = asyncio.create_task(
            conversation.reply(prompt="Hello", model="configured-model")
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        client.close.assert_not_awaited()
        finish.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        client.close.assert_awaited_once_with()
        client.account.assert_not_awaited()

    asyncio.run(scenario())
