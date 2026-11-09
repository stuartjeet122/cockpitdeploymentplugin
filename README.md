# Cockpit App Manager

App Manager is a Cockpit navigation plugin with a Python command-line backend for registering and operating multiple web applications on an Ubuntu host.

## Installation

Review `config/defaults.json`, then install from the project directory:

```bash
sudo bash ./scripts/install.sh
```

The installer requires Ubuntu, root privileges, Python 3.9 or newer, systemd, and Cockpit 318 or newer. It checks backend imports both before and after installation. It installs the Cockpit assets under `/usr/share/cockpit/app-manager`, the backend and adapter module as exact files under the shared `/usr/libexec/cockpit` directory, and state under `/var/lib/cockpit/app-manager`. It does not recursively change ownership of shared Cockpit directories or `/opt/webapps`, and it preserves existing registry/configuration data on repeat installation.

The UI requests Cockpit's required superuser mode when launching the backend. The user must be authorized for Cockpit privilege escalation by the host's existing polkit/Cockpit policy; the installer does not grant extra users system-wide privileges. Registry and runtime configuration files are mode `0600`; state/source parent directories are not listable by application accounts.

The backend stores its registry at `/var/lib/cockpit/app-manager/apps.json` and configuration at `/var/lib/cockpit/app-manager/config.json`. Registry/configuration JSON is atomically replaced and protected by file locks.

## Uninstall

Use the uninstall helper to remove App Manager safely without disturbing the existing Nginx configuration, Cloudflare Tunnel, or the root website.

```bash
sudo bash ./scripts/uninstall.sh
```

Default behavior removes only App Manager-owned Cockpit plugin and backend files. It preserves application source repositories, production deployments under `/opt/webapps`, and the existing server infrastructure.

For full destructive removal, use the explicit cleanup path. It removes only registry-identified app data, verified dedicated `app-<id>` accounts, positively marked App Manager service units, and positively marked snippets that are not referenced by active Nginx configuration. Unverified or active snippets block cleanup of that app and its account/service; Nginx configuration is never edited:

```bash
sudo bash ./scripts/uninstall.sh --remove-managed-data --yes
```

To preview the destructive cleanup plan without deleting anything:

```bash
sudo bash ./scripts/uninstall.sh --remove-managed-data --dry-run
```

Never run the destructive uninstall path unless you intentionally want to remove App Manager-managed application state and deployed releases.

## Backend commands

The executable returns one JSON object on stdout for successful and failed operations; diagnostic errors are also written to stderr. Commands include:

```text
list | overview | add <json> | edit <app-id> <json>
deploy <app-id> | pull <app-id>
start <app-id> | stop <app-id> | restart <app-id>
logs <app-id> [--lines N] | health <app-id>
remove <app-id> | rollback <app-id> [release-id]
deployments | settings [<json>]
action <action> <app-id>
```

Project conventions and deployment requirements are documented in `AGENTS.md` and must be followed for all future changes.

`action` supports the operations used by the Cockpit UI: `open`, `deploy`, `pull`, `start`, `stop`, `restart`, `logs`, `remove`, `health`, and `rollback`.

## Deployment and safety

- Ports are allocated from 8100–8999. Port 8080 is excluded. Registration holds the registry lock while checking existing allocations and listening sockets and recording the new port.
- Git remotes must use HTTPS or SSH. Updates reject local changes and branch changes, fetch the selected branch, and only merge with `--ff-only`.
- Each app uses a dedicated system account named `app-<app-id>`. Git operations, dependency installation, and build commands run as that account, never as root. The generated systemd service also uses that account and binds framework defaults to loopback where the framework supports it.
- Generated systemd services drop all capabilities and enable filesystem, device, kernel, namespace/personality, and privilege-escalation hardening. The deployed release is read-only at runtime; writes are limited to that app's private home, config, and log paths. App ports are unprivileged, so `CAP_NET_BIND_SERVICE` is not granted.
- Source trees, releases, application configuration, and logs use app-specific paths. Deployment history and retained releases support rollback.
- The backend does not edit an existing Nginx server configuration or Cloudflare Tunnel configuration. The generated Nginx location redirects `/app` to `/app/`. Next.js receives the full path prefix and must be built with `basePath: "/<app-id>"`. Go, Node.js, and Python proxy routes strip the prefix upstream and send `X-Forwarded-Prefix` plus `APP_BASE_PATH` so application routers and generated URLs can honor it. Static routes serve the release tree directly and fall back to `index.html` for nested pages. The proxy emits the WebSocket `Upgrade` header and a fixed `Connection: upgrade` header without requiring a global `map` or editing unrelated Nginx config; ordinary requests continue to be proxied (with upstream keep-alive disabled). Operators must integrate and validate snippets in the intended Nginx `server` block themselves.
- Removing an app unregisters it and stops/removes its App Manager service, but intentionally retains source and release data.

Applications must provide an appropriate start entry point. Framework adapters install/build dependencies and use a conventional start command; applications with nonstandard startup behavior can supply `build_config.start_command` as an array of executable arguments (not a shell string). The app must listen on the assigned `PORT` and on `127.0.0.1`. Health checks use the configured local path and require a 2xx response.

## Development tests

```bash
python -m unittest discover -s tests -v
```

The test suite covers validation, framework detection, JSON/locking behavior, concurrent port allocation, health checks, fast-forward Git updates, subprocess failures, installer ownership boundaries, generated systemd hardening, Nginx prefix/WebSocket behavior, and uninstall safety. Systemd, Cockpit privilege escalation, account creation, and live deployment behavior must additionally be validated on a disposable Ubuntu host.
