#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DEST="/usr/share/cockpit/app-manager"
BACKEND_DEST="/usr/libexec/cockpit"
CONFIG_DEST="/etc/cockpit/app-manager"
STATE_ROOT="/var/lib/cockpit/app-manager"
PRODUCTION_ROOT="/opt/webapps"

sudo install -d "$PLUGIN_DEST" "$BACKEND_DEST" "$CONFIG_DEST" "$STATE_ROOT/sources" "$PRODUCTION_ROOT"

sudo cp -a "$APP_DIR/cockpit/." "$PLUGIN_DEST/"
sudo install -m 0755 "$APP_DIR/backend/app_manager.py" "$BACKEND_DEST/app-manager"
sudo install -m 0755 "$APP_DIR/backend/adapters.py" "$BACKEND_DEST/app-manager-adapters.py"

sudo install -d "$CONFIG_DEST/templates"
sudo cp -a "$APP_DIR/config/." "$CONFIG_DEST/"
sudo cp -a "$APP_DIR/templates/." "$CONFIG_DEST/templates/"

if [ ! -f "$STATE_ROOT/apps.json" ]; then
  sudo tee "$STATE_ROOT/apps.json" >/dev/null <<'JSON'
{
  "apps": []
}
JSON
fi

if [ ! -f "$STATE_ROOT/config.json" ]; then
  sudo cp "$CONFIG_DEST/defaults.json" "$STATE_ROOT/config.json"
fi

sudo chown -R root:root "$PLUGIN_DEST" "$BACKEND_DEST" "$CONFIG_DEST" "$STATE_ROOT" "$PRODUCTION_ROOT"

printf '\nCockpit App Manager installed.\n'
printf 'Plugin directory: %s\n' "$PLUGIN_DEST"
printf 'Backend command: %s\n' "$BACKEND_DEST/app-manager"
printf 'State root: %s\n' "$STATE_ROOT"
