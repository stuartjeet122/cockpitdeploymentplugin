#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DEST="${APP_MANAGER_PLUGIN_DEST:-/usr/share/cockpit/app-manager}"
BACKEND_DEST="${APP_MANAGER_BACKEND_DEST:-/usr/libexec/cockpit/app-manager}"
CONFIG_DEST="${APP_MANAGER_CONFIG_DEST:-/etc/cockpit/app-manager}"
STATE_ROOT="${APP_MANAGER_STATE_ROOT:-/var/lib/cockpit/app-manager}"
SOURCE_ROOT="${APP_MANAGER_SOURCE_ROOT:-${STATE_ROOT}/sources}"
PRODUCTION_ROOT="${APP_MANAGER_PRODUCTION_ROOT:-/opt/webapps}"
LOG_ROOT="${APP_MANAGER_LOG_ROOT:-/var/log/cockpit-app-manager}"
SYSTEMD_DEST="${APP_MANAGER_SYSTEMD_DIR:-/etc/systemd/system}"

fail() {
  printf 'Installation failed: %s\n' "$*" >&2
  exit 1
}

require_directory_path() {
  local path="$1"
  local parent="$path"
  while [[ -n "$parent" && "$parent" == */* ]]; do
    parent="${parent%/*}"
    if [[ -z "$parent" ]]; then
      parent="/"
    fi
    if [[ -L "$parent" ]]; then
      fail "Refusing to use a symlink in an installation path: $parent"
    fi
    if [[ "$parent" == "/" ]]; then
      break
    fi
  done
  if [[ -L "$path" ]]; then
    fail "Refusing to use a symlink as an installation directory: $path"
  fi
  return 0
}

preflight() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "Run this installer as root (for example: sudo bash ./scripts/install.sh)."
  [[ -f /etc/os-release ]] || fail "Cannot identify the host operating system."
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || fail "Ubuntu Server is required; found ${ID:-unknown}."

  local required_command
  for required_command in install python3 dpkg-query dpkg cockpit-bridge systemctl systemd-run ss nginx git runuser setpriv logrotate ssh-keygen; do
    command -v "$required_command" >/dev/null 2>&1 || fail "Required command is missing: $required_command"
  done
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' ||
    fail "Python 3.9 or newer is required."
  local missing_cache_tools=()
  for required_command in node pnpm verdaccio; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
      missing_cache_tools+=("$required_command")
      continue
    fi
    case "$(command -v "$required_command")" in
      /usr/bin/*|/usr/local/bin/*) ;;
      *) fail "$required_command must be installed in /usr/bin or /usr/local/bin for isolated services." ;;
    esac
  done
  if [[ "${#missing_cache_tools[@]}" -gt 0 ]]; then
    fail "Missing shared JavaScript cache tools: ${missing_cache_tools[*]}. Provision system-wide Node.js 18.18+ and npm first if absent. Then, after administrator review, run: sudo npm install --global --prefix /usr/local --ignore-scripts pnpm@10 verdaccio@6 . Verify /usr/local/bin or /usr/bin contains the tools, then retry the plugin update. No application services were changed by this preflight failure."
  fi
  local node_version verdaccio_version
  node_version="$(cd /; node --version)" || fail "Could not query the system-wide Node.js version."
  dpkg --compare-versions "${node_version#v}" ge 18.18.0 ||
    fail "Shared JavaScript cache requires Node.js 18.18 or newer; provision a supported system-wide Node.js release before retrying."
  local pnpm_version
  pnpm_version="$(cd /; pnpm --version)"
  [[ "$pnpm_version" == 10.* ]] && dpkg --compare-versions "$pnpm_version" ge 10.3.0 ||
    fail "Managed builds require pnpm 10.3 or newer within the tested 10.x series."
  verdaccio_version="$(cd /; verdaccio --version)" || fail "Could not query the system-wide Verdaccio version."
  [[ "${verdaccio_version#v}" == 6.* ]] ||
    fail "Shared JavaScript cache requires Verdaccio 6.x; provision the supported major version before retrying."
  [[ -f "$APP_DIR/config/npm-cache.json" && -f "$APP_DIR/templates/npm-cache.service" ]] ||
    fail "Shared registry cache configuration or service template is missing."
  local cache_unit="$SYSTEMD_DEST/cockpit-app-manager-npm-cache.service"
  [[ ! -L "$cache_unit" ]] || fail "Shared cache unit must not be a symlink."
  if [[ -e "$cache_unit" ]]; then
    grep -Fqx '# Managed by Cockpit App Manager' "$cache_unit" &&
      grep -Fqx 'Description=Cockpit App Manager shared npm registry cache' "$cache_unit" ||
      fail "Refusing to overwrite an unmanaged registry cache unit."
  elif [[ -n "$(ss -H -ltn 'sport = :4873')" ]]; then
    fail "Port 4873 is occupied; refusing to replace an existing registry."
  fi
  [[ ! -L "$CONFIG_DEST/npm-cache.json" ]] || fail "Shared cache configuration must not be a symlink."
  if [[ -e "$CONFIG_DEST/npm-cache.json" ]]; then
    grep -Fq '"_comment": "Managed by Cockpit App Manager"' "$CONFIG_DEST/npm-cache.json" ||
      fail "Refusing to overwrite an unmanaged registry cache configuration."
  fi
  [[ "$CONFIG_DEST" == /etc/cockpit/app-manager ]] ||
    fail "Shared registry cache currently requires the standard /etc/cockpit/app-manager config directory."

  local cockpit_version
  cockpit_version="$(dpkg-query -W -f='${Version}' cockpit-bridge 2>/dev/null)" ||
    fail "Cockpit is not installed (missing cockpit-bridge package)."
  dpkg --compare-versions "$cockpit_version" ge 318 ||
    fail "Cockpit $cockpit_version is too old; this plugin requires Cockpit 318 or newer."

  [[ -f "$APP_DIR/cockpit/manifest.json" ]] || fail "Cockpit plugin manifest is missing."
  [[ -f "$APP_DIR/cockpit/index.html" ]] || fail "Cockpit plugin entry point is missing."
  [[ -f "$APP_DIR/cockpit/app-manager.css" ]] || fail "Cockpit plugin stylesheet is missing."
  [[ -f "$APP_DIR/cockpit/app-manager.js" ]] || fail "Cockpit plugin JavaScript is missing."
  [[ -f "$APP_DIR/cockpit/icons.js" ]] || fail "Offline icons are missing; run npm ci and npm run build:icons in the project."
  [[ -f "$APP_DIR/backend/app_manager.py" && -f "$APP_DIR/backend/adapters.py" ]] ||
    fail "Required backend modules are missing."
  [[ -f "$APP_DIR/backend/prepare_next_assets.cjs" ]] || fail "Next.js asset preparation helper is missing."
  [[ -f "$APP_DIR/config/defaults.json" ]] || fail "Default configuration is missing."
  [[ -f "$APP_DIR/templates/nginx.conf.tpl" && -f "$APP_DIR/templates/app.service.tpl" &&
    -f "$APP_DIR/templates/auto-deploy.service" && -f "$APP_DIR/templates/auto-deploy.timer" &&
    -f "$APP_DIR/templates/app-manager.logrotate" ]] ||
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
  require_directory_path "$SYSTEMD_DEST"
  require_directory_path /etc/logrotate.d
  [[ ! -L /etc/logrotate.d/cockpit-app-manager ]] || fail "Log rotation configuration must not be a symlink."
  if [[ -e /etc/logrotate.d/cockpit-app-manager ]]; then
    grep -q '^# Managed by Cockpit App Manager$' /etc/logrotate.d/cockpit-app-manager ||
      fail "Refusing to overwrite an unmanaged log rotation configuration."
  fi
}

install_files() {
  install -d -o root -g root -m 0755 "$PLUGIN_DEST"
  # /usr/libexec/cockpit is shared with Cockpit; create only this plugin's
  # dedicated subdirectory and never change shared directory ownership.
  install -d -o root -g root -m 0755 "$BACKEND_DEST"
  install -d -o root -g root -m 0755 "$CONFIG_DEST" "$CONFIG_DEST/templates"
  install -d -o root -g root -m 0755 "$SYSTEMD_DEST"

  # State is traversable by dedicated app users but not listable. Registry and
  # configuration remain root-readable only.
  install -d -o root -g root -m 0711 "$STATE_ROOT" "$SOURCE_ROOT"
  chmod 0711 "$STATE_ROOT" "$SOURCE_ROOT"
  mkdir -p -- "$PRODUCTION_ROOT"
  install -d -o root -g root -m 0755 "$LOG_ROOT"

  install -o root -g root -m 0644 "$APP_DIR/cockpit/index.html" "$PLUGIN_DEST/index.html"
  install -o root -g root -m 0644 "$APP_DIR/cockpit/manifest.json" "$PLUGIN_DEST/manifest.json"
  install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.css" "$PLUGIN_DEST/app-manager.css"
  install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.js" "$PLUGIN_DEST/app-manager.js"
  install -o root -g root -m 0644 "$APP_DIR/cockpit/icons.js" "$PLUGIN_DEST/icons.js"

  install -o root -g root -m 0755 "$APP_DIR/backend/app_manager.py" "$BACKEND_DEST/app-manager"
  install -o root -g root -m 0644 "$APP_DIR/backend/adapters.py" "$BACKEND_DEST/adapters.py"
  install -o root -g root -m 0644 "$APP_DIR/backend/runtime_checks.py" "$BACKEND_DEST/runtime_checks.py"
  install -o root -g root -m 0644 "$APP_DIR/backend/prepare_next_assets.cjs" "$BACKEND_DEST/prepare_next_assets.cjs"
  install -o root -g root -m 0644 "$APP_DIR/config/defaults.json" "$CONFIG_DEST/defaults.json"
  install -o root -g root -m 0644 "$APP_DIR/templates/nginx.conf.tpl" "$CONFIG_DEST/templates/nginx.conf.tpl"
  install -o root -g root -m 0644 "$APP_DIR/templates/app.service.tpl" "$CONFIG_DEST/templates/app.service.tpl"
  install -o root -g root -m 0644 "$APP_DIR/templates/app-manager.logrotate" /etc/logrotate.d/cockpit-app-manager
  install -o root -g root -m 0644 "$APP_DIR/templates/auto-deploy.service" "$SYSTEMD_DEST/cockpit-app-manager-auto-deploy.service"
  install -o root -g root -m 0644 "$APP_DIR/templates/auto-deploy.timer" "$SYSTEMD_DEST/cockpit-app-manager-auto-deploy.timer"
  install -o root -g root -m 0644 "$APP_DIR/config/npm-cache.json" "$CONFIG_DEST/npm-cache.json"
  install -o root -g root -m 0644 "$APP_DIR/templates/npm-cache.service" "$SYSTEMD_DEST/cockpit-app-manager-npm-cache.service"

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
  systemctl daemon-reload
  systemctl enable --now cockpit-app-manager-npm-cache.service
  systemctl restart cockpit-app-manager-npm-cache.service
  python3 - <<'PY'
import json
import time
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(30):
  try:
    with opener.open("http://127.0.0.1:4873/-/ping", timeout=2) as response:
      json.load(response)
    break
  except Exception as exc:
    if attempt == 29:
      raise SystemExit("Shared registry cache is unavailable; inspect journalctl -u cockpit-app-manager-npm-cache: " + str(exc))
    time.sleep(0.5)
PY
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import app_manager; app_manager._assert_npm_cache_service()' "$APP_DIR/backend" ||
    fail "The shared registry listener is not owned by the managed cache service."
  systemctl enable --now cockpit-app-manager-auto-deploy.timer

  local plugin_commit=""
  if command -v git >/dev/null 2>&1; then
    plugin_commit="$(git -C "$APP_DIR" rev-parse --verify HEAD 2>/dev/null || true)"
  fi
  if [[ ! "$plugin_commit" =~ ^[0-9a-f]{40,64}$ ]]; then
    plugin_commit=""
  fi
  if [[ -L "$STATE_ROOT/plugin-version.json" ]]; then
    fail "Plugin version metadata must not be a symlink."
  fi
  if [[ -e "$STATE_ROOT/plugin-version.json" && ! -f "$STATE_ROOT/plugin-version.json" ]]; then
    fail "Plugin version metadata must be a regular file."
  fi
  local version_tmp="$STATE_ROOT/.plugin-version.json.$$"
  local plugin_version="${APP_MANAGER_RELEASE_VERSION:-}"
  if [[ -n "$plugin_version" && ! "$plugin_version" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    fail "Invalid plugin release version."
  fi
  install -o root -g root -m 0600 /dev/null "$version_tmp"
  if [[ -n "$plugin_commit" ]]; then
    if [[ -n "$plugin_version" ]]; then
      printf '{"commit":"%s","version":"%s"}\n' "$plugin_commit" "$plugin_version" >"$version_tmp"
    else
      printf '{"commit":"%s"}\n' "$plugin_commit" >"$version_tmp"
    fi
  else
    printf '{"commit":null}\n' >"$version_tmp"
  fi
  mv -f -- "$version_tmp" "$STATE_ROOT/plugin-version.json"
}

preflight
install_files

printf '\nCockpit App Manager installed.\n'
printf 'Plugin directory: %s\n' "$PLUGIN_DEST"
printf 'Backend command: %s\n' "$BACKEND_DEST/app-manager"
printf 'State root: %s\n' "$STATE_ROOT"
printf 'Cockpit privilege escalation: required by the plugin when launching the backend.\n'
