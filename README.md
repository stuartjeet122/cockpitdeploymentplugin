# Cockpit App Manager

App Manager is the repository's Cockpit plugin for building and deploying native Next.js, React, Go, Node.js, Python, and static web applications. Separate Portainer and Nginx Proxy Manager (NPM) tabs open those external services using addresses configured in Settings. App Manager does not manage Docker or Nginx, or install or upgrade either service.

The native Cockpit interface opens on Applications, with search, status filters, and a guided repository, configuration, and review flow. Environment variables can be saved before the first deployment; saved values are masked until explicitly revealed. App-scoped service controls, local health checks, logs, and deployment settings remain available without leaving Cockpit. Only the current release and latest deployment record are kept. Deployments run independently of the browser session, so the live log can be reopened after a refresh; an active deployment can be aborted from the log dialog.

The installer enables a systemd timer that checks each repository branch for applications that have been deployed at least once. Deploy an application once manually; subsequent updates are checked every five minutes without requiring an open Cockpit page. Each app can pause automatic deployment or restrict it to a UTC window in **Edit configuration**. A window crossing midnight is supported; equal start/end times mean all day. Failures are shown on the application and in the timer's systemd journal, with retry backoff from five minutes to one hour. The hardened deployment service writes only to managed app/state roots and App Manager's systemd units; it does not read or modify Nginx configuration. User-namespace isolation is explicitly disabled for this root-owned timer because it must switch to each app's unprivileged service account for Git operations. Pausing auto-deploy does not stop the app or disable manual deployment.

The header's **Check for updates** selects the latest commit on the upstream `main` branch; no release tag or signing key is required. The dialog shows the installed and available commit IDs. Installation clones the full branch as `nobody`, checks that it is still at the selected commit, refuses non-fast-forward updates, and copies it into a root-private staging directory. Before running the installer as root, it checks every staged file against the selected commit's Git blob hashes and rejects unexpected files. Git replacement objects are disabled during verification. Only successful installation schedules a Cockpit restart after five seconds; running applications are not restarted. If restart scheduling fails, the UI reports that installation succeeded but an administrator must restart Cockpit manually.

## Main-branch Update Trust

Main-branch updates are not cryptographically signed: anyone able to change the upstream `main` branch can publish code that the host will install as root. Enable this only if you trust the GitHub repository and maintain strong account security and branch protections. The update verifies the exact commit returned by GitHub over HTTPS, prevents branch rollback/rewrite by requiring a fast-forward from the installed commit, and verifies the downloaded files against that commit's Git object IDs; these checks do not prove who authored or authorized the commit. If signature-based release approval is required, do not use the automatic main-branch updater.

## Installation

Review `config/defaults.json`, then install from the project directory:

```bash
sudo bash ./scripts/install.sh
```

The shared JavaScript cache additionally requires Node.js 18.18 or newer, pnpm 10.3+ within the 10.x series, and Verdaccio 6.x installed in `/usr/local/bin` or `/usr/bin`. Install these host tools through your normal administrator-approved package process before running the installer; it will not replace existing global Node tools automatically. The cache uses the standard `/etc/cockpit/app-manager` configuration location. Port 4873 must be free on first installation. Other runtimes still need their own tools.

### Upgrading from an npm-only installation

An older installation may not have pnpm or Verdaccio. In that case **Check for
updates** fails during installer preflight, before copying plugin files or
changing application services. Retrying the update alone cannot provision these
new host prerequisites. The previous plugin and application releases remain in use.

On the Ubuntu host, first check the system-wide tools (not an interactive user's
nvm installation):

```bash
env PATH=/usr/local/bin:/usr/bin:/bin node --version
env PATH=/usr/local/bin:/usr/bin:/bin npm --version
env PATH=/usr/local/bin:/usr/bin:/bin pnpm --version
env PATH=/usr/local/bin:/usr/bin:/bin verdaccio --version
```

An administrator must provision a supported system-wide Node.js/npm if missing.
The cache floor is Node 18.18; Next.js 16 applications require at least Node 20.9.
Prefer a currently supported Node.js LTS release compatible with the applications.
After reviewing existing global tool versions and the package source, an
administrator can provision missing pnpm/Verdaccio with:

```bash
cd /
sudo npm install --global --prefix /usr/local --ignore-scripts 'pnpm@10' 'verdaccio@6'
/usr/local/bin/pnpm --version
/usr/local/bin/verdaccio --version
```

This explicitly installs administrator-selected host tools, suppressing package
installation hooks; App Manager never runs this command automatically. Do not
force-overwrite conflicting binaries, disable TLS verification, install into an
application release, or change global ownership to work around an installation
error. User-home nvm/Corepack shims are not available to the isolated services.
Once the version/path checks pass, retry **Check for updates** and verify that the
managed loopback cache starts. Then redeploy the failed application.

A previous `npm ci` error listing missing platform packages is a separate,
incomplete-lockfile problem. Missing Linux packages can matter even when the lock
was generated on Windows. The new manager imports npm locks into pnpm in the
release only; this does not certify or repair the source lockfile. If dependency
validation still fails, repair and validate that application's lockfile in its
source repository, not under `/opt/webapps/.../releases`. Do not replace frozen
installs with a silent non-frozen fallback or delete the running release.

The installer requires Ubuntu, root privileges, Python 3.9 or newer, systemd, Cockpit 318 or newer, Git with SSH signing support, `ssh-keygen`, `logrotate`, `runuser` and `setpriv` from `util-linux`, and `ss` from `iproute2`. Install the runtime and package manager needed by each application separately, including `python3-venv` for Python apps. Preflight rejects missing App Manager host tools before installation. It checks backend imports both before and after installation and installs only App Manager under `/usr/share/cockpit/app-manager`. The backend and adapter module live under the dedicated root-owned, readable/traversable `/usr/libexec/cockpit/app-manager` directory, with state under `/var/lib/cockpit/app-manager`. Cockpit plugin/code directories are normalized to mode `0755`, and installed code/assets are readable by Cockpit; sensitive state remains root-only rather than being made globally writable. No CDN or Node.js runtime is needed by the plugin UI. It does not recursively change ownership of shared Cockpit directories or `/opt/webapps`, and it preserves existing registry/configuration data on repeat installation.

Application Git and build commands switch directly to the dedicated app UID and group using `setpriv`, without opening an interactive PAM session. The root timer helper retains `CAP_SETUID` through `AmbientCapabilities` because systemd 259's seccomp setup can otherwise remove it despite the bounding set. Before executing app commands, `setpriv` clears supplementary groups and inheritable/ambient capabilities; changing to the app UID drops permitted/effective capabilities. `no_new_privs` prevents privilege escalation, and the timer's systemd sandbox remains in effect. There is no root fallback if switching identity fails. After updating the plugin, existing automatic-deployment warnings clear on the next successful repository check; a previous failure's retry backoff can delay that check by up to one hour.

The UI requests Cockpit's required superuser mode when launching the backend. The user must be authorized for Cockpit privilege escalation by the host's existing polkit/Cockpit policy; the installer does not grant extra users system-wide privileges. Registry and runtime configuration files are mode `0600`; state/source parent directories are not listable by application accounts.

The backend stores its registry at `/var/lib/cockpit/app-manager/apps.json` and configuration at `/var/lib/cockpit/app-manager/config.json`. Registry/configuration JSON is atomically replaced and protected by file locks. App environment files are stored outside source and release trees at `/var/lib/cockpit/app-manager/environment/<app-id>.env`, owned by root and readable only by that application's service group. The UI lets you enter values during setup or import `.env`, `.env.*`, and `.txt` files with one assignment per line; the review shows names only. Values are applied by systemd when the application starts; restart or redeploy after editing.

Each application has an application path prefix, such as `/shop/`, or `/` for a root-based virtual host. App Manager uses this value for build-time asset handling and the `APP_BASE_PATH` environment variable; deployment does not create domains or edit/reload Nginx configuration. Configure routing separately in NPM's own interface, proxying dynamic apps to their displayed `127.0.0.1:<port>` address. Existing host and legacy App Manager Nginx configuration is left untouched by deployment, application removal, installation, and default uninstall.

Removing an application permanently deletes that application's managed source, releases, storage, logs, environment secrets, dedicated account, and service files. If a virtual host points to the app, remove or update that route separately in NPM before deleting the app. Export any data you need before confirming removal.

## First deployment

1. Open **App Manager** in Cockpit and select **New application**. Supply the HTTPS or SSH repository URL, application name, and production branch. Configure private-repository access for the dedicated application account on the host; never embed credentials in a repository URL.
2. Select the framework or automatic detection, a unique application ID, and an application path prefix such as `/shop/` or `/`. Set a health path that returns 2xx without authentication or redirects for dynamic apps. Add the environment variables needed during build and runtime. App Manager accepts a relative Next.js/React app directory and explicit dynamic start commands as JSON argument arrays.
3. Review the application path, source, and environment variable names. Leave **Deploy now** enabled to save and deploy, or disable it to register without starting a deployment. Follow the deployment log; errors leave the last working release active when recovery succeeds.
4. Configure domains and routing separately in NPM, opened through the Nginx Proxy Manager tab. Environment edits require a restart or redeployment to affect a running process. The deployment log can be copied or downloaded as a `.txt` file from its dialog. Local health checks use the configured path and do not test external reachability.

## React deployments

Select **React (Vite / Create React App)** in **New application**, or use **Auto detect** for a frontend declaring `react` and either `vite` or `react-scripts`. Next.js, Go, and Python detection take priority. An explicit start command or a nonstandard/production `start` script retains Node.js detection rather than silently dropping its server. For a nested React project, set **Project directory**, such as `frontend`; npm-style workspaces reuse their root package manager and lockfile.

The project must define a production `build` script: `vite build`, `tsc -b && vite build`, a Vite build with additional arguments, or `react-scripts build`. App Manager uses the same unprivileged, isolated release build and managed pnpm/cache workflow as other JavaScript apps. It does not run `npm start`, `vite dev`, or a development server. Custom/server-rendered React startup belongs under **Node.js** or **Next.js**, not this static frontend option.

pnpm's existing dependency-script approval policy is unchanged. If installation reports an unapproved dependency such as `esbuild`, review it and declare the approved package in `pnpm.onlyBuiltDependencies` in the source `package.json` or the supported workspace policy, then redeploy.

Vite receives `--base` from the application path and an explicit `--outDir`; Create React App receives managed `PUBLIC_URL` and `BUILD_PATH` environment values. These override saved values for React builds. Output defaults to `dist` for Vite or `build` for Create React App. Set **React output directory** (or `build_config.output_directory` in Edit configuration) to use another relative output directory. Generated output must not already be checked into the source at that path. Deployment requires a fresh regular `index.html` and rejects symlinks escaping the output directory.

Production files are activated at `/opt/webapps/<app-id>/current/app/<output-directory>/`, beneath the project directory if configured. The application card displays that content path instead of a runtime port. No systemd runtime, host web-server change, public domain, or proxy route is created. Configure your existing web server to serve this directory, and configure SPA history fallback to this application's `index.html` for client-side routes. An NPM proxy host alone cannot serve these files without a separately configured static upstream. For React Router under a prefix, configure its basename too, for example `basename={import.meta.env.BASE_URL}` in Vite.

Save public build-time values such as `VITE_*` or `REACT_APP_*` before deployment, and redeploy after changing them. They become visible in browser bundles: never expose API keys, database passwords, or other secrets. React health checks verify the deployed `index.html`, not external HTTP reachability or SPA routing.

## Portainer and Nginx Proxy Manager

App Manager no longer includes Docker/Compose deployments, container management,
or Nginx Proxy Manager API management. Use the separate **Portainer** and
**Nginx Proxy Manager** tabs to open those services in a new browser tab.
App Manager does not embed them, proxy their APIs, or collect their credentials.

Set each browser-facing URL or path in **Settings**, for example
`https://portainer.example/`, `/manage-docker/`, or `/nginx/`. Root-relative
paths use the browser's current origin. HTTP(S) URLs and root-relative paths
are accepted; credentials, protocol-relative URLs, queries, and fragments are
rejected. Leave a field blank to disable its link. Only these addresses are
saved, under `integrations.portainer_url` and `integrations.npm_url`.
Existing configured addresses are preserved exactly, not rewritten to a local
API endpoint. Review older settings: `127.0.0.1` refers to the browser's computer,
not the Ubuntu server, and an API-only address may need replacing.

Existing Docker registrations remain visible as unsupported, with all actions
disabled and automatic deployment skipped. Their containers, images, volumes,
services, managed files, and registry entries are not stopped or deleted.
Manage or migrate these applications externally. New Docker registrations and
the old `integration` CLI command are rejected. A repository containing a
Dockerfile can still be detected as a supported native framework; a
Dockerfile/Compose-only repository is unsupported.
Even `uninstall.sh --remove-managed-data` skips these unsupported applications
and retains the registry; clean them up externally after migration.

Older releases installed separate Docker Manager and Nginx Manager Cockpit
entries. Re-run the installer from this checkout, then sign out of Cockpit and
back in to refresh navigation. The installer retires only verified legacy UI
assets, leaving unrelated files and host services/configuration untouched.

Configure domains, upstreams, certificates, and access rules directly in NPM.
Native dynamic apps listen on their displayed `127.0.0.1:<port>` address; a
bridged NPM container needs an administrator-configured path to that loopback
upstream. Static files require a separately configured static web server.
App Manager never changes existing Nginx or Cloudflare routing.

## Runtime Operations

For a running dynamic application, redeployment reserves a second port and starts an alternate managed systemd service against the new release's absolute path. The current service remains live until candidate health and loopback-listener checks pass. Only then does App Manager switch the active release, retire the previous service, and prune its release. A failed candidate leaves the old service running. This reduces activation downtime but is not a guarantee of uninterrupted long-lived connections; applications must tolerate overlapping instances and handle termination gracefully. First deployments, stopped apps, and static/runtime conversions use the normal activation/recovery flow. Candidate ports remain within 8100-8999 and are reserved against concurrent registration.

Successful deployments delete the managed Git checkout at `sources/<app-id>`. Later checks use `git ls-remote` without recloning; a changed revision triggers a fresh clone. Before building, Git verifies that it descends from the recorded live commit, rejecting rewritten history. Failed checkouts are retained for diagnosis and existing dirty-repository protections still apply. The active runtime release is not deleted: Node.js and Python need application source to execute, and static assets must remain available. This is checkout cleanup, not a promise that interpretable runtime source can be removed or hidden.

**Check health** records systemd state, local readiness, a check time, and failure details. Static deployments are checked for a deployed `index.html`; dynamic apps are probed on their loopback port. App Manager does not check public DNS, external routing, Cloudflare, or virtual-host reachability. Status observations older than ten minutes are not used to override saved lifecycle state.

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

Default behavior removes only verified App Manager and legacy custom Docker/Nginx plugin assets plus backend/template files. Unknown files and non-empty shared directories are preserved. It also preserves application source repositories, production deployments under `/opt/webapps`, all Nginx configuration (including legacy App Manager snippets), and the existing server infrastructure.

For full destructive removal, use the explicit cleanup path. It removes registry-identified app data, verified dedicated `app-<id>` accounts, and positively marked App Manager service units. Nginx configuration is never edited; remove any virtual hosts or legacy snippets separately before deleting apps whose routes still point to those releases:

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
list [--framework <name> | --exclude-framework <name>]
overview [--framework <name> | --exclude-framework <name>]
deployments [--framework <name> | --exclude-framework <name>]
add <json> | edit <app-id> <json>
deploy <app-id> | deployment-start <app-id> | deployment-abort <app-id> <deployment-id>
deployment-logs <app-id> [deployment-id] [--status-only] | auto-deploy [--retry-now]
pull <app-id>
start <app-id> | stop <app-id> | restart <app-id>
logs <app-id> [--lines N] | health <app-id>
preflight <app-id> | metrics <app-id>
review-update <app-id> | approve <app-id> <reviewed-commit>
env-get <app-id> | env-save <app-id>  # env-save reads content from stdin
remove <app-id>
settings [<json>]
plugin-update-check | plugin-update
action <action> <app-id>
```

After fixing an automatic-deployment failure, retry immediately instead of waiting for backoff:

```bash
sudo /usr/libexec/cockpit/app-manager/app-manager auto-deploy --retry-now
```

This bypasses only retry backoff. Paused apps, UTC deployment windows, first-deployment requirements, active deployments, and approval policies still apply. New commits can deploy automatically under the existing policy; this is not a read-only check. Stored errors clear only after a successful check, and failed retries retain the error and schedule backoff again. Refreshing the Cockpit dashboard or restarting the timer does not bypass backoff.

Project conventions and deployment requirements are documented in `AGENTS.md` and must be followed for all future changes.

`action` supports the operations used by the Cockpit UI: `deploy`, `pull`, `start`, `stop`, `restart`, `logs`, `remove`, and `health`.

## Deployment and safety

- Build commands run in uniquely named `cockpit-app-manager-build-*.scope` units. Cancellation and build timeouts terminate that build scope, including detached descendants, without targeting the application's live runtime service. Live output draining remains cancellable after the command launcher exits, so open child-process pipes cannot hide an abort request.
- After a worker interruption, retrying `deployment-abort <app-id> <deployment-id>` can finalize an abortable record when the app lock is free and the exact worker unit is inactive or missing with no pending systemd job. Older records without worker-unit metadata can use the unit name derived from their app and deployment IDs only after reaching the running build phase; pending records remain excluded to avoid racing worker startup. Recovery only updates deployment metadata and logs; it does not stop runtime services, roll back activation, delete releases, or terminate orphaned build processes.
- Ports are allocated from 8100–8999. Port 8080 is excluded. Registration holds the registry lock while checking existing allocations and listening sockets and recording the new port.
- Git remotes must use HTTPS or SSH. Retained checkouts reject local changes and branch changes and only merge with `--ff-only`. Fresh checkouts must descend from the recorded deployed commit before activation.
- Each app uses a dedicated system account named `app-<app-id>`. Git operations, dependency/build commands, and generated runtime services run as that account. Runtime startup must pass a local health check and an actual TCP listener check; public IPv4 or IPv6 bindings on the assigned port are rejected and stopped. App Manager does not activate or modify a web-server route.
- Generated systemd services drop all capabilities and enable filesystem, device, kernel, namespace/personality, and privilege-escalation hardening. Release files are root-owned and read-only; static release content is readable by Nginx. Service writes are limited to that app's private home, config, log, and declared persistent-storage paths. App ports are unprivileged, so `CAP_NET_BIND_SERVICE` is not granted.
- Sources, releases, application configuration, and logs use app-specific paths. Successful source checkouts are deleted. Environment secrets remain outside checkout/release trees in root-owned files with app-group-only read access. Only the latest deployment record and current release are retained after successful cleanup; failed service shutdown preserves files for administrator recovery rather than deleting a live release.
- App deployments never read, write, test, reload, or restart host Nginx configuration. Manage domains and virtual hosts separately in NPM's own interface. Existing host and legacy App Manager configuration is preserved; removing an app does not remove a referenced proxy route.
- Removing an app unregisters it and stops/removes its App Manager service and managed data. Remove any virtual-host references separately before deleting a deployed app.

Applications must provide an appropriate start entry point. Framework adapters install/build dependencies and use a conventional start command; applications with nonstandard startup behavior must supply `build_config.start_command` as an array of executable arguments (not a shell string). Commands are not run through a shell: shell expansion, pipes, and command substitution are not available. Dynamic apps must read the assigned `PORT` and bind to `127.0.0.1`. Local health checks use the configured path and require a final 2xx response. Bounded redirects within the same scheme, host, and port are followed; redirects to other applications or external authentication providers are rejected. Local probes bypass environment proxies. A static health result verifies the deployed entry point exists, not external network reachability. No adapter can guarantee that an arbitrary repository, missing secret, incompatible runtime, or third-party dependency will deploy successfully.

| Framework | Default startup contract |
| --- | --- |
| Next.js | A production Next.js app with a valid `build` script and the configured application base path. Static exports are deployed without a runtime service and need external web-server routing. |
| Node.js | A `scripts.start` entry or explicit start command. Managed deployments convert dependencies to pnpm in the release copy; build dependencies are installed before building. `HOST`, `PORT`, and `NODE_ENV=production` are supplied without guessing CLI flags. |
| Go | A root `main` package, or one uniquely detected `main` package below the root. Set `build_config.go_package` (for example `./cmd/server`) when multiple executables exist. The executable must honor the supplied host and port environment. |
| Python | A WSGI `app:app` entry, served by the release virtual environment's Gunicorn. ASGI apps, Django, and other module layouts require an explicit compatible start command and dependencies. |
| Static | An `index.html` at the repository root, deployed without a runtime service. Configure external web-server routing separately. An explicit custom start command opts into a runtime service. |

### Startup, assets, and application path prefixes

Managed environment values are passed to build commands and loaded by systemd for dynamic app runtimes. Applications read their process environment rather than requiring a `.env` file beside the source. Save values in App Manager before deploying. A restart updates runtime-only values, but Next.js `NEXT_PUBLIC_*`, Vite `VITE_*`, and other compiled browser configuration require a new deployment. Only framework-approved public variables belong in browser bundles; never expose database passwords or API secrets. Plain static HTML has neither a server-side process environment nor an automatic environment substitution step.

Every managed dynamic application receives its configured application path prefix without the trailing slash as `APP_BASE_PATH`. App Manager does not add `X-Forwarded-Prefix` or rewrite arbitrary Swagger/OpenAPI documents: their `servers` (OpenAPI) or `basePath` (Swagger 2) must match the path configured in the external reverse proxy. Configure the app to derive its base path from `APP_BASE_PATH`; this is necessary for Swagger “Try it out” requests as well as other generated API clients. Static sites have no runtime process to configure, so their Swagger documents and asset paths must already match the configured path.

Environment files are not browser resources; their values are not automatically prefixed, published, or substituted into application URLs. Configure the external web server to deny access to sensitive dotfiles when proxying or serving app content.

Go metadata commands preserve successful stdout internally for JSON parsing, while live logs and stderr remain redacted. This prevents a route or environment value that overlaps a package path or name from corrupting package discovery. Failed-command output and timeout diagnostics remain redacted; ordinary build commands keep the default redacted capture behavior.

Go deployments query the toolchain's host target and explicitly build for `GOOS=linux` and `GOARCH=GOHOSTARCH`, overriding cross-compilation targets inherited from application environment values or saved Go settings. `go list` inspects packages in the isolated release, preferring a root `main` package, then a unique nested `main` package. Library-only and ambiguous projects fail before building. Set the **Go main package** setup field, or edit the application's build configuration to `{"go_package":"./cmd/server"}`, to select an entry point explicitly. That path must be local and may not contain traversal components or wildcard patterns. The chosen package and target are recorded in the deployment log. The runtime working directory remains the repository's copied root so relative assets retain their existing base.

Go builds must produce a regular Linux ELF executable with execute permission before activation. A successful compiler exit is not sufficient: building a library package or targeting Windows/macOS can produce an artifact systemd cannot execute. Remaining non-ELF artifacts identify library archives separately and suggest checking `GOFLAGS`/build-mode settings. A `203/EXEC` startup failure is reported separately from HTTP readiness, with a bounded recent unit journal when available. Inspect its `Failed at step EXEC` message to distinguish missing paths/loaders, execute/traversal permission errors, `noexec` mounts, and incompatible binaries; increasing the health timeout will not fix these errors.

For `203/EXEC`, failure diagnostics also capture the built executable's path permissions (`namei`) and filesystem mount options (`findmnt`) before rollback deletes the failed release. `Permission denied` can mean missing directory traversal/execute access, a `noexec` mount, or a host security policy; it does not justify recursively broadening permissions or disabling the sandbox. Shared mount settings and directory permissions are not changed automatically. Generated units use an unquoted absolute `EnvironmentFile=-/path` value, with literal percent signs escaped, so systemd does not mistake surrounding quotes for part of the filename. The obsolete `CPUAccounting=` directive is omitted; CPU quota limits remain enabled.

Failed readiness checks include the expected local URL, assigned port, systemd exit state, and a bounded tail of runtime stderr when available. Configured environment values are redacted from these diagnostics. A Go app must use the assigned address, not a hard-coded `:8080` or `:3000`; for example, pass `net.JoinHostPort(os.Getenv("HOST"), os.Getenv("PORT"))` to `http.ListenAndServe` and handle its error. Runtime files use the copied app directory as their working directory. Releases remain read-only: SQLite databases, uploads, generated files, and other mutable data belong in `APP_DATA_DIR` or a declared `APP_STORAGE_<NAME>` directory, not beside the executable or Python module.

All framework adapters prepare HTML files and web manifests in the isolated release before compilation or dependency packaging, including Go-embedded HTML, Node assets, Python templates, and static sites. Root-relative `src`, `href`, `poster`, and `data` URLs are prefixed when the target exists inside the release or conventional `public`, `static`, `dist`, or `build` directories. A root `<base href="/">` is adjusted to the app route. Web manifests have their root-relative identity, scope, launch URL, and known local icons scoped to the app. Node builds are scanned again for generated HTML/manifests; a standard `vite build` script receives Vite's native `--base` argument. Source checkouts, external/CDN URLs, already-prefixed URLs, script bodies, and links to other applications are unchanged. Dependency and symlinked directories are excluded from preparation. Custom CSS URLs, `srcset`, arbitrary JavaScript-generated requests, and server-generated HTML still need application-level base-path handling using `APP_BASE_PATH` or safely configured `X-Forwarded-Prefix` support. A request for `/icons/logo.svg` targets the existing root site; use `/your-app/icons/logo.svg`. The plugin intentionally does not capture root-level asset paths or rewrite arbitrary application responses.

The dashboard reports local runtime readiness only. A healthy local application does not guarantee external DNS, NPM routing, Cloudflare, or authentication behavior; validate those separately in the external services without changing unrelated site configuration.

Next.js deployments detect `next.config.js`, `.mjs`, `.cjs`, `.ts`, `.mts`, and `.cts` files and automatically apply `basePath: process.env.APP_BASE_PATH || ""` and `trailingSlash: true` in the isolated release copy, including when an existing config exports an object or a config function. Existing config contents are retained in a release-local backup and merged before the build; the source checkout is not modified. The resolved config is checked before building so the configured application path and generated assets stay aligned. `output: "export"` is supported: the generated `out/` directory is deployed without a runtime service and must be routed by the external web server. Non-export Next.js apps continue to run with `next start`. Dynamic Next.js local health URLs include the application path prefix.

Base-path-aware Next.js builds use a release-only build wrapper: the package manager runs the project's existing `prebuild` first, then App Manager prepares local assets for the configured application path, then the original build runs, followed by `postbuild`. This lets asset-generation hooks publish guide illustrations and other files before URL preparation. The helper uses Next.js's bundled Babel parser and the unprivileged build runner; malformed source fails preparation rather than being rewritten by regular expressions. Literal local asset URLs, file-based App Router icons such as `/icon.svg`, metadata manifests, custom route handlers, and recognized local API paths receive the application prefix. Known local icons and scope/start URLs in public JSON manifests are also prepared. Native history push/replace calls scope same-origin root paths, and `usePathname()` results have non-root trailing slashes removed so application route comparisons still work with `trailingSlash: true`. Next-managed navigation links keep their native base-path handling. Fullscreen calls are gated by active user input where the browser exposes `navigator.userActivation`; startup does not attempt to bypass browser fullscreen policy.

Query strings and fragments are retained. External URLs, module imports, private dotfile paths, already-prefixed paths, and unmatched URLs are unchanged. Git source, configuration values, dependency directories, nested packages, scripts, tests, and generated outputs are not rewritten. Custom CSS URLs, `srcset` lists, general computed asset paths, dynamic/grouped routes, and shared workspace packages still need application-level base-path handling. Redeploy after installing this plugin update; restarting an old build does not apply source adjustments. Browser preload-unused messages alone do not indicate missing environment variables or failed database requests.

A generic "Neon unavailable" warning does not by itself indicate missing environment variables. First check whether the browser's API request uses the application's prefix: `/scrumtimer/api/teams`, not `/api/teams`. If the prefixed endpoint still returns 503, check variable presence in the running service, database reachability, and required migrations without sharing credentials or environment file contents.

Next.js deployments detect a unique app in the repository or accept its relative path in `build_config.app_directory` (for example, `apps/web`). The conventional Next.js `app/` source directory does not change the package root. Workspaces use the root lockfile while keeping build and runtime working directories at the selected app's `package.json`. Build copies are group-readable/writable by the isolated app account, while the cloned source remains unchanged. Preflight logs include resolved paths, file metadata, original lockfile version, exact command and working directory; failed deployment logs are retained even when the failed release copy is cleaned up.

### pnpm conversion and shared downloads

Every managed Next.js/Node deployment uses pnpm for dependency installation, build scripts, production dependencies, and default startup. Original npm, Yarn, Bun, and pnpm projects are detected from their lockfiles. Malformed npm locks and conflicting managers' locks fail explicitly. `npm-shrinkwrap.json` takes precedence over `package-lock.json`. npm/Yarn lockfiles are imported with `pnpm import`; import prefers their existing versions but is not guaranteed to preserve every resolution. Existing pnpm locks use frozen installation. Bun's lock formats cannot be imported by pnpm, so Bun and lockfile-free projects resolve `package.json` ranges afresh and generate a release-only lockfile; logs state this limitation. Subsequent production installation is frozen. No lockfile or manager metadata is committed back to Git.

npm workspace declarations are translated into `pnpm-workspace.yaml` when that file is absent; existing pnpm workspace files are retained. The release's manager pin is removed so the host pnpm is used rather than downloading another manager. Custom start commands and package scripts are not rewritten; scripts explicitly invoking npm/Yarn/Bun still require that executable. Project `prebuild` and `postbuild` hooks remain enabled. pnpm's dependency-script approvals remain in force, with `strict-dep-builds=true`: unapproved native/dependency install scripts fail instead of silently producing an incomplete build. Review and declare the needed packages in `pnpm.onlyBuiltDependencies` in `package.json` or the supported workspace policy before redeploying.

The installer starts `cockpit-app-manager-npm-cache.service`, a Verdaccio read-through registry bound only to `127.0.0.1:4873`. It proxies public npm over verified HTTPS, disables the web UI and registration, and denies publishing/unpublishing. A separate systemd dynamic user exclusively owns `/var/cache/cockpit-app-manager-npm-cache` and `/var/lib/cockpit-app-manager-npm-cache`; application users cannot write that storage. Before pnpm commands run, the privileged backend verifies the listener UID matches the managed cache's systemd process, without adding process-tracing capabilities to the deployment timer. Nginx, Cloudflare Tunnel, port 8080, and the existing root site are not changed.

Public registry downloads are shared across applications. Each app keeps its own persistent pnpm store under its managed home and its own installed dependencies. Release dependency files are copied rather than hardlinked to writable store files. This saves repeat downloads, not all cross-app disk duplication. Private scoped registries remain configured separately and bypass the public cache, as do direct Git/tarball dependencies. Unscoped credentials and a custom default registry are rejected rather than being forwarded to the public cache; scope private registries and credentials to their original URLs. Keep secrets in the app environment, not committed configuration.

Inspect cache failures with `journalctl -u cockpit-app-manager-npm-cache.service`. Ordinary uninstall stops/removes the verified cache unit and configuration but retains cached downloads; destructive app cleanup also leaves this shared cache untouched. Cache/state directories may be removed manually by an administrator only after stopping the cache service. Reinstall/redeploy to apply conversion to existing applications; running releases are not rewritten.

### Selecting Python for an application

Python deployments default to the host's `python3`. In **Edit configuration**,
add `"python_interpreter": "python3.12"` to the existing **Build configuration**
JSON object to select a separately provisioned version. Keep the existing
`start_command` and other settings. Preflight, native prerequisite checks and
venv creation use that command; a missing command fails without fallback.
Only `python3` or versioned names such as `python3.12` are accepted, not paths
or shell strings. The interpreter and matching venv support must be installed
by an administrator on the service account's system PATH. Redeploy to create
a new venv; do not replace Ubuntu's system Python. The inspected drum dependency
pins target Python 3.11-3.13, not 3.14.

## Native multi-service Python APIs

Python apps can opt into a loopback Uvicorn API and one native Celery worker,
shared persistent storage/environment, read-only models, bounded prerequisite
checks, separate worker controls/limits/logs and per-app upload settings.
Configure route authentication and access rules in NPM's own interface.
See the [native Python deployment runbook](docs/native-python.md) for configuration,
administrator setup, failure recovery and the proposed drum API changes.
The separate drum API repository is unchanged; compatibility requires those
approved changes and real disposable-Ubuntu acceptance checks. Routes are blocked
by default until an administrator explicitly confirms upstream authentication.

## Development tests

The optional real package-tool checks use temporary pnpm/Verdaccio tools and a local fixture upstream, without modifying global tools. On a POSIX development host run `APP_MANAGER_TEST_PACKAGE_TOOLS=1 npm exec --yes --package=pnpm@10 --package=verdaccio@6 -- node --test tests/ui.test.cjs`. They verify pnpm build hooks, frozen production installation, registration/publishing rejection, and one cached tarball download across two independent app stores. Actual systemd isolation still requires disposable Ubuntu testing.

The generated offline icon bundle is included for host installation. To rebuild it and run frontend tests, use a current Node.js LTS release:

```bash
npm ci
npm run build:icons
npm run test:ui
npm run lint
python -m unittest discover -s tests -q
```

Python tests cover validation, framework detection, JSON/locking behavior, concurrent port allocation, health/metrics sampling, environment/runtime preflight, persistent storage, stale approval rejection, bounded/cancellable build slots, safe settings rejection, fast-forward Git updates, deployment recovery, static deployment, listener isolation, plugin update/restart failures, installer ownership boundaries, generated systemd hardening, and uninstall safety. Frontend tests exercise guided setup, external manager links/settings, unsupported legacy Docker registrations, environment masking and stdin submission, registration retries, search/filter behavior, preflight/metrics reports, exact-commit approval, storage/runtime settings, duplicate deployment prevention, and update/restart feedback.

POSIX shell and file-permission integration tests are skipped on Windows. Local unit tests and mocked browser tests are not a production deployment certification. Before production use, validate on a disposable Ubuntu host: Cockpit privilege escalation; account and systemd sandbox setup; browser links to Portainer and NPM; routing through NPM; one deployment per supported framework; health-failure recovery and cancellation; automatic deployments; and plugin updates reconnecting after the Cockpit restart. Confirm the existing root site and `stuserver-websites` service remain unchanged throughout.

GitHub Actions runs unit/UI checks and an explicitly gated Ubuntu VM integration job. The latter uses isolated managed roots, exercises static, Node.js, Go, Python, and Next.js deployments and systemd services, candidate replacement/failure recovery, checkout cleanup, persistent-file survival, real systemd metrics, local health, and SSH tag verification. It checks that existing Nginx files and legacy service state remain unchanged. React production builds and static activation have local unit/UI coverage; validate them on a disposable Ubuntu host before production use. Repository downloads/history and the failure-health boundary are fixture-controlled; this does not certify remote credentials, real HTTPS/Cloudflare routing, Cockpit/polkit, approval with real remote changes, cross-process queue cancellation after host interruption, or a full plugin-update/reconnect cycle. Those remain disposable-host acceptance checks.

Run only on a fresh disposable Ubuntu VM with systemd, Git, Node/npm, Go, and Python venv support:

```bash
sudo env APP_MANAGER_DISPOSABLE_UBUNTU=1 python3 -m unittest discover -s tests -p test_ubuntu_integration.py -v
```

Never set the disposable-VM flag on a production server. The suite starts real services and creates/removes its own dedicated test users, units, and paths.
