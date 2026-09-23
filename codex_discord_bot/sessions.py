"""Connect Discord conversation locations to resumable Codex threads.

DiscordContext creates the storage key; _SessionFile validates stored TOML;
ChannelSessions owns the live mapping and orders operations for each key.
"""

import asyncio
import dataclasses
import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Annotated

import pydantic

from codex_discord_bot import conversation


class SessionError(Exception):
    """Safe to display without exposing stored state."""


@dataclasses.dataclass(frozen=True)
class DiscordContext:
    """Identify a Discord conversation; a thread uses its own channel ID."""

    channel_id: int
    guild_id: int | None = None

    def __post_init__(self) -> None:
        if self.channel_id is None:
            raise ValueError("A channel ID is required.")
        for value in (self.channel_id, self.guild_id):
            if value is not None and (type(value) is not int or not 0 < value < 2**64):
                raise ValueError("Context IDs must be positive Discord snowflakes.")

    @property
    def key(self) -> str:
        if self.guild_id is None:
            return f"private:channel:{self.channel_id}"
        return f"guild:{self.guild_id}:channel:{self.channel_id}"


class _SessionFile(pydantic.BaseModel):
    """Validate the TOML table of Discord context keys and Codex thread IDs."""

    model_config = pydantic.ConfigDict(strict=True, extra="forbid")

    sessions: dict[
        Annotated[
            str,
            pydantic.StringConstraints(
                pattern=r"^(guild:[1-9][0-9]*|private):channel:[1-9][0-9]*$"
            ),
        ],
        Annotated[
            str,
            pydantic.StringConstraints(min_length=1, pattern=r"\S"),
        ],
    ]


def _quote(value: str) -> str:
    # JSON escaping also works for TOML, except TOML forbids a literal DEL.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


class ChannelSessions:
    """Run and persist a Codex conversation for each DiscordContext.

    One process owns the mapping file. Calls in the same context wait for each
    other; different contexts can run concurrently. The mapping is loaded at
    startup and updated in memory only after an atomic file replacement.
    """

    def __init__(self, *, mapping_path: Path, model: str) -> None:
        self._mapping_path = mapping_path
        self._codex_model = model
        self._locks_by_context: dict[str, asyncio.Lock] = {}
        # Reject corrupt state before the first model request.
        self._thread_ids_by_context = self._read()

    def _context_lock(self, *, context: DiscordContext) -> asyncio.Lock:
        return self._locks_by_context.setdefault(context.key, asyncio.Lock())

    def _read(self) -> dict[str, str]:
        try:
            with self._mapping_path.open("rb") as source:
                return _SessionFile.model_validate(tomllib.load(source)).sessions
        except FileNotFoundError:
            return {}
        except tomllib.TOMLDecodeError, pydantic.ValidationError:
            raise SessionError(
                "Invalid sessions.toml. Restore a valid backup before continuing."
            ) from None

    def _write(self, *, mapping: dict[str, str]) -> None:
        """Replace the whole mapping without exposing a partially written file."""
        self._mapping_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = "[sessions]\n" + "".join(
            f"{_quote(value=key)} = {_quote(value=value)}\n"
            for key, value in sorted(mapping.items())
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._mapping_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                os.replace(temporary_path, self._mapping_path)
            finally:
                temporary_path.unlink(missing_ok=True)

    async def current(self, *, context: DiscordContext) -> str | None:
        """Wait for any pending turn before reporting the saved thread ID."""
        async with self._context_lock(context=context):
            return self._thread_ids_by_context.get(context.key)

    async def reset(self, *, context: DiscordContext) -> None:
        """Forget this reference; the next reply starts a new thread."""
        async with self._context_lock(context=context):
            if context.key in self._thread_ids_by_context:
                mapping = self._thread_ids_by_context.copy()
                del mapping[context.key]
                self._write(mapping=mapping)
                self._thread_ids_by_context = mapping

    async def reply(
        self,
        *,
        context: DiscordContext,
        prompt: str,
    ) -> conversation.Reply:
        """Save new thread IDs after success; a crash before saving can orphan history."""
        async with self._context_lock(context=context):
            thread_id = self._thread_ids_by_context.get(context.key)

            result = await conversation.reply(
                prompt=prompt,
                model=self._codex_model,
                thread_id=thread_id,
            )
            if thread_id is None:
                # Empty SDK threads cannot resume. Publish only after a completed reply.
                # No await during the write, so other contexts cannot overwrite it.
                mapping = self._thread_ids_by_context.copy()
                mapping[context.key] = result.thread_id
                self._write(mapping=mapping)
                self._thread_ids_by_context = mapping
            return result
