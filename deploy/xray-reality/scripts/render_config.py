#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlencode


REQUIRED_KEYS = [
    "XRAY_LISTEN_HOST",
    "XRAY_LISTEN_PORT",
    "XRAY_PUBLIC_HOST",
    "XRAY_CLIENT_UUID",
    "XRAY_FLOW",
    "XRAY_REALITY_PRIVATE_KEY",
    "XRAY_REALITY_PUBLIC_KEY",
    "XRAY_REALITY_SHORT_ID",
    "XRAY_SERVER_NAME",
    "XRAY_DEST",
    "XRAY_FINGERPRINT",
    "XRAY_LOGLEVEL",
    "XRAY_NODE_TAG",
]


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid line in {path}: {raw_line}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def validate_env(values: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ValueError(f"missing required values: {', '.join(missing)}")

    try:
        port = int(values["XRAY_LISTEN_PORT"])
    except ValueError as exc:
        raise ValueError("XRAY_LISTEN_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("XRAY_LISTEN_PORT must be in 1..65535")

    public_port_value = values.get("XRAY_PUBLIC_PORT", "").strip()
    if public_port_value:
        try:
            public_port = int(public_port_value)
        except ValueError as exc:
            raise ValueError("XRAY_PUBLIC_PORT must be an integer") from exc
        if public_port < 1 or public_port > 65535:
            raise ValueError("XRAY_PUBLIC_PORT must be in 1..65535")

    short_id = values["XRAY_REALITY_SHORT_ID"]
    if not re.fullmatch(r"[0-9a-fA-F]{1,16}", short_id):
        raise ValueError("XRAY_REALITY_SHORT_ID must be 1-16 hex characters")

    if ":" not in values["XRAY_DEST"]:
        raise ValueError("XRAY_DEST must look like host:port")


def env_bool(values: dict[str, str], key: str, default: bool) -> bool:
    raw = str(values.get(key, "1" if default else "0")).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def env_nonnegative_int(values: dict[str, str], key: str, default: int) -> int:
    raw = str(values.get(key, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def build_stream_sockopt(values: dict[str, str]) -> dict:
    sockopt: dict[str, object] = {}
    if env_bool(values, "XRAY_TCP_FAST_OPEN", True):
        sockopt["tcpFastOpen"] = True

    keepalive_idle = env_nonnegative_int(values, "XRAY_TCP_KEEPALIVE_IDLE", 180)
    if keepalive_idle > 0:
        sockopt["tcpKeepAliveIdle"] = keepalive_idle

    keepalive_interval = env_nonnegative_int(values, "XRAY_TCP_KEEPALIVE_INTERVAL", 30)
    if keepalive_interval > 0:
        sockopt["tcpKeepAliveInterval"] = keepalive_interval

    return sockopt


def load_optional_json(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"dynamic routing file must contain a JSON object: {path}")
    return payload


def merge_dynamic_routing(config: dict, dynamic_payload: dict | None) -> dict:
    if not dynamic_payload:
        return config

    extra_outbounds = dynamic_payload.get("outbounds", [])
    if extra_outbounds:
        if not isinstance(extra_outbounds, list):
            raise ValueError("dynamic outbounds must be a JSON list")
        config["outbounds"].extend(extra_outbounds)

    extra_routing = dynamic_payload.get("routing", {})
    if extra_routing:
        if not isinstance(extra_routing, dict):
            raise ValueError("dynamic routing must be a JSON object")
        routing = config.setdefault("routing", {})
        if "domainStrategy" in extra_routing:
            routing["domainStrategy"] = extra_routing["domainStrategy"]
        rules = extra_routing.get("rules", [])
        if rules:
            if not isinstance(rules, list):
                raise ValueError("dynamic routing rules must be a JSON list")
            routing.setdefault("rules", [])
            routing["rules"] = list(rules) + list(routing["rules"])

    return config


def build_server_config(values: dict[str, str], dynamic_payload: dict | None = None) -> dict:
    stream_sockopt = build_stream_sockopt(values)
    config = {
        "log": {
            "loglevel": values["XRAY_LOGLEVEL"],
            "access": "/var/log/xray/access.log",
            "error": "/var/log/xray/error.log",
        },
        "inbounds": [
            {
                "listen": values["XRAY_LISTEN_HOST"],
                "port": int(values["XRAY_LISTEN_PORT"]),
                "protocol": "vless",
                "settings": {
                    "clients": [
                        {
                            "id": values["XRAY_CLIENT_UUID"],
                            "flow": values["XRAY_FLOW"],
                        }
                    ],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "sockopt": stream_sockopt,
                    "realitySettings": {
                        "show": False,
                        "dest": values["XRAY_DEST"],
                        "xver": 0,
                        "serverNames": [values["XRAY_SERVER_NAME"]],
                        "privateKey": values["XRAY_REALITY_PRIVATE_KEY"],
                        "shortIds": [values["XRAY_REALITY_SHORT_ID"]],
                    },
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": True,
                },
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }
    return merge_dynamic_routing(config, dynamic_payload)


def resolve_public_port(values: dict[str, str]) -> int:
    public_port_value = values.get("XRAY_PUBLIC_PORT", "").strip()
    if public_port_value:
        return int(public_port_value)
    return int(values["XRAY_LISTEN_PORT"])


def build_client_config(values: dict[str, str]) -> dict:
    public_port = resolve_public_port(values)
    stream_sockopt = build_stream_sockopt(values)
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10808,
                "protocol": "socks",
                "settings": {"udp": False},
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": values["XRAY_PUBLIC_HOST"],
                            "port": public_port,
                            "users": [
                                {
                                    "id": values["XRAY_CLIENT_UUID"],
                                    "encryption": "none",
                                    "flow": values["XRAY_FLOW"],
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "sockopt": stream_sockopt,
                    "realitySettings": {
                        "serverName": values["XRAY_SERVER_NAME"],
                        "fingerprint": values["XRAY_FINGERPRINT"],
                        "publicKey": values["XRAY_REALITY_PUBLIC_KEY"],
                        "shortId": values["XRAY_REALITY_SHORT_ID"],
                    },
                },
            }
        ],
    }


def build_share_url(values: dict[str, str]) -> str:
    public_port = resolve_public_port(values)
    params = urlencode(
        {
            "encryption": "none",
            "flow": values["XRAY_FLOW"],
            "security": "reality",
            "sni": values["XRAY_SERVER_NAME"],
            "fp": values["XRAY_FINGERPRINT"],
            "pbk": values["XRAY_REALITY_PUBLIC_KEY"],
            "sid": values["XRAY_REALITY_SHORT_ID"],
            "type": "tcp",
            "headerType": "none",
        }
    )
    tag = quote(values["XRAY_NODE_TAG"], safe="")
    return (
        f"vless://{values['XRAY_CLIENT_UUID']}@{values['XRAY_PUBLIC_HOST']}:"
        f"{public_port}?{params}#{tag}"
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def main() -> int:
    base_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Render Xray REALITY config from .env")
    parser.add_argument("--env-file", default=str(base_dir / ".env"))
    parser.add_argument("--config-out", default=str(base_dir / "runtime" / "config.json"))
    parser.add_argument("--client-out", default=str(base_dir / "runtime" / "client-test.json"))
    parser.add_argument("--share-out", default=str(base_dir / "runtime" / "client-share.txt"))
    parser.add_argument("--dynamic-routing-file", default=str(base_dir / "runtime" / "dynamic-routing.json"))
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.is_file():
        print(f"env file not found: {env_path}", file=sys.stderr)
        return 1

    try:
        values = load_env_file(env_path)
        validate_env(values)
        dynamic_payload = load_optional_json(Path(args.dynamic_routing_file))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    write_json(Path(args.config_out), build_server_config(values, dynamic_payload))
    write_json(Path(args.client_out), build_client_config(values))

    share_path = Path(args.share_out)
    share_path.parent.mkdir(parents=True, exist_ok=True)
    share_path.write_text(build_share_url(values) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
