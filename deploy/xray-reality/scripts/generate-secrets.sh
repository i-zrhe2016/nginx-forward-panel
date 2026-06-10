#!/usr/bin/env bash
set -euo pipefail

image="${XRAY_IMAGE:-ghcr.io/xtls/xray-core:26.5.3}"

if command -v xray >/dev/null 2>&1; then
  key_output="$(xray x25519)"
else
  key_output="$(docker run --rm "$image" x25519)"
fi

private_key="$(printf '%s\n' "$key_output" | awk -F': ' '/^PrivateKey:/ {print $2}')"
public_key="$(printf '%s\n' "$key_output" | awk -F': ' '/^Password \(PublicKey\):/ {print $2}')"

if [[ -z "$private_key" || -z "$public_key" ]]; then
  printf 'failed to parse x25519 output\n' >&2
  exit 1
fi

if command -v uuidgen >/dev/null 2>&1; then
  uuid_value="$(uuidgen | tr '[:upper:]' '[:lower:]')"
else
  uuid_value="$(python3 -c 'import uuid; print(uuid.uuid4())')"
fi

short_id="$(openssl rand -hex 8)"

cat <<EOF
XRAY_CLIENT_UUID=$uuid_value
XRAY_REALITY_PRIVATE_KEY=$private_key
XRAY_REALITY_PUBLIC_KEY=$public_key
XRAY_REALITY_SHORT_ID=$short_id
EOF
