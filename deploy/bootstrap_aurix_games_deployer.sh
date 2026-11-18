#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 || $# -ne 1 || ! -f $1 ]]; then
  echo "usage: sudo $0 /path/to/approved-ed25519-public-key" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC_KEY="$1"
if [[ "$(awk 'NF { n++ } END { print n+0 }' "$PUBLIC_KEY")" != 1 ]]; then
  echo "provide exactly one SSH public key" >&2
  exit 1
fi
read -r KEY_TYPE KEY_DATA _ < "$PUBLIC_KEY"
if [[ "$KEY_TYPE" != ssh-ed25519 || ! "$KEY_DATA" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]; then
  echo "the Games deploy identity must use one Ed25519 public key" >&2
  exit 1
fi
ssh-keygen -lf "$PUBLIC_KEY" >/dev/null

USER=aurix-games-deploy
HOME_DIR=/var/lib/aurix-games-deploy
WRAPPER=/usr/local/sbin/aurix-games-deploy-apply
AUTH_LINE="restrict,command=\"/usr/bin/sudo -n $WRAPPER\" $KEY_TYPE $KEY_DATA aurix-games-ci"
AUTHORIZED_KEYS="$HOME_DIR/.ssh/authorized_keys"
if [[ -L "$HOME_DIR" || -L "$HOME_DIR/.ssh" || -L "$AUTHORIZED_KEYS" ]]; then
  echo "deploy account SSH paths must not be symlinks" >&2
  exit 1
fi
if [[ -s "$AUTHORIZED_KEYS" ]]; then
  mapfile -t EXISTING_KEYS < "$AUTHORIZED_KEYS"
  if [[ ${#EXISTING_KEYS[@]} -ne 1 || "${EXISTING_KEYS[0]}" != "$AUTH_LINE" ]]; then
    echo "existing deploy keys need explicit review before rotation" >&2
    exit 1
  fi
fi
HELPER=/usr/local/lib/aurix-games-deployer
RECOVERY_WRAPPER=/usr/local/sbin/aurix-games-deploy-recover
RECOVERY_UNIT=aurix-games-deploy-recovery.service
UNITS=(
  aurix-games.service aurix-games-edge.service
  aurix-games-catalog.timer aurix-games-dispatch.timer aurix-games-fulfill.timer
  aurix-games-reconcile.timer aurix-games-integrity.timer aurix-games-backup.timer
  aurix-games-catalog.service aurix-games-dispatch.service aurix-games-fulfill.service
  aurix-games-reconcile.service aurix-games-integrity.service aurix-games-backup.service
)
for unit in "${UNITS[@]}"; do
  systemctl cat "$unit" >/dev/null
done

getent group "$USER" >/dev/null || groupadd --system "$USER"
if ! id "$USER" >/dev/null 2>&1; then
  useradd --system --gid "$USER" --home-dir "$HOME_DIR" --create-home --shell /bin/sh "$USER"
else
  usermod --home "$HOME_DIR" --shell /bin/sh "$USER"
fi
passwd --lock "$USER" >/dev/null
install -d "$HOME_DIR"
chown root:root "$HOME_DIR"
chmod 0755 "$HOME_DIR"
install -d -o root -g root -m 0755 "$HELPER/deploy" "$HELPER/scripts"
install -o root -g root -m 0644 "$ROOT/deploy/aurix_games_deploy.py" "$HELPER/deploy/aurix_games_deploy.py"
install -o root -g root -m 0644 "$ROOT/scripts/package_game_store.py" "$HELPER/scripts/package_game_store.py"
install -o root -g root -m 0644 "$ROOT/scripts/game_store_scope.py" "$HELPER/scripts/game_store_scope.py"
install -o root -g root -m 0644 "$ROOT/scripts/__init__.py" "$HELPER/scripts/__init__.py"

cat > "$WRAPPER" <<'EOF'
#!/bin/sh
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /opt/aurix-venv/bin/python /usr/local/lib/aurix-games-deployer/deploy/aurix_games_deploy.py
EOF
cat > "$RECOVERY_WRAPPER" <<'EOF'
#!/bin/sh
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /opt/aurix-venv/bin/python /usr/local/lib/aurix-games-deployer/deploy/aurix_games_deploy.py --recover
EOF
chown root:root "$WRAPPER" "$RECOVERY_WRAPPER"
chmod 0755 "$WRAPPER" "$RECOVERY_WRAPPER"

install -d "$HOME_DIR/.ssh"
chown root:root "$HOME_DIR/.ssh"
chmod 0755 "$HOME_DIR/.ssh"
if [[ ! -s "$AUTHORIZED_KEYS" ]]; then
  printf '%s\n' "$AUTH_LINE" > "$AUTHORIZED_KEYS"
fi
chown root:root "$AUTHORIZED_KEYS"
chmod 0644 "$AUTHORIZED_KEYS"

SUDOERS=/etc/sudoers.d/aurix-games-deploy
printf '%s ALL=(root) NOPASSWD: %s\n' "$USER" "$WRAPPER" > "$SUDOERS"
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS"

cat > "/etc/systemd/system/$RECOVERY_UNIT" <<EOF
[Unit]
Description=Recover an interrupted AuriX Games deployment before service startup
Before=${UNITS[*]}

[Service]
Type=oneshot
ExecStart=$RECOVERY_WRAPPER
TimeoutStartSec=240

[Install]
WantedBy=multi-user.target
EOF
for unit in "${UNITS[@]}"; do
  install -d -o root -g root -m 0755 "/etc/systemd/system/$unit.d"
  cat > "/etc/systemd/system/$unit.d/aurix-games-deploy-recovery.conf" <<EOF
[Unit]
Requires=$RECOVERY_UNIT
After=$RECOVERY_UNIT
EOF
done
systemctl daemon-reload
systemctl enable "$RECOVERY_UNIT"
echo "Games deploy identity and boot recovery installed. No Games service was restarted."
