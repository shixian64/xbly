# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONPATH=/app

RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid "${APP_GID}" app; \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --requirement requirements.txt

# Keep the build context explicit.  APK files, Git history, local sessions,
# secrets and unrelated analysis artifacts are never copied into an image.
COPY --chown=${APP_UID}:${APP_GID} bbw_protocol ./bbw_protocol
COPY --chown=${APP_UID}:${APP_GID} bbw_web ./bbw_web
COPY --chown=${APP_UID}:${APP_GID} bbw_prod ./bbw_prod
COPY --chown=${APP_UID}:${APP_GID} bbw_agent ./bbw_agent
COPY --chown=${APP_UID}:${APP_GID} docs/api_catalog.json ./docs/api_catalog.json
COPY --chown=${APP_UID}:${APP_GID} alembic.ini ./alembic.ini
COPY --chown=${APP_UID}:${APP_GID} migrations ./migrations
COPY --chown=${APP_UID}:${APP_GID} docker/healthcheck.py ./docker/healthcheck.py
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 0555 /usr/local/bin/docker-entrypoint.sh \
    && chmod -R a-w /app \
    && find /app -type d -exec chmod 0555 {} \; \
    && find /app -type f -exec chmod 0444 {} \;

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "bbw_web.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
