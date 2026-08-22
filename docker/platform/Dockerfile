FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN groupadd --gid 65532 mewcode \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin mewcode \
    && pip install --no-cache-dir uv==0.12.5

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY mewcode ./mewcode
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev \
    && chown -R 65532:65532 /app

USER 65532:65532

ENTRYPOINT ["/app/.venv/bin/mewcode-platform"]
