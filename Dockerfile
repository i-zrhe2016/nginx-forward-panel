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
    PANEL_HOST=0.0.0.0 \
    PANEL_PORT=18080 \
    DEFAULT_UPSTREAM_HOST=nat.qq.pw \
    DEFAULT_UPSTREAM_PORT=31098 \
    SEED_LISTEN_PORT=31098 \
    PROXY_CONNECT_TIMEOUT=5s \
    PROXY_TIMEOUT=600s \
    MAINTENANCE_INTERVAL=10

CMD ["python3", "/app/panel.py"]
