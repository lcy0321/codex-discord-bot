"""Shared JSON diagnostics for service events and Discord interactions."""

import json
import traceback
from pathlib import Path

import discord


def write(
    *,
    event: str,
    interaction: discord.Interaction | None = None,
    error: Exception | None = None,
    **fields: object,
) -> None:
    """Emit a JSON event with optional interaction metadata and error diagnostics.

    Read interaction state at emission so acknowledgment and expiry stay current.
    """
    data: dict[str, object] = {
        "time": discord.utils.utcnow().isoformat(),
        "level": "ERROR" if error is not None else "INFO",
        "event": event,
    }
    if interaction is not None:
        command = interaction.command.qualified_name if interaction.command else None
        if command is None and interaction.type is discord.InteractionType.modal_submit:
            command = "Ask Codex"
        data.update(
            command=command,
            interaction_id=interaction.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            age_seconds=round(
                (discord.utils.utcnow() - interaction.created_at).total_seconds(), 3
            ),
            acknowledged=interaction.response.is_done(),
            expired=interaction.is_expired(),
        )
    if error is not None:
        data.update(error=type(error).__name__, error_message=str(error))
        if error.__cause__ is not None:
            data.update(
                cause=type(error.__cause__).__name__,
                cause_message=str(error.__cause__),
            )
        if isinstance(error, discord.HTTPException):
            data.update(http_status=error.status, discord_code=error.code)
        if isinstance(error, OSError):
            data["errno"] = error.errno
        # Full traceback text includes source lines that may contain private data.
        data["frames"] = [
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(error.__traceback__)
        ]
    data.update(fields)
    print(json.dumps(data, ensure_ascii=False), flush=True)
