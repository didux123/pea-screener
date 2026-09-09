# Un seul conteneur : les tâches planifiées et le service des rapports.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

ENV TZ=Europe/Paris \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# supercronic : un cron qui tourne en avant-plan, sans démon, et journalise sur la sortie
# standard. Épinglé par version et empreinte.
ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_SHA1=71b0d58cc53f6bd72cf2f293e09e294b79c666d8
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
 && echo "${SUPERCRONIC_SHA1}  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod +x /usr/local/bin/supercronic \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY pea/ ./pea/
COPY config.toml README.md ./
COPY deploy/ ./deploy/
RUN uv sync --frozen --no-dev \
 && chmod +x deploy/entrypoint.sh \
 && useradd --create-home --uid 10001 pea \
 && mkdir -p /app/data \
 && chown -R pea:pea /app

USER pea
EXPOSE 8080
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
