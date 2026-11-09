#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DEST="${APP_MANAGER_PLUGIN_DEST:-/usr/share/cockpit/app-manager}"
BACKEND_DEST="${APP_MANAGER_BACKEND_DEST:-/usr/libexec/cockpit}"
CONFIG_DEST="${APP_MANAGER_CONFIG_DEST:-/etc/cockpit/app-manager}"
STATE_ROOT="${APP_MANAGER_STATE_ROOT:-/var/lib/cockpit/app-manager}"
SOURCE_ROOT="${APP_MANAGER_SOURCE_ROOT:-${STATE_ROOT}/sources}"
PRODUCTION_ROOT="${APP_MANAGER_PRODUCTION_ROOT:-/opt/webapps}"
LOG_ROOT="${APP_MANAGER_LOG_ROOT:-/var/log/cockpit-app-manager}"

fail() {
  printf 'Installation failed: %s\n' "$*" >&2
  exit 1
}

require_directory_path() {
  local path="$1"
  local parent="$path"
  while [[ -n "$parent" && "$parent" == */* ]]; do
    parent="${parent%/*}"
    [[ -n "$parent" ]] || parent="/"
    [[ -L "$parent" ]] && fail "Refusing to use a symlink in an installation path: $parent"
    [[ "$parent" == "/" ]] && break
  done
  [[ -L "$path" ]] && fail "Refusing to use a symlink as an installation directory: $path"
}

preflight() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "Run this installer as root (for example: sudo bash ./scripts/install.sh)."
  [[ -f /etc/os-release ]] || fail "Cannot identify the host operating system."
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || fail "Ubuntu Server is required; found ${ID:-unknown}."

  local required_command
  for required_command in install python3 dpkg-query dpkg cockpit-bridge systemctl; do
    command -v "$required_command" >/dev/null 2>&1 || fail "Required command is missing: $required_command"
  done
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' ||
    fail "Python 3.9 or newer is required."

  local cockpit_version
  cockpit_version="$(dpkg-query -W -f='${Version}' cockpit-bridge 2>/dev/null)" ||
    fail "Cockpit is not installed (missing cockpit-bridge package)."
  dpkg --compare-versions "$cockpit_version" ge 318 ||
    fail "Cockpit $cockpit_version is too old; this plugin requires Cockpit 318 or newer."

  [[ -f "$APP_DIR/cockpit/manifest.json" ]] || fail "Cockpit plugin manifest is missing."
  [[ -f "$APP_DIR/cockpit/index.html" ]] || fail "Cockpit plugin entry point is missing."
  [[ -f "$APP_DIR/backend/app_manager.py" && -f "$APP_DIR/backend/adapters.py" ]] ||
    fail "Required backend modules are missing."
  [[ -f "$APP_DIR/config/defaults.json" ]] || fail "Default configuration is missing."
  [[ -f "$APP_DIR/templates/nginx.conf.tpl" && -f "$APP_DIR/templates/app.service.tpl" ]] ||
    fail "Deployment templates are missing."
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import app_manager, adapters' "$APP_DIR/backend" ||
    fail "Backend modules cannot be imported from the project."

  require_directory_path "$PLUGIN_DEST"
  require_directory_path "$BACKEND_DEST"
  require_directory_path "$CONFIG_DEST"
  require_directory_path "$STATE_ROOT"
  require_directory_path "$SOURCE_ROOT"
  require_directory_path "$PRODUCTION_ROOT"
  require_directory_path "$LOG_ROOT"
}

install_files() {
  install -d -o root -g root -m 0755 "$PLUGIN_DEST"
  # /usr/libexec/cockpit is shared with Cockpit; create it if absent but never
  # change its ownership or permissions.
  mkdir -p -- "$BACKEND_DEST"
  install -d -o root -g root -m 0755 "$CONFIG_DEST" "$CONFIG_DEST/templates"

  # State is traversable by dedicated app users but not listable. Registry and
  # configuration remain root-readable only.
  install -d -o root -g root -m 0711 "$STATE_ROOT" "$SOURCE_ROOT"
  chmod 0711 "$STATE_ROOT" "$SOURCE_ROOT"
  mkdir -p -- "$PRODUCTION_ROOT"
  install -d -o root -g root -m 0755 "$LOG_ROOT"

  install -o root -g root -m 0644 "$APP_DIR/cockpit/index.html" "$PLUGIN_DEST/index.html"
  install -o root -g root -m 0644 "$APP_DIR/cockpit/manifest.json" "$PLUGIN_DEST/manifest.json"
  if [[ -f "$APP_DIR/cockpit/app-manager.js" ]]; then
    install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.js" "$PLUGIN_DEST/app-manager.js"
  fi

  install -o root -g root -m 0755 "$APP_DIR/backend/app_manager.py" "$BACKEND_DEST/app-manager"
  install -o root -g root -m 0644 "$APP_DIR/backend/adapters.py" "$BACKEND_DEST/adapters.py"
  install -o root -g root -m 0644 "$APP_DIR/config/defaults.json" "$CONFIG_DEST/defaults.json"
  install -o root -g root -m 0644 "$APP_DIR/templates/nginx.conf.tpl" "$CONFIG_DEST/templates/nginx.conf.tpl"
  install -o root -g root -m 0644 "$APP_DIR/templates/app.service.tpl" "$CONFIG_DEST/templates/app.service.tpl"

  if [[ -L "$STATE_ROOT/apps.json" || -L "$STATE_ROOT/config.json" ]]; then
    fail "Registry and configuration files must not be symlinks."
  fi
  if [[ ! -e "$STATE_ROOT/apps.json" ]]; then
    install -o root -g root -m 0600 /dev/null "$STATE_ROOT/apps.json"
    printf '{\n  "apps": []\n}\n' >"$STATE_ROOT/apps.json"
  fi
  if [[ ! -e "$STATE_ROOT/config.json" ]]; then
    install -o root -g root -m 0600 "$CONFIG_DEST/defaults.json" "$STATE_ROOT/config.json"
  fi
  chown root:root "$STATE_ROOT/apps.json" "$STATE_ROOT/config.json"
  chmod 0600 "$STATE_ROOT/apps.json" "$STATE_ROOT/config.json"

  # Execute the installed entry point so its relative adapters import is
  # verified from the installation directory, not only from the source tree.
  python3 "$BACKEND_DEST/app-manager" --help >/dev/null ||
    fail "The installed backend or one of its imports failed."
  [[ -r "$BACKEND_DEST/adapters.py" && -x "$BACKEND_DEST/app-manager" ]] ||
    fail "Installed backend files have incorrect permissions."
}

preflight
install_files

printf '\nCockpit App Manager installed.\n'
printf 'Plugin directory: %s\n' "$PLUGIN_DEST"
printf 'Backend command: %s\n' "$BACKEND_DEST/app-manager"
printf 'State root: %s\n' "$STATE_ROOT"
printf 'Cockpit privilege escalation: required by the plugin when launching the backend.\n'
