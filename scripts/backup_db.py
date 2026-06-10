#!/usr/bin/env python3
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def non_negative_int(value):
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def parse_args():
    keep_days = os.environ.get("DB_BACKUP_KEEP_DAYS", "7").strip() or "7"
    parser = argparse.ArgumentParser(description="Back up the panel SQLite database.")
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/panel.db"))
    parser.add_argument("--backup-dir", default=os.environ.get("DB_BACKUP_DIR", "/backups"))
    parser.add_argument("--keep-days", type=non_negative_int, default=non_negative_int(keep_days))
    parser.add_argument("--prefix", default=os.environ.get("DB_BACKUP_PREFIX", "xray-routing-panel"))
    return parser.parse_args()


def backup_name(prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}.db"


def prune_backups(backup_dir, prefix, keep_days):
    if keep_days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    removed = 0
    for path in backup_dir.glob(f"{prefix}-*.db"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def main():
    args = parse_args()
    source = Path(args.db_path)
    backup_dir = Path(args.backup_dir)

    if not source.exists():
        print(f"[backup] source database not found: {source}", file=sys.stderr)
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    final_path = backup_dir / backup_name(args.prefix)
    temp_path = backup_dir / f".{final_path.name}.tmp"

    try:
        if temp_path.exists():
            temp_path.unlink()

        with sqlite3.connect(str(source), timeout=30) as src_conn:
            with sqlite3.connect(str(temp_path)) as dst_conn:
                src_conn.backup(dst_conn)

        os.replace(temp_path, final_path)
        removed = prune_backups(backup_dir, args.prefix, args.keep_days)
        print(f"[backup] wrote {final_path}")
        if removed:
            print(f"[backup] pruned {removed} old backup(s)")
        return 0
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        print(f"[backup] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
