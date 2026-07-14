FROM python:3.12-slim

# git for PR workspaces; node for the codex and claude CLIs (versions pinned on purpose)
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @openai/codex@0.144.0 @anthropic-ai/claude-code@2.1.207 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# /data/themis must exist in the image so the named volume mounted there
# inherits themis ownership; otherwise Docker creates it root-owned and the
# pending-learnings store cannot write.
RUN useradd -m themis \
    && mkdir -p /data/codex /data/themis /tmp/themis \
    && chown -R themis:themis /data/codex /data/themis /tmp/themis
USER themis
ENV CODEX_HOME=/data/codex \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["python", "-m", "themis"]
