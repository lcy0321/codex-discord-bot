import sys

import pydantic
import pytest

from codex_discord_bot import __main__, config


@pytest.fixture(autouse=True)
def _settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value="secret-value")
    monkeypatch.setenv(name="DISCORD_OWNER_ID", value="42")
    monkeypatch.delenv(name="CODEX_MODEL", raising=False)


def test_defaults_and_secret_repr() -> None:
    settings = config.Settings()
    assert settings.discord_owner_id == 42
    assert settings.codex_model == "gpt-5.6-luna"
    assert isinstance(settings.discord_bot_token, pydantic.SecretStr)
    assert settings.discord_bot_token.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(settings)
    assert "secret-value" not in settings.model_dump_json()
    assert "secret-value" not in str(settings.model_dump())


@pytest.mark.parametrize("owner", ["", "0", "-1", "1.5", "abc", "１２", str(2**64)])
def test_invalid_owner(owner: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name="DISCORD_OWNER_ID", value=owner)
    with pytest.raises(pydantic.ValidationError, match="discord_owner_id"):
        config.Settings()


@pytest.mark.parametrize("token", ["", "  "])
def test_blank_token(token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value=token)
    with pytest.raises(pydantic.ValidationError, match="discord_bot_token"):
        config.Settings()


@pytest.mark.parametrize("field", ["DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID"])
def test_missing_required_setting(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(name=field)
    with pytest.raises(pydantic.ValidationError, match=field.lower()) as caught:
        config.Settings()
    assert "secret-value" not in str(caught.value)


def test_model_override_and_blank_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name="CODEX_MODEL", value=" custom-model ")
    assert config.Settings().codex_model == "custom-model"

    monkeypatch.setenv(name="CODEX_MODEL", value=" ")
    with pytest.raises(pydantic.ValidationError, match="codex_model"):
        config.Settings()


def test_cli_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-value")
    monkeypatch.setenv("DISCORD_OWNER_ID", "private-invalid-value")
    monkeypatch.setattr(
        target=sys, name="argv", value=["codex-discord-bot", "check-config"]
    )
    assert __main__._main() == 2
    output = capsys.readouterr()
    assert "discord_owner_id" in output.err
    assert "private-invalid-value" not in output.err
    assert "secret-value" not in output.err


def test_cli_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-value")
    monkeypatch.setenv("DISCORD_OWNER_ID", "42")
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.setattr(
        target=sys, name="argv", value=["codex-discord-bot", "check-config"]
    )
    assert __main__._main() == 0
    assert "secret-value" not in capsys.readouterr().out


def test_token_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name="DISCORD_BOT_TOKEN", value="  secret-value  ")
    assert config.Settings().discord_bot_token.get_secret_value() == "secret-value"
