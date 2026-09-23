"""Use one SDK process per reply so cancellation cannot stop another context."""

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncGenerator

import openai_codex.types

from codex_discord_bot import auth

# Conversation guidance only; managed policy enforces tool denial.
_INSTRUCTIONS = (
    "You are a conversational assistant. Reply using text only. "
    "Do not use tools, access files, or perform actions outside this conversation."
)


class ConversationError(Exception):
    """Safe to display without exposing prompts or runtime diagnostics."""


@dataclasses.dataclass(frozen=True)
class Reply:
    thread_id: str
    text: str


@contextlib.asynccontextmanager
async def _open_codex(*, timeout: float) -> AsyncGenerator[openai_codex.AsyncCodex]:
    """Own the SDK process and request deadline; close outside the deadline."""
    codex = openai_codex.AsyncCodex(config=auth.prepare_runtime())
    try:
        async with asyncio.timeout(delay=timeout):
            initialization = asyncio.create_task(codex.__aenter__())
            try:
                # SDK startup runs in a worker thread that task cancellation cannot stop.
                # Shield keeps its task awaitable until the process is available to close.
                await asyncio.shield(initialization)
            except asyncio.CancelledError:
                # The caller already cancelled; a startup error must not mask that outcome.
                with contextlib.suppress(Exception):
                    # Closing now could miss a process the worker has yet to create.
                    await initialization
                # Propagate cancellation so callers and asyncio.timeout can observe it.
                raise
            yield codex
    finally:
        await codex.close()


async def _run_turn(
    *,
    thread: openai_codex.AsyncThread,
    prompt: str,
) -> openai_codex.TurnResult:
    """Interrupt cancelled turns before the owning context closes the process."""
    turn = await thread.turn(input=prompt)
    try:
        return await turn.run()
    except TimeoutError, asyncio.CancelledError:
        try:
            async with asyncio.timeout(delay=5):
                await turn.interrupt()
        except TimeoutError, openai_codex.CodexError:
            # Preserve the original cancellation or timeout; _open_codex still
            # closes the process if this attempt to interrupt it fails.
            pass
        raise
    except RuntimeError:
        # The SDK embeds server diagnostics in failed-turn RuntimeError.
        raise ConversationError(
            "Codex could not complete the reply. Check auth-status and retry."
        ) from None


def _extract_text(*, result: openai_codex.TurnResult) -> str:
    if result.status != openai_codex.types.TurnStatus.completed:
        raise ConversationError("Codex reply was interrupted.")

    # Managed policy blocks tool execution; this check only validates output.
    if any(
        item.root.type
        not in {"userMessage", "agentMessage", "reasoning", "contextCompaction"}
        for item in result.items
    ):
        raise ConversationError("Codex attempted an unsupported non-text operation.")
    if result.final_response is None or not result.final_response.strip():
        raise ConversationError("Codex returned no text.")
    return result.final_response


async def reply(
    *,
    prompt: str,
    model: str,
    thread_id: str | None = None,
    timeout: float = 300,
) -> Reply:
    """Return a text reply; omit thread_id to start a new conversation.

    Callers must serialize turns per thread. Failures never reset a conversation
    and may leave history. Cancellation closes this request's SDK process;
    initialization and cleanup may extend the timeout.
    """
    try:
        async with _open_codex(timeout=timeout) as codex:
            auth.validate_account(response=await codex.account())
            if thread_id is None:
                thread = await codex.thread_start(
                    model=model,
                    base_instructions=_INSTRUCTIONS,
                    sandbox=openai_codex.Sandbox.read_only,
                    approval_mode=openai_codex.ApprovalMode.deny_all,
                    ephemeral=False,
                )
            else:
                thread = await codex.thread_resume(
                    thread_id=thread_id,
                    model=model,
                    base_instructions=_INSTRUCTIONS,
                    sandbox=openai_codex.Sandbox.read_only,
                    approval_mode=openai_codex.ApprovalMode.deny_all,
                )
            result = await _run_turn(thread=thread, prompt=prompt)
    except TimeoutError:
        raise ConversationError("Codex reply timed out.") from None
    except openai_codex.CodexError:
        raise ConversationError(
            "Codex request failed. Check auth-status; the conversation was not reset."
        ) from None

    return Reply(thread_id=thread.id, text=_extract_text(result=result))
