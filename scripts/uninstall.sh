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

print_usage() {
  cat <<'EOF'
Usage: bash ./scripts/uninstall.sh [--dry-run] [--remove-managed-data] [--yes]

Default behavior removes only App Manager-owned Cockpit plugin and backend files.
It leaves application source repositories, production releases, Nginx configuration,
and Cloudflare Tunnel setup in place.

--remove-managed-data  Remove App Manager-managed application records, source trees,
                      production releases, service files, and app state.
                      This action is destructive and must be explicitly confirmed.
--dry-run             Show actions without making changes.
--yes                 Skip interactive confirmation for destructive removal.
EOF
}

log() {
  printf '%s\n' "$*"
}

refuse_target() {
  local path="$1"
  log "Refusing to remove protected path: $path" >&2
  return 1
}

safe_remove() {
  local target="${1:-}"
  if [[ -z "$target" ]]; then
    return 0
  fi
  case "$target" in
    "/"|"/opt"|"/opt/webapps"|"/var/lib/cockpit"|"/var/lib/cockpit/app-manager"|"/etc"|"/etc/cockpit"|"/usr"|"/usr/share"|"/usr/share/cockpit"|"/usr/libexec"|"/usr/libexec/cockpit")
      refuse_target "$target"
      return 1
      ;;
  esac

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: rm -rf -- $target"
    return 0
  fi

  if [[ ! -e "$target" && ! -L "$target" ]]; then
    return 0
  fi

  rm -rf -- "$target"
}

parse_registry_ids() {
  local registry_path="${STATE_ROOT}/apps.json"
  if [[ ! -f "$registry_path" ]]; then
    return 0
  fi

  local python_bin=""
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
      python_bin="$candidate"
      break
    fi
  done
  if [[ -z "$python_bin" ]]; then
    log "Python is required to inspect the App Manager registry." >&2
    return 1
  fi

  "$python_bin" - "$registry_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    raise SystemExit(0)
if not isinstance(data, dict):
    raise SystemExit(0)
apps = data.get('apps', [])
if not isinstance(apps, list):
    raise SystemExit(0)
for app in apps:
    if isinstance(app, dict) and isinstance(app.get('id'), str) and app['id']:
        print(app['id'])
PY
}

remove_managed_units() {
  if [[ ! -d "$SYSTEMD_DIR" ]]; then
    return 0
  fi
  local service_name
  while IFS= read -r service_name; do
    [[ -n "$service_name" ]] || continue
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: systemctl disable --now $service_name"
      log "DRY-RUN: rm -f -- ${SYSTEMD_DIR}/${service_name}"
      continue
    fi
    if command -v systemctl >/dev/null 2>&1; then
      systemctl disable --now "$service_name" >/dev/null 2>&1 || true
    fi
    rm -f -- "${SYSTEMD_DIR}/${service_name}"
  done < <(find "$SYSTEMD_DIR" -maxdepth 1 -type f -name 'cockpit-app-*.service' -printf '%f\n' 2>/dev/null || true)

  if command -v systemctl >/dev/null 2>&1; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: systemctl daemon-reload"
    else
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
  fi
}

remove_managed_app_data() {
  local app_id
  while IFS= read -r app_id; do
    app_id="${app_id%$'\r'}"
    [[ -n "$app_id" ]] || continue
    local app_dir="${PRODUCTION_ROOT}/${app_id}"
    local source_dir="${STATE_ROOT}/sources/${app_id}"
    local home_dir="${STATE_ROOT}/homes/${app_id}"
    local app_log_dir="${LOG_ROOT}/${app_id}"
    local service_name="cockpit-app-${app_id}.service"

    safe_remove "$source_dir"
    safe_remove "$home_dir"
    safe_remove "$app_log_dir"
    safe_remove "$app_dir"
    safe_remove "${SYSTEMD_DIR}/${service_name}"
  done < <(parse_registry_ids)

  local managed_paths=(
    "${STATE_ROOT}/apps.json"
    "${STATE_ROOT}/config.json"
    "${STATE_ROOT}/sources"
    "${STATE_ROOT}/homes"
    "${STATE_ROOT}/locks"
    "${LOG_ROOT}"
  )
  local path
  for path in "${managed_paths[@]}"; do
    if [[ "$path" == "$LOG_ROOT" ]]; then
      if [[ -d "$LOG_ROOT" ]]; then
        find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -print -quit | grep -q . || true
      fi
    fi
    safe_remove "$path"
  done

  remove_managed_units
}

remove_default_plugin_files() {
  safe_remove "$PLUGIN_DEST"
  safe_remove "$CONFIG_DEST"
  safe_remove "${BACKEND_DEST}/app-manager"
  safe_remove "${BACKEND_DEST}/adapters.py"
  safe_remove "${BACKEND_DEST}/app-manager-adapters.py"
}

if [[ $# -gt 0 ]]; then
  for arg in "$@"; do
    case "$arg" in
      --dry-run)
        DRY_RUN=1
        ;;
      --remove-managed-data)
        REMOVE_MANAGED_DATA=1
        ;;
      --yes)
        AUTO_CONFIRM=1
        ;;
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
    printf 'This will remove App Manager-managed app data, production releases, service units, and state files.\n' >&2
    printf 'Type REMOVE to continue: ' >&2
    read -r response || response=''
    if [[ "$response" != "REMOVE" ]]; then
      log 'Aborted: confirmation string did not match REMOVE.' >&2
      exit 1
    fi
  fi
  log 'Removing App Manager-managed applications and data...'
  remove_managed_app_data
  log 'Finished App Manager data removal.'
else
  log 'Removing only App Manager-owned Cockpit and backend files; preserving app data and production releases.'
  remove_default_plugin_files
  log 'Finished App Manager plugin uninstallation.'
fi

exit 0
