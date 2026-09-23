import asyncio
import sys
from pathlib import Path
from unittest import mock

import openai_codex
import pytest

from codex_discord_bot import __main__, auth


def test_runtime_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name="CODEX_HOME", value=str(tmp_path))
    monkeypatch.delenv(name="OPENAI_API_KEY", raising=False)
    monkeypatch.delenv(name="CODEX_API_KEY", raising=False)

    credential = tmp_path / "auth.json"
    credential.write_text(data="credential-canary")
    conversation = tmp_path / "sessions"
    conversation.mkdir()
    (conversation / "rollout.jsonl").write_text(data="conversation-canary")
    (tmp_path / "config.toml").write_text(data="outdated configuration")

    auth.prepare_runtime()

    assert credential.read_text() == "credential-canary"
    assert (conversation / "rollout.jsonl").read_text() == "conversation-canary"
    assert 'forced_login_method = "chatgpt"' in (tmp_path / "config.toml").read_text()
    assert tmp_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "CODEX_API_KEY"])
def test_api_key_rejected(variable: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name="CODEX_HOME", value="/unused")
    monkeypatch.setenv(name=variable, value="secret-canary")
    with pytest.raises(auth.AuthenticationError, match="API keys are not supported"):
        auth.prepare_runtime()


def test_missing_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(name="CODEX_HOME", raising=False)
    with pytest.raises(KeyError, match="CODEX_HOME"):
        auth.prepare_runtime()


@pytest.mark.parametrize("kind", [None, "apiKey", "chatgpt"])
def test_account_boundary(kind: str | None) -> None:
    client = mock.AsyncMock()
    client.account.return_value.account = (
        None if kind is None else mock.Mock(root=mock.Mock(type=kind))
    )

    with (
        mock.patch.object(auth, "prepare_runtime"),
        mock.patch.object(openai_codex, "AsyncCodex") as factory,
    ):
        factory.return_value.__aenter__.return_value = client
        if kind == "chatgpt":
            asyncio.run(auth.check_status(refresh=True))
        else:
            with pytest.raises(auth.AuthenticationError):
                asyncio.run(auth.check_status(refresh=True))
        client.account.assert_awaited_once_with(refresh_token=True)
        client.thread_start.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [openai_codex.CodexError("secret-canary"), TimeoutError("secret-canary")],
)
def test_cli_runtime_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(
        target=sys,
        name="argv",
        value=["codex-discord-bot", "auth-status", "--refresh"],
    )
    with (
        mock.patch.object(auth, "prepare_runtime", side_effect=error),
        pytest.raises(type(error)) as caught,
    ):
        __main__._main()
    assert caught.value is error


@pytest.mark.parametrize("success", [True, False])
def test_device_login(success: bool, capsys: pytest.CaptureFixture[str]) -> None:
    client = mock.AsyncMock()
    handle = mock.AsyncMock()
    handle.verification_url = "https://auth.openai.com/codex/device"
    handle.user_code = "TEST-CODE"
    handle.wait.return_value.success = success
    client.login_chatgpt_device_code.return_value = handle
    client.account.return_value.account.root.type = "chatgpt"

    with (
        mock.patch.object(auth, "prepare_runtime"),
        mock.patch.object(openai_codex, "AsyncCodex") as factory,
    ):
        factory.return_value.__aenter__.return_value = client
        if success:
            asyncio.run(auth.login())
            client.account.assert_awaited_once_with()
        else:
            with pytest.raises(auth.AuthenticationError, match="Login failed"):
                asyncio.run(auth.login())
            client.account.assert_not_awaited()
        client.login_chatgpt_device_code.assert_awaited_once_with()
        client.login_api_key.assert_not_called()

    output = capsys.readouterr().out
    assert "TEST-CODE" in output


@pytest.mark.parametrize(
    "error",
    [
        openai_codex.CodexError("runtime failure"),
        TimeoutError("timeout"),
        PermissionError("state permission"),
        ValueError("invalid configuration"),
        RuntimeError("runtime failure"),
    ],
)
def test_check_status_preserves_runtime_error(error: Exception) -> None:
    with (
        mock.patch.object(auth, "prepare_runtime", side_effect=error),
        pytest.raises(type(error)) as caught,
    ):
        asyncio.run(auth.check_status())

    assert caught.value is error


@pytest.mark.parametrize(
    ("arguments", "login", "refresh"),
    [
        (["login"], True, False),
        (["auth-status"], False, False),
        (["auth-status", "--refresh"], False, True),
    ],
)
def test_cli_auth_dispatch(
    arguments: list[str],
    login: bool,
    refresh: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        target=sys,
        name="argv",
        value=["codex-discord-bot", *arguments],
    )
    with (
        mock.patch.object(auth, "login", new_callable=mock.AsyncMock) as login_command,
        mock.patch.object(
            auth, "check_status", new_callable=mock.AsyncMock
        ) as status_command,
    ):
        assert __main__._main() == 0

    if login:
        login_command.assert_awaited_once_with()
        status_command.assert_not_awaited()
    else:
        status_command.assert_awaited_once_with(refresh=refresh)
        login_command.assert_not_awaited()
