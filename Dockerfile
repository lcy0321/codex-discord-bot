FROM python:3.14-slim-trixie

WORKDIR /app

RUN --mount=from=ghcr.io/astral-sh/uv:0.12,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy \
    uv sync --locked --no-dev --compile-bytecode
RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid 10001 --create-home bot \
    && mkdir -p /state/codex /work \
    && chown -R bot:bot /state /work \
    && chmod 700 /state /state/codex

COPY codex_discord_bot/ /app/codex_discord_bot/
COPY deploy/config.toml /app/deploy/config.toml
COPY deploy/requirements.toml /etc/codex/requirements.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_HOME=/state/codex

USER 10001:10001

ENTRYPOINT ["python", "-m", "codex_discord_bot"]

CMD ["run"]
