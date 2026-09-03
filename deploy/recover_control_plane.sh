#!/usr/bin/env bash
set -euo pipefail

env_file="/etc/aurix-bot/aurix.env"
skip_service_start=0
skip_fleet_check=0

usage() {
  cat <<'USAGE'
Usage: deploy/recover_control_plane.sh [--env-file PATH] [--skip-service-start] [--skip-fleet-check]

Rebuild this host as an AuriX control plane from the current source checkout
and a private environment file. Run after cloning the repository on a fresh
Ubuntu control-plane VM and restoring the private .env/recovery bundle.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      env_file="${2:-}"
      shift 2
      ;;
    --skip-service-start)
      skip_service_start=1
      shift
      ;;
    --skip-fleet-check)
      skip_fleet_check=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$(id -u)" == "0" ]] || {
  echo "recovery must run as root" >&2
  exit 1
}
[[ "$env_file" = /* ]] || {
  echo "--env-file must be absolute" >&2
  exit 1
}
[[ -r "$env_file" ]] || {
  echo "environment file is not readable: $env_file" >&2
  exit 1
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_root="$(cd -- "$script_dir/.." && pwd)"

set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

# JSON values are valid dotenv strings but are not shell-safe when unquoted
# (the example intentionally keeps them readable). Preserve a simple marker for
# the later unit-install branch, then let Python's canonical dotenv parser load
# the exact values for validation and backup verification.
fleet_configured=0
if [[ -n "${AURIX_FLEET_NODES_JSON:-}" ]]; then
  fleet_configured=1
fi
backup_supabase_configured=0
if [[ -n "${AURIX_BACKUP_SUPABASE_BUCKET:-}" ]]; then
  backup_supabase_configured=1
fi
unset PAYMENT_RECIPIENTS_JSON OUTLINE_SERVERS_JSON AURIX_FLEET_NODES_JSON

command -v python3 >/dev/null || {
  echo "python3 is required" >&2
  exit 1
}

release_id="$(date -u +%Y%m%dT%H%M%SZ)"
if git -C "$source_root" rev-parse --verify HEAD >/dev/null 2>&1; then
  release_id="$(git -C "$source_root" rev-parse HEAD)"
fi

release_root="${AURIX_DEPLOY_RELEASES_DIR:-/opt/aurix-releases}"
current_link="${AURIX_DEPLOY_CURRENT_LINK:-/opt/aurix-current}"
release_dir="$release_root/$release_id"
build_dir="$release_root/.recovery-$release_id-$$"

install -d -o root -g root -m 0755 "$release_root" /var/lib/aurix-deploy
install -d -o root -g root -m 0750 /etc/aurix-bot
if ! id aurix >/dev/null 2>&1; then
  useradd --system --home-dir /opt/aurix-bot --create-home --shell /usr/sbin/nologin aurix
fi
install -d -o aurix -g aurix -m 0750 /var/lib/aurix-bot

rm -rf "$build_dir"
mkdir -p "$build_dir"
if git -C "$source_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$source_root" archive --format=tar HEAD | tar -C "$build_dir" -xf -
else
  tar --exclude=.git --exclude=.venv --exclude=__pycache__ -C "$source_root" -cf - . |
    tar -C "$build_dir" -xf -
fi

python3 -m venv "$build_dir/.venv"
"$build_dir/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-cache-dir --requirement "$build_dir/requirements.txt"
"$build_dir/.venv/bin/python" -m compileall -q "$build_dir"
"$build_dir/.venv/bin/python" "$build_dir/deploy/digitalocean_preflight.py" \
  --live --env-file "$env_file"

# A surviving Supabase project is part of the recovery boundary. If its
# private archive bucket was removed, recreate it before archive verification;
# recovery must not stop for a manual bucket-bootstrap step. The helper refuses
# public buckets and never creates the bucket implicitly during normal backups.
if [[ "$backup_supabase_configured" == "1" ]]; then
  "$build_dir/.venv/bin/python" "$build_dir/deploy/recovery_storage.py" \
    ensure --env-file "$env_file"
fi

if [[ -n "${DATABASE_PATH:-}" && -z "${COMMERCE_DATABASE_URL:-}" ]]; then
  "$build_dir/.venv/bin/python" "$build_dir/deploy/database_backup.py" verify \
    --env-file "$env_file"
fi

if [[ "$fleet_configured" == "1" && "$skip_fleet_check" == "0" ]]; then
  "$build_dir/.venv/bin/python" "$build_dir/deploy/fleet_backup.py" verify \
    --node all --env-file "$env_file"
  "$build_dir/.venv/bin/python" "$build_dir/deploy/fleet_reconcile.py" validate \
    --env-file "$env_file"
  "$build_dir/.venv/bin/python" "$build_dir/deploy/fleet_reconcile.py" check \
    --env-file "$env_file"
fi

find "$build_dir" -type d -exec chmod a+rx {} +
find "$build_dir" -type f -exec chmod a+r {} +
if [[ -e "$release_dir" ]]; then
  rm -rf "$build_dir"
else
  mv "$build_dir" "$release_dir"
fi

temporary_link="$(dirname "$current_link")/.aurix-current-$$"
ln -sfn "$release_dir" "$temporary_link"
mv -Tf "$temporary_link" "$current_link"

install -o root -g root -m 0644 "$release_dir/deploy/aurix-bot.service" /etc/systemd/system/aurix-bot.service
install -o root -g root -m 0644 "$release_dir/deploy/aurix-autodeploy.service" /etc/systemd/system/aurix-autodeploy.service
install -o root -g root -m 0644 "$release_dir/deploy/aurix-autodeploy.timer" /etc/systemd/system/aurix-autodeploy.timer
install -o root -g root -m 0644 "$release_dir/deploy/aurix-database-backup.service" /etc/systemd/system/aurix-database-backup.service
install -o root -g root -m 0644 "$release_dir/deploy/aurix-database-backup.timer" /etc/systemd/system/aurix-database-backup.timer
for unit in aurix-fleet-backup.service aurix-fleet-backup.timer aurix-fleet-reconcile.service aurix-fleet-reconcile.timer; do
  install -o root -g root -m 0644 "$release_dir/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable aurix-autodeploy.timer >/dev/null
if [[ -n "${DATABASE_PATH:-}" && -z "${COMMERCE_DATABASE_URL:-}" ]]; then
  systemctl enable aurix-database-backup.timer >/dev/null
fi
if [[ "$fleet_configured" == "1" ]]; then
  systemctl enable aurix-fleet-backup.timer aurix-fleet-reconcile.timer >/dev/null
fi

if [[ "$skip_service_start" == "0" ]]; then
  systemctl restart aurix-bot.service
  systemctl is-active --quiet aurix-bot.service
fi

echo "AuriX control-plane recovery prepared at $release_id"
