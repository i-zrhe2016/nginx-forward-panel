FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cron \
        nginx-full \
        ca-certificates \
        python3 \
        python3-flask \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/nginx /var/cache/nginx /etc/nginx/streams-enabled /data

COPY nginx.conf /etc/nginx/nginx.conf
COPY app /app
COPY scripts /app/scripts

ENV DATA_DIR=/data \
    DB_PATH=/data/panel.db \
    NGINX_CONFIG_PATH=/etc/nginx/nginx.conf \
    STREAMS_DIR=/etc/nginx/streams-enabled \
    STREAM_ACCESS_LOG=/var/log/nginx/stream-access.log \
    NGINX_PID_PATH=/var/run/nginx.pid \
    XRAY_CLIENT_CONFIG_PATH=/xray-runtime/client-test.json \
    PANEL_HOST=0.0.0.0 \
    PANEL_PORT=18080 \
    PANEL_PUBLIC_URL= \
    DEFAULT_UPSTREAM_HOST=127.0.0.1 \
    DEFAULT_UPSTREAM_PORT=443 \
    SEED_LISTEN_PORT=31098 \
    PROXY_CONNECT_TIMEOUT=5s \
    PROXY_TIMEOUT=600s \
    STREAM_LISTEN_BACKLOG=4096 \
    STREAM_LISTEN_FASTOPEN=256 \
    STREAM_LISTEN_SO_KEEPALIVE=on \
    STREAM_PROXY_SOCKET_KEEPALIVE=1 \
    MAINTENANCE_INTERVAL=10

CMD ["python3", "/app/panel.py"]
