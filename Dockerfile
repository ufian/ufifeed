FROM ghcr.io/astral-sh/uv:0.9.18 AS uv
FROM python:3.13-slim AS codex
COPY scripts/install_codex.py /tmp/install_codex.py
RUN python /tmp/install_codex.py --destination /usr/local/bin/codex

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=codex /usr/local/bin/codex /usr/local/bin/codex
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && groupadd --gid 1000 ufifeed && useradd --uid 1000 --gid 1000 --no-create-home ufifeed
COPY config/ufifeed.example.toml ./config/ufifeed.example.toml
USER 1000:1000
ENTRYPOINT ["ufifeed"]
CMD ["run"]
