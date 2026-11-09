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
- `auto` — uses the framework detector before deployment.

If detection is uncertain or the app uses a nonstandard startup pattern, the operator should supply a matching `build_config.start_command` rather than silently guessing.

## Backend development conventions

- All CLI commands must emit only JSON to stdout.
- Errors must be delivered on stderr and accompanied by a structured JSON failure object on stdout when the command exits non-zero.
- All registry and config writes must be atomic JSON writes.
- Registry and config reads must validate structure to handle corrupted files safely.
- Use subprocess argument arrays (`subprocess.run([...])`) and enforce timeouts for all command execution.
- Git and application build commands must run as the dedicated unprivileged service account, never as root.
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

## Deployment architecture

- Sources live under `/var/lib/cockpit/app-manager/sources/<app-id>/`.
- Production releases live under `/opt/webapps/<app-id>/releases/`.
- The active release is symlinked at `/opt/webapps/<app-id>/current`.
- Each app gets its own service account, systemd unit, logs, config directory, and loopback-bound runtime port.
- Dynamic apps run on `127.0.0.1` only, never on a public interface.
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
- Validate JSON behavior, registry safety, locking, port allocation, Git update safety, and framework detection.
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
