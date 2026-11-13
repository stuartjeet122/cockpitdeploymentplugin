# AGENTS.md

## Project architecture

This repository is a native Cockpit plugin for hosting multiple web apps on an Ubuntu Server host without disrupting the existing site infrastructure. The app is split into three layers:

- Cockpit UI assets in `cockpit/` that load under the Cockpit navigation menu as `App Manager`.
- Python backend logic in `backend/app_manager.py` that implements validation, registry management, deployment, and system service control.
- Framework-specific deployment behavior in `backend/adapters.py` for Next.js, Go, Node.js, Python, and static HTML projects.

The backend is designed to run as a privileged Cockpit helper. It stores state under `/var/lib/cockpit/app-manager` and exposes a JSON-only CLI that the Cockpit UI shells out to.

## Supported application frameworks

The project supports the following frameworks and fallback detection rules:

- `nextjs` — detected via `next.config.*` or a `next` dependency in `package.json`.
- `go` — detected via `go.mod`.
- `node` — detected via `package.json` when not a Next.js app.
- `python` — detected via `requirements.txt` or `pyproject.toml`.
- `static` — detected via `index.html`.
- `docker` — detected via a root-level `Dockerfile` or Compose manifest (`compose.yaml`, `compose.yml`, `docker-compose.yaml`, or `docker-compose.yml`); select it explicitly for custom paths.
- `auto` — uses the framework detector before deployment.

Dockerfile deployments use `build_config.dockerfile`, `build_config.app_directory`, and `build_config.container_port`; the Dockerfile's `CMD` or `ENTRYPOINT` starts the app. Compose deployments use `build_config.compose_file`, `build_config.compose_service`, `build_config.app_directory`, and `build_config.container_port`. Compose projects are managed as one app-scoped project: only the selected web service is published on host loopback, while other services remain internal. Host ports, host filesystem mounts, external volumes/networks, host namespaces, and privileged container settings are rejected. Compose updates restart the project in place to preserve app-scoped named volumes; they are not zero-downtime. For other frameworks, if detection is uncertain or the app uses a nonstandard startup pattern, supply a matching `build_config.start_command` rather than silently guessing.

## Backend development conventions

- All CLI commands must emit only JSON to stdout.
- Errors must be delivered on stderr and accompanied by a structured JSON failure object on stdout when the command exits non-zero.
- All registry and config writes must be atomic JSON writes.
- Registry and config reads must validate structure to handle corrupted files safely.
- Use subprocess argument arrays (`subprocess.run([...])`) and enforce timeouts for all command execution.
- Git and non-Docker application build commands must run as the dedicated unprivileged service account. Docker image builds and Compose project operations use the host Docker daemon; never give app accounts access to its root-equivalent socket, and deploy only trusted Dockerfiles or Compose projects.
- Never read or mutate the legacy registry at `/home/stuserver/data/websites.json`.
- Never accept arbitrary shell commands, arbitrary file paths, or unsafe Git URLs.
- Handle missing apps, duplicate registrations, and port conflicts explicitly.

## Frontend development conventions

- The Cockpit UI is plain HTML/JavaScript in `cockpit/`.
- The plugin must remain a native Cockpit integration, not a standalone site.
- UI actions should call the backend with structured JSON input and render backend responses cleanly.
- Keep URL handling path-based (for example `/website1/`, `/api/`, etc.) rather than creating per-process subdomains.

## Security requirements

- Do not store passwords, tokens, or credentials in plain-text registry files.
- Accept only HTTPS or SSH Git remotes; reject dangerous or malformed URLs.
- Prevent path traversal and symlink escape via `validate_allowed_path()` and path-boundary checks.
- Reserve ports from 8100–8999 and never reuse port 8080 or an active listening port.
- Validate branch names and Git repo URLs before cloning or pulling.
- Refuse to overwrite local Git changes. Only fast-forward clean repositories when safe.
- Keep app runtime files under managed directories and restrict file ownership to dedicated service accounts.
- Docker runtime ports must publish to host loopback only; containers drop Linux capabilities, use `no-new-privileges`, and receive configured resource limits.
- Compose projects must not publish host ports or mount host paths; external volumes/networks and host namespace or privileged-container settings are rejected.

## Deployment architecture

- Sources live under `/var/lib/cockpit/app-manager/sources/<app-id>/`.
- Production releases live under `/opt/webapps/<app-id>/releases/`.
- The active release is symlinked at `/opt/webapps/<app-id>/current`.
- Retain only the current release and the latest deployment record for each app; an in-progress deployment is separately abortable.
- A systemd timer checks configured repository branches every five minutes and deploys new commits using the same safe fast-forward workflow.
- Each app gets its own service account, systemd unit, logs, config directory, and loopback-bound runtime port.
- Dynamic non-Docker apps run on `127.0.0.1` only, never on a public interface.
- Docker containers bind their internal service to `0.0.0.0` and are published only on the assigned `127.0.0.1` host port; a root-owned systemd unit invokes Docker without sharing the daemon socket with the app account.
- Static apps should use direct Nginx path routing with the minimum runtime footprint. Do not spin up an unnecessary Python HTTP server unless a deployment explicitly requires it.

## Directory structure

```text
.
├── backend/
│   ├── adapters.py
│   ├── app_manager.py
│   └── __init__.py
├── cockpit/
│   ├── app-manager.js
│   ├── index.html
│   └── manifest.json
├── config/
│   └── defaults.json
├── scripts/
│   └── install.sh
├── templates/
│   ├── auto-deploy.service
│   ├── auto-deploy.timer
│   ├── nginx.conf.tpl
│   └── app.service.tpl
├── tests/
│   └── test_app_manager.py
├── README.md
├── AGENTS.md
├── package.json
└── ...
```

## Testing requirements

- Run the Python unit suite with `python -m unittest discover -s tests -q`.
- Preserve compatibility with Ubuntu Server and Cockpit.
- Validate JSON behavior, registry safety, locking, port allocation, Git update safety, automatic deployment, deployment cancellation, and framework detection.
- Test on a disposable Ubuntu host for live systemd and deployment behaviors before production use.

## Installation restrictions

- Do not replace or overwrite the existing root website, Nginx config, or Cloudflare Tunnel configuration.
- Do not modify the legacy registry `/home/stuserver/data/websites.json` as part of App Manager work.
- Do not use port `8080` for managed apps.
- Do not create conflicting systemd units or change the host’s existing website service state without explicit safe migration steps.
- Preserve the current `stuserver-websites` service and the existing `stuserver.online` site.

## Existing server infrastructure that must never be disrupted

- Ubuntu Server minimal installation
- Cockpit installation
- Nginx installation
- Cloudflare Tunnel configuration
- Existing public site at `https://stuserver.online/`
- Existing Go site on `127.0.0.1:8080`
- Existing Go project at `/opt/stuserver-directory/stuserverwebsiteslist`
- Existing systemd service `stuserver-websites`

The App Manager must coexist with this environment and must route only under application-prefixed paths such as `/website1/` and `/api/` without disturbing the root site or its infrastructure.
