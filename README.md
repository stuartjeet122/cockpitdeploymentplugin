# Cockpit App Manager

App Manager is a Cockpit navigation plugin with a Python command-line backend for registering and operating multiple web applications on an Ubuntu host.

The native Cockpit interface opens on Applications, with search, status filters, and a guided repository, configuration, and review flow. Environment variables can be saved before the first deployment; saved values are masked until explicitly revealed. Service controls, health checks, logs, and deployment settings remain available without leaving Cockpit. Only the current release and latest deployment record are kept. Deployments run independently of the browser session, so the live log can be reopened after a refresh; an active deployment can be aborted from the log dialog.

The installer enables a systemd timer that checks each repository branch for applications that have been deployed at least once, then deploys new commits using the same fast-forward-only Git update safety. Deploy an application once manually to establish its initial release; subsequent repository updates deploy automatically every five minutes. The timer runs on the host and does not depend on the Cockpit page being open.

The header's **Check for updates** action checks the fixed upstream GitHub `main` branch. Checking alone does not restart anything. If a newer revision is available, **Install update and restart Cockpit** downloads the source as the unprivileged `nobody` account and invokes the project's installer as root. Only after a successful installation does it schedule `cockpit.service` to restart after five seconds, allowing the result to reach the browser first. Cockpit sessions may disconnect briefly; running applications are not restarted. The page then reloads to load the installed assets. If restart scheduling fails, the UI reports that installation succeeded but an administrator must restart Cockpit manually. The updater does not modify managed application data or host website configuration.

## Installation

Review `config/defaults.json`, then install from the project directory:

```bash
sudo bash ./scripts/install.sh
```

The installer requires Ubuntu, root privileges, Python 3.9 or newer, systemd, Cockpit 318 or newer, Nginx, Git, `runuser`, and `ss` from `iproute2`. Install the runtime and package manager needed by each application separately, including `python3-venv` for Python apps. Preflight rejects missing host tools before installation. It checks backend imports both before and after installation. It installs the self-contained stylesheet and offline Lucide icons with the other Cockpit assets under `/usr/share/cockpit/app-manager`, the backend and adapter module under the dedicated `/usr/libexec/cockpit/app-manager` directory, and state under `/var/lib/cockpit/app-manager`. No CDN or Node.js runtime is needed by the plugin UI itself. It does not recursively change ownership of shared Cockpit directories or `/opt/webapps`, and it preserves existing registry/configuration data on repeat installation.

The UI requests Cockpit's required superuser mode when launching the backend. The user must be authorized for Cockpit privilege escalation by the host's existing polkit/Cockpit policy; the installer does not grant extra users system-wide privileges. Registry and runtime configuration files are mode `0600`; state/source parent directories are not listable by application accounts.

The backend stores its registry at `/var/lib/cockpit/app-manager/apps.json` and configuration at `/var/lib/cockpit/app-manager/config.json`. Registry/configuration JSON is atomically replaced and protected by file locks. App environment files are stored outside source and release trees at `/var/lib/cockpit/app-manager/environment/<app-id>.env`, owned by root and readable only by that application's service group. The UI shows one key/value row per variable and imports `.env`, `.env.*`, or text files with one assignment per line. Values are applied by systemd when the application starts; restart or redeploy after editing.

Each application has a configurable single-prefix route such as `/shop/`. When a manual or automatic deployment starts, App Manager writes its Nginx snippet to `/etc/nginx/snippets/cockpit-app-manager/<app-id>.conf` and adds a marked include to the unique HTTPS `server` block whose `server_name` exactly matches `stuserver.online`. It selects the block from `nginx -T`, saves a private pre-change backup under `/var/lib/cockpit/app-manager/nginx-backups`, runs `nginx -t`, verifies the generated snippet is loaded, and reloads Nginx. If the matching server block is missing or ambiguous, or validation/reload fails, deployment reports the error and restores the edited configuration and snippet. No other virtual host or root-site directives are changed. The public URL and Open action are withheld until route activation succeeds.

```nginx
include /etc/nginx/snippets/cockpit-app-manager/*.conf;
```

App Manager does not create a new virtual host or change Cloudflare Tunnel settings. Removing an application removes only its verified App Manager snippet and reloads Nginx if that snippet was active; the managed include remains harmlessly in the site's server block for future deployments.

## First deployment

1. Open **App Manager** in Cockpit and select **New application**. Supply the HTTPS or SSH repository URL, application name, and production branch. Configure private-repository access for the dedicated application account on the host; never embed credentials in a repository URL.
2. Select the framework or automatic detection, a unique application ID, and a public prefix such as `/shop/`. Set a health path that returns 2xx without authentication or redirects. Add the environment variables needed during build and runtime. Advanced configuration accepts a relative Next.js app directory and an explicit start command as a JSON argument array.
3. Review the public route, source, and environment variable names. Leave **Deploy now** enabled to save and deploy, or disable it to register without starting a deployment. Follow the deployment log; errors leave the last working release active when recovery succeeds.
4. Open the public application only after activation succeeds. Editing a route does not move the active deployment immediately; redeploy to activate the new route. Environment edits require a restart or redeployment to affect a running process.

## Uninstall

Use the uninstall helper to remove App Manager safely without disturbing the existing Nginx configuration, Cloudflare Tunnel, or the root website.

```bash
sudo bash ./scripts/uninstall.sh
```

Default behavior removes only verified App Manager Cockpit plugin, backend, and template files. Unknown files and non-empty shared directories are preserved. It also preserves application source repositories, production deployments under `/opt/webapps`, and the existing server infrastructure. The managed include line and its private pre-change backup are retained; removing those shared site integration files is an explicit manual administrator action.

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
deploy <app-id> | deployment-start <app-id> | deployment-abort <app-id> <deployment-id>
deployment-logs <app-id> [deployment-id] | auto-deploy
pull <app-id>
start <app-id> | stop <app-id> | restart <app-id>
logs <app-id> [--lines N] | health <app-id>
env-get <app-id> | env-save <app-id>  # env-save reads content from stdin
remove <app-id>
deployments | settings [<json>]
plugin-update-check | plugin-update
action <action> <app-id>
```

Project conventions and deployment requirements are documented in `AGENTS.md` and must be followed for all future changes.

`action` supports the operations used by the Cockpit UI: `open`, `deploy`, `pull`, `start`, `stop`, `restart`, `logs`, `remove`, and `health`.

## Deployment and safety

- Ports are allocated from 8100–8999. Port 8080 is excluded. Registration holds the registry lock while checking existing allocations and listening sockets and recording the new port.
- Git remotes must use HTTPS or SSH. Updates reject local changes and branch changes, fetch the selected branch, and only merge with `--ff-only`.
- Each app uses a dedicated system account named `app-<app-id>`. Git operations, dependency installation, and build commands run as that account, never as root. The generated systemd service also uses that account. Runtime startup must pass a local health check and an actual TCP listener check; public IPv4 or IPv6 bindings on the assigned port are rejected and stopped before the new route is activated.
- Generated systemd services drop all capabilities and enable filesystem, device, kernel, namespace/personality, and privilege-escalation hardening. Release files are root-owned and read-only; static release content is readable by Nginx. Service writes are limited to that app's private home, config, and log paths. App ports are unprivileged, so `CAP_NET_BIND_SERVICE` is not granted.
- Source trees, releases, application configuration, and logs use app-specific paths. Environment secrets are atomically saved outside source checkouts in root-owned files with app-group-only read access. Only the latest deployment record and current release are retained.
- The only shared Nginx change is the validated, backed-up managed include described above; root-site directives and Cloudflare Tunnel configuration are preserved. Each owned snippet redirects `/app` to `/app/` without losing query parameters. Next.js receives `APP_BASE_PATH` during build and runtime and keeps that prefix upstream. Go, Node.js, and Python proxy routes strip the prefix upstream and send `X-Forwarded-Prefix` plus `APP_BASE_PATH`; root-relative redirects, loopback upstream redirects, and cookie paths are rewritten under the application prefix. Application-generated asset URLs and absolute public URLs must also honor the prefix. Static routes serve assets directly from the release tree and fall back to `index.html` for nested pages; no app service or Python HTTP server is created by default. WebSocket requests receive `Connection: upgrade`; ordinary HTTP requests omit the `Connection` header. This per-location handling requires no global `map`.
- Removing an app unregisters it, removes its verified App Manager route snippet, and stops/removes its App Manager service, but intentionally retains source and release data.

Applications must provide an appropriate start entry point. Framework adapters install/build dependencies and use a conventional start command; applications with nonstandard startup behavior must supply `build_config.start_command` as an array of executable arguments (not a shell string). Commands are not run through a shell: shell expansion, pipes, and command substitution are not available. Dynamic apps must read the assigned `PORT` and bind to `127.0.0.1`. Health checks use the configured local path and require a 2xx response, not a login redirect. A static health result verifies the deployed entry point exists, not external network reachability. No adapter can guarantee that an arbitrary repository, missing secret, incompatible runtime, or third-party dependency will deploy successfully.

| Framework | Default startup contract |
| --- | --- |
| Next.js | A production Next.js app with a valid `build` script and the configured public base path. Static export output is not supported by this runtime adapter. |
| Node.js | A `scripts.start` entry or explicit start command. The package manager comes from the lockfile; build dependencies are installed before building. `HOST`, `PORT`, and `NODE_ENV=production` are supplied without guessing CLI flags. |
| Go | A buildable main package at the repository root (`go build .`). The executable must honor the supplied host and port environment. |
| Python | A WSGI `app:app` entry, served by the release virtual environment's Gunicorn. ASGI apps, Django, and other module layouts require an explicit compatible start command and dependencies. |
| Static | An `index.html` at the repository root, served directly by Nginx. An explicit custom start command opts into a runtime service. |

Next.js configuration must resolve to `basePath: process.env.APP_BASE_PATH || ""` and `trailingSlash: true`, including API routes and `/_next/` assets. If no Next.js config exists, the adapter creates these defaults in the release copy only. An existing config is preserved and checked through Next.js before the build; incompatible base paths, trailing-slash behavior, and `output: "export"` fail with a diagnostic instead of silently publishing broken routes. Next.js local health URLs include the application prefix.

Next.js deployments detect a unique app in the repository or accept its relative path in `build_config.app_directory` (for example, `apps/web`). The conventional Next.js `app/` source-code directory does not change the package root: npm runs from the directory containing `package.json`. npm workspaces use the workspace root lockfile while keeping build and runtime working directories at the selected app's `package.json`. The adapter selects npm, pnpm, Yarn, or Bun from the applicable lockfile; `npm-shrinkwrap.json` takes precedence over `package-lock.json`. npm uses `npm ci` only with a readable, valid npm lockfile and `npm install` otherwise; malformed or unsupported lockfiles fail with an explicit diagnostic. Build copies are group-readable/writable by the isolated app account, while the cloned source remains unchanged. Preflight logs include resolved paths, file metadata, lockfile version, exact command and working directory; failed deployment logs are retained even when the failed release copy is cleaned up.

## Development tests

The generated offline icon bundle is included for host installation. To rebuild it and run frontend tests, use a current Node.js LTS release:

```bash
npm ci
npm run build:icons
npm run test:ui
npm run lint
python -m unittest discover -s tests -q
```

Python tests cover validation, framework detection, JSON/locking behavior, concurrent port allocation, health checks, safe settings rejection, fast-forward Git updates, deployment recovery, static activation, listener isolation, plugin update/restart failures, installer ownership boundaries, generated systemd hardening, Nginx prefix/WebSocket behavior, and uninstall safety. Frontend tests exercise guided setup, environment masking and stdin submission, registration retries, search/filter behavior, duplicate deployment prevention, and update/restart feedback.

POSIX shell and file-permission integration tests are skipped on Windows. Local unit tests and mocked browser tests are not a production deployment certification. Before production use, validate on a disposable Ubuntu host: Cockpit privilege escalation; account and systemd sandbox setup; real Nginx syntax, routing, assets, cookies, redirects and WebSockets; one deployment per supported framework; health-failure recovery and cancellation; automatic deployments; and plugin updates reconnecting after the Cockpit restart. Confirm the existing root site and `stuserver-websites` service remain unchanged throughout.
