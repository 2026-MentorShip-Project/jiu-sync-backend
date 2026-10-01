FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

ARG ENV=dev

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project $(if [ "$ENV" = "prod" ]; then echo "--no-dev"; fi)

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked $(if [ "$ENV" = "prod" ]; then echo "--no-dev"; fi)

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
# bind / gthread / workers / threads / timeout / worker_tmp_dir 與 prometheus multiprocess
# 目錄的 hook 都在 gunicorn.conf.py(add-observability-stack design.md D2)。
CMD ["gunicorn", "-c", "gunicorn.conf.py", "config.wsgi:application"]
