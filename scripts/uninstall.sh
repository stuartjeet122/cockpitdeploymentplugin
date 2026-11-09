#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DEST="${APP_MANAGER_PLUGIN_DEST:-/usr/share/cockpit/app-manager}"
BACKEND_DEST="${APP_MANAGER_BACKEND_DEST:-/usr/libexec/cockpit}"
CONFIG_DEST="${APP_MANAGER_CONFIG_DEST:-/etc/cockpit/app-manager}"
STATE_ROOT="${APP_MANAGER_STATE_ROOT:-/var/lib/cockpit/app-manager}"
PRODUCTION_ROOT="${APP_MANAGER_PRODUCTION_ROOT:-/opt/webapps}"
LOG_ROOT="${APP_MANAGER_LOG_ROOT:-/var/log/cockpit-app-manager}"
SYSTEMD_DIR="${APP_MANAGER_SYSTEMD_DIR:-/etc/systemd/system}"

DRY_RUN=0
REMOVE_MANAGED_DATA=0
AUTO_CONFIRM=0
UNITS_CHANGED=0

print_usage() {
  cat <<'EOF'
Usage: bash ./scripts/uninstall.sh [--dry-run] [--remove-managed-data] [--yes]

Default behavior removes only App Manager-owned Cockpit plugin and backend files.
It preserves application data, Nginx configuration, and Cloudflare Tunnel setup.

--remove-managed-data  Remove registered App Manager app data and verified service
                       units. This destructive action requires explicit confirmation.
--dry-run              Show actions without making changes.
--yes                  Skip interactive confirmation for destructive removal.
EOF
}

log() {
  printf '%s\n' "$*"
}

fail() {
  log "Uninstall failed: $*" >&2
  exit 1
}

is_valid_app_id() {
  [[ "$1" =~ ^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$ ]]
}

has_symlink_ancestor() {
  local parent="${1%/*}"
  while [[ -n "$parent" && "$parent" != "$1" ]]; do
    [[ -L "$parent" ]] && return 0
    [[ "$parent" == */* ]] || break
    parent="${parent%/*}"
  done
  return 1
}

safe_remove() {
  local target="${1:-}"
  if [[ -z "$target" ]]; then
    return 0
  fi
  case "$target" in
    "/"|"/opt"|"/opt/webapps"|"/var/lib/cockpit"|"/var/lib/cockpit/app-manager"|"/etc"|"/etc/cockpit"|"/usr"|"/usr/share"|"/usr/share/cockpit"|"/usr/libexec"|"/usr/libexec/cockpit"|"/etc/systemd"|"/etc/systemd/system")
      fail "Refusing to remove a protected shared path: $target"
      ;;
  esac
  if has_symlink_ancestor "$target"; then
    fail "Refusing to remove a path beneath a symlink: $target"
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: remove $target"
    return 0
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    rm -rf -- "$target"
  fi
}

parse_registry_records() {
  local registry_path="${STATE_ROOT}/apps.json"
  [[ -f "$registry_path" ]] || return 0

  local python_bin=""
  local candidate
  for candidate in python python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      python_bin="$candidate"
      break
    fi
  done
  [[ -n "$python_bin" ]] || fail "Python is required to validate the App Manager registry."

  "$python_bin" - "$registry_path" "${STATE_ROOT}/sources" "$PRODUCTION_ROOT" "$LOG_ROOT" <<'PY'
import json
import re
import sys
from pathlib import Path

registry_path, source_root, production_root, log_root = map(Path, sys.argv[1:])
try:
    data = json.loads(registry_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    print(f"Could not safely parse App Manager registry: {exc}", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
    print("App Manager registry must contain an apps array.", file=sys.stderr)
    raise SystemExit(1)
pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")

def managed_child(raw_path, root, app_id, field):
    if not isinstance(raw_path, str) or any(ord(char) < 32 or ord(char) == 127 for char in raw_path):
        raise ValueError(f"Application {app_id!r} has an invalid {field}.")
    candidate = Path(raw_path).resolve(strict=False)
    root = root.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Application {app_id!r} has an unmanaged {field}.") from exc
    if candidate == root or candidate.name != app_id:
        raise ValueError(f"Application {app_id!r} has an unmanaged {field}.")
    return str(candidate)

for app in data["apps"]:
    app_id = app.get("id") if isinstance(app, dict) else None
    if not isinstance(app_id, str) or not pattern.fullmatch(app_id):
        print("App Manager registry contains an invalid application ID.", file=sys.stderr)
        raise SystemExit(1)
    try:
        source = managed_child(app.get("source_path"), source_root, app_id, "source path")
        production = managed_child(app.get("production_path"), production_root, app_id, "production path")
        logs = managed_child(app.get("log_path"), log_root, app_id, "log path")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print("\t".join((app_id, source, production, logs)))
PY
}

remove_managed_unit() {
  local app_id="$1"
  local service_name="cockpit-app-${app_id}.service"
  local service_path="${SYSTEMD_DIR}/${service_name}"
  [[ -e "$service_path" || -L "$service_path" ]] || return 0
  if has_symlink_ancestor "$service_path"; then
    fail "Refusing to modify a systemd unit beneath a symlink: $service_path"
  fi

  if [[ -L "$service_path" || ! -f "$service_path" ]] ||
    ! grep -Fqx "Description=Cockpit App Manager - ${app_id}" "$service_path" ||
    ! grep -Fqx "User=app-${app_id}" "$service_path"; then
    log "Leaving unverified systemd unit untouched: $service_name" >&2
    return 0
  fi

  command -v systemctl >/dev/null 2>&1 ||
    fail "systemctl is required to safely remove App Manager unit $service_name."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: systemctl disable --now $service_name"
    log "DRY-RUN: remove $service_path"
    UNITS_CHANGED=1
    return 0
  fi
  systemctl disable --now "$service_name" >/dev/null ||
    fail "Could not safely stop and disable App Manager unit $service_name."
  rm -f -- "$service_path"
  UNITS_CHANGED=1
}

remove_managed_app_data() {
  local registry_records
  registry_records="$(parse_registry_records)" || fail "The registry could not be validated; no managed app data was removed."
  local app_id source_dir app_dir app_log_dir
  while IFS=$'\t' read -r app_id source_dir app_dir app_log_dir; do
    [[ -n "$app_id" ]] || continue
    is_valid_app_id "$app_id" || fail "Invalid application ID in registry."

    remove_managed_unit "$app_id"
    safe_remove "$source_dir"
    safe_remove "${STATE_ROOT}/homes/${app_id}"
    safe_remove "${STATE_ROOT}/locks/${app_id}.lock"
    safe_remove "$app_log_dir"
    safe_remove "$app_dir"
  done <<<"$registry_records"

  safe_remove "${STATE_ROOT}/apps.json"
  safe_remove "${STATE_ROOT}/apps.json.lock"
  safe_remove "${STATE_ROOT}/config.json"
  safe_remove "${STATE_ROOT}/config.json.lock"

  if [[ "$UNITS_CHANGED" -eq 1 && "$DRY_RUN" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || fail "Could not reload systemd after removing App Manager units."
  fi
}

remove_default_plugin_files() {
  safe_remove "$PLUGIN_DEST"
  safe_remove "$CONFIG_DEST"
  safe_remove "${BACKEND_DEST}/app-manager"
  safe_remove "${BACKEND_DEST}/adapters.py"
}

if [[ $# -gt 0 ]]; then
  for arg in "$@"; do
    case "$arg" in
      --dry-run) DRY_RUN=1 ;;
      --remove-managed-data) REMOVE_MANAGED_DATA=1 ;;
      --yes) AUTO_CONFIRM=1 ;;
      -h|--help)
        print_usage
        exit 0
        ;;
      *)
        log "Unknown argument: $arg" >&2
        print_usage >&2
        exit 2
        ;;
    esac
  done
fi

if [[ "$REMOVE_MANAGED_DATA" -eq 1 ]]; then
  if [[ "$DRY_RUN" -eq 0 && "$AUTO_CONFIRM" -eq 0 ]]; then
    printf 'This will remove registered App Manager app data and verified service units.\n' >&2
    printf 'Type REMOVE to continue: ' >&2
    read -r response || response=''
    [[ "$response" == "REMOVE" ]] || fail "Confirmation string did not match REMOVE."
  fi
  log 'Removing registered App Manager application data...'
  remove_managed_app_data
  log 'Finished App Manager data removal.'
else
  log 'Removing only App Manager-owned Cockpit and backend files; preserving app data and production releases.'
  remove_default_plugin_files
  log 'Finished App Manager plugin uninstallation.'
fi
