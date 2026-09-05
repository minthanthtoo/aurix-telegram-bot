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
  # Outline does not require a remotely exposed Docker daemon. Remove legacy
  # installer/firewall drift even when nothing currently listens on these ports.
  ufw --force delete allow 2375/tcp >/dev/null 2>&1 || true
  ufw --force delete allow 2376/tcp >/dev/null 2>&1 || true
  ufw limit "$AURIX_NODE_SSH_PORT"/tcp >/dev/null
  ufw allow from "$AURIX_CONTROL_PLANE_SOURCE" to any port "$AURIX_NODE_API_PORT" proto tcp >/dev/null
  ufw allow "$AURIX_NODE_KEYS_PORT"/tcp >/dev/null
  ufw allow "$AURIX_NODE_KEYS_PORT"/udp >/dev/null
  ufw --force enable >/dev/null
}
configure_firewall

disable_probe_agent() {
  # Keep previously installed files for forensic/recovery purposes, but stop
  # the node from making any control-plane calls when the feature is disabled.
  systemctl disable --now aurix-fleet-probe-agent.timer >/dev/null 2>&1 || true
  systemctl disable --now aurix-fleet-probe-agent.service >/dev/null 2>&1 || true
}

install_probe_agent() {
  local enabled="${AURIX_PROBE_AGENT_INSTALL_ENABLED:-0}"
  case "$enabled" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *)
      disable_probe_agent
      return 0
      ;;
  esac
  require_value AURIX_PROBE_API_URL
  require_value AURIX_PROBE_AGENT_ID
  require_value AURIX_PROBE_AGENT_BUNDLE_B64
  require_value AURIX_PROBE_AGENT_BUNDLE_SHA256
  [[ "$AURIX_PROBE_API_URL" =~ ^https://[^[:space:]/]+(/[^[:space:]]*)?$ ]] || \
    fail "AURIX_PROBE_API_URL must be an HTTPS URL"
  [[ "$AURIX_PROBE_AGENT_ID" == "$AURIX_NODE_ID" ]] || \
    fail "probe agent id must match the node id"
  [[ "$AURIX_PROBE_AGENT_BUNDLE_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || \
    fail "invalid probe-agent bundle SHA-256"
  [[ "$AURIX_PROBE_AGENT_BUNDLE_B64" != *$'\n'* && "$AURIX_PROBE_AGENT_BUNDLE_B64" != *$'\r'* ]] || \
    fail "probe-agent bundle contains an invalid newline"
  [[ "$AURIX_PROBE_AGENT_SECRET" =~ ^[^[:space:]]{16,256}$ ]] || \
    fail "invalid probe-agent secret"

  command -v base64 >/dev/null 2>&1 || fail "base64 is required for probe-agent installation"
  command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required for probe-agent installation"
  command -v tar >/dev/null 2>&1 || fail "tar is required for probe-agent installation"
  command -v useradd >/dev/null 2>&1 || fail "useradd is required for probe-agent installation"
  id aurix >/dev/null 2>&1 || useradd --system --user-group --home-dir /nonexistent \
    --no-create-home --shell /usr/sbin/nologin aurix
  getent group aurix >/dev/null 2>&1 || fail "aurix system group is unavailable"
  install -d -o root -g root -m 0755 /opt/aurix-agent
  install -d -o root -g aurix -m 0750 /etc/aurix-bot

  local bundle_file bundle_dir release_dir expected_path
  bundle_file="$(mktemp /tmp/aurix-probe-agent-bundle.XXXXXX)"
  bundle_dir="$(mktemp -d /tmp/aurix-probe-agent.XXXXXX)"
  trap 'rm -f "${bundle_file:-}"; rm -rf "${bundle_dir:-}"' RETURN
  printf '%s' "$AURIX_PROBE_AGENT_BUNDLE_B64" | base64 --decode >"$bundle_file" || \
    fail "probe-agent bundle is not valid base64"
  printf '%s  %s\n' "$AURIX_PROBE_AGENT_BUNDLE_SHA256" "$bundle_file" | \
    sha256sum --check --status || fail "probe-agent bundle checksum mismatch"
  tar -tzf "$bundle_file" >/dev/null || fail "probe-agent bundle is not a gzip tar archive"
  while IFS= read -r expected_path; do
    case "$expected_path" in
      fleet_probe.py|fleet_probe_api.py|fleet_probe_agent.py) ;;
      *) fail "probe-agent bundle contains an unexpected path" ;;
    esac
  done < <(tar -tzf "$bundle_file")
  tar -xzf "$bundle_file" -C "$bundle_dir" --no-same-owner --no-same-permissions
  for expected_path in fleet_probe.py fleet_probe_api.py fleet_probe_agent.py; do
    [[ -s "$bundle_dir/$expected_path" ]] || fail "probe-agent bundle is incomplete"
  done

  release_dir="/opt/aurix-agent/$AURIX_FLEET_REVISION"
  [[ "$AURIX_FLEET_REVISION" =~ ^[0-9a-fA-F]{40}$ ]] || fail "probe-agent revision is invalid"
  install -d -o root -g root -m 0755 "$release_dir"
  for expected_path in fleet_probe.py fleet_probe_api.py fleet_probe_agent.py; do
    install -o root -g root -m 0644 "$bundle_dir/$expected_path" "$release_dir/$expected_path"
  done
  ln -sfn "$release_dir" /opt/aurix-agent/current

  local env_file
  env_file="$(mktemp /etc/aurix-bot/.aurix-agent.env.XXXXXX)"
  umask 077
  printf 'AURIX_PROBE_API_URL=%q\nAURIX_PROBE_AGENT_ID=%q\nAURIX_PROBE_AGENT_SECRET=%q\n' \
    "$AURIX_PROBE_API_URL" "$AURIX_PROBE_AGENT_ID" "$AURIX_PROBE_AGENT_SECRET" >"$env_file"
  chown root:aurix "$env_file"
  chmod 0640 "$env_file"
  mv -f "$env_file" /etc/aurix-bot/aurix-agent.env
  trap - RETURN
  rm -f "$bundle_file"
  rm -rf "$bundle_dir"

  cat > /etc/systemd/system/aurix-fleet-probe-agent.service <<'UNIT'
[Unit]
Description=AuriX node-side fleet probe agent
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=aurix
Group=aurix
EnvironmentFile=/etc/aurix-bot/aurix-agent.env
ExecStart=/usr/bin/python3 /opt/aurix-agent/current/fleet_probe_agent.py --limit 10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UNIT
  cat > /etc/systemd/system/aurix-fleet-probe-agent.timer <<'UNIT'
[Unit]
Description=Run AuriX node-side fleet probes

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
RandomizedDelaySec=15s
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now aurix-fleet-probe-agent.timer >/dev/null || \
    fail "probe-agent timer could not be enabled"
  # Queue one immediate run without making a temporary control-plane outage
  # fail Outline bootstrap; the timer will retry on its normal cadence.
  systemctl start --no-block aurix-fleet-probe-agent.service >/dev/null || \
    fail "probe-agent service could not be started"
}

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
import hashlib
import http.client
import json
import os
import re
import ssl
import urllib.parse

fields = {}
outline_dir = os.environ["OUTLINE_DIR"]
with open(outline_dir + "/access.txt", encoding="utf-8") as stream:
    for line in stream:
        key, _, value = line.strip().partition(":")
        fields[key] = value
api = fields["apiUrl"].rstrip("/")
parsed = urllib.parse.urlsplit(api)
expected = fields.get("certSha256", "").replace(":", "").lower()
if (
    parsed.scheme != "https"
    or not parsed.hostname
    or parsed.port is None
    or not re.fullmatch(r"[0-9a-f]{64}", expected)
):
    raise RuntimeError("invalid pinned Outline management identity")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        certificate = self.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(certificate).hexdigest()
        if actual != expected:
            self.close()
            raise RuntimeError("Outline certificate pin mismatch")


context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE


def request(method, suffix):
    connection = PinnedHTTPSConnection(
        parsed.hostname, parsed.port, timeout=15, context=context
    )
    try:
        connection.request(
            method,
            parsed.path.rstrip("/") + suffix,
            headers={"Accept": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
        if response.status >= 400:
            raise RuntimeError("Outline management request failed")
        return response.status, body
    finally:
        connection.close()


_, body = request("GET", "/access-keys")
keys = json.loads(body.decode("utf-8")).get("accessKeys", [])
for item in keys:
    key_id = urllib.parse.quote(str(item["id"]), safe="")
    request("DELETE", "/access-keys/" + key_id)
PY
fi

install_probe_agent

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
import hashlib
import http.client
import json
import os
import re
import ssl
import urllib.parse

fields = {}
outline_dir = os.environ["OUTLINE_DIR"]
with open(outline_dir + "/access.txt", encoding="utf-8") as stream:
    for line in stream:
        key, _, value = line.strip().partition(":")
        fields[key] = value
api = fields["apiUrl"].rstrip("/")
parsed = urllib.parse.urlsplit(api)
expected = fields.get("certSha256", "").replace(":", "").lower()
if (
    parsed.scheme != "https"
    or not parsed.hostname
    or parsed.port is None
    or not re.fullmatch(r"[0-9a-f]{64}", expected)
):
    raise RuntimeError("invalid pinned Outline management identity")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        certificate = self.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(certificate).hexdigest()
        if actual != expected:
            self.close()
            raise RuntimeError("Outline certificate pin mismatch")


context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE


def request(method, suffix):
    connection = PinnedHTTPSConnection(
        parsed.hostname, parsed.port, timeout=15, context=context
    )
    try:
        connection.request(
            method,
            parsed.path.rstrip("/") + suffix,
            headers={"Accept": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
        if response.status >= 400:
            raise RuntimeError("Outline management request failed")
        return response.status, body
    finally:
        connection.close()


_, server_body = request("GET", "/server")
_, keys_body = request("GET", "/access-keys")
server = json.loads(server_body.decode("utf-8"))
keys = json.loads(keys_body.decode("utf-8")).get("accessKeys", [])
print(json.dumps({
    "status": "ready",
    "outline_version": str(server.get("version") or "unknown"),
    "remote_key_count": len(keys),
}, sort_keys=True))
PY
