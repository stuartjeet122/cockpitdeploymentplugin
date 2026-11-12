#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DEST="${APP_MANAGER_PLUGIN_DEST:-/usr/share/cockpit/app-manager}"
BACKEND_DEST="${APP_MANAGER_BACKEND_DEST:-/usr/libexec/cockpit/app-manager}"
CONFIG_DEST="${APP_MANAGER_CONFIG_DEST:-/etc/cockpit/app-manager}"
STATE_ROOT="${APP_MANAGER_STATE_ROOT:-/var/lib/cockpit/app-manager}"
SOURCE_ROOT="${APP_MANAGER_SOURCE_ROOT:-${STATE_ROOT}/sources}"
PRODUCTION_ROOT="${APP_MANAGER_PRODUCTION_ROOT:-/opt/webapps}"
LOG_ROOT="${APP_MANAGER_LOG_ROOT:-/var/log/cockpit-app-manager}"
SYSTEMD_DIR="${APP_MANAGER_SYSTEMD_DIR:-/etc/systemd/system}"
NGINX_SNIPPET_DIR="${APP_MANAGER_NGINX_SNIPPET_DIR:-/etc/nginx/snippets/cockpit-app-manager}"
NGINX_CONFIG_ROOT="${APP_MANAGER_NGINX_CONFIG_ROOT:-/etc/nginx}"

DRY_RUN=0
REMOVE_MANAGED_DATA=0
AUTO_CONFIRM=0
UNITS_CHANGED=0
APP_CLEANUP_BLOCKED=0

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
  [[ -e "$target" || -L "$target" ]] || return 0
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: remove $target"
    return 0
  fi
  rm -rf -- "$target"
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

  "$python_bin" - "$registry_path" "$SOURCE_ROOT" "$PRODUCTION_ROOT" "$LOG_ROOT" <<'PY'
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
  local slot="${2:-}"
  local service_name="cockpit-app-${app_id}${slot}.service"
  local service_path="${SYSTEMD_DIR}/${service_name}"
  [[ -e "$service_path" || -L "$service_path" ]] || return 0
  if has_symlink_ancestor "$service_path"; then
    fail "Refusing to modify a systemd unit beneath a symlink: $service_path"
  fi

  if [[ -L "$service_path" || ! -f "$service_path" ]] ||
    ! grep -Fqx "# Managed by Cockpit App Manager" "$service_path" ||
    ! grep -Fqx "Description=Cockpit App Manager - ${app_id}" "$service_path" ||
    ! grep -Fqx "User=app-${app_id}" "$service_path"; then
    log "Leaving unverified systemd unit untouched: $service_name" >&2
    APP_CLEANUP_BLOCKED=1
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

remove_managed_user() {
  local username="app-${1}"
  local passwd_entry group_entry uid gid group_gid account_name password account_tail expected_home_suffix
  command -v getent >/dev/null 2>&1 ||
    fail "getent is required to verify managed user $username."
  passwd_entry="$(getent passwd "$username" || true)"
  [[ -n "$passwd_entry" ]] || return 0
  IFS=: read -r account_name password uid gid _ <<<"$passwd_entry"
  [[ "$uid" =~ ^[0-9]+$ && "$uid" -gt 0 ]] ||
    fail "Refusing to remove $username: account UID is invalid or privileged."
  account_tail="${passwd_entry#"$username:"}"
  expected_home_suffix=":${STATE_ROOT}/homes/${1}:/usr/sbin/nologin"
  [[ "$account_name" == "$username" && "$account_tail" == *"$expected_home_suffix" ]] ||
    fail "Refusing to remove $username: home or shell does not match App Manager's managed account."
  group_entry="$(getent group "$username" || true)"
  [[ -n "$group_entry" ]] ||
    fail "Refusing to remove $username: its private group cannot be verified."
  IFS=: read -r _ _ group_gid _ <<<"$group_entry"
  [[ "$group_gid" == "$gid" ]] ||
    fail "Refusing to remove $username: primary group is not its matching private group."
  local all_groups group_name group_password group_members
  all_groups="$(getent group)" ||
    fail "Could not verify group memberships for managed user $username."
  [[ -n "$all_groups" ]] ||
    fail "Could not enumerate group memberships for managed user $username."
  while IFS=: read -r group_name group_password group_gid group_members; do
    [[ "$group_name" != "$username" ]] || continue
    [[ ",${group_members}," != *",${username},"* ]] ||
      fail "Refusing to remove $username: it belongs to an additional group."
  done <<<"$all_groups"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: userdel -- $username"
    return 0
  fi
  command -v userdel >/dev/null 2>&1 ||
    fail "userdel is required to remove verified App Manager account $username."
  userdel -- "$username" ||
    fail "Could not remove verified App Manager account $username."
}

remove_managed_snippet() {
  local app_id="$1"
  local snippet="${NGINX_SNIPPET_DIR}/${app_id}.conf"
  [[ -e "$snippet" || -L "$snippet" ]] || return 0
  if [[ -L "$snippet" || ! -f "$snippet" ]] ||
    ! grep -Fqx "# Managed by Cockpit App Manager: ${app_id}" "$snippet"; then
    log "Leaving unverified Nginx snippet untouched: $snippet" >&2
    APP_CLEANUP_BLOCKED=1
    return 0
  fi
  if has_symlink_ancestor "$snippet"; then
    fail "Refusing to modify an Nginx snippet beneath a symlink: $snippet"
  fi

  if [[ -d "$NGINX_CONFIG_ROOT" ]] &&
    { grep -R -F -q -- "$snippet" "$NGINX_CONFIG_ROOT" ||
      grep -R -F -q -- "$NGINX_SNIPPET_DIR" "$NGINX_CONFIG_ROOT" ||
      grep -R -F -q -- "$(basename "$NGINX_SNIPPET_DIR")" "$NGINX_CONFIG_ROOT" ||
      grep -R -F -q -- "${app_id}.conf" "$NGINX_CONFIG_ROOT"; }; then
    log "Preserving active Nginx snippet because an existing Nginx config includes it: $snippet" >&2
    APP_CLEANUP_BLOCKED=1
    return 0
  fi
  safe_remove "$snippet"
}

remove_managed_app_data() {
  local registry_records
  registry_records="$(parse_registry_records)" || fail "The registry could not be validated; no managed app data was removed."
  local apps_preserved=0
  local app_id source_dir app_dir app_log_dir
  while IFS=$'\t' read -r app_id source_dir app_dir app_log_dir; do
    [[ -n "$app_id" ]] || continue
    is_valid_app_id "$app_id" || fail "Invalid application ID in registry."

    APP_CLEANUP_BLOCKED=0
    remove_managed_snippet "$app_id"
    if [[ "$APP_CLEANUP_BLOCKED" -eq 1 ]]; then
      log "Preserving application, account, and service because its Nginx snippet is active or unverified: $app_id" >&2
      apps_preserved=1
      continue
    fi
    remove_managed_unit "$app_id"
    remove_managed_unit "$app_id" "@secondary"
    if [[ "$APP_CLEANUP_BLOCKED" -eq 1 ]]; then
      log "Preserving application data and account because its systemd unit is not verifiably managed: $app_id" >&2
      apps_preserved=1
      continue
    fi
    remove_managed_user "$app_id"
    safe_remove "$source_dir"
    safe_remove "${STATE_ROOT}/homes/${app_id}"
    safe_remove "${STATE_ROOT}/locks/${app_id}.lock"
    safe_remove "$app_log_dir"
    safe_remove "$app_dir"
  done <<<"$registry_records"

  if [[ "$apps_preserved" -eq 1 ]]; then
    log "Preserving App Manager registry and global state because one or more applications were retained."
  else
    safe_remove "${STATE_ROOT}/apps.json"
    safe_remove "${STATE_ROOT}/apps.json.lock"
    safe_remove "${STATE_ROOT}/config.json"
    safe_remove "${STATE_ROOT}/config.json.lock"
  fi

  if [[ "$UNITS_CHANGED" -eq 1 ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: systemctl daemon-reload"
    elif command -v systemctl >/dev/null 2>&1; then
      systemctl daemon-reload || fail "Could not reload systemd after removing App Manager units."
    fi
  fi
}

remove_default_plugin_files() {
  local -a planned_removals=()
  remove_owned_file() {
    local target="$1"
    local signature="$2"
    [[ -e "$target" || -L "$target" ]] || return 0
    if [[ -L "$target" || ! -f "$target" ]] || has_symlink_ancestor "$target" ||
      ! grep -Fq -- "$signature" "$target"; then
      log "Leaving unverified installation file untouched: $target" >&2
      return 0
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: remove $target"
      planned_removals+=("$target")
    else
      rm -f -- "$target"
    fi
  }
  remove_empty_directory() {
    local target="$1"
    local entries entry candidate planned nonempty
    [[ -d "$target" && ! -L "$target" ]] || return 0
    if has_symlink_ancestor "$target"; then
      log "Leaving installation directory beneath a symlink untouched: $target" >&2
      return 0
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      entries="$(find "$target" -mindepth 1 -maxdepth 1 -print)"
      while IFS= read -r entry; do
        [[ -n "$entry" ]] || continue
        planned=0
        for candidate in "${planned_removals[@]}"; do
          if [[ "$candidate" == "$entry" ]]; then
            planned=1
            break
          fi
        done
        if [[ "$planned" -eq 0 ]]; then
          nonempty=1
          break
        fi
      done <<<"$entries"
      if [[ "${nonempty:-0}" -eq 1 ]]; then
        log "Leaving non-empty installation directory untouched: $target"
        return 0
      fi
    elif [[ -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      log "Leaving non-empty installation directory untouched: $target"
      return 0
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "DRY-RUN: rmdir -- $target"
      planned_removals+=("$target")
    else
      rmdir -- "$target"
    fi
  }

  remove_owned_file "${PLUGIN_DEST}/index.html" "<title>App Manager</title>"
  remove_owned_file "${PLUGIN_DEST}/manifest.json" '"name": "app-manager"'
  remove_owned_file "${PLUGIN_DEST}/app-manager.css" "Cockpit App Manager standalone stylesheet"
  remove_owned_file "${PLUGIN_DEST}/app-manager.js" "App Manager Cockpit UI loaded"
  remove_owned_file "${PLUGIN_DEST}/icons.js" "Cockpit App Manager offline icons"
  remove_owned_file "${CONFIG_DEST}/defaults.json" '"nginx_template"'
  remove_owned_file "${CONFIG_DEST}/npm-cache.json" '"_comment": "Managed by Cockpit App Manager"'
  remove_owned_file "${CONFIG_DEST}/templates/nginx.conf.tpl" "{{ app_route_location }}"
  remove_owned_file "${CONFIG_DEST}/templates/app.service.tpl" "# Managed by Cockpit App Manager"
  remove_owned_file "${BACKEND_DEST}/app-manager" 'APP_NAME = "app-manager"'
  remove_owned_file "${BACKEND_DEST}/adapters.py" "class NextJSAdapter"

  remove_empty_directory "${CONFIG_DEST}/templates"
  if [[ "$BACKEND_DEST" == "/usr/libexec/cockpit" ]]; then
    log "Preserving shared Cockpit backend directory: $BACKEND_DEST"
  else
    remove_empty_directory "$BACKEND_DEST"
  fi
  remove_empty_directory "$PLUGIN_DEST"
  remove_empty_directory "$CONFIG_DEST"
}

remove_auto_deploy_units() {
  local service_path="${SYSTEMD_DIR}/cockpit-app-manager-auto-deploy.service"
  local timer_path="${SYSTEMD_DIR}/cockpit-app-manager-auto-deploy.timer"
  local unit
  for unit in "$service_path" "$timer_path"; do
    [[ -e "$unit" || -L "$unit" ]] || continue
    if [[ -L "$unit" || ! -f "$unit" ]] || has_symlink_ancestor "$unit" ||
      ! grep -Fqx "# Managed by Cockpit App Manager" "$unit"; then
      log "Leaving unverified automatic-deployment unit untouched: $unit" >&2
      return 0
    fi
  done
  if [[ -e "$service_path" ]] &&
    { ! grep -Fqx "Description=Cockpit App Manager automatic repository deployment" "$service_path" ||
      ! grep -Fqx "ExecStart=/usr/libexec/cockpit/app-manager/app-manager auto-deploy" "$service_path"; }; then
    log "Leaving unverified automatic-deployment service untouched: $service_path" >&2
    return 0
  fi
  if [[ -e "$timer_path" ]] &&
    { ! grep -Fqx "Description=Check managed repositories for deployment updates" "$timer_path" ||
      ! grep -Fqx "Unit=cockpit-app-manager-auto-deploy.service" "$timer_path"; }; then
    log "Leaving unverified automatic-deployment timer untouched: $timer_path" >&2
    return 0
  fi
  if [[ ! -e "$service_path" && ! -e "$timer_path" ]]; then
    return 0
  fi
  command -v systemctl >/dev/null 2>&1 ||
    fail "systemctl is required to safely remove automatic-deployment units."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN: systemctl disable --now cockpit-app-manager-auto-deploy.timer"
    log "DRY-RUN: systemctl stop cockpit-app-manager-auto-deploy.service"
    [[ -e "$timer_path" ]] && log "DRY-RUN: remove $timer_path"
    [[ -e "$service_path" ]] && log "DRY-RUN: remove $service_path"
    log "DRY-RUN: systemctl daemon-reload"
    return 0
  fi
  systemctl disable --now cockpit-app-manager-auto-deploy.timer >/dev/null 2>&1 || true
  systemctl stop cockpit-app-manager-auto-deploy.service >/dev/null 2>&1 || true
  rm -f -- "$timer_path" "$service_path"
  systemctl daemon-reload || fail "Could not reload systemd after removing automatic-deployment units."
}

remove_npm_cache_unit() {
  local service_path="${SYSTEMD_DIR}/cockpit-app-manager-npm-cache.service"
  [[ -e "$service_path" || -L "$service_path" ]] || return 0
  if [[ -L "$service_path" || ! -f "$service_path" ]] || has_symlink_ancestor "$service_path" ||
    ! grep -Fqx '# Managed by Cockpit App Manager' "$service_path" ||
    ! grep -Fqx 'Description=Cockpit App Manager shared npm registry cache' "$service_path" ||
    ! grep -Fqx 'User=cockpit-am-npm-cache' "$service_path"; then
    fail "Refusing to remove an unverified registry cache service."
  fi
  command -v systemctl >/dev/null 2>&1 || fail "systemctl is required to stop the registry cache."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log 'DRY-RUN: systemctl disable --now cockpit-app-manager-npm-cache.service'
    log "DRY-RUN: remove $service_path"
    return 0
  fi
  systemctl disable --now cockpit-app-manager-npm-cache.service >/dev/null || fail "Could not stop the registry cache."
  rm -f -- "$service_path"
  systemctl daemon-reload || fail "Could not reload systemd after removing the registry cache service."
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
  remove_auto_deploy_units
  remove_npm_cache_unit
  remove_default_plugin_files
  log 'Finished App Manager plugin uninstallation.'
fi
