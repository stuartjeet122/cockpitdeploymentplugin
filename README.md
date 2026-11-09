# Cockpit App Manager

App Manager is a Cockpit navigation plugin with a Python command-line backend for registering and operating multiple web applications on an Ubuntu host.

## Installation

Review `config/defaults.json`, then install from the project directory:

```bash
sudo bash ./scripts/install.sh
```

The installer installs the Cockpit assets under `/usr/share/cockpit/app-manager`, the backend and adapter module under `/usr/libexec/cockpit`, and state under `/var/lib/cockpit/app-manager`. Cockpit invokes the backend with its privileged prompt; deployment operations require that privilege.

The backend stores its registry at `/var/lib/cockpit/app-manager/apps.json` and configuration at `/var/lib/cockpit/app-manager/config.json`. Registry/configuration JSON is atomically replaced and protected by file locks.

## Uninstall

Use the uninstall helper to remove App Manager safely without disturbing the existing Nginx configuration, Cloudflare Tunnel, or the root website.

```bash
sudo bash ./scripts/uninstall.sh
```

Default behavior removes only App Manager-owned Cockpit plugin and backend files. It preserves application source repositories, production deployments under `/opt/webapps`, and the existing server infrastructure.

For a full destructive removal of App Manager-managed application data, run the explicit cleanup path with confirmation:

```bash
sudo bash ./scripts/uninstall.sh --remove-managed-data --yes
```

To preview the actions without deleting anything:

```bash
sudo bash ./scripts/uninstall.sh --dry-run
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
- Source trees, releases, application configuration, and logs use app-specific paths. Deployment history and retained releases support rollback.
- The backend does not edit an existing Nginx server configuration or Cloudflare Tunnel configuration. URL-prefix routing must be installed and verified separately; the presence of `templates/nginx.conf.tpl` does not mean routing is active.
- Removing an app unregisters it and stops/removes its App Manager service, but intentionally retains source and release data.

Applications must provide an appropriate start entry point. Framework adapters install/build dependencies and use a conventional start command; applications with nonstandard startup behavior can supply `build_config.start_command` as an array of executable arguments (not a shell string). The app must listen on the assigned `PORT` and on `127.0.0.1`. Health checks use the configured local path and require a 2xx response.

## Development tests

```bash
python -m unittest discover -s tests -v
```

The test suite covers validation, framework detection, JSON/locking behavior, concurrent port allocation, health checks, fast-forward Git updates, and subprocess failures. Systemd, account creation, and live deployment behavior must additionally be validated on a disposable Ubuntu host.
