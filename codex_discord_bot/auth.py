import asyncio
import os
import tempfile
from pathlib import Path

import openai_codex.types


class AuthenticationError(Exception):
    """Safe to display without exposing runtime diagnostics."""


def prepare_runtime() -> openai_codex.CodexConfig:
    """Copy deployed configuration into CODEX_HOME, preserving credentials and sessions."""
    home = Path(os.environ["CODEX_HOME"])
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
        raise AuthenticationError("API keys are not supported; use ChatGPT login.")

    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(mode=0o700)

    template = Path(__file__).resolve().parent.parent / "codex_runtime" / "config.toml"
    with tempfile.NamedTemporaryFile(dir=home, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(template.read_bytes())
            output.close()
            temporary.replace(target=home / "config.toml")
        finally:
            temporary.unlink(missing_ok=True)

    return openai_codex.CodexConfig(cwd="/work")


def validate_account(*, response: openai_codex.types.GetAccountResponse) -> None:
    """Raise AuthenticationError unless the response identifies a ChatGPT account."""
    if response.account is None:
        raise AuthenticationError("Not logged in; run login.")
    if response.account.root.type != "chatgpt":
        raise AuthenticationError("ChatGPT OAuth required; run login.")


async def login() -> None:
    """Complete device-code login and verify that it yielded a ChatGPT account."""
    runtime = prepare_runtime()
    # Device-code login allows time for the user to finish in the browser.
    async with (
        asyncio.timeout(delay=960),
        openai_codex.AsyncCodex(config=runtime) as codex,
    ):
        handle = await codex.login_chatgpt_device_code()
        print(f"Open {handle.verification_url}", flush=True)
        print(f"Device code: {handle.user_code}", flush=True)
        completed = await handle.wait()
        if not completed.success:
            raise AuthenticationError("Login failed; run login.")

        validate_account(response=await codex.account())

    print("ChatGPT OAuth available.")


async def check_status(*, refresh: bool = False) -> None:
    """Check ChatGPT authentication; refresh=True asks the SDK to renew it."""
    runtime = prepare_runtime()
    async with (
        asyncio.timeout(delay=60),
        openai_codex.AsyncCodex(config=runtime) as codex,
    ):
        validate_account(response=await codex.account(refresh_token=refresh))

    print("ChatGPT OAuth refresh succeeded." if refresh else "ChatGPT OAuth available.")
