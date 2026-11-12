# Cockpit App Manager

App Manager is a Cockpit navigation plugin with a Python command-line backend for registering and operating multiple web applications on an Ubuntu host.

The native Cockpit interface opens on Applications, with search, status filters, and a guided repository, configuration, and review flow. Environment variables can be saved before the first deployment; saved values are masked until explicitly revealed. Service controls, health checks, logs, and deployment settings remain available without leaving Cockpit. Only the current release and latest deployment record are kept. Deployments run independently of the browser session, so the live log can be reopened after a refresh; an active deployment can be aborted from the log dialog.

The installer enables a systemd timer that checks each repository branch for applications that have been deployed at least once. Deploy an application once manually; subsequent updates are checked every five minutes without requiring an open Cockpit page. Each app can pause automatic deployment or restrict it to a UTC window in **Edit configuration**. A window crossing midnight is supported; equal start/end times mean all day. Failures are shown on the application and in the timer's systemd journal, with retry backoff from five minutes to one hour. The hardened deployment service can write only to managed app/state roots, `/etc/nginx`, and `/var/log/nginx` (needed for Nginx's configured error log); it does not change Nginx file ownership or modes. User-namespace isolation is explicitly disabled for this root-owned timer because it must switch to each app's unprivileged service account for Git operations. Pausing auto-deploy does not stop the app or disable manual deployment.

The header's **Check for updates** selects the latest commit on the upstream `main` branch; no release tag or signing key is required. The dialog shows the installed and available commit IDs. Installation clones the full branch as `nobody`, checks that it is still at the selected commit, refuses non-fast-forward updates, and copies it into a root-private staging directory. Before running the installer as root, it checks every staged file against the selected commit's Git blob hashes and rejects unexpected files. Git replacement objects are disabled during verification. Only successful installation schedules a Cockpit restart after five seconds; running applications are not restarted. If restart scheduling fails, the UI reports that installation succeeded but an administrator must restart Cockpit manually.

## Main-branch Update Trust

Main-branch updates are not cryptographically signed: anyone able to change the upstream `main` branch can publish code that the host will install as root. Enable this only if you trust the GitHub repository and maintain strong account security and branch protections. The update verifies the exact commit returned by GitHub over HTTPS, prevents branch rollback/rewrite by requiring a fast-forward from the installed commit, and verifies the downloaded files against that commit's Git object IDs; these checks do not prove who authored or authorized the commit. If signature-based release approval is required, do not use the automatic main-branch updater.

## Installation

Review `config/defaults.json`, then install from the project directory:

```bash
sudo bash ./scripts/install.sh
```

The installer requires Ubuntu, root privileges, Python 3.9 or newer, systemd, Cockpit 318 or newer, Nginx, Git with SSH signing support, `ssh-keygen`, `logrotate`, `runuser`, and `ss` from `iproute2`. Install the runtime and package manager needed by each application separately, including `python3-venv` for Python apps. Preflight rejects missing host tools before installation. It checks backend imports both before and after installation. It installs the self-contained stylesheet and offline Lucide icons with the other Cockpit assets under `/usr/share/cockpit/app-manager`, the backend and adapter module under the dedicated root-owned, readable/traversable `/usr/libexec/cockpit/app-manager` directory, and state under `/var/lib/cockpit/app-manager`. Cockpit plugin/code directories are normalized to mode `0755`, and installed code/assets are readable by Cockpit; sensitive state remains root-only rather than being made globally writable. No CDN or Node.js runtime is needed by the plugin UI itself. It does not recursively change ownership of shared Cockpit directories or `/opt/webapps`, and it preserves existing registry/configuration data on repeat installation.

The UI requests Cockpit's required superuser mode when launching the backend. The user must be authorized for Cockpit privilege escalation by the host's existing polkit/Cockpit policy; the installer does not grant extra users system-wide privileges. Registry and runtime configuration files are mode `0600`; state/source parent directories are not listable by application accounts.

The backend stores its registry at `/var/lib/cockpit/app-manager/apps.json` and configuration at `/var/lib/cockpit/app-manager/config.json`. Registry/configuration JSON is atomically replaced and protected by file locks. App environment files are stored outside source and release trees at `/var/lib/cockpit/app-manager/environment/<app-id>.env`, owned by root and readable only by that application's service group. The UI lets you enter values during setup or import `.env`, `.env.*`, and `.txt` files with one assignment per line; the review shows names only. Values are applied by systemd when the application starts; restart or redeploy after editing.

Each application has a configurable single-prefix route such as `/shop/`. When a manual or automatic deployment starts, App Manager writes its Nginx snippet to `/etc/nginx/snippets/cockpit-app-manager/<app-id>.conf` and adds a marked include to the unique HTTPS `server` block whose `server_name` exactly matches `stuserver.online`. It selects the block from `nginx -T`, saves a private pre-change backup under `/var/lib/cockpit/app-manager/nginx-backups`, runs `nginx -t`, verifies the generated snippet is loaded, and reloads Nginx. If the matching server block is missing or ambiguous, or validation/reload fails, deployment reports the error and restores the edited configuration and snippet. No other virtual host or root-site directives are changed. The public URL and Open action are withheld until route activation succeeds.

```nginx
include /etc/nginx/snippets/cockpit-app-manager/*.conf;
```

App Manager does not create a new virtual host or change Cloudflare Tunnel settings. Removing an application removes only its verified App Manager snippet and reloads Nginx if that snippet was active; the managed include remains harmlessly in the site's server block for future deployments. Removal also permanently deletes that application's managed source, releases, storage, logs, environment secrets, dedicated account, and service files. Export any data you need before confirming removal.

## First deployment

1. Open **App Manager** in Cockpit and select **New application**. Supply the HTTPS or SSH repository URL, application name, and production branch. Configure private-repository access for the dedicated application account on the host; never embed credentials in a repository URL.
2. Select the framework or automatic detection, a unique application ID, and a public prefix such as `/shop/`. Set a health path that returns 2xx without authentication or redirects. Add the environment variables needed during build and runtime. Advanced configuration accepts a relative Next.js app directory and an explicit start command as a JSON argument array.
3. Review the public route, source, and environment variable names. Leave **Deploy now** enabled to save and deploy, or disable it to register without starting a deployment. Follow the deployment log; errors leave the last working release active when recovery succeeds.
4. Open the public application only after activation succeeds. Editing a route does not move the active deployment immediately; redeploy to activate the new route. Environment edits require a restart or redeployment to affect a running process. The deployment log can be copied or downloaded as a `.txt` file from its dialog. For health checks, select a path that returns 2xx without authentication, redirects, or an edge-security challenge.

## Runtime Operations

For a running dynamic application, redeployment reserves a second port and starts an alternate managed systemd service against the new release's absolute path. The current service remains live until candidate health and loopback-listener checks pass. Only then does App Manager switch the active release and Nginx route, retire the previous service, and prune its release. A failed candidate leaves the old service running. This reduces activation downtime but is not a guarantee of uninterrupted long-lived connections; applications must tolerate overlapping instances and handle termination gracefully. First deployments, stopped apps, and static/runtime conversions use the normal activation/recovery flow. Candidate ports remain within 8100-8999 and are reserved against concurrent registration.

Successful deployments delete the managed Git checkout at `sources/<app-id>`. Later checks use `git ls-remote` without recloning; a changed revision triggers a fresh clone. Before building, Git verifies that it descends from the recorded live commit, rejecting rewritten history. Failed checkouts are retained for diagnosis and existing dirty-repository protections still apply. The active runtime release is not deleted: Node.js and Python need application source to execute, and static assets must remain available. This is checkout cleanup, not a promise that interpretable runtime source can be removed or hidden.

**Check health** records independent systemd state, local readiness, public-route reachability, a check time, and failure details. The timer refreshes health for previously deployed apps, including paused ones. Public checks use normal TLS verification and do not follow redirects. They can fail independently of a healthy local application, for example when external DNS, Cloudflare, or authentication blocks the host's request. Status observations older than ten minutes are not used to override saved lifecycle state.

Per-app limits are editable before redeployment: runtime defaults are 1024 MiB memory, 100% CPU quota, and 256 tasks; build defaults are 2048 MiB, 100%, and 512 tasks. Builds run in constrained transient systemd scopes while their commands remain unprivileged. Log rotation checks each application's stdout/stderr files for a 10 MiB threshold on timer runs and daily, retaining five compressed rotations. This is periodic rotation, not a strict instantaneous disk quota. Default uninstall retains rotation rules because running applications and their logs are retained.

## Preflight and Build Capacity

**Run preflight** checks required environment names, free space on the source/production filesystems, repository access/history, framework entry points, package managers, and runtime minimums without activating a release. Every deployment enforces the same checks before building; disk/environment gates also run before cloning. Preflight can create the dedicated account and retain a managed checkout. It never imports Python application code or runs repository build scripts. A passing report does not guarantee dependency downloads, compilation, application behavior, or a not-yet-running health endpoint; candidate health checks still gate activation.

In **Edit configuration**, set required environment names such as `DATABASE_URL, API_KEY` and minimum runtime versions as a JSON object such as `{"node":"20.9","npm":"10"}`. Missing or empty required values fail preflight; reports contain names, never saved values. Supported runtime requirement keys are `node`, `npm`, `pnpm`, `yarn`, `bun`, `go`, and `python3`, with numeric minimums rather than version ranges. Framework floors cannot be lowered by an override. Go's `go.mod` directive is checked; Next.js 16 requires Node 20.9 or newer. Python's default `app:app` entry is inspected syntactically; recognized ASGI applications and Django require an explicit compatible argument-array start command and dependencies. Existing Next.js routing/output configuration is checked during the actual build.

**Settings** controls minimum free disk space (default 512 MiB) and simultaneous builds (default 1, range 1-8). Build slots are shared across manual and automatic deployments, enforced with process locks, and released on failure. Waiting deployments show their queued phase in the live log and remain abortable. Waiting is non-FIFO and times out after one hour. Reducing capacity does not terminate builds already running; new builds wait until the active count falls below the new limit. Activation and runtime services are outside the build slots; per-app CPU, memory, and task limits still apply.

## Persistent Storage

Every app has a `data` directory under `/opt/webapps/<app-id>/shared/`. Add named directories such as `uploads` in **Edit configuration**; only isolated lowercase slug names are accepted, never host mount paths. The directory name `dir` is reserved to avoid an environment-name collision. Dynamic services receive `APP_DATA_DIR`, `APP_STORAGE_DIR`, and `APP_STORAGE_<NAME>` (for example `APP_STORAGE_UPLOADS`). These names are reserved and cannot be replaced through the environment editor. Declared directories are writable by that app's service account while release files remain read-only. Environment paths are runtime values, not build-time substitutions.

Persistent files survive redeployments, source checkout cleanup, release pruning, and ordinary uninstall. Removing a directory name from configuration does not delete its files. Removing an application or using explicit destructive uninstall permanently removes managed app data, including storage. This feature is not a backup system. Storage is not automatically exposed through a public Nginx route; applications must deliberately serve uploaded content. Old and candidate instances share these directories during a deployment, so file formats and database/schema changes must tolerate overlapping versions. Configure storage before deploying or redeploy to apply changes.

## Deployment Approval

Set **Deployment policy** to **Approval required** to replace the card's deployment button with **Review update**. Review shows the live/candidate revisions, up to 20 commit summaries, and a bounded file-change summary. **Approve deployment** starts a background deployment for exactly that reviewed commit. Changed repository, branch, live revision, or remote HEAD invalidates the approval; review again rather than silently deploying a different revision. Direct deployment commands cannot bypass approval mode.

For previously deployed apps, the timer detects updates in approval mode and records a pending review instead of building. Automatic-deployment pause/windows/backoff still govern timer checks. Review and approve the first deployment manually. Approval is an operator confirmation, not a separate-user or multi-party authorization system.

## Application Metrics

**Metrics and storage** shows systemd CPU usage, current/peak memory where available, task count, restarts, uptime, and logical file sizes for production files, persistent data, logs, and the retained source checkout. The timer refreshes deployed-app observations every five minutes, including paused apps; **Refresh metrics** requests an immediate sample. CPU percentage requires two samples from the same service invocation and resets after a restart; 100% represents one CPU core. Metrics are current observations, not a historical time-series database.

Directory scans do not follow symlinks and are bounded to 10,000 entries and approximately one second per directory tree; partial results are marked. Production totals include shared storage and must not be added to persistent-data totals. Unsupported counters are shown as unavailable. Static apps without a custom runtime have no systemd process counters. Use the sample timestamp when interpreting potentially stale observations.

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
deployment-logs <app-id> [deployment-id] [--status-only] | auto-deploy
pull <app-id>
start <app-id> | stop <app-id> | restart <app-id>
logs <app-id> [--lines N] | health <app-id>
preflight <app-id> | metrics <app-id>
review-update <app-id> | approve <app-id> <reviewed-commit>
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
- Git remotes must use HTTPS or SSH. Retained checkouts reject local changes and branch changes and only merge with `--ff-only`. Fresh checkouts must descend from the recorded deployed commit before activation.
- Each app uses a dedicated system account named `app-<app-id>`. Git operations, dependency installation, and build commands run as that account, never as root. The generated systemd service also uses that account. Runtime startup must pass a local health check and an actual TCP listener check; public IPv4 or IPv6 bindings on the assigned port are rejected and stopped before the new route is activated.
- Generated systemd services drop all capabilities and enable filesystem, device, kernel, namespace/personality, and privilege-escalation hardening. Release files are root-owned and read-only; static release content is readable by Nginx. Service writes are limited to that app's private home, config, log, and declared persistent-storage paths. App ports are unprivileged, so `CAP_NET_BIND_SERVICE` is not granted.
- Sources, releases, application configuration, and logs use app-specific paths. Successful source checkouts are deleted. Environment secrets remain outside checkout/release trees in root-owned files with app-group-only read access. Only the latest deployment record and current release are retained after successful cleanup; failed service shutdown preserves files for administrator recovery rather than deleting a live release.
- The only shared Nginx change is the validated, backed-up managed include described above; root-site directives and Cloudflare Tunnel configuration are preserved. Each owned snippet redirects `/app` to `/app/` without losing query parameters. Next.js receives `APP_BASE_PATH` during build and runtime and keeps that prefix upstream. Go, Node.js, and Python proxy routes strip the prefix upstream and send `X-Forwarded-Prefix` plus `APP_BASE_PATH`; root-relative redirects, loopback upstream redirects, and cookie paths are rewritten under the application prefix. Application-generated asset URLs and absolute public URLs must also honor the prefix. Static routes serve assets directly from the release tree and fall back to `index.html` for nested pages; no app service or Python HTTP server is created by default. WebSocket requests receive `Connection: upgrade`; ordinary HTTP requests omit the `Connection` header. This per-location handling requires no global `map`.
- Removing an app unregisters it, removes its verified App Manager route snippet, and stops/removes its App Manager service, but intentionally retains source and release data.

Applications must provide an appropriate start entry point. Framework adapters install/build dependencies and use a conventional start command; applications with nonstandard startup behavior must supply `build_config.start_command` as an array of executable arguments (not a shell string). Commands are not run through a shell: shell expansion, pipes, and command substitution are not available. Dynamic apps must read the assigned `PORT` and bind to `127.0.0.1`. Health checks use the configured local path and require a final 2xx response. Bounded redirects within the same scheme, host, and port are followed; redirects to other applications or external authentication providers are rejected. Local probes bypass environment proxies and supply the public Host and forwarded-prefix/protocol headers. A static health result verifies the deployed entry point exists, not external network reachability. No adapter can guarantee that an arbitrary repository, missing secret, incompatible runtime, or third-party dependency will deploy successfully.

| Framework | Default startup contract |
| --- | --- |
| Next.js | A production Next.js app with a valid `build` script and the configured public base path. Static exports are served by Nginx without a runtime service. |
| Node.js | A `scripts.start` entry or explicit start command. The package manager comes from the lockfile; build dependencies are installed before building. `HOST`, `PORT`, and `NODE_ENV=production` are supplied without guessing CLI flags. |
| Go | A root `main` package, or one uniquely detected `main` package below the root. Set `build_config.go_package` (for example `./cmd/server`) when multiple executables exist. The executable must honor the supplied host and port environment. |
| Python | A WSGI `app:app` entry, served by the release virtual environment's Gunicorn. ASGI apps, Django, and other module layouts require an explicit compatible start command and dependencies. |
| Static | An `index.html` at the repository root, served directly by Nginx. An explicit custom start command opts into a runtime service. |

### Startup, assets, and public 403 responses

Go deployments query the toolchain's host target and explicitly build for `GOOS=linux` and `GOARCH=GOHOSTARCH`, overriding cross-compilation targets inherited from application environment values or saved Go settings. `go list` inspects packages in the isolated release, preferring a root `main` package, then a unique nested `main` package. Library-only and ambiguous projects fail before building. Set the **Go main package** setup field, or edit the application's build configuration to `{"go_package":"./cmd/server"}`, to select an entry point explicitly. That path must be local and may not contain traversal components or wildcard patterns. The chosen package and target are recorded in the deployment log. The runtime working directory remains the repository's copied root so relative assets retain their existing base.

Go builds must produce a regular Linux ELF executable with execute permission before activation. A successful compiler exit is not sufficient: building a library package or targeting Windows/macOS can produce an artifact systemd cannot execute. Remaining non-ELF artifacts identify library archives separately and suggest checking `GOFLAGS`/build-mode settings. A `203/EXEC` startup failure is reported separately from HTTP readiness, with a bounded recent unit journal when available. Inspect its `Failed at step EXEC` message to distinguish missing paths/loaders, execute/traversal permission errors, `noexec` mounts, and incompatible binaries; increasing the health timeout will not fix these errors.

For `203/EXEC`, failure diagnostics also capture the built executable's path permissions (`namei`) and filesystem mount options (`findmnt`) before rollback deletes the failed release. `Permission denied` can mean missing directory traversal/execute access, a `noexec` mount, or a host security policy; it does not justify recursively broadening permissions or disabling the sandbox. Shared mount settings and directory permissions are not changed automatically. Generated units use an unquoted absolute `EnvironmentFile=-/path` value, with literal percent signs escaped, so systemd does not mistake surrounding quotes for part of the filename. The obsolete `CPUAccounting=` directive is omitted; CPU quota limits remain enabled.

Failed readiness checks include the expected local URL, assigned port, systemd exit state, and a bounded tail of runtime stderr when available. Configured environment values are redacted from these diagnostics. A Go app must use the assigned address, not a hard-coded `:8080` or `:3000`; for example, pass `net.JoinHostPort(os.Getenv("HOST"), os.Getenv("PORT"))` to `http.ListenAndServe` and handle its error. Runtime files use the copied app directory as their working directory. Releases remain read-only: SQLite databases, uploads, generated files, and other mutable data belong in `APP_DATA_DIR` or a declared `APP_STORAGE_<NAME>` directory, not beside the executable or Python module.

Static HTML deployments prefix root-relative `src`, `href`, `poster`, and `data` URLs when the target is an existing file inside that app's copied release. Source files, external/CDN URLs, already-prefixed URLs, script bodies, and links to other applications are unchanged. CSS URLs, `srcset`, web manifests, and JavaScript-generated requests must still be relative or explicitly use the app route. Go/Node/Python HTML responses must generate asset URLs using `APP_BASE_PATH` (or safely configured `X-Forwarded-Prefix` support). A request for `/icons/logo.svg` targets the existing root site; use `/your-app/icons/logo.svg`. The plugin intentionally does not capture root-level asset paths or rewrite arbitrary application responses.

The dashboard distinguishes local readiness from public reachability. A healthy local app with a public HTTP 403 remains Live but shows `Blocked · 403`. Cloudflare-marked responses suggest inspecting the matching WAF/Access rule; other 403 responses suggest Nginx permissions/index files or application access rules. These are diagnostic hints, not proof of which layer blocked the request. Use an unauthenticated health endpoint and a narrowly scoped edge rule where needed; do not disable global security, change the existing root site's routing, or use `chmod 777`. No host Nginx, Cloudflare, or legacy service changes are made by these diagnostics.

Next.js deployments detect `next.config.js`, `.mjs`, `.cjs`, `.ts`, `.mts`, and `.cts` files and automatically apply `basePath: process.env.APP_BASE_PATH || ""` and `trailingSlash: true` in the isolated release copy, including when an existing config exports an object or a config function. Existing config contents are retained in a release-local backup and merged before the build; the source checkout is not modified. The resolved config is checked before building so the public route and generated assets stay aligned. `output: "export"` is supported: the generated `out/` directory is served directly by Nginx under the app route, without a runtime service. Non-export Next.js apps continue to run with `next start`. Dynamic Next.js local health URLs include the application prefix.

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

Python tests cover validation, framework detection, JSON/locking behavior, concurrent port allocation, health/metrics sampling, environment/runtime preflight, persistent storage, stale approval rejection, bounded/cancellable build slots, safe settings rejection, fast-forward Git updates, deployment recovery, static activation, listener isolation, plugin update/restart failures, installer ownership boundaries, generated systemd hardening, Nginx prefix/WebSocket behavior, and uninstall safety. Frontend tests exercise guided setup, environment masking and stdin submission, registration retries, search/filter behavior, preflight/metrics reports, exact-commit approval, storage/runtime settings, duplicate deployment prevention, and update/restart feedback.

POSIX shell and file-permission integration tests are skipped on Windows. Local unit tests and mocked browser tests are not a production deployment certification. Before production use, validate on a disposable Ubuntu host: Cockpit privilege escalation; account and systemd sandbox setup; real Nginx syntax, routing, assets, cookies, redirects and WebSockets; one deployment per supported framework; health-failure recovery and cancellation; automatic deployments; and plugin updates reconnecting after the Cockpit restart. Confirm the existing root site and `stuserver-websites` service remain unchanged throughout.

GitHub Actions runs unit/UI checks and an explicitly gated Ubuntu VM integration job. The latter creates a separate loopback Nginx test virtual host and isolated managed roots, exercises all five real framework adapters and systemd services, candidate replacement/failure recovery, checkout cleanup, persistent-file survival, real systemd metrics, public health, and SSH tag verification. It checks unchanged pre-existing Nginx file hashes and legacy service state. Repository downloads/history and the failure-health boundary are fixture-controlled; this does not certify remote credentials, real HTTPS/Cloudflare routing, Cockpit/polkit, approval with real remote changes, cross-process queue cancellation after host interruption, or a full plugin-update/reconnect cycle. Those remain disposable-host acceptance checks.

Run only on a fresh disposable Ubuntu VM with Nginx, systemd, Git, Node/npm, Go, and Python venv support installed:

```bash
sudo env APP_MANAGER_DISPOSABLE_UBUNTU=1 python3 -m unittest discover -s tests -p test_ubuntu_integration.py -v
```

Never set the disposable-VM flag on a production server. The suite starts real services and creates/removes its own dedicated test users, units, and paths.
