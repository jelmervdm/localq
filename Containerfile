FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/jelmervdm/localq" \
      org.opencontainers.image.description="Local smart-appliance cloud emulator and Home Assistant MQTT auto-discovery bridge" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 localq \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin localq \
    && mkdir -p /data/certs \
    && chown -R 10001:10001 /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --no-deps .

USER 10001

ENV LOCALQ_BIND_HOST=0.0.0.0 \
    LOCALQ_HTTPS_PORT=8443 \
    LOCALQ_MQTT_TLS_PORT=8883 \
    LOCALQ_CERTS_DIR=/data/certs \
    PYTHONUNBUFFERED=1

EXPOSE 8443 8883

ENTRYPOINT ["localqd"]
