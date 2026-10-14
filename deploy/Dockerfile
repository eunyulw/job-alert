# Fast company job alert monitor: one non-root process, no published port, state on /app/data.
FROM ghcr.io/astral-sh/uv:0.11.3 AS uv

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH
RUN groupadd --system jobalert && useradd --system --gid jobalert --home /app jobalert
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv

# Dependencies first, from the lockfile alone, so a source change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

COPY config ./config
COPY deploy/healthcheck.py ./deploy/healthcheck.py
COPY scripts/backup_sqlite.py ./scripts/backup_sqlite.py
RUN mkdir -p /app/data && chown jobalert:jobalert /app/data
USER jobalert

HEALTHCHECK --interval=5m --timeout=10s --start-period=15m --retries=2 \
    CMD ["python", "/app/deploy/healthcheck.py"]
CMD ["job-alert", "run", "--config", "/app/config", "--db", "/app/data/job-alert.sqlite3", \
     "--digest-report", "/app/data/reports/daily-digest.md"]
