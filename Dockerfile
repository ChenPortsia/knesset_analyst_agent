# App image: pipeline + CLI. Also used by the ui and scheduler services.
FROM python:3.12-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.11.0 /uv /usr/local/bin/uv

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.33
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer-nogui tzdata curl ca-certificates \
    && curl -fsSL -o /usr/local/bin/supercronic \
       "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN uv sync --locked --no-dev

CMD ["knesset", "status"]
