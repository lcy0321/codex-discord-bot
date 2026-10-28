"""Check request metadata and error diagnostics at the logging boundary."""

import datetime
import json
from unittest import mock

import discord
import pytest

from codex_discord_bot import event_log


def _interaction(*, identifier: int) -> mock.Mock:
    current = mock.Mock(spec=discord.Interaction)
    current.id = identifier
    current.user = mock.Mock(id=identifier + 10)
    current.guild_id = 1
    current.channel_id = identifier + 100
    current.command = mock.Mock(qualified_name="codex")
    current.type = discord.InteractionType.application_command
    current.created_at = discord.utils.utcnow() - datetime.timedelta(seconds=5)
    current.response.is_done.return_value = False
    current.is_expired.return_value = False
    return current


def test_service_event_has_no_request_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_log.write(event="discord_ready")
    record = json.loads(capsys.readouterr().out)
    assert set(record) == {
        "time",
        "level",
        "event",
    }
    assert record["level"] == "INFO"


def test_log_reads_interaction_state_at_emission(
    capsys: pytest.CaptureFixture[str],
) -> None:
    current = _interaction(identifier=1)
    event_log.write(event="before", interaction=current)
    current.response.is_done.return_value = True
    current.is_expired.return_value = True
    event_log.write(event="after", interaction=current)
    before, after = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert before["acknowledged"] is False and before["expired"] is False
    assert after["acknowledged"] is True and after["expired"] is True
    assert before["interaction_id"] == after["interaction_id"] == 1
    assert after["age_seconds"] >= 5


@pytest.mark.parametrize(
    ("error", "details"),
    [
        (
            PermissionError(13, "storage unavailable"),
            {"error": "PermissionError", "errno": 13},
        ),
        (
            discord.NotFound(
                mock.Mock(status=404, reason="Not Found"),
                {"code": 10015, "message": "Unknown Webhook"},
            ),
            {"error": "NotFound", "http_status": 404, "discord_code": 10015},
        ),
    ],
)
def test_error_event_includes_request_and_exception_details(
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    details: dict[str, str | int],
) -> None:
    with pytest.raises(type(error)) as caught:
        raise error from RuntimeError("backend unavailable")
    event_log.write(
        event="command_failed",
        interaction=_interaction(identifier=2),
        error=caught.value,
    )

    record = json.loads(capsys.readouterr().out)
    assert record["level"] == "ERROR"
    assert record["event"] == "command_failed"
    assert record["command"] == "codex"
    assert record["interaction_id"] == 2
    assert record["guild_id"] == 1
    assert record["user_id"] == 12
    assert record["channel_id"] == 102
    assert record["error_message"] == str(error)
    assert record["cause"] == "RuntimeError"
    assert record["cause_message"] == "backend unavailable"
    for key, expected in details.items():
        assert record[key] == expected
    assert record["frames"]
