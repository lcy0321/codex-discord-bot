import argparse
import asyncio
import os
import sys
from pathlib import Path

import pydantic

from codex_discord_bot import auth, bot, config, conversation, sessions


def _chat(*, model: str, context: sessions.DiscordContext) -> None:
    manager = sessions.ChannelSessions(
        mapping_path=Path(os.environ["CODEX_HOME"]).parent / "sessions.toml",
        model=model,
    )
    print(f"Model: {model}. Context: {context.key}.", flush=True)
    print("Enter a message, /session, /new, or /exit.", flush=True)

    # Reuse one event loop for the manager's locks; input stays outside async work.
    with asyncio.Runner() as runner:
        while True:
            try:
                prompt = input("> ").strip()
            except EOFError:
                return

            if not prompt:
                continue
            if prompt == "/exit":
                return

            try:
                if prompt == "/session":
                    thread_id = runner.run(manager.current(context=context))
                    print(thread_id or "No session yet.", flush=True)
                elif prompt == "/new":
                    runner.run(manager.reset(context=context))
                    print("New conversation on the next message.", flush=True)
                else:
                    reply = runner.run(manager.reply(context=context, prompt=prompt))
                    print(reply.text, flush=True)
            # Expected failures leave the saved context available for another message.
            except (
                auth.AuthenticationError,
                conversation.ConversationError,
                sessions.SessionError,
            ) as error:
                print(f"Error: {error}", file=sys.stderr)


def _main() -> int:
    """Translate expected failures to exit codes; leave unexpected errors visible."""
    parser = argparse.ArgumentParser(description="Private Discord Codex service")

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config")
    commands.add_parser("run", help="Run the Discord bot")
    commands.add_parser("login")

    chat = commands.add_parser("chat", help="Chat in a persistent channel context")
    chat.add_argument("--channel-id", type=int, required=True)
    chat.add_argument("--guild-id", type=int)

    status = commands.add_parser("auth-status")
    status.add_argument("--refresh", action="store_true")

    arguments = parser.parse_args()

    try:
        match arguments.command:
            case "check-config":
                config.Settings()
                print(
                    "Configuration valid. Discord and Codex connections were not opened."
                )
            case "chat":
                settings = config.Settings()
                try:
                    context = sessions.DiscordContext(
                        channel_id=arguments.channel_id, guild_id=arguments.guild_id
                    )
                except ValueError as error:
                    parser.error(str(error))
                _chat(model=settings.codex_model, context=context)
            case "run":
                asyncio.run(bot.run(settings=config.Settings()))
            case "login":
                asyncio.run(auth.login())
            case "auth-status":
                asyncio.run(auth.check_status(refresh=arguments.refresh))
    except pydantic.ValidationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except (sessions.SessionError, auth.AuthenticationError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except asyncio.CancelledError:
        # SIGTERM cancels run(); asyncio.run has already drained pending SDK tasks.
        return 0
    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
