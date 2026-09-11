#!/usr/bin/env bash
set -euo pipefail

# Run on sg-a only. This script deliberately owns the disposable xray@ instance
# and never edits the occupied /usr/local/etc/xray/config.json.

BASE=/run/aurix-xray-canary
CONFIG=/usr/local/etc/xray/aurix-canary.json
UNIT=xray@aurix-canary.service
API=127.0.0.1:10085
PORT=18443
XRAY=/usr/local/bin/xray

cleanup() {
  set +e
  systemctl disable --now "$UNIT" >/dev/null 2>&1
  rm -f "$CONFIG"
  if test -f "$BASE/firewall-added" && command -v ufw >/dev/null 2>&1; then
    ufw delete allow "$PORT/tcp" >/dev/null 2>&1
  fi
  rm -rf "$BASE"
}

start_canary() {
  test "$(id -u)" -eq 0
  test -x "$XRAY"
  systemctl is-active --quiet xray.service
  test ! -e "$CONFIG"
  test ! -e "$BASE"
  if ss -ltnH | awk '{print $4}' | grep -Eq "(:|])${PORT}$"; then
    echo "refusing: TCP $PORT is already bound" >&2
    exit 2
  fi

  install -d -m 0700 "$BASE"
  trap 'rc=$?; if test "$rc" -ne 0; then cleanup; fi; exit "$rc"' EXIT
  umask 077

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    if ufw status | grep -Eq "(^|[[:space:]])${PORT}/tcp([[:space:]]|$)"; then
      echo "refusing: pre-existing UFW rule for TCP $PORT" >&2
      exit 2
    fi
    ufw allow "$PORT/tcp" comment 'aurix-xray-canary' >/dev/null
    : > "$BASE/firewall-added"
  fi

  local key_output private_key public_key uuid_a uuid_b short_id
  key_output="$($XRAY x25519)"
  private_key="$(printf '%s\n' "$key_output" | awk -F': ' '/^PrivateKey:/{print $2}')"
  public_key="$(printf '%s\n' "$key_output" | awk -F': ' '/^Password \(PublicKey\):/{print $2}')"
  uuid_a="$($XRAY uuid | tr -d '\r\n')"
  uuid_b="$($XRAY uuid | tr -d '\r\n')"
  short_id="$(openssl rand -hex 8)"
  test -n "$private_key" && test -n "$public_key" && test -n "$uuid_a" && test -n "$uuid_b"

  cat > "$CONFIG" <<EOF
{
  "log": {"loglevel": "warning"},
  "api": {"tag": "canary-api", "services": ["HandlerService", "StatsService"]},
  "stats": {},
  "policy": {
    "levels": {"0": {"statsUserUplink": true, "statsUserDownlink": true}},
    "system": {"statsInboundUplink": true, "statsInboundDownlink": true}
  },
  "inbounds": [
    {
      "tag": "canary-api",
      "listen": "127.0.0.1",
      "port": 10085,
      "protocol": "dokodemo-door",
      "settings": {"address": "127.0.0.1"}
    },
    {
      "tag": "canary-vless",
      "listen": "0.0.0.0",
      "port": $PORT,
      "protocol": "vless",
      "settings": {"clients": [], "decryption": "none"},
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "127.0.0.1:4431",
          "xver": 0,
          "serverNames": ["speed.cloudflare.com"],
          "privateKey": "$private_key",
          "shortIds": ["$short_id"]
        }
      }
    }
  ],
  "outbounds": [
    {"protocol": "freedom", "tag": "canary-api"},
    {"protocol": "freedom", "tag": "direct"},
    {"protocol": "blackhole", "tag": "blocked"}
  ],
  "routing": {
    "rules": [
      {"type": "field", "inboundTag": ["canary-api"], "outboundTag": "canary-api"}
    ]
  }
}
EOF
  # The instance drops to nobody, so keep the config root-owned but readable
  # only through its service group.
  chown root:nogroup "$CONFIG"
  chmod 0640 "$CONFIG"
  "$XRAY" run -test -config "$CONFIG" >/dev/null

  systemctl start "$UNIT"
  systemctl is-active --quiet "$UNIT"
  sleep 1
  ss -ltnH | awk '{print $4}' | grep -Eq "(:|])${PORT}$"

  printf '%s' '{"inbounds":[{"tag":"canary-vless","port":18443,"protocol":"vless","settings":{"clients":[{"id":"'"$uuid_a"'","level":0,"email":"xray-canary-customer-a","flow":"xtls-rprx-vision"}],"decryption":"none"}}]}' > "$BASE/add-a.json"
  printf '%s' '{"inbounds":[{"tag":"canary-vless","port":18443,"protocol":"vless","settings":{"clients":[{"id":"'"$uuid_b"'","level":0,"email":"xray-canary-customer-b","flow":"xtls-rprx-vision"}],"decryption":"none"}}]}' > "$BASE/add-b.json"
  local add_output
  # The installed CLI requires the inbound port in each dynamic-add object,
  # even though the API call targets an existing tagged inbound.
  add_output="$("$XRAY" api adu --server="$API" "$BASE/add-a.json" "$BASE/add-b.json")"
  printf 'ADD_USERS_RESULT=%s\n' "$add_output"
  local count_output count
  count_output="$("$XRAY" api inboundusercount --server="$API" -tag=canary-vless)"
  count="$(printf '%s\n' "$count_output" | grep -oE '[0-9]+' | tail -n 1)"
  printf 'USER_COUNT_RAW=%s\n' "$count_output"
  test "$count" = 2

  cat > "$BASE/client-a.env" <<EOF
ADDRESS=157.245.63.95
PORT=$PORT
PUBLIC_KEY=$public_key
SHORT_ID=$short_id
SERVER_NAME=speed.cloudflare.com
UUID=$uuid_a
EMAIL=xray-canary-customer-a
EOF
  cat > "$BASE/client-b.env" <<EOF
ADDRESS=157.245.63.95
PORT=$PORT
PUBLIC_KEY=$public_key
SHORT_ID=$short_id
SERVER_NAME=speed.cloudflare.com
UUID=$uuid_b
EMAIL=xray-canary-customer-b
EOF
  chmod 0600 "$BASE/client-a.env" "$BASE/client-b.env"
  echo "CANARY_READY=1"
  echo "UNIT=$UNIT"
  echo "PORT=$PORT"
  echo "API=$API"
  echo "USER_COUNT=2"
  echo "CONFIG_TEST=pass"
  trap - EXIT
}

cleanup_canary() {
  test "$(id -u)" -eq 0
  systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
  rm -f "$CONFIG"
  if test -f "$BASE/firewall-added" && command -v ufw >/dev/null 2>&1; then
    ufw delete allow "$PORT/tcp" >/dev/null 2>&1 || true
  fi
  rm -rf "$BASE"
  xray_active=0
  systemctl is-active --quiet xray.service && xray_active=1
  occupied_bound=0
  ss -ltnH | awk '{print $4}' | grep -Eq "(:|])8443$" && occupied_bound=1
  canary_free=1
  ss -ltnH | awk '{print $4}' | grep -Eq "(:|])${PORT}$" && canary_free=0
  test ! -e "$CONFIG"
  test "$xray_active" -eq 1
  test "$occupied_bound" -eq 1
  test "$canary_free" -eq 1
  "$XRAY" run -test -config /usr/local/etc/xray/config.json >/dev/null
  echo "CLEANUP_VERIFIED=1"
  echo "OCCUPIED_XRAY_ACTIVE=1"
  echo "OCCUPIED_XRAY_8443=bound"
  echo "CANARY_CONFIG=absent"
  echo "CANARY_PORT=$PORT free"
}

case "${1:-}" in
  start) start_canary ;;
  cleanup) cleanup_canary ;;
  *) echo "usage: $0 {start|cleanup}" >&2; exit 64 ;;
esac
