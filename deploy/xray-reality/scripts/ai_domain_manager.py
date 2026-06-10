#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


TIMESTAMP_RE = re.compile(r"^(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?) ")
TARGET_RE = re.compile(r" accepted (?P<proto>[a-z]+):(?P<target>\S+)")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
PLACEHOLDER_RE = re.compile(r"__([A-Z0-9_]+)__")
UNSET_PROXY_PROTOCOL = "replace_me"
FORCED_AI_ROUTE_DOMAIN_SUFFIXES = (
    "anthropic.com",
    "api.ip.sb",
    "api.ipify.org",
    "checkip.amazonaws.com",
    "cip.cc",
    "claude.ai",
    "claude.com",
    "claudeusercontent.com",
    "ident.me",
    "icanhazip.com",
    "ifconfig.co",
    "ifconfig.me",
    "ip-api.com",
    "ipapi.co",
    "ipinfo.io",
    "ip.sb",
    "ipify.org",
    "ippure.com",
    "ipw.cn",
    "ipv4.icanhazip.com",
    "ipv6.icanhazip.com",
    "myip.ipip.net",
    "myexternalip.com",
    "seeip.org",
)
KNOWN_AI_DOMAIN_SUFFIXES = (
    "ai.google.dev",
    "aistudio.google.com",
    "anthropic.com",
    "chatgpt.com",
    "claude.ai",
    "codeium.com",
    "cohere.com",
    "copilot.microsoft.com",
    "cursor.com",
    "deepseek.com",
    "fal.ai",
    "fireworks.ai",
    "gemini.google.com",
    "grok.com",
    "groq.com",
    "huggingface.co",
    "ideogram.ai",
    "kimi.moonshot.cn",
    "leonardo.ai",
    "lovable.dev",
    "midjourney.com",
    "mistral.ai",
    "moonshot.cn",
    "notebooklm.google.com",
    "openai.com",
    "openrouter.ai",
    "perplexity.ai",
    "poe.com",
    "replicate.com",
    "runwayml.com",
    "stability.ai",
    "together.ai",
    "v0.dev",
    "windsurf.com",
    "x.ai",
)


def env_int(name, default):
    raw = str(os.environ.get(name, default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def env_bool(name, default):
    raw = str(os.environ.get(name, default)).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def utc_now():
    return datetime.now(timezone.utc)


def format_timestamp(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def split_target_host(target):
    if target.startswith("[") and "]:" in target:
        host, _, _ = target[1:].partition("]:")
        return host.strip().lower()
    if ":" not in target:
        return target.strip().lower()
    host, _, _ = target.rpartition(":")
    return host.strip().lower()


def parse_log_line(line):
    ts_match = TIMESTAMP_RE.match(line)
    target_match = TARGET_RE.search(line)
    if ts_match is None or target_match is None:
        return None
    try:
        seen_at = datetime.strptime(
            f"{ts_match.group('date')} {ts_match.group('time')}",
            "%Y/%m/%d %H:%M:%S.%f" if "." in ts_match.group("time") else "%Y/%m/%d %H:%M:%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    host = split_target_host(target_match.group("target"))
    if not DOMAIN_RE.fullmatch(host):
        return None

    return {
        "seen_at": seen_at,
        "protocol": target_match.group("proto"),
        "domain": host,
    }


def load_json(path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def normalize_log_state(state):
    events = []
    for item in state.get("events", []):
        try:
            events.append(
                {
                    "seen_at": datetime.fromisoformat(item["seen_at"]).astimezone(timezone.utc),
                    "protocol": str(item["protocol"]).strip().lower(),
                    "domain": str(item["domain"]).strip().lower(),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    state["events"] = events
    state["log_inode"] = str(state.get("log_inode", ""))
    try:
        state["log_offset"] = int(state.get("log_offset", 0))
    except (TypeError, ValueError):
        state["log_offset"] = 0
    return state


def load_log_state(path):
    return normalize_log_state(load_json(path, {"log_inode": "", "log_offset": 0, "events": []}))


def save_log_state(path, state):
    serializable = {
        "log_inode": state["log_inode"],
        "log_offset": state["log_offset"],
        "events": [
            {
                "seen_at": format_timestamp(item["seen_at"]),
                "protocol": item["protocol"],
                "domain": item["domain"],
            }
            for item in state["events"]
        ],
    }
    save_json(path, serializable)


def sync_log(log_path, state):
    if not log_path.exists():
        return

    stat = log_path.stat()
    current_inode = str(stat.st_ino)
    current_offset = int(state["log_offset"])
    if state["log_inode"] != current_inode or stat.st_size < current_offset:
        current_offset = 0

    with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
        handle.seek(current_offset)
        for line in handle:
            parsed = parse_log_line(line)
            if parsed is None:
                continue
            state["events"].append(parsed)
        state["log_offset"] = handle.tell()
    state["log_inode"] = current_inode


def purge_old_events(state, lookback_seconds, now):
    cutoff = now - timedelta(seconds=lookback_seconds)
    state["events"] = [item for item in state["events"] if item["seen_at"] >= cutoff]
    return cutoff


def load_decisions(path):
    payload = load_json(path, {"domains": {}})
    domains = payload.get("domains", {})
    if not isinstance(domains, dict):
        domains = {}
    return {"domains": domains}


def normalize_classification(value):
    raw = str(value or "").strip().lower()
    if raw in {"ai", "yes", "true", "related", "ai_related"}:
        return "ai"
    if raw in {"not_ai", "no", "false", "unrelated", "non_ai"}:
        return "not_ai"
    return "unknown"


def matches_domain_suffixes(domain, suffixes):
    return any(domain == suffix or domain.endswith(f".{suffix}") for suffix in suffixes)


def matches_forced_ai_route_domain(domain):
    return matches_domain_suffixes(domain, FORCED_AI_ROUTE_DOMAIN_SUFFIXES)


def matches_known_ai_domain(domain):
    return matches_domain_suffixes(domain, KNOWN_AI_DOMAIN_SUFFIXES)


def sync_builtin_domain_decisions(decisions, decisions_path, observed_domains):
    changed = False
    candidate_domains = set(decisions["domains"]) | set(observed_domains) | set(FORCED_AI_ROUTE_DOMAIN_SUFFIXES)
    classified_at = format_timestamp(utc_now())
    for domain in sorted(candidate_domains):
        if matches_forced_ai_route_domain(domain):
            payload = {
                "classification": "ai",
                "reason": "matched_forced_ai_route_domain",
                "classified_at": classified_at,
                "source": "builtin",
                "model": "builtin-forced-ai-route-domains",
            }
        elif matches_known_ai_domain(domain):
            payload = {
                "classification": "ai",
                "reason": "matched_known_ai_domain",
                "classified_at": classified_at,
                "source": "builtin",
                "model": "builtin-known-ai-domains",
            }
        else:
            continue

        existing = decisions["domains"].get(domain)
        if existing:
            same = (
                existing.get("classification") == payload["classification"]
                and existing.get("reason") == payload["reason"]
                and existing.get("source") == payload["source"]
                and existing.get("model") == payload["model"]
            )
            if same:
                continue

        decisions["domains"][domain] = payload
        changed = True

    if changed:
        save_json(decisions_path, decisions)
    return changed


def extract_output_text(payload):
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    texts = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    return "\n".join(texts).strip()


def validate_classification_results(domains, parsed):
    if not isinstance(parsed, list):
        raise RuntimeError("classifier output must be a JSON list")

    results = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain", "")).strip().lower()
        if domain not in domains:
            continue
        results[domain] = {
            "classification": normalize_classification(item.get("classification", "")),
            "reason": str(item.get("reason", "")).strip(),
        }

    missing = [domain for domain in domains if domain not in results]
    if missing:
        raise RuntimeError(f"classifier output missing domains: {', '.join(missing)}")
    return results


def sync_codex_home(source_home, runtime_home):
    runtime_home.mkdir(parents=True, exist_ok=True)
    synced_any = False
    for name in ("config.toml", "auth.json"):
        source = source_home / name
        if not source.is_file():
            continue
        target = runtime_home / name
        if not target.exists() or source.read_bytes() != target.read_bytes():
            shutil.copy2(source, target)
        synced_any = True
    if not synced_any:
        raise RuntimeError(f"codex source home is missing config/auth files: {source_home}")


def resolve_codex_cli_js(codex_cli_js, codex_node_versions_root):
    explicit = Path(codex_cli_js) if codex_cli_js else None
    if explicit and explicit.is_file():
        return explicit

    root = Path(codex_node_versions_root) if codex_node_versions_root else None
    if root and root.is_dir():
        matches = sorted(root.glob("*/lib/node_modules/@openai/codex/bin/codex.js"))
        if matches:
            return matches[-1]
    return None


def resolve_node_bin(node_bin, codex_node_versions_root):
    explicit = str(node_bin or "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    from_path = shutil.which(explicit or "node")
    if from_path:
        return from_path

    root = Path(codex_node_versions_root) if codex_node_versions_root else None
    if root and root.is_dir():
        matches = sorted(root.glob("*/bin/node"))
        if matches:
            return str(matches[-1])
    raise RuntimeError("node binary not found; install nodejs or set CODEX_NODE_BIN")


def build_codex_command(args):
    if args.codex_bin:
        resolved = shutil.which(args.codex_bin) or args.codex_bin
        return [resolved]

    cli_js = resolve_codex_cli_js(args.codex_cli_js, args.codex_node_versions_root)
    if cli_js is None:
        raise RuntimeError("codex cli js not found; mount host node modules or set CODEX_CLI_JS/CODEX_BIN")

    node_bin = resolve_node_bin(args.codex_node_bin, args.codex_node_versions_root)
    return [node_bin, str(cli_js)]


def classify_domains_via_codex(domains, args):
    sync_codex_home(args.codex_source_home, args.codex_runtime_home)
    args.codex_workdir.mkdir(parents=True, exist_ok=True)

    output_path = args.codex_workdir / "codex-last-message.json"
    output_path.unlink(missing_ok=True)

    prompt = json.dumps(
        {
            "task": "classify_domains",
            "rules": {
                "classify_as_ai_when": [
                    "the domain is primarily an AI product",
                    "the domain is an AI model provider",
                    "the domain is an AI coding tool",
                    "the domain is an AI chat product",
                    "the domain is an AI inference platform",
                    "the domain is an AI-focused developer platform",
                ],
                "otherwise": "not_ai",
            },
            "domains": domains,
            "return_format": [
                {
                    "domain": "example.com",
                    "classification": "ai|not_ai",
                    "reason": "short reason",
                }
            ],
            "output_constraints": [
                "Return JSON only",
                "Return exactly one item per input domain",
                "Do not include markdown",
            ],
        },
        ensure_ascii=True,
    )

    command = build_codex_command(args) + [
        "exec",
        "-C",
        str(args.codex_workdir),
        "--skip-git-repo-check",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--output-last-message",
        str(output_path),
    ]
    if args.codex_model:
        command.extend(["--model", args.codex_model])
    command.append(prompt)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(args.codex_runtime_home)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=args.codex_timeout_seconds,
        env=env,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "codex exec failed"
        raise RuntimeError(detail)
    if not output_path.is_file():
        raise RuntimeError("codex did not produce output-last-message")

    raw = output_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError("codex output-last-message was empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"codex output was not valid JSON: {raw}") from exc
    return validate_classification_results(domains, parsed)


def classify_domains_via_openai(domains, api_key, model, base_url, timeout_seconds):
    system_prompt = (
        "You classify internet domains. Return JSON only. "
        "For each input domain, decide whether the website is primarily an AI product, AI model provider, "
        "AI coding tool, AI chat product, AI inference platform, or an AI-focused developer platform. "
        "Use classification 'ai' only when the domain is clearly AI-related. Use 'not_ai' otherwise."
    )
    user_prompt = json.dumps(
        {
            "task": "classify_domains",
            "domains": domains,
            "return_format": [
                {
                    "domain": "example.com",
                    "classification": "ai|not_ai",
                    "reason": "short reason",
                }
            ],
        },
        ensure_ascii=True,
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "store": False,
    }

    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        base_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"openai http {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"openai request failed: {exc}") from exc

    payload = json.loads(raw)
    text = extract_output_text(payload)
    if not text:
        raise RuntimeError("openai response did not contain output text")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"openai output was not valid JSON: {text}") from exc
    return validate_classification_results(domains, parsed)


def read_panel_target(panel_db_path, preferred_listen_port):
    if not panel_db_path.is_file():
        return None
    conn = sqlite3.connect(panel_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if preferred_listen_port:
            row = conn.execute(
                """
                SELECT listen_port, upstream_host, upstream_port, note, updated_at
                FROM ports
                WHERE enabled = 1 AND listen_port = ?
                LIMIT 1
                """,
                (preferred_listen_port,),
            ).fetchone()
            if row:
                return dict(row)
        row = conn.execute(
            """
            SELECT listen_port, upstream_host, upstream_port, note, updated_at
            FROM ports
            WHERE enabled = 1
            ORDER BY updated_at DESC, listen_port ASC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ensure_ai_domain_schema(conn):
    conn.executescript(
        """
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


def save_ai_domains_to_panel_db(panel_db_path, report, decisions):
    status = {
        "status": "skipped",
        "reason": "",
        "path": str(panel_db_path),
        "domains_upserted": 0,
        "observations_upserted": 0,
    }
    if not panel_db_path.is_file():
        status["reason"] = "panel_db_missing"
        return status

    observed_ai_items = [item for item in report["domains"] if item["classification"] == "ai"]
    observed_ai_by_domain = {item["domain"]: item for item in observed_ai_items}
    ai_domains = sorted(
        domain
        for domain, item in decisions["domains"].items()
        if item.get("classification") == "ai"
    )
    conn = sqlite3.connect(panel_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        ensure_ai_domain_schema(conn)

        for item in observed_ai_items:
            decision = decisions["domains"].get(item["domain"], {})
            protocols = json.dumps(item["protocols"], ensure_ascii=True)
            conn.execute(
                """
                INSERT INTO ai_domain_observations (
                    domain,
                    window_start,
                    window_end,
                    hits,
                    classification,
                    reason,
                    source,
                    model,
                    protocols,
                    first_seen,
                    last_seen,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain, window_start, window_end) DO UPDATE SET
                    hits = excluded.hits,
                    classification = excluded.classification,
                    reason = excluded.reason,
                    source = excluded.source,
                    model = excluded.model,
                    protocols = excluded.protocols,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    created_at = excluded.created_at
                """,
                (
                    item["domain"],
                    report["window_start"],
                    report["window_end"],
                    item["hits"],
                    item["classification"],
                    item["reason"],
                    str(decision.get("source", "")).strip(),
                    str(decision.get("model", "")).strip(),
                    protocols,
                    item.get("first_seen"),
                    item.get("last_seen"),
                    report["generated_at"],
                ),
            )
            status["observations_upserted"] += 1

        for domain in ai_domains:
            item = observed_ai_by_domain.get(
                domain,
                {
                    "domain": domain,
                    "classification": "ai",
                    "reason": decisions["domains"].get(domain, {}).get("reason", ""),
                    "protocols": [],
                    "first_seen": None,
                    "last_seen": None,
                },
            )
            decision = decisions["domains"].get(domain, {})
            aggregate = conn.execute(
                """
                SELECT
                    COALESCE(SUM(hits), 0) AS total_hits,
                    MIN(COALESCE(first_seen, last_seen)) AS first_seen,
                    MAX(last_seen) AS last_seen
                FROM ai_domain_observations
                WHERE domain = ?
                """,
                (domain,),
            ).fetchone()
            existing = conn.execute(
                "SELECT last_protocols FROM ai_domains WHERE domain = ?",
                (domain,),
            ).fetchone()
            protocols = json.dumps(item["protocols"], ensure_ascii=True)
            if not item["protocols"] and existing and existing["last_protocols"]:
                protocols = existing["last_protocols"]
            conn.execute(
                """
                INSERT INTO ai_domains (
                    domain,
                    classification,
                    reason,
                    source,
                    model,
                    first_seen,
                    last_seen,
                    total_hits,
                    last_protocols,
                    last_report_window_start,
                    last_report_window_end,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    classification = excluded.classification,
                    reason = excluded.reason,
                    source = excluded.source,
                    model = excluded.model,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    total_hits = excluded.total_hits,
                    last_protocols = excluded.last_protocols,
                    last_report_window_start = excluded.last_report_window_start,
                    last_report_window_end = excluded.last_report_window_end,
                    updated_at = excluded.updated_at
                """,
                (
                    domain,
                    item["classification"],
                    item["reason"],
                    str(decision.get("source", "")).strip(),
                    str(decision.get("model", "")).strip(),
                    aggregate["first_seen"] or item.get("first_seen"),
                    aggregate["last_seen"] or item.get("last_seen"),
                    int(aggregate["total_hits"]),
                    protocols,
                    report["window_start"],
                    report["window_end"],
                    report["generated_at"],
                ),
            )
            status["domains_upserted"] += 1

        stale_domains = [
            domain
            for domain, item in decisions["domains"].items()
            if item.get("classification") != "ai"
        ]
        if stale_domains:
            conn.executemany("DELETE FROM ai_domains WHERE domain = ?", ((domain,) for domain in stale_domains))

        conn.commit()
        status["status"] = "written"
        return status
    finally:
        conn.close()


def join_host_port(host, port):
    host_text = str(host).strip()
    if ":" in host_text and not host_text.startswith("["):
        host_text = f"[{host_text}]"
    return f"{host_text}:{int(port)}"


def build_default_proxy_payload(ai_target):
    return {
        "outbounds": [
            {
                "tag": "ai_proxy",
                "protocol": "freedom",
                "settings": {
                    "domainStrategy": "AsIs",
                    "redirect": join_host_port(ai_target["upstream_host"], ai_target["upstream_port"]),
                    "proxyProtocol": 0,
                    "finalRules": [{"action": "allow"}],
                },
            }
        ]
    }


def render_proxy_template(template_path, ai_target, panel_target):
    if not template_path or not template_path.is_file():
        return build_default_proxy_payload(ai_target), "builtin_freedom_redirect"
    raw = template_path.read_text(encoding="utf-8")
    replacements = {
        "AI_UPSTREAM_HOST": str(ai_target["upstream_host"]),
        "AI_UPSTREAM_PORT": str(ai_target["upstream_port"]),
        "PANEL_LISTEN_PORT": str(panel_target["listen_port"]) if panel_target else "",
        "PANEL_UPSTREAM_HOST": str(panel_target["upstream_host"]) if panel_target else str(ai_target["upstream_host"]),
        "PANEL_UPSTREAM_PORT": (
            str(panel_target["upstream_port"]) if panel_target else str(ai_target["upstream_port"])
        ),
    }

    def replace(match):
        return replacements.get(match.group(1), match.group(0))

    rendered = PLACEHOLDER_RE.sub(replace, raw)
    try:
        parsed = json.loads(rendered)
    except json.JSONDecodeError as exc:
        return None, f"invalid_proxy_template_json: {exc}"

    if isinstance(parsed, dict) and "outbounds" in parsed:
        outbounds = parsed.get("outbounds")
    elif isinstance(parsed, list):
        outbounds = parsed
    else:
        outbounds = [parsed]

    if not isinstance(outbounds, list) or not outbounds:
        return None, "proxy_template_has_no_outbounds"

    first = outbounds[0]
    if not isinstance(first, dict):
        return None, "proxy_template_first_outbound_invalid"
    if str(first.get("protocol", "")).strip() == UNSET_PROXY_PROTOCOL:
        return None, "proxy_template_protocol_placeholder_not_replaced"
    if str(first.get("tag", "")).strip() != "ai_proxy":
        first["tag"] = "ai_proxy"
    return {"outbounds": outbounds}, ""


def write_routing_fragment(path, ai_domains, proxy_payload):
    if not ai_domains or proxy_payload is None:
        path.unlink(missing_ok=True)
        return False
    fragment = {
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "domain": [f"domain:{domain}" for domain in sorted(ai_domains)],
                    "outboundTag": "ai_proxy",
                }
            ],
        },
        "outbounds": proxy_payload["outbounds"],
    }
    save_json(path, fragment)
    return True


def build_domain_report(state, cutoff, now, decisions, ai_target, panel_target, route_status):
    domains = {}
    protocols = {}
    for item in state["events"]:
        domain_item = domains.setdefault(
            item["domain"],
            {
                "domain": item["domain"],
                "hits": 0,
                "first_seen": item["seen_at"],
                "last_seen": item["seen_at"],
                "protocols": set(),
                "classification": decisions["domains"].get(item["domain"], {}).get("classification", "unknown"),
                "reason": decisions["domains"].get(item["domain"], {}).get("reason", ""),
            },
        )
        domain_item["hits"] += 1
        domain_item["protocols"].add(item["protocol"])
        if item["seen_at"] < domain_item["first_seen"]:
            domain_item["first_seen"] = item["seen_at"]
        if item["seen_at"] > domain_item["last_seen"]:
            domain_item["last_seen"] = item["seen_at"]
        protocols[item["protocol"]] = protocols.get(item["protocol"], 0) + 1

    domain_items = sorted(
        (
            {
                "domain": item["domain"],
                "hits": item["hits"],
                "first_seen": format_timestamp(item["first_seen"]),
                "last_seen": format_timestamp(item["last_seen"]),
                "protocols": sorted(item["protocols"]),
                "classification": item["classification"],
                "reason": item["reason"],
            }
            for item in domains.values()
        ),
        key=lambda item: (-item["hits"], item["domain"]),
    )
    ai_domains = [item["domain"] for item in domain_items if item["classification"] == "ai"]
    return {
        "generated_at": format_timestamp(now),
        "window_start": format_timestamp(cutoff),
        "window_end": format_timestamp(now),
        "unique_domains": len(domain_items),
        "ai_domains": ai_domains,
        "domains": domain_items,
        "protocols": [
            {"protocol": protocol, "hits": hits}
            for protocol, hits in sorted(protocols.items())
        ],
        "ai_target": ai_target,
        "panel_target": panel_target,
        "route_status": route_status,
    }


def write_domain_report(output_dir, report):
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = output_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    latest_json = output_dir / "latest.json"
    latest_txt = output_dir / "latest.txt"
    stamp = report["window_end"].replace(":", "").replace("-", "").replace("+00:00", "Z")
    history_json = history_dir / f"{stamp}.json"
    history_txt = history_dir / f"{stamp}.txt"

    payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    latest_json.write_text(payload, encoding="utf-8")
    history_json.write_text(payload, encoding="utf-8")

    lines = [
        f"generated_at: {report['generated_at']}",
        f"window_start: {report['window_start']}",
        f"window_end: {report['window_end']}",
        f"unique_domains: {report['unique_domains']}",
        f"ai_domains: {len(report['ai_domains'])}",
        f"route_status: {report['route_status'].get('status', 'unknown')}",
    ]
    if report.get("panel_db_status"):
        lines.append(
            "panel_db_status: "
            f"{report['panel_db_status'].get('status', 'unknown')} "
            f"(ai_domains={report['panel_db_status'].get('domains_upserted', 0)}, "
            f"observations={report['panel_db_status'].get('observations_upserted', 0)})"
        )
    if report.get("ai_target"):
        lines.append(
            "ai_target: "
            f"{report['ai_target']['upstream_host']}:{report['ai_target']['upstream_port']}"
        )
    if report["panel_target"]:
        lines.append(
            "panel_target: "
            f"{report['panel_target']['upstream_host']}:{report['panel_target']['upstream_port']} "
            f"(listen_port={report['panel_target']['listen_port']})"
        )
    lines.append("")
    if report["domains"]:
        for item in report["domains"]:
            protocols = ",".join(item["protocols"])
            lines.append(
                f"{item['domain']}\thits={item['hits']}\tclass={item['classification']}\t"
                f"last_seen={item['last_seen']}\tprotocols={protocols}"
            )
    else:
        lines.append("no domains observed in the last window")
    text = "\n".join(lines) + "\n"
    latest_txt.write_text(text, encoding="utf-8")
    history_txt.write_text(text, encoding="utf-8")


def rerender_config(render_script, env_file, config_out, client_out, share_out, dynamic_routing_file):
    command = [
        sys.executable,
        str(render_script),
        "--env-file",
        str(env_file),
        "--config-out",
        str(config_out),
        "--client-out",
        str(client_out),
        "--share-out",
        str(share_out),
        "--dynamic-routing-file",
        str(dynamic_routing_file),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "render_config failed"
        raise RuntimeError(detail)


def restart_xray_container(container_name, timeout_seconds):
    if not container_name:
        return
    completed = subprocess.run(
        ["docker", "restart", container_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "docker restart failed"
        raise RuntimeError(detail)


def classify_pending_domains(decisions, decisions_path, observed_domains, args):
    known = set(decisions["domains"])
    pending = sorted(domain for domain in observed_domains if domain not in known)
    if not pending:
        return []

    classified_at = format_timestamp(utc_now())
    remaining = []
    for domain in pending:
        if matches_known_ai_domain(domain):
            decisions["domains"][domain] = {
                "classification": "ai",
                "reason": "matched_known_ai_domain",
                "classified_at": classified_at,
                "source": "builtin",
                "model": "builtin-known-ai-domains",
            }
        else:
            remaining.append(domain)
    if len(remaining) != len(pending):
        save_json(decisions_path, decisions)

    if not remaining:
        return []

    if args.codex_classifier_enabled:
        unresolved = []
        for start in range(0, len(remaining), args.batch_size):
            batch = remaining[start:start + args.batch_size]
            try:
                results = classify_domains_via_codex(batch, args)
            except Exception as exc:
                print(f"[ai_domain_manager] codex classifier unavailable: {exc}", file=sys.stderr, flush=True)
                unresolved = remaining[start:]
                break
            classified_at = format_timestamp(utc_now())
            for domain in batch:
                result = results[domain]
                decisions["domains"][domain] = {
                    "classification": result["classification"],
                    "reason": result["reason"],
                    "classified_at": classified_at,
                    "source": "codex",
                    "model": args.codex_model or "config-default",
                }
            save_json(decisions_path, decisions)
        else:
            return []
        remaining = unresolved

    if not remaining or not args.openai_api_key:
        return remaining

    for start in range(0, len(remaining), args.batch_size):
        batch = remaining[start:start + args.batch_size]
        results = classify_domains_via_openai(
            batch,
            args.openai_api_key,
            args.openai_model,
            args.openai_base_url,
            args.openai_timeout_seconds,
        )
        classified_at = format_timestamp(utc_now())
        for domain in batch:
            result = results[domain]
            decisions["domains"][domain] = {
                "classification": result["classification"],
                "reason": result["reason"],
                "classified_at": classified_at,
                "source": "openai",
                "model": args.openai_model,
            }
        save_json(decisions_path, decisions)
    return []


def run_once(args):
    now = utc_now()
    log_state = load_log_state(args.log_state_path)
    sync_log(args.log_path, log_state)
    cutoff = purge_old_events(log_state, args.lookback_seconds, now)

    decisions = load_decisions(args.classification_state_path)
    observed_domains = {item["domain"] for item in log_state["events"]}
    sync_builtin_domain_decisions(decisions, args.classification_state_path, observed_domains)
    ai_target = {
        "upstream_host": args.ai_upstream_host,
        "upstream_port": args.ai_upstream_port,
    }
    panel_target = read_panel_target(args.panel_db_path, args.panel_route_listen_port)
    route_status = {"status": "disabled", "reason": ""}

    pending_without_classifier = classify_pending_domains(
        decisions,
        args.classification_state_path,
        observed_domains,
        args,
    )

    ai_domains = sorted(
        domain
        for domain, item in decisions["domains"].items()
        if item.get("classification") == "ai"
    )

    proxy_payload = None
    if ai_domains:
        proxy_payload, proxy_error = render_proxy_template(args.proxy_template_path, ai_target, panel_target)
        if proxy_payload is None:
            args.dynamic_routing_path.unlink(missing_ok=True)
            route_status = {"status": "pending_proxy_template", "reason": proxy_error}
        else:
            applied = write_routing_fragment(args.dynamic_routing_path, ai_domains, proxy_payload)
            route_status = {
                "status": "applied" if applied else "disabled",
                "reason": proxy_error if applied else "no_ai_domains",
            }
    else:
        args.dynamic_routing_path.unlink(missing_ok=True)
        route_status = {"status": "idle", "reason": "no_ai_domains"}

    previous_config = args.config_out.read_text(encoding="utf-8") if args.config_out.is_file() else ""

    rerender_config(
        args.render_script,
        args.env_file,
        args.config_out,
        args.client_out,
        args.share_out,
        args.dynamic_routing_path,
    )
    current_config = args.config_out.read_text(encoding="utf-8") if args.config_out.is_file() else ""
    config_changed = current_config != previous_config
    if config_changed and args.restart_container_name:
        restart_xray_container(args.restart_container_name, args.docker_timeout_seconds)

    report = build_domain_report(log_state, cutoff, now, decisions, ai_target, panel_target, route_status)
    if pending_without_classifier:
        report["route_status"]["pending_domains_without_classifier"] = pending_without_classifier
    report["route_status"]["config_changed"] = config_changed
    report["panel_db_status"] = save_ai_domains_to_panel_db(args.panel_db_path, report, decisions)
    write_domain_report(args.report_output_dir, report)
    save_log_state(args.log_state_path, log_state)
    save_json(args.classification_state_path, decisions)
    print(
        "[ai_domain_manager] "
        f"domains={report['unique_domains']} ai_domains={len(report['ai_domains'])} "
        f"route_status={report['route_status']['status']}",
        flush=True,
    )


def seconds_until_next_boundary(interval_seconds):
    now = time.time()
    next_boundary = ((int(now) // interval_seconds) + 1) * interval_seconds
    return max(1, next_boundary - now)


def build_args():
    parser = argparse.ArgumentParser(description="Classify Xray destination domains and maintain dynamic AI routing.")
    parser.add_argument("--workspace-dir", default=os.environ.get("XRAY_WORKSPACE_DIR", "/workspace"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=env_int("AI_DOMAIN_INTERVAL_SECONDS", 3600))
    parser.add_argument("--lookback-seconds", type=int, default=env_int("AI_DOMAIN_LOOKBACK_SECONDS", 3600))
    parser.add_argument("--batch-size", type=int, default=env_int("AI_DOMAIN_BATCH_SIZE", 50))
    parser.add_argument("--openai-timeout-seconds", type=int, default=env_int("OPENAI_TIMEOUT_SECONDS", 45))
    parser.add_argument("--codex-timeout-seconds", type=int, default=env_int("CODEX_TIMEOUT_SECONDS", 180))
    parser.add_argument("--docker-timeout-seconds", type=int, default=env_int("DOCKER_TIMEOUT_SECONDS", 30))
    parser.add_argument("--panel-route-listen-port", type=int, default=env_int("PANEL_ROUTE_LISTEN_PORT", 0))
    args = parser.parse_args()

    workspace = Path(args.workspace_dir)
    args.log_path = Path(os.environ.get("XRAY_ACCESS_LOG_PATH", str(workspace / "logs" / "access.log")))
    args.report_output_dir = Path(os.environ.get("AI_DOMAIN_REPORT_OUTPUT_DIR", str(workspace / "reports" / "hourly-domains")))
    args.log_state_path = Path(os.environ.get("AI_DOMAIN_LOG_STATE_PATH", str(args.report_output_dir / ".state.json")))
    args.classification_state_path = Path(
        os.environ.get("AI_DOMAIN_CLASSIFICATION_STATE_PATH", str(workspace / "runtime" / "ai-domain-decisions.json"))
    )
    args.dynamic_routing_path = Path(
        os.environ.get("AI_DOMAIN_DYNAMIC_ROUTING_PATH", str(workspace / "runtime" / "dynamic-routing.json"))
    )
    args.env_file = Path(os.environ.get("XRAY_ENV_FILE", str(workspace / ".env")))
    args.render_script = Path(os.environ.get("XRAY_RENDER_SCRIPT", str(workspace / "scripts" / "render_config.py")))
    args.config_out = Path(os.environ.get("XRAY_CONFIG_OUT", str(workspace / "runtime" / "config.json")))
    args.client_out = Path(os.environ.get("XRAY_CLIENT_OUT", str(workspace / "runtime" / "client-test.json")))
    args.share_out = Path(os.environ.get("XRAY_SHARE_OUT", str(workspace / "runtime" / "client-share.txt")))
    args.panel_db_path = Path(os.environ.get("PANEL_DB_PATH", "/panel-data/panel.db"))
    args.proxy_template_path = Path(
        os.environ.get("AI_PROXY_OUTBOUND_TEMPLATE_PATH", str(workspace / "ai-proxy-outbound.json"))
    )
    args.restart_container_name = os.environ.get("XRAY_RESTART_CONTAINER", "").strip()
    args.codex_classifier_enabled = env_bool("CODEX_CLASSIFIER_ENABLED", "1")
    args.codex_source_home = Path(os.environ.get("CODEX_SOURCE_HOME", "/host-codex-home"))
    args.codex_runtime_home = Path(
        os.environ.get("CODEX_RUNTIME_HOME", str(workspace / "runtime" / "codex-home"))
    )
    args.codex_workdir = Path(os.environ.get("CODEX_WORKDIR", "/tmp/codex-domain-classifier"))
    args.codex_node_bin = os.environ.get("CODEX_NODE_BIN", "/usr/bin/node").strip()
    args.codex_cli_js = os.environ.get("CODEX_CLI_JS", "").strip()
    args.codex_node_versions_root = os.environ.get("CODEX_NODE_VERSIONS_ROOT", "/host-node-versions").strip()
    args.codex_bin = os.environ.get("CODEX_BIN", "").strip()
    args.codex_model = os.environ.get("CODEX_MODEL", "").strip()
    args.openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    args.openai_model = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5"
    args.openai_base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/responses").strip()
    args.ai_upstream_host = os.environ.get("AI_UPSTREAM_HOST", "nat.qq.pw").strip()
    args.ai_upstream_port = env_int("AI_UPSTREAM_PORT", 31098)
    return args


def main():
    args = build_args()

    if args.interval_seconds <= 0:
        print("AI_DOMAIN_INTERVAL_SECONDS must be > 0", file=sys.stderr)
        return 1
    if args.lookback_seconds <= 0:
        print("AI_DOMAIN_LOOKBACK_SECONDS must be > 0", file=sys.stderr)
        return 1
    if args.batch_size <= 0:
        print("AI_DOMAIN_BATCH_SIZE must be > 0", file=sys.stderr)
        return 1
    if not args.ai_upstream_host:
        print("AI_UPSTREAM_HOST must not be empty", file=sys.stderr)
        return 1
    if args.ai_upstream_port <= 0 or args.ai_upstream_port > 65535:
        print("AI_UPSTREAM_PORT must be in 1..65535", file=sys.stderr)
        return 1

    while True:
        try:
            run_once(args)
        except Exception as exc:
            print(f"[ai_domain_manager] error: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 1
        else:
            if args.once:
                return 0
        time.sleep(seconds_until_next_boundary(args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
