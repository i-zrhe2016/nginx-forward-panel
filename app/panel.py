#!/usr/bin/env python3
import atexit
import base64
import json
import os
import re
import secrets
import socket
import signal
import sqlite3
import string
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for


def parse_optional_env_port(value, field_name):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是数字。") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{field_name} 必须在 1-65535 之间。")
    return port


def parse_nonnegative_env_int(value, field_name):
    try:
        number = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是非负整数。") from exc
    if number < 0:
        raise ValueError(f"{field_name} 必须是非负整数。")
    return number


def parse_bool_env(value, default=False):
    raw = str(value if value is not None else ("1" if default else "0")).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "panel.db"))
NGINX_CONFIG_PATH = Path(os.environ.get("NGINX_CONFIG_PATH", "/etc/nginx/nginx.conf"))
STREAMS_DIR = Path(os.environ.get("STREAMS_DIR", "/etc/nginx/streams-enabled"))
GENERATED_STREAM_CONFIG = STREAMS_DIR / "ports.conf"
STREAM_ACCESS_LOG = Path(os.environ.get("STREAM_ACCESS_LOG", "/var/log/nginx/stream-access.log"))
NGINX_PID_PATH = Path(os.environ.get("NGINX_PID_PATH", "/var/run/nginx.pid"))
XRAY_CLIENT_CONFIG_PATH = Path(os.environ.get("XRAY_CLIENT_CONFIG_PATH", "/xray-runtime/client-test.json"))
SUBSCRIPTION_NAME_PREFIX = os.environ.get("SUBSCRIPTION_NAME_PREFIX", "reality").strip() or "reality"

PANEL_HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "18080"))
PANEL_PUBLIC_URL = os.environ.get("PANEL_PUBLIC_URL", "").strip().rstrip("/")
PANEL_USERNAME = os.environ.get("PANEL_USERNAME", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")

DEFAULT_UPSTREAM_HOST = os.environ.get("DEFAULT_UPSTREAM_HOST", "127.0.0.1")
DEFAULT_UPSTREAM_PORT = int(os.environ.get("DEFAULT_UPSTREAM_PORT", "443"))
SEED_LISTEN_PORT = os.environ.get("SEED_LISTEN_PORT", "31098").strip()
PROXY_CONNECT_TIMEOUT = os.environ.get("PROXY_CONNECT_TIMEOUT", "5s")
PROXY_TIMEOUT = os.environ.get("PROXY_TIMEOUT", "600s")
STREAM_LISTEN_BACKLOG = parse_nonnegative_env_int(
    os.environ.get("STREAM_LISTEN_BACKLOG", "4096"),
    "STREAM_LISTEN_BACKLOG",
)
STREAM_LISTEN_FASTOPEN = parse_nonnegative_env_int(
    os.environ.get("STREAM_LISTEN_FASTOPEN", "256"),
    "STREAM_LISTEN_FASTOPEN",
)
STREAM_LISTEN_SO_KEEPALIVE = os.environ.get("STREAM_LISTEN_SO_KEEPALIVE", "on").strip() or "on"
STREAM_PROXY_SOCKET_KEEPALIVE = parse_bool_env(
    os.environ.get("STREAM_PROXY_SOCKET_KEEPALIVE", "1"),
    default=True,
)
MAINTENANCE_INTERVAL = int(os.environ.get("MAINTENANCE_INTERVAL", "10"))
PROBE_ENABLED = os.environ.get("PROBE_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}
PROBE_INTERVAL = int(os.environ.get("PROBE_INTERVAL", "60"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "3"))
PROBE_TEST_LISTEN_PORT = parse_optional_env_port(
    os.environ.get("PROBE_TEST_LISTEN_PORT", ""),
    "PROBE_TEST_LISTEN_PORT",
)
AUTH_ENABLED = bool(PANEL_USERNAME or PANEL_PASSWORD)
PROBE_DASHBOARD_RANGES = {
    "1h": {"hours": 1, "label": "1小时"},
    "24h": {"hours": 24, "label": "24小时"},
    "7d": {"hours": 24 * 7, "label": "7天"},
}

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

app = Flask(__name__, template_folder="templates", static_folder="static")


class ValidationError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso_now():
    return utc_now().isoformat(timespec="seconds")


def parse_port(value, field_name):
    try:
        port = int(str(value).strip())
    except ValueError as exc:
        raise ValidationError(f"{field_name} 必须是数字。") from exc
    if port < 1 or port > 65535:
        raise ValidationError(f"{field_name} 必须在 1-65535 之间。")
    return port


def parse_note(value):
    note = str(value or "").strip()
    if len(note) > 200:
        raise ValidationError("备注不能超过 200 个字符。")
    return note


def parse_data_size(value, field_name):
    raw = str(value or "").strip()
    if not raw:
        return None

    match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)\s*([kmgtp]?i?b?)?", raw)
    if match is None:
        raise ValidationError(f"{field_name} 格式不正确，可填写 10G、500MB 或 1048576。")

    amount = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multipliers = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "tib": 1024**4,
        "p": 1024**5,
        "pb": 1024**5,
        "pib": 1024**5,
    }
    if unit not in multipliers:
        raise ValidationError(f"{field_name} 单位不支持。")
    if unit in {"b", ""} and not amount.is_integer():
        raise ValidationError(f"{field_name} 以字节为单位时必须是整数。")

    size = int(amount * multipliers[unit])
    if size <= 0:
        raise ValidationError(f"{field_name} 必须大于 0。")
    return size


def parse_expiry(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        local_dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("到期时间格式不正确。") from exc
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
    expires_at = local_dt.astimezone(timezone.utc)
    if expires_at <= utc_now():
        raise ValidationError("到期时间必须晚于当前时间。")
    return expires_at.isoformat(timespec="seconds")


def human_bytes(value):
    size = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return "0 B"


def format_display_time(value):
    if not value:
        return "永久"
    dt = datetime.fromisoformat(value).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_input_time(value):
    if not value:
        return ""
    dt = datetime.fromisoformat(value).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%dT%H:%M")


def localize_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ)


def generate_subscription_token(length=12):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalize_subscription_name(note, listen_port):
    note_text = re.sub(r"\s+", " ", str(note or "").strip())
    if note_text:
        return f"{note_text}-{listen_port}"
    return f"{SUBSCRIPTION_NAME_PREFIX}-{listen_port}"


def yaml_quote(value):
    return json.dumps(str(value), ensure_ascii=False)


def current_request_scheme():
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    return forwarded or request.scheme


def current_request_host():
    forwarded = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    return forwarded or request.host


def external_url_for(endpoint, **values):
    if PANEL_PUBLIC_URL:
        return f"{PANEL_PUBLIC_URL}{url_for(endpoint, **values)}"
    return f"{current_request_scheme()}://{current_request_host()}{url_for(endpoint, **values)}"


def parse_xray_client_profile():
    if not XRAY_CLIENT_CONFIG_PATH.is_file():
        return None, f"未找到 Xray 客户端配置：{XRAY_CLIENT_CONFIG_PATH}"

    try:
        payload = json.loads(XRAY_CLIENT_CONFIG_PATH.read_text(encoding="utf-8"))
        outbounds = payload.get("outbounds", [])
        if not isinstance(outbounds, list) or not outbounds:
            raise ValueError("outbounds 为空")
        outbound = next((item for item in outbounds if item.get("protocol") == "vless"), outbounds[0])
        vnext = outbound["settings"]["vnext"][0]
        user = vnext["users"][0]
        reality = outbound["streamSettings"]["realitySettings"]
        profile = {
            "server": str(vnext["address"]).strip(),
            "uuid": str(user["id"]).strip(),
            "flow": str(user.get("flow", "")).strip(),
            "server_name": str(reality["serverName"]).strip(),
            "public_key": str(reality["publicKey"]).strip(),
            "short_id": str(reality["shortId"]).strip(),
            "fingerprint": str(reality.get("fingerprint", "chrome")).strip() or "chrome",
        }
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"Xray 客户端配置解析失败：{exc}"

    missing = [key for key, value in profile.items() if not value]
    if missing:
        return None, f"Xray 客户端配置缺少字段：{', '.join(missing)}"
    return profile, ""


def build_vless_share_link(profile, listen_port, note):
    params = urlencode(
        {
            "encryption": "none",
            "flow": profile["flow"],
            "security": "reality",
            "sni": profile["server_name"],
            "fp": profile["fingerprint"],
            "pbk": profile["public_key"],
            "sid": profile["short_id"],
            "type": "tcp",
            "headerType": "none",
        }
    )
    tag = quote(normalize_subscription_name(note, listen_port), safe="")
    return f"vless://{profile['uuid']}@{profile['server']}:{int(listen_port)}?{params}#{tag}"


def build_v2ray_subscription_content(profile, listen_port, note):
    share_link = build_vless_share_link(profile, listen_port, note)
    return base64.b64encode(f"{share_link}\n".encode("utf-8")).decode("ascii")


def build_clash_subscription_content(profile, listen_port, note):
    proxy_name = normalize_subscription_name(note, listen_port)
    lines = [
        "port: 7890",
        "socks-port: 7891",
        "allow-lan: true",
        "mode: rule",
        "log-level: info",
        "ipv6: true",
        "",
        "dns:",
        "  enable: true",
        "  listen: 0.0.0.0:1053",
        "  ipv6: true",
        "  enhanced-mode: fake-ip",
        "  nameserver:",
        "    - https://dns.google/dns-query",
        "    - https://1.1.1.1/dns-query",
        "",
        "proxies:",
        f"  - name: {yaml_quote(proxy_name)}",
        "    type: vless",
        f"    server: {yaml_quote(profile['server'])}",
        f"    port: {int(listen_port)}",
        f"    uuid: {yaml_quote(profile['uuid'])}",
        "    udp: true",
        "    tls: true",
        "    network: tcp",
        f"    servername: {yaml_quote(profile['server_name'])}",
        f"    flow: {yaml_quote(profile['flow'])}",
        "    reality-opts:",
        f"      public-key: {yaml_quote(profile['public_key'])}",
        f"      short-id: {yaml_quote(profile['short_id'])}",
        f"    client-fingerprint: {yaml_quote(profile['fingerprint'])}",
        "",
        "proxy-groups:",
        "  - name: PROXY",
        "    type: select",
        "    proxies:",
        f"      - {yaml_quote(proxy_name)}",
        "      - DIRECT",
        "",
        "  - name: AUTO",
        "    type: url-test",
        "    url: http://www.gstatic.com/generate_204",
        "    interval: 300",
        "    proxies:",
        f"      - {yaml_quote(proxy_name)}",
        "",
        "rule-providers:",
        "  BanAD:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/BanAD.yaml",
        "    path: ./ruleset/BanAD.yaml",
        "    interval: 86400",
        "",
        "  BanEasyList:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/BanEasyList.yaml",
        "    path: ./ruleset/BanEasyList.yaml",
        "    interval: 86400",
        "",
        "  BanEasyListChina:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/BanEasyListChina.yaml",
        "    path: ./ruleset/BanEasyListChina.yaml",
        "    interval: 86400",
        "",
        "  BanEasyPrivacy:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/BanEasyPrivacy.yaml",
        "    path: ./ruleset/BanEasyPrivacy.yaml",
        "    interval: 86400",
        "",
        "  BanProgramAD:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/BanProgramAD.yaml",
        "    path: ./ruleset/BanProgramAD.yaml",
        "    interval: 86400",
        "",
        "  Download:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Download.yaml",
        "    path: ./ruleset/Download.yaml",
        "    interval: 86400",
        "",
        "  LocalAreaNetwork:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/LocalAreaNetwork.yaml",
        "    path: ./ruleset/LocalAreaNetwork.yaml",
        "    interval: 86400",
        "",
        "  UnBan:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/UnBan.yaml",
        "    path: ./ruleset/UnBan.yaml",
        "    interval: 86400",
        "",
        "  ChinaDomain:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/ChinaDomain.yaml",
        "    path: ./ruleset/ChinaDomain.yaml",
        "    interval: 86400",
        "",
        "  ChinaCompanyIp:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/ChinaCompanyIp.yaml",
        "    path: ./ruleset/ChinaCompanyIp.yaml",
        "    interval: 86400",
        "",
        "  ChinaIp:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/ChinaIp.yaml",
        "    path: ./ruleset/ChinaIp.yaml",
        "    interval: 86400",
        "",
        "  ChinaMedia:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/ChinaMedia.yaml",
        "    path: ./ruleset/ChinaMedia.yaml",
        "    interval: 86400",
        "",
        "  ProxyGFWlist:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/ProxyGFWlist.yaml",
        "    path: ./ruleset/ProxyGFWlist.yaml",
        "    interval: 86400",
        "",
        "  ProxyMedia:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/ProxyMedia.yaml",
        "    path: ./ruleset/ProxyMedia.yaml",
        "    interval: 86400",
        "",
        "  Apple:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Apple.yaml",
        "    path: ./ruleset/Apple.yaml",
        "    interval: 86400",
        "",
        "  Bilibili:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Bilibili.yaml",
        "    path: ./ruleset/Bilibili.yaml",
        "    interval: 86400",
        "",
        "  BilibiliHMT:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/BilibiliHMT.yaml",
        "    path: ./ruleset/BilibiliHMT.yaml",
        "    interval: 86400",
        "",
        "  Google:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Google.yaml",
        "    path: ./ruleset/Google.yaml",
        "    interval: 86400",
        "",
        "  GoogleCN:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/GoogleCN.yaml",
        "    path: ./ruleset/GoogleCN.yaml",
        "    interval: 86400",
        "",
        "  GoogleFCM:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/GoogleFCM.yaml",
        "    path: ./ruleset/GoogleFCM.yaml",
        "    interval: 86400",
        "",
        "  Microsoft:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Microsoft.yaml",
        "    path: ./ruleset/Microsoft.yaml",
        "    interval: 86400",
        "",
        "  NetEaseMusic:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/NetEaseMusic.yaml",
        "    path: ./ruleset/NetEaseMusic.yaml",
        "    interval: 86400",
        "",
        "  Netflix:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Netflix.yaml",
        "    path: ./ruleset/Netflix.yaml",
        "    interval: 86400",
        "",
        "  OneDrive:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/OneDrive.yaml",
        "    path: ./ruleset/OneDrive.yaml",
        "    interval: 86400",
        "",
        "  Spotify:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Spotify.yaml",
        "    path: ./ruleset/Spotify.yaml",
        "    interval: 86400",
        "",
        "  Steam:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Steam.yaml",
        "    path: ./ruleset/Steam.yaml",
        "    interval: 86400",
        "",
        "  SteamCN:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/SteamCN.yaml",
        "    path: ./ruleset/SteamCN.yaml",
        "    interval: 86400",
        "",
        "  Telegram:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/Telegram.yaml",
        "    path: ./ruleset/Telegram.yaml",
        "    interval: 86400",
        "",
        "  YouTube:",
        "    type: http",
        "    behavior: classical",
        "    url: https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers/Ruleset/YouTube.yaml",
        "    path: ./ruleset/YouTube.yaml",
        "    interval: 86400",
        "",
        "rules:",
        "  - RULE-SET,Download,DIRECT",
        "  - RULE-SET,LocalAreaNetwork,DIRECT",
        "  - RULE-SET,BanAD,REJECT",
        "  - RULE-SET,BanEasyList,REJECT",
        "  - RULE-SET,BanEasyListChina,REJECT",
        "  - RULE-SET,BanEasyPrivacy,REJECT",
        "  - RULE-SET,BanProgramAD,REJECT",
        "  - DOMAIN-SUFFIX,cn,DIRECT",
        "  - RULE-SET,ChinaCompanyIp,DIRECT",
        "  - RULE-SET,ChinaDomain,DIRECT",
        "  - RULE-SET,ChinaIp,DIRECT",
        "  - RULE-SET,SteamCN,DIRECT",
        "  - RULE-SET,ProxyGFWlist,PROXY",
        "  - RULE-SET,Telegram,PROXY",
        "  - RULE-SET,UnBan,DIRECT",
        "  - RULE-SET,ChinaMedia,DIRECT",
        "  - RULE-SET,ProxyMedia,PROXY",
        "  - RULE-SET,Bilibili,DIRECT",
        "  - RULE-SET,BilibiliHMT,PROXY",
        "  - RULE-SET,NetEaseMusic,DIRECT",
        "  - RULE-SET,GoogleCN,DIRECT",
        "  - RULE-SET,GoogleFCM,PROXY",
        "  - RULE-SET,Google,PROXY",
        "  - RULE-SET,Apple,PROXY",
        "  - RULE-SET,Microsoft,PROXY",
        "  - RULE-SET,OneDrive,PROXY",
        "  - RULE-SET,Netflix,PROXY",
        "  - RULE-SET,Spotify,PROXY",
        "  - RULE-SET,Steam,PROXY",
        "  - RULE-SET,YouTube,PROXY",
        "  - GEOIP,CN,DIRECT",
        "  - MATCH,PROXY",
        "",
    ]
    return "\n".join(lines)


def status_payload(enabled, expires_at, traffic_limit_bytes=None, traffic_usage_bytes=0):
    expired = False
    if expires_at:
        expired = datetime.fromisoformat(expires_at) <= utc_now()
    if expired:
        return {"code": "expired", "label": "已过期"}
    if traffic_limit_bytes is not None and traffic_usage_bytes >= int(traffic_limit_bytes):
        return {"code": "quota", "label": "已达流量上限"}
    if enabled:
        return {"code": "active", "label": "运行中"}
    return {"code": "disabled", "label": "已停用"}


def request_auth_failed():
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="nginx-forward-panel"'},
    )


class PanelState:
    def __init__(self):
        self.write_lock = threading.Lock()
        self.stop_event = threading.Event()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STREAMS_DIR.mkdir(parents=True, exist_ok=True)
        STREAM_ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init_db(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listen_port INTEGER NOT NULL UNIQUE,
                    upstream_host TEXT NOT NULL,
                    upstream_port INTEGER NOT NULL,
                    expires_at TEXT,
                    traffic_limit_bytes INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS traffic_totals (
                    listen_port INTEGER PRIMARY KEY,
                    total_connections INTEGER NOT NULL DEFAULT 0,
                    total_bytes_sent INTEGER NOT NULL DEFAULT 0,
                    total_bytes_received INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT
                );

                CREATE TABLE IF NOT EXISTS traffic_daily (
                    listen_port INTEGER NOT NULL,
                    stat_date TEXT NOT NULL,
                    total_connections INTEGER NOT NULL DEFAULT 0,
                    total_bytes_sent INTEGER NOT NULL DEFAULT 0,
                    total_bytes_received INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (listen_port, stat_date)
                );

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS upstream_probes (
                    listen_port INTEGER PRIMARY KEY,
                    is_reachable INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    failure_reason TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS upstream_probe_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listen_port INTEGER NOT NULL,
                    is_reachable INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    failure_reason TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS ai_domains (
                    domain TEXT PRIMARY KEY,
                    classification TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    first_seen TEXT,
                    last_seen TEXT,
                    total_hits INTEGER NOT NULL DEFAULT 0,
                    last_protocols TEXT NOT NULL DEFAULT '[]',
                    last_report_window_start TEXT,
                    last_report_window_end TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_domain_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    hits INTEGER NOT NULL,
                    classification TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    protocols TEXT NOT NULL DEFAULT '[]',
                    first_seen TEXT,
                    last_seen TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_domain_observations_window
                ON ai_domain_observations(domain, window_start, window_end);

                CREATE INDEX IF NOT EXISTS idx_ai_domain_observations_domain
                ON ai_domain_observations(domain);
                """
            )
            self.ensure_port_schema(conn)

    def ensure_port_schema(self, conn):
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(ports)").fetchall()
        }
        if "traffic_limit_bytes" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN traffic_limit_bytes INTEGER")

    def ensure_subscription_token_in_tx(self, conn):
        token = str(self.get_state(conn, "subscription_token", "") or "").strip()
        if token:
            return token
        token = generate_subscription_token()
        self.set_state(conn, "subscription_token", token)
        return token

    def normalize_upstream_targets(self):
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE ports
                SET upstream_host = ?, upstream_port = ?
                WHERE upstream_host != ? OR upstream_port != ?
                """,
                (
                    DEFAULT_UPSTREAM_HOST,
                    DEFAULT_UPSTREAM_PORT,
                    DEFAULT_UPSTREAM_HOST,
                    DEFAULT_UPSTREAM_PORT,
                ),
            )
            conn.commit()

    def seed_defaults(self):
        if not SEED_LISTEN_PORT:
            return
        listen_port = parse_port(SEED_LISTEN_PORT, "默认监听端口")
        with self.connect() as conn:
            exists = conn.execute("SELECT COUNT(*) FROM ports").fetchone()[0]
            if exists:
                return
            now = utc_iso_now()
            conn.execute(
                """
                INSERT INTO ports (
                    listen_port, upstream_host, upstream_port, expires_at, enabled, note, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, 1, ?, ?, ?)
                """,
                (
                    listen_port,
                    DEFAULT_UPSTREAM_HOST,
                    DEFAULT_UPSTREAM_PORT,
                    "默认初始化端口",
                    now,
                    now,
                ),
            )

    def bootstrap(self):
        self.init_db()
        self.seed_defaults()
        self.normalize_upstream_targets()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.ensure_subscription_token_in_tx(conn)
            conn.commit()
        self.sync_traffic_logs()
        self.disable_auto_stopped_ports(reload_nginx=False)
        self.write_current_config()
        self.start_nginx()

    def get_state(self, conn, key, default=None):
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]

    def set_state(self, conn, key, value):
        conn.execute(
            """
            INSERT INTO app_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    def sync_traffic_logs(self):
        if not STREAM_ACCESS_LOG.exists():
            return 0

        with self.write_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                stat = STREAM_ACCESS_LOG.stat()
                current_inode = str(stat.st_ino)
                current_offset = int(self.get_state(conn, "stream_log_offset", "0"))
                recorded_inode = self.get_state(conn, "stream_log_inode", "")

                if recorded_inode != current_inode or stat.st_size < current_offset:
                    current_offset = 0

                aggregates = {}
                with STREAM_ACCESS_LOG.open("r", encoding="utf-8", errors="ignore") as handle:
                    handle.seek(current_offset)
                    for line in handle:
                        parsed = self.parse_stream_log_line(line)
                        if parsed is None:
                            continue
                        listen_port, bytes_sent, bytes_received, stat_date, seen_at = parsed
                        item = aggregates.setdefault(
                            (listen_port, stat_date),
                            {
                                "connections": 0,
                                "bytes_sent": 0,
                                "bytes_received": 0,
                                "last_seen": seen_at,
                            },
                        )
                        item["connections"] += 1
                        item["bytes_sent"] += bytes_sent
                        item["bytes_received"] += bytes_received
                        if seen_at > item["last_seen"]:
                            item["last_seen"] = seen_at
                    new_offset = handle.tell()

                for (listen_port, stat_date), item in aggregates.items():
                    conn.execute(
                        """
                        INSERT INTO traffic_totals (
                            listen_port, total_connections, total_bytes_sent, total_bytes_received, last_seen
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(listen_port) DO UPDATE SET
                            total_connections = total_connections + excluded.total_connections,
                            total_bytes_sent = total_bytes_sent + excluded.total_bytes_sent,
                            total_bytes_received = total_bytes_received + excluded.total_bytes_received,
                            last_seen = CASE
                                WHEN traffic_totals.last_seen IS NULL OR traffic_totals.last_seen < excluded.last_seen
                                THEN excluded.last_seen
                                ELSE traffic_totals.last_seen
                            END
                        """,
                        (
                            listen_port,
                            item["connections"],
                            item["bytes_sent"],
                            item["bytes_received"],
                            item["last_seen"],
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO traffic_daily (
                            listen_port, stat_date, total_connections, total_bytes_sent, total_bytes_received
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(listen_port, stat_date) DO UPDATE SET
                            total_connections = total_connections + excluded.total_connections,
                            total_bytes_sent = total_bytes_sent + excluded.total_bytes_sent,
                            total_bytes_received = total_bytes_received + excluded.total_bytes_received
                        """,
                        (
                            listen_port,
                            stat_date,
                            item["connections"],
                            item["bytes_sent"],
                            item["bytes_received"],
                        ),
                    )

                self.set_state(conn, "stream_log_inode", current_inode)
                self.set_state(conn, "stream_log_offset", str(new_offset))
                conn.commit()
                return len(aggregates)

    def parse_stream_log_line(self, line):
        parts = line.strip().split("\t")
        if len(parts) < 4:
            return None
        try:
            seen_at = datetime.fromisoformat(parts[0]).astimezone(timezone.utc).isoformat(timespec="seconds")
            listen_port = int(parts[1])
            bytes_sent = int(parts[2])
            bytes_received = int(parts[3])
        except (ValueError, IndexError):
            return None
        stat_date = seen_at[:10]
        return listen_port, bytes_sent, bytes_received, stat_date, seen_at

    def query_ports(self):
        today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    p.*,
                    COALESCE(t.total_connections, 0) AS total_connections,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received,
                    t.last_seen AS last_seen,
                    pr.is_reachable AS probe_is_reachable,
                    pr.checked_at AS probe_checked_at,
                    pr.failure_reason AS probe_failure_reason,
                    COALESCE(d.total_connections, 0) AS today_connections,
                    COALESCE(d.total_bytes_sent, 0) AS today_bytes_sent,
                    COALESCE(d.total_bytes_received, 0) AS today_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                LEFT JOIN upstream_probes pr ON pr.listen_port = p.listen_port
                LEFT JOIN traffic_daily d ON d.listen_port = p.listen_port AND d.stat_date = ?
                ORDER BY p.listen_port ASC
                """,
                (today,),
            ).fetchall()

        ports = []
        for row in rows:
            item = dict(row)
            item["expires_at_display"] = format_display_time(item["expires_at"])
            item["expires_at_input"] = format_input_time(item["expires_at"])
            item["last_seen_display"] = format_display_time(item["last_seen"]) if item["last_seen"] else "暂无"
            item["probe_checked_at_display"] = (
                format_display_time(item["probe_checked_at"]) if item["probe_checked_at"] else "暂无"
            )
            item["probe_status"] = "unknown"
            item["probe_status_label"] = "未检测"
            item["probe_failure_reason"] = item["probe_failure_reason"] or ""
            if item["probe_is_reachable"] is not None:
                if int(item["probe_is_reachable"]):
                    item["probe_status"] = "healthy"
                    item["probe_status_label"] = "后端可达"
                else:
                    item["probe_status"] = "unhealthy"
                    item["probe_status_label"] = "后端不可达"
            item["traffic_usage_bytes"] = int(item["total_bytes_sent"]) + int(item["total_bytes_received"])
            item["traffic_limit_display"] = (
                human_bytes(item["traffic_limit_bytes"]) if item["traffic_limit_bytes"] is not None else "无限制"
            )
            item["traffic_limit_input"] = (
                human_bytes(item["traffic_limit_bytes"]) if item["traffic_limit_bytes"] is not None else ""
            )
            item["traffic_used_display"] = human_bytes(item["traffic_usage_bytes"])
            if item["traffic_limit_bytes"] is None:
                item["traffic_remaining_display"] = "无限制"
            else:
                item["traffic_remaining_display"] = human_bytes(
                    max(int(item["traffic_limit_bytes"]) - item["traffic_usage_bytes"], 0)
                )
            status = status_payload(
                bool(item["enabled"]),
                item["expires_at"],
                item["traffic_limit_bytes"],
                item["traffic_usage_bytes"],
            )
            item["status"] = status["code"]
            item["status_label"] = status["label"]
            ports.append(item)
        return ports

    def get_subscription_token(self):
        with self.write_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                token = self.ensure_subscription_token_in_tx(conn)
                conn.commit()
                return token

    def rotate_subscription_token(self):
        with self.write_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                token = generate_subscription_token()
                self.set_state(conn, "subscription_token", token)
                conn.commit()
                return token

    def get_port_subscription_record(self, listen_port):
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    p.id,
                    p.listen_port,
                    p.note,
                    p.enabled,
                    p.expires_at,
                    p.traffic_limit_bytes,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                WHERE p.listen_port = ?
                LIMIT 1
                """,
                (listen_port,),
            ).fetchone()

        if row is None:
            return None

        item = dict(row)
        item["traffic_usage_bytes"] = int(item["total_bytes_sent"]) + int(item["total_bytes_received"])
        status = status_payload(
            bool(item["enabled"]),
            item["expires_at"],
            item["traffic_limit_bytes"],
            item["traffic_usage_bytes"],
        )
        item["status"] = status["code"]
        item["status_label"] = status["label"]
        return item

    def query_summary(self, ports):
        summary = {
            "total_ports": len(ports),
            "active_ports": 0,
            "expired_ports": 0,
            "quota_ports": 0,
            "disabled_ports": 0,
            "total_connections": 0,
            "total_bytes_sent": 0,
            "total_bytes_received": 0,
        }
        for port in ports:
            summary["total_connections"] += port["total_connections"]
            summary["total_bytes_sent"] += port["total_bytes_sent"]
            summary["total_bytes_received"] += port["total_bytes_received"]
            if port["status"] == "active":
                summary["active_ports"] += 1
            elif port["status"] == "expired":
                summary["expired_ports"] += 1
            elif port["status"] == "quota":
                summary["quota_ports"] += 1
            else:
                summary["disabled_ports"] += 1
        return summary

    def validate_port_payload(self, form):
        return {
            "listen_port": parse_port(form.get("listen_port"), "监听端口"),
            "upstream_host": DEFAULT_UPSTREAM_HOST,
            "upstream_port": DEFAULT_UPSTREAM_PORT,
            "expires_at": parse_expiry(form.get("expires_at")),
            "traffic_limit_bytes": parse_data_size(form.get("traffic_limit"), "流量上限"),
            "note": parse_note(form.get("note")),
        }

    def create_port(self, payload):
        def operation(conn):
            now = utc_iso_now()
            conn.execute(
                """
                INSERT INTO ports (
                    listen_port, upstream_host, upstream_port, expires_at, traffic_limit_bytes, enabled, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    payload["listen_port"],
                    payload["upstream_host"],
                    payload["upstream_port"],
                    payload["expires_at"],
                    payload["traffic_limit_bytes"],
                    payload["note"],
                    now,
                    now,
                ),
            )

        self.apply_mutation(operation)

    def update_port(self, port_id, payload):
        def operation(conn):
            now = utc_iso_now()
            existing = conn.execute("SELECT id FROM ports WHERE id = ?", (port_id,)).fetchone()
            if existing is None:
                raise ValidationError("端口记录不存在。")
            conn.execute(
                """
                UPDATE ports
                SET listen_port = ?, upstream_host = ?, upstream_port = ?, expires_at = ?, traffic_limit_bytes = ?, note = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["listen_port"],
                    payload["upstream_host"],
                    payload["upstream_port"],
                    payload["expires_at"],
                    payload["traffic_limit_bytes"],
                    payload["note"],
                    now,
                    port_id,
                ),
            )

        self.apply_mutation(operation)

    def toggle_port(self, port_id):
        def operation(conn):
            row = conn.execute(
                "SELECT id, listen_port, enabled, expires_at, traffic_limit_bytes FROM ports WHERE id = ?",
                (port_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")
            next_enabled = 0 if row["enabled"] else 1
            if next_enabled and row["expires_at"]:
                expires_at = datetime.fromisoformat(row["expires_at"])
                if expires_at <= utc_now():
                    raise ValidationError("端口已过期，请先修改到期时间再启用。")
            if next_enabled and row["traffic_limit_bytes"] is not None:
                usage_bytes = self.get_port_usage_bytes(conn, row["listen_port"])
                if usage_bytes >= int(row["traffic_limit_bytes"]):
                    raise ValidationError("端口已达到流量上限，请先提高上限再启用。")
            conn.execute(
                "UPDATE ports SET enabled = ?, updated_at = ? WHERE id = ?",
                (next_enabled, utc_iso_now(), port_id),
            )

        self.apply_mutation(operation)

    def delete_port(self, port_id):
        def operation(conn):
            row = conn.execute("SELECT listen_port FROM ports WHERE id = ?", (port_id,)).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")
            listen_port = row["listen_port"]
            conn.execute("DELETE FROM ports WHERE id = ?", (port_id,))
            conn.execute("DELETE FROM traffic_totals WHERE listen_port = ?", (listen_port,))
            conn.execute("DELETE FROM traffic_daily WHERE listen_port = ?", (listen_port,))

        self.apply_mutation(operation)

    def disable_expired_ports(self, reload_nginx=True):
        return self.disable_auto_stopped_ports(reload_nginx=reload_nginx)

    def run_upstream_probes(self):
        if not PROBE_ENABLED:
            return 0

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT listen_port, upstream_host, upstream_port
                FROM ports
                ORDER BY listen_port ASC
                """
            ).fetchall()

        results = []
        for row in rows:
            checked_at = utc_iso_now()
            reachable = 0
            failure_reason = ""
            try:
                with socket.create_connection(
                    (row["upstream_host"], int(row["upstream_port"])),
                    timeout=PROBE_TIMEOUT,
                ):
                    reachable = 1
            except OSError as exc:
                failure_reason = str(exc)[:200]
            results.append((row["listen_port"], reachable, checked_at, failure_reason))

        with self.write_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for item in results:
                    conn.execute(
                        """
                        INSERT INTO upstream_probes (listen_port, is_reachable, checked_at, failure_reason)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(listen_port) DO UPDATE SET
                            is_reachable = excluded.is_reachable,
                            checked_at = excluded.checked_at,
                            failure_reason = excluded.failure_reason
                        """,
                        item,
                    )
                    conn.execute(
                        """
                        INSERT INTO upstream_probe_history (
                            listen_port, is_reachable, checked_at, failure_reason
                        ) VALUES (?, ?, ?, ?)
                        """,
                        item,
                    )
                cutoff = (
                    datetime.now(timezone.utc).timestamp() - 7 * 24 * 3600
                )
                conn.execute(
                    """
                    DELETE FROM upstream_probe_history
                    WHERE strftime('%s', checked_at) < ?
                    """,
                    (int(cutoff),),
                )
                conn.commit()
        return len(results)

    def get_probe_dashboard(self, range_key):
        active_range_key = range_key if range_key in PROBE_DASHBOARD_RANGES else "24h"
        active_range = PROBE_DASHBOARD_RANGES[active_range_key]
        since_dt = utc_now().timestamp() - active_range["hours"] * 3600
        with self.connect() as conn:
            if PROBE_TEST_LISTEN_PORT is not None:
                test_port = conn.execute(
                    """
                    SELECT listen_port, upstream_host, upstream_port, note, enabled
                    FROM ports
                    WHERE listen_port = ?
                    LIMIT 1
                    """,
                    (PROBE_TEST_LISTEN_PORT,),
                ).fetchone()
            else:
                test_port = conn.execute(
                    """
                    SELECT listen_port, upstream_host, upstream_port, note, enabled
                    FROM ports
                    WHERE enabled = 1
                    ORDER BY listen_port ASC
                    LIMIT 1
                    """
                ).fetchone()
            if test_port is None:
                return {
                    "test_port": None,
                    "summary": None,
                    "chart_points": [],
                    "recent_checks": [],
                    "requested_test_port": PROBE_TEST_LISTEN_PORT,
                    "range_key": active_range_key,
                    "range_label": active_range["label"],
                    "range_options": self.probe_dashboard_range_options(active_range_key),
                }

            history_rows = conn.execute(
                """
                SELECT is_reachable, checked_at, failure_reason
                FROM upstream_probe_history
                WHERE listen_port = ?
                ORDER BY checked_at DESC
                LIMIT 300
                """,
                (test_port["listen_port"],),
            ).fetchall()

        filtered_rows = []
        for row in history_rows:
            checked_local = localize_time(row["checked_at"])
            if checked_local is None:
                continue
            if checked_local.timestamp() >= since_dt:
                filtered_rows.append(row)

        history = list(reversed(filtered_rows[:120]))
        chart_points = []
        healthy_count = 0
        unhealthy_count = 0
        last_success = None
        last_failure = None
        for index, row in enumerate(history):
            checked_local = localize_time(row["checked_at"])
            is_reachable = bool(row["is_reachable"])
            if is_reachable:
                healthy_count += 1
                last_success = checked_local
            else:
                unhealthy_count += 1
                last_failure = checked_local
            chart_points.append(
                {
                    "x": index,
                    "y": 1 if is_reachable else 0,
                    "label": checked_local.strftime("%m-%d %H:%M:%S") if checked_local else "",
                    "status": "healthy" if is_reachable else "unhealthy",
                }
            )

        recent_checks = []
        for row in filtered_rows[:12]:
            checked_local = localize_time(row["checked_at"])
            recent_checks.append(
                {
                    "status": "healthy" if row["is_reachable"] else "unhealthy",
                    "status_label": "可达" if row["is_reachable"] else "不可达",
                    "checked_at_display": checked_local.strftime("%Y-%m-%d %H:%M:%S") if checked_local else "暂无",
                    "failure_reason": row["failure_reason"] or "",
                }
            )

        total_checks = healthy_count + unhealthy_count
        uptime_ratio = (healthy_count / total_checks * 100) if total_checks else 0.0
        current_status = "unknown"
        current_status_label = "未检测"
        if filtered_rows:
            current_status = "healthy" if filtered_rows[0]["is_reachable"] else "unhealthy"
            current_status_label = "后端可达" if filtered_rows[0]["is_reachable"] else "后端不可达"

        return {
            "test_port": {
                "listen_port": test_port["listen_port"],
                "fixed": PROBE_TEST_LISTEN_PORT is not None,
                "enabled": bool(test_port["enabled"]),
            },
            "summary": {
                "current_status": current_status,
                "current_status_label": current_status_label,
                "total_checks": total_checks,
                "healthy_count": healthy_count,
                "unhealthy_count": unhealthy_count,
                "uptime_ratio": f"{uptime_ratio:.1f}",
                "last_success_display": last_success.strftime("%Y-%m-%d %H:%M:%S") if last_success else "暂无",
                "last_failure_display": last_failure.strftime("%Y-%m-%d %H:%M:%S") if last_failure else "暂无",
            },
            "chart_points": chart_points,
            "recent_checks": recent_checks,
            "requested_test_port": PROBE_TEST_LISTEN_PORT,
            "range_key": active_range_key,
            "range_label": active_range["label"],
            "range_options": self.probe_dashboard_range_options(active_range_key),
        }

    def probe_dashboard_range_options(self, active_range_key):
        return [
            {
                "key": key,
                "label": config["label"],
                "active": key == active_range_key,
            }
            for key, config in PROBE_DASHBOARD_RANGES.items()
        ]

    def disable_auto_stopped_ports(self, reload_nginx=True):
        with self.write_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = self.disable_auto_stopped_ports_in_tx(conn)
                if changed:
                    self.persist_and_reload(conn, reload_nginx=reload_nginx)
                else:
                    conn.commit()
                return changed

    def apply_mutation(self, operation):
        with self.write_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(conn)
                    self.disable_auto_stopped_ports_in_tx(conn)
                    self.persist_and_reload(conn, reload_nginx=True)
                    return result
                except Exception:
                    conn.rollback()
                    raise

    def get_port_usage_bytes(self, conn, listen_port):
        row = conn.execute(
            """
            SELECT
                COALESCE(total_bytes_sent, 0) + COALESCE(total_bytes_received, 0) AS usage_bytes
            FROM traffic_totals
            WHERE listen_port = ?
            """,
            (listen_port,),
        ).fetchone()
        if row is None:
            return 0
        return int(row["usage_bytes"])

    def mark_stream_log_consumed(self, conn):
        if not STREAM_ACCESS_LOG.exists():
            return
        stat = STREAM_ACCESS_LOG.stat()
        self.set_state(conn, "stream_log_inode", str(stat.st_ino))
        self.set_state(conn, "stream_log_offset", str(stat.st_size))

    def reset_port_traffic(self, port_id):
        def operation(conn):
            row = conn.execute(
                """
                SELECT
                    p.id,
                    p.listen_port,
                    p.enabled,
                    p.expires_at,
                    p.traffic_limit_bytes,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                WHERE p.id = ?
                """,
                (port_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")

            self.mark_stream_log_consumed(conn)
            conn.execute(
                """
                UPDATE traffic_totals
                SET total_bytes_sent = 0, total_bytes_received = 0
                WHERE listen_port = ?
                """,
                (row["listen_port"],),
            )
            conn.execute(
                """
                UPDATE traffic_daily
                SET total_bytes_sent = 0, total_bytes_received = 0
                WHERE listen_port = ?
                """,
                (row["listen_port"],),
            )

            now_dt = utc_now()
            expired = False
            if row["expires_at"]:
                expired = datetime.fromisoformat(row["expires_at"]) <= now_dt
            usage_bytes = int(row["total_bytes_sent"]) + int(row["total_bytes_received"])
            quota_reached = (
                row["traffic_limit_bytes"] is not None
                and usage_bytes >= int(row["traffic_limit_bytes"])
            )

            next_enabled = int(row["enabled"])
            restored = False
            if quota_reached and not expired:
                next_enabled = 1
                restored = True

            conn.execute(
                "UPDATE ports SET enabled = ?, updated_at = ? WHERE id = ?",
                (next_enabled, now_dt.isoformat(timespec="seconds"), port_id),
            )
            return restored

        return self.apply_mutation(operation)

    def disable_auto_stopped_ports_in_tx(self, conn):
        now_dt = utc_now()
        now_text = now_dt.isoformat(timespec="seconds")
        rows = conn.execute(
            """
            SELECT
                p.id,
                p.listen_port,
                p.expires_at,
                p.traffic_limit_bytes,
                COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                COALESCE(t.total_bytes_received, 0) AS total_bytes_received
            FROM ports p
            LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
            WHERE p.enabled = 1
            """,
        ).fetchall()
        changed = 0
        for row in rows:
            expired = False
            if row["expires_at"]:
                expired = datetime.fromisoformat(row["expires_at"]) <= now_dt
            usage_bytes = int(row["total_bytes_sent"]) + int(row["total_bytes_received"])
            quota_reached = (
                row["traffic_limit_bytes"] is not None
                and usage_bytes >= int(row["traffic_limit_bytes"])
            )
            if expired or quota_reached:
                conn.execute(
                    "UPDATE ports SET enabled = 0, updated_at = ? WHERE id = ?",
                    (now_text, row["id"]),
                )
                changed += 1
        return changed

    def persist_and_reload(self, conn, reload_nginx):
        previous_config = GENERATED_STREAM_CONFIG.read_text(encoding="utf-8") if GENERATED_STREAM_CONFIG.exists() else None
        config_text = self.render_stream_config(conn)
        GENERATED_STREAM_CONFIG.write_text(config_text, encoding="utf-8")
        try:
            self.nginx_config_test()
            if reload_nginx and self.nginx_running():
                self.nginx_reload()
        except Exception:
            if previous_config is None:
                GENERATED_STREAM_CONFIG.unlink(missing_ok=True)
            else:
                GENERATED_STREAM_CONFIG.write_text(previous_config, encoding="utf-8")
            raise
        conn.commit()

    def render_stream_config(self, conn):
        rows = conn.execute(
            """
            SELECT
                p.listen_port,
                p.upstream_host,
                p.upstream_port
            FROM ports
            AS p
            LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
            WHERE p.enabled = 1
              AND (p.expires_at IS NULL OR p.expires_at > ?)
              AND (
                    p.traffic_limit_bytes IS NULL
                    OR COALESCE(t.total_bytes_sent, 0) + COALESCE(t.total_bytes_received, 0) < p.traffic_limit_bytes
                  )
            ORDER BY p.listen_port ASC
            """,
            (utc_iso_now(),),
        ).fetchall()
        blocks = [
            "# Generated by nginx-forward-panel.",
            "# Do not edit this file manually.",
            "",
        ]
        for row in rows:
            listen_options = [str(row["listen_port"]), "reuseport"]
            if STREAM_LISTEN_BACKLOG > 0:
                listen_options.append(f"backlog={STREAM_LISTEN_BACKLOG}")
            if STREAM_LISTEN_FASTOPEN > 0:
                listen_options.append(f"fastopen={STREAM_LISTEN_FASTOPEN}")
            if STREAM_LISTEN_SO_KEEPALIVE:
                listen_options.append(f"so_keepalive={STREAM_LISTEN_SO_KEEPALIVE}")
            blocks.extend(
                [
                    "server {",
                    f"    listen {' '.join(listen_options)};",
                    f"    proxy_connect_timeout {PROXY_CONNECT_TIMEOUT};",
                    f"    proxy_timeout {PROXY_TIMEOUT};",
                    "    proxy_socket_keepalive on;" if STREAM_PROXY_SOCKET_KEEPALIVE else "",
                    f"    proxy_pass {row['upstream_host']}:{row['upstream_port']};",
                    "}",
                    "",
                ]
            )
        return "\n".join(line for line in blocks if line).strip() + "\n"

    def write_current_config(self):
        with self.connect() as conn:
            GENERATED_STREAM_CONFIG.write_text(self.render_stream_config(conn), encoding="utf-8")
        self.nginx_config_test()

    def nginx_config_test(self):
        self.run_command(["nginx", "-c", str(NGINX_CONFIG_PATH), "-t"], "nginx 配置校验失败")

    def start_nginx(self):
        self.run_command(["nginx", "-c", str(NGINX_CONFIG_PATH)], "nginx 启动失败")

    def nginx_reload(self):
        self.run_command(["nginx", "-c", str(NGINX_CONFIG_PATH), "-s", "reload"], "nginx 重载失败")

    def nginx_stop(self):
        if not self.nginx_running():
            return
        try:
            self.run_command(["nginx", "-c", str(NGINX_CONFIG_PATH), "-s", "quit"], "nginx 停止失败")
        except RuntimeError:
            pass

    def nginx_pid(self):
        if not NGINX_PID_PATH.exists():
            return None
        try:
            pid = int(NGINX_PID_PATH.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def nginx_running(self):
        return self.nginx_pid() is not None

    def run_command(self, command, error_prefix):
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode == 0:
            return completed
        detail = completed.stderr.strip() or completed.stdout.strip() or "未知错误"
        raise RuntimeError(f"{error_prefix}: {detail}")

    def maintenance_loop(self):
        last_probe_at = 0.0
        while not self.stop_event.wait(MAINTENANCE_INTERVAL):
            try:
                self.sync_traffic_logs()
                self.disable_auto_stopped_ports(reload_nginx=True)
                now_monotonic = time.monotonic()
                if PROBE_ENABLED and now_monotonic - last_probe_at >= PROBE_INTERVAL:
                    self.run_upstream_probes()
                    last_probe_at = now_monotonic
            except Exception:
                continue

    def stop(self):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.sync_traffic_logs()
        self.nginx_stop()


state = PanelState()


@app.before_request
def ensure_basic_auth():
    if request.path == "/healthz":
        return None
    if request.endpoint in {"subscription_default", "subscription_clash", "subscription_v2ray"}:
        return None
    if not AUTH_ENABLED:
        return None
    auth = request.authorization
    if auth and auth.username == PANEL_USERNAME and auth.password == PANEL_PASSWORD:
        return None

    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            if username == PANEL_USERNAME and password == PANEL_PASSWORD:
                return None
        except Exception:
            pass
    return request_auth_failed()


@app.template_filter("human_bytes")
def human_bytes_filter(value):
    return human_bytes(value)


@app.route("/", methods=["GET"])
def index():
    state.sync_traffic_logs()
    state.disable_auto_stopped_ports(reload_nginx=True)
    ports = state.query_ports()
    summary = state.query_summary(ports)
    subscription_profile, subscription_error = parse_xray_client_profile()
    subscription = {
        "available": subscription_profile is not None,
        "error": subscription_error,
        "token": "",
        "client_config_path": str(XRAY_CLIENT_CONFIG_PATH),
        "server": subscription_profile["server"] if subscription_profile else "",
    }
    if subscription_profile is not None:
        subscription["token"] = state.get_subscription_token()
        for port in ports:
            port["subscription"] = {
                "clash_url": external_url_for(
                    "subscription_clash",
                    token=subscription["token"],
                    listen_port=port["listen_port"],
                ),
                "v2ray_url": external_url_for(
                    "subscription_v2ray",
                    token=subscription["token"],
                    listen_port=port["listen_port"],
                ),
                "default_url": external_url_for(
                    "subscription_default",
                    token=subscription["token"],
                    listen_port=port["listen_port"],
                ),
                "share_link": build_vless_share_link(subscription_profile, port["listen_port"], port["note"]),
            }
    return render_template(
        "index.html",
        ports=ports,
        summary=summary,
        default_upstream_host=DEFAULT_UPSTREAM_HOST,
        default_upstream_port=DEFAULT_UPSTREAM_PORT,
        timezone_label=datetime.now().astimezone().strftime("%Z"),
        message=request.args.get("message", "").strip(),
        level=request.args.get("level", "info").strip(),
        nginx_running=state.nginx_running(),
        panel_host=PANEL_HOST,
        panel_port=PANEL_PORT,
        panel_public_url=(f"{PANEL_PUBLIC_URL}/" if PANEL_PUBLIC_URL else ""),
        probe_enabled=PROBE_ENABLED,
        subscription=subscription,
    )


@app.route("/probe-dashboard", methods=["GET"])
def probe_dashboard():
    if not PROBE_ENABLED:
        return redirect(url_for("index", message="探针检测已停用。", level="info"), code=303)
    state.sync_traffic_logs()
    dashboard = state.get_probe_dashboard(request.args.get("range", "24h").strip())
    return render_template(
        "probe_dashboard.html",
        dashboard=dashboard,
        timezone_label=datetime.now().astimezone().strftime("%Z"),
        nginx_running=state.nginx_running(),
        panel_host=PANEL_HOST,
        panel_port=PANEL_PORT,
        panel_public_url=(f"{PANEL_PUBLIC_URL}/" if PANEL_PUBLIC_URL else ""),
    )


@app.route("/healthz", methods=["GET"])
def healthz():
    state.sync_traffic_logs()
    healthy = state.nginx_running()
    status_code = 200 if healthy else 500
    return jsonify({"ok": healthy, "nginx_running": healthy}), status_code


def build_subscription_response(token, listen_port, output_format):
    expected_token = state.get_subscription_token()
    if token != expected_token:
        abort(404)

    profile, _ = parse_xray_client_profile()
    if profile is None:
        abort(404)

    port = state.get_port_subscription_record(listen_port)
    if port is None:
        abort(404)

    if output_format == "v2ray":
        content = build_v2ray_subscription_content(profile, listen_port, port["note"])
        content_type = "text/plain; charset=utf-8"
    else:
        content = build_clash_subscription_content(profile, listen_port, port["note"])
        content_type = "text/yaml; charset=utf-8"

    return Response(content, content_type=content_type)


@app.route("/subscriptions/rotate", methods=["POST"])
def rotate_subscription():
    state.rotate_subscription_token()
    return message_redirect("订阅链接已重新生成，旧链接已失效。", "success")


@app.route("/<token>/<int:listen_port>", methods=["GET"])
def subscription_default(token, listen_port):
    return build_subscription_response(token, listen_port, "clash")


@app.route("/<token>/<int:listen_port>/clash", methods=["GET"])
def subscription_clash(token, listen_port):
    return build_subscription_response(token, listen_port, "clash")


@app.route("/<token>/<int:listen_port>/v2ray", methods=["GET"])
def subscription_v2ray(token, listen_port):
    return build_subscription_response(token, listen_port, "v2ray")


@app.route("/ports/create", methods=["POST"])
def create_port():
    try:
        payload = state.validate_port_payload(request.form)
        state.create_port(payload)
        return message_redirect("端口已创建并写入 nginx。", "success")
    except sqlite3.IntegrityError:
        return message_redirect("监听端口已存在，请更换其他端口。", "error")
    except (ValidationError, RuntimeError) as exc:
        return message_redirect(str(exc), "error")


@app.route("/ports/<int:port_id>/update", methods=["POST"])
def update_port(port_id):
    try:
        payload = state.validate_port_payload(request.form)
        state.update_port(port_id, payload)
        return message_redirect("端口配置已更新。", "success")
    except sqlite3.IntegrityError:
        return message_redirect("监听端口已存在，请更换其他端口。", "error")
    except (ValidationError, RuntimeError) as exc:
        return message_redirect(str(exc), "error")


@app.route("/ports/<int:port_id>/toggle", methods=["POST"])
def toggle_port(port_id):
    try:
        state.toggle_port(port_id)
        return message_redirect("端口状态已切换。", "success")
    except (ValidationError, RuntimeError) as exc:
        return message_redirect(str(exc), "error")


@app.route("/ports/<int:port_id>/delete", methods=["POST"])
def delete_port(port_id):
    try:
        state.delete_port(port_id)
        return message_redirect("端口已删除。", "success")
    except (ValidationError, RuntimeError) as exc:
        return message_redirect(str(exc), "error")


@app.route("/ports/<int:port_id>/reset-traffic", methods=["POST"])
def reset_port_traffic(port_id):
    try:
        restored = state.reset_port_traffic(port_id)
        message = "流量已重置，端口已恢复启用。" if restored else "流量已重置。"
        return message_redirect(message, "success")
    except (ValidationError, RuntimeError) as exc:
        return message_redirect(str(exc), "error")


def message_redirect(message, level):
    return redirect(url_for("index", message=message, level=level), code=303)


def handle_shutdown(signum, _frame):
    raise KeyboardInterrupt(f"received signal {signum}")


def main():
    state.bootstrap()
    worker = threading.Thread(target=state.maintenance_loop, daemon=True)
    worker.start()
    atexit.register(state.stop)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    try:
        app.run(host=PANEL_HOST, port=PANEL_PORT, threaded=True, use_reloader=False)
    finally:
        state.stop()


if __name__ == "__main__":
    main()
