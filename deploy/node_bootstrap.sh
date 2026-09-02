#!/usr/bin/env bash
# Idempotent Outline data-plane bootstrap executed by the central fleet
# reconciler over SSH.  It intentionally receives no Telegram, database,
# provider, or receipt-processing credentials.

set -euo pipefail

readonly DEFAULT_INSTALLER_URL="https://raw.githubusercontent.com/OutlineFoundation/outline-server/de566bfc9ff4429ae839e304b4d7e03b703ce415/src/server_manager/install_scripts/install_server.sh"
readonly DEFAULT_INSTALLER_SHA256="6cadc866cf901beb89473e61ab9ca7c63fac56b7d614be6c592b25a1465fa0d2"

fail() {
  printf 'aurix-node-bootstrap: %s\n' "$1" >&2
  exit 1
}

require_value() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "$value" ]] || fail "missing $name"
}

require_value AURIX_NODE_ID
require_value AURIX_NODE_HOST
require_value AURIX_NODE_API_PORT
require_value AURIX_NODE_KEYS_PORT
require_value AURIX_NODE_SSH_PORT
require_value AURIX_CONTROL_PLANE_SOURCE

[[ "$EUID" -eq 0 ]] || fail "root SSH access is required"
[[ "$AURIX_NODE_ID" =~ ^[A-Za-z0-9_-]{1,24}$ ]] || fail "invalid node id"
[[ "$AURIX_NODE_HOST" =~ ^[A-Za-z0-9.:_-]{1,255}$ ]] || fail "invalid node host"
[[ "$AURIX_NODE_API_PORT" =~ ^[0-9]{1,5}$ ]] || fail "invalid management port"
[[ "$AURIX_NODE_KEYS_PORT" =~ ^[0-9]{1,5}$ ]] || fail "invalid access-key port"
[[ "$AURIX_NODE_SSH_PORT" =~ ^[0-9]{1,5}$ ]] || fail "invalid SSH port"
(( AURIX_NODE_API_PORT > 0 && AURIX_NODE_API_PORT <= 65535 )) || fail "invalid management port"
(( AURIX_NODE_KEYS_PORT > 0 && AURIX_NODE_KEYS_PORT <= 65535 )) || fail "invalid access-key port"
(( AURIX_NODE_SSH_PORT > 0 && AURIX_NODE_SSH_PORT <= 65535 )) || fail "invalid SSH port"
(( AURIX_NODE_API_PORT != AURIX_NODE_KEYS_PORT )) || fail "management and access-key ports must differ"

OUTLINE_DIR=/opt/outline
if [[ -s /root/shadowbox/access.txt || -s /root/shadowbox/persisted-state/start_container.sh ]]; then
  OUTLINE_DIR=/root/shadowbox
fi
export OUTLINE_DIR

export DEBIAN_FRONTEND=noninteractive
missing_packages=()
command -v curl >/dev/null 2>&1 || missing_packages+=(curl)
command -v openssl >/dev/null 2>&1 || missing_packages+=(openssl)
command -v python3 >/dev/null 2>&1 || missing_packages+=(python3)
command -v ufw >/dev/null 2>&1 || missing_packages+=(ufw)
[[ -s /etc/ssl/certs/ca-certificates.crt ]] || missing_packages+=(ca-certificates)
if (( ${#missing_packages[@]} )); then
  apt-get update -qq
  apt-get install -y -qq "${missing_packages[@]}" >/dev/null
fi
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq docker.io >/dev/null
fi
systemctl enable --now docker >/dev/null

configure_firewall() {
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  # The upstream installer may create a broad API rule. Remove that exact rule
  # before restoring the source-restricted management rule.
  ufw --force delete allow "$AURIX_NODE_API_PORT"/tcp >/dev/null 2>&1 || true
  ufw limit "$AURIX_NODE_SSH_PORT"/tcp >/dev/null
  ufw allow from "$AURIX_CONTROL_PLANE_SOURCE" to any port "$AURIX_NODE_API_PORT" proto tcp >/dev/null
  ufw allow "$AURIX_NODE_KEYS_PORT"/tcp >/dev/null
  ufw allow "$AURIX_NODE_KEYS_PORT"/udp >/dev/null
  ufw --force enable >/dev/null
}
configure_firewall

if [[ "${AURIX_HARDEN_SSH:-1}" == "1" ]]; then
  install -d -m 0755 /etc/ssh/sshd_config.d
  install -m 0600 /dev/null /etc/ssh/sshd_config.d/99-aurix-fleet.conf
  printf '%s\n' \
    'PermitRootLogin prohibit-password' \
    'PasswordAuthentication no' \
    'KbdInteractiveAuthentication no' \
    'PubkeyAuthentication yes' \
    'MaxAuthTries 3' \
    > /etc/ssh/sshd_config.d/99-aurix-fleet.conf
  sshd -t
  systemctl reload ssh
fi

fresh_install=0
start_script="$OUTLINE_DIR/persisted-state/start_container.sh"
if [[ ! -s "$start_script" ]]; then
  [[ ! -e "$OUTLINE_DIR/persisted-state" && ! -s "$OUTLINE_DIR/access.txt" ]] || \
    fail "Outline state exists without a usable start script; restore instead of reinstalling"
  installer_url="${AURIX_OUTLINE_INSTALLER_URL:-$DEFAULT_INSTALLER_URL}"
  installer_sha256="${AURIX_OUTLINE_INSTALLER_SHA256:-$DEFAULT_INSTALLER_SHA256}"
  [[ "$installer_url" =~ ^https://raw\.githubusercontent\.com/OutlineFoundation/outline-server/[0-9a-f]{40}/ ]] || \
    fail "installer URL must pin an official OutlineFoundation commit"
  [[ "$installer_sha256" =~ ^[0-9a-f]{64}$ ]] || fail "invalid installer SHA-256"
  installer_file="$(mktemp /tmp/aurix-outline-installer.XXXXXX)"
  installer_log="$(mktemp /tmp/aurix-outline-install-log.XXXXXX)"
  trap 'rm -f "${installer_file:-}" "${installer_log:-}"' EXIT
  curl --fail --silent --show-error --location "$installer_url" --output "$installer_file"
  printf '%s  %s\n' "$installer_sha256" "$installer_file" | sha256sum --check --status || \
    fail "Outline installer checksum mismatch"
  chmod 0700 "$installer_file"
  if ! SB_DEFAULT_SERVER_NAME="AuriX ${AURIX_NODE_ID}" \
      "$installer_file" \
      --hostname "$AURIX_NODE_HOST" \
      --api-port "$AURIX_NODE_API_PORT" \
      --keys-port "$AURIX_NODE_KEYS_PORT" \
      >"$installer_log" 2>&1; then
    fail "official Outline installation failed"
  fi
  fresh_install=1
  rm -f "$installer_file" "$installer_log"
  trap - EXIT
elif ! docker inspect shadowbox >/dev/null 2>&1; then
  bash "$start_script" >/dev/null || fail "persisted Outline container failed to start"
fi
configure_firewall

# An interrupted official installer can leave the certificate and running
# container before appending apiUrl. Reconstruct only from locally persisted
# state; never generate a second management identity over existing key state.
if ! grep -q '^apiUrl:' "$OUTLINE_DIR/access.txt"; then
  certificate="$OUTLINE_DIR/persisted-state/shadowbox-selfsigned.crt"
  [[ -s "$start_script" && -s "$certificate" ]] || fail "incomplete Outline management state"
  api_prefix="$(grep -o 'SB_API_PREFIX=[^\"]*' "$start_script" | head -1 | cut -d= -f2)"
  fingerprint="$(openssl x509 -in "$certificate" -noout -sha256 -fingerprint | cut -d= -f2 | tr -d :)"
  [[ "$api_prefix" =~ ^[A-Za-z0-9_-]{16,64}$ ]] || fail "invalid persisted API prefix"
  [[ "$fingerprint" =~ ^[0-9A-Fa-f]{64}$ ]] || fail "invalid persisted certificate"
  printf 'apiUrl:https://%s:%s/%s\ncertSha256:%s\n' \
    "$AURIX_NODE_HOST" "$AURIX_NODE_API_PORT" "$api_prefix" "$fingerprint" \
    > "$OUTLINE_DIR/access.txt"
fi
chmod 0640 "$OUTLINE_DIR/access.txt"

if [[ "$fresh_install" == "1" ]]; then
  # The official installer creates one convenience key. It is untracked by
  # AuriX, so a new node must be empty before control-plane activation.
  python3 - <<'PY'
import json
import ssl
import urllib.parse
import urllib.request

fields = {}
import os
outline_dir = os.environ["OUTLINE_DIR"]
with open(outline_dir + "/access.txt", encoding="utf-8") as stream:
    for line in stream:
        key, _, value = line.strip().partition(":")
        fields[key] = value
api = fields["apiUrl"].rstrip("/")
context = ssl._create_unverified_context()
with urllib.request.urlopen(api + "/access-keys", context=context, timeout=15) as response:
    keys = json.load(response).get("accessKeys", [])
for item in keys:
    key_id = urllib.parse.quote(str(item["id"]), safe="")
    request = urllib.request.Request(api + "/access-keys/" + key_id, method="DELETE")
    urllib.request.urlopen(request, context=context, timeout=15).read()
PY
fi

if [[ ! -e /swapfile && "${AURIX_NODE_SWAP_MB:-1024}" =~ ^[0-9]+$ ]] && \
   (( AURIX_NODE_SWAP_MB > 0 )); then
  fallocate -l "${AURIX_NODE_SWAP_MB}M" /swapfile
  chmod 0600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
fi

install -d -m 0750 /etc/aurix-node
printf '%s\n' "${AURIX_FLEET_REVISION:-unknown}" > /etc/aurix-node/revision
chmod 0640 /etc/aurix-node/revision

python3 - <<'PY'
import json
import ssl
import urllib.request

fields = {}
import os
outline_dir = os.environ["OUTLINE_DIR"]
with open(outline_dir + "/access.txt", encoding="utf-8") as stream:
    for line in stream:
        key, _, value = line.strip().partition(":")
        fields[key] = value
api = fields["apiUrl"].rstrip("/")
context = ssl._create_unverified_context()
with urllib.request.urlopen(api + "/server", context=context, timeout=15) as response:
    server = json.load(response)
with urllib.request.urlopen(api + "/access-keys", context=context, timeout=15) as response:
    keys = json.load(response).get("accessKeys", [])
print(json.dumps({
    "status": "ready",
    "outline_version": str(server.get("version") or "unknown"),
    "remote_key_count": len(keys),
}, sort_keys=True))
PY
