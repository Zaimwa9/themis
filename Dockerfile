FROM python:3.12-slim

# git for PR workspaces; node for the codex CLI (version pinned on purpose)
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @openai/codex@0.144.0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd -m themis \
    && mkdir -p /data/codex /tmp/themis \
    && chown -R themis:themis /data/codex /tmp/themis
USER themis
ENV CODEX_HOME=/data/codex \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["python", "-m", "themis"]
