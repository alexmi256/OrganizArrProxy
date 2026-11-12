# syntax=docker/dockerfile:1

# ---- build wheels / virtualenv -------------------------------------------
FROM python:3.12-alpine AS builder

# Installed from PyPI rather than the uv image so the build does not depend on
# ghcr.io (which some registries/CI mirrors block). Bump with the lockfile.
ARG UV_VERSION=0.13.7
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime --------------------------------------------------------------
FROM python:3.12-alpine AS runtime

RUN apk add --no-cache ca-certificates \
    && addgroup -S -g 10001 seedpath \
    && adduser -S -D -H -u 10001 -G seedpath seedpath \
    && mkdir -p /config /app \
    && chown seedpath:seedpath /config

COPY --from=builder --chown=seedpath:seedpath /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_PATH=/config/state.db \
    LOG_FORMAT=json

WORKDIR /app
USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/livez', timeout=3)"]

ENTRYPOINT ["seedpath"]
CMD ["serve"]
