# Local mirror of the Space runtime. Python matches the ZeroGPU Space (3.12.12).
# Linux PyPI torch wheels bundle the CUDA runtime, so no CUDA base image is needed;
# the host driver is exposed via `gpus` in docker compose.
FROM python:3.12.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra gpu

COPY app.py alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7860/health/live')"

CMD ["python", "app.py"]
