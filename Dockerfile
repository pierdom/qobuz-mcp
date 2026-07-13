FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Step 1: install dependencies only — cached until the lockfile changes
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Step 2: copy source, install the project wheel
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable

# Non-root user + writable data dir for the OIDCProxy encrypted client store.
# FastMCP derives its data dir from platformdirs → XDG_DATA_HOME; mount /data as a
# volume when using OIDC so DCR registrations / tokens survive container recreation.
RUN useradd --system --no-create-home qobuzmcp \
    && mkdir -p /data && chown qobuzmcp /data
USER qobuzmcp
ENV XDG_DATA_HOME=/data

# Defaults for remote hosting; override QOBUZ_MCP_TRANSPORT=stdio for local use.
ENV QOBUZ_MCP_TRANSPORT=http \
    QOBUZ_MCP_HTTP_HOST=0.0.0.0 \
    QOBUZ_MCP_HTTP_PORT=8000

EXPOSE 8000

CMD ["/app/.venv/bin/python", "-m", "qobuz_mcp"]
