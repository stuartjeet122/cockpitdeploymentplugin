# Cockpit App Manager

A native Cockpit plugin for managing websites and web applications on Ubuntu Server while preserving the existing root website and Cloudflare Tunnel setup.

## Goals

- Integrate directly into Cockpit navigation.
- Manage applications by URL prefix rather than hostname/subdomain.
- Keep the existing root website at `https://stuserver.online/` untouched.
- Maintain a separate registry that does not mutate the existing Go registry file.
- Support a modular adapter for Next.js, Go, Node.js, Python, and static deployments.

## Recommended installation

1. Review the configuration defaults in `config/defaults.json`.
2. Run the installer:

```bash
sudo bash ./scripts/install.sh
```

3. Reload Cockpit or open `https://<server>:9090/cockpit/@localhost/app-manager/index.html`

## Architecture overview

- `cockpit/` — Cockpit UI assets (`manifest.json`, `index.html`, `app-manager.js`)
- `backend/` — Python backend implementing registry management, Git operations, security checks, and adapters
- `templates/` — example service and NGINX snippets
- `config/` — default configuration and registry template
- `tests/` — unit tests for framework detection and registry logic

## Safe operations

- All external commands execute as argument arrays, never by concatenating user input into shell strings.
- Git operations refuse to overwrite uncommitted changes or a diverged working tree.
- The plugin only reads the legacy registry file for discovery and never mutates it.
- Destructive commands such as `git reset --hard` are intentionally avoided.

## Supported frameworks

- Next.js
- Go
- Node.js
- Python
- Static HTML/CSS/JavaScript

## Deployment model

Applications use a release directory layout under `/opt/webapps/<app-id>/` with `releases/` and a `current` symlink. The plugin keeps a source clone under `/var/lib/cockpit/app-manager/sources/<app-id>/` so Git updates can be fetched without disturbing the active release.

## Security and operational restrictions

- Applications bind to `127.0.0.1` only.
- Ports are allocated from `8100-8999` only.
- Port `8080` is never assigned to a new app because it is already used by the existing root website.
- No credentials or SSH private keys are stored in the app registry.
- Only explicitly allowed source roots are permitted: `/opt`, `/home/sjeetoo`, and `/srv`.

## Testing

```bash
npm test
```

or

```bash
python -m unittest discover -s tests -v
```
