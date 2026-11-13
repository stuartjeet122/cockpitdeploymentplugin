# Native Python API and worker deployment

## Compatibility status

No Docker is used. This is an opt-in extension; existing Python/Gunicorn,
Node, Next.js, Go and static deployment defaults remain unchanged. The drum
transcription API is the first target, **not yet certified compatible**.
Its separate repository has not been edited. Real Ubuntu/systemd, model inference,
audio rendering and authenticated-prefix checks are still required.

Do not run setup on the live server until the disposable-host checks below pass.
Do not replace the root site, Cloudflare Tunnel, port 8080, or
`stuserver-websites`. Only the app's managed prefix and units are involved.

## Required API-side changes (approval required)

The inspected API reads only `REDIS_URL` and `CORS_ORIGINS` from the environment.
In its `backend/app/core/config.py`, `BASE_STORAGE_DIR`, `ADTOF_WEIGHTS_PATH`
and `OAF_ONNX_PATH` are relative `ClassVar` constants. Merely providing
`APP_STORAGE_*` variables does not change them and startup will try to write
inside a read-only release.

The smallest proposed change is to make these three paths environment-aware:

- `BASE_STORAGE_DIR`: use `APP_STORAGE_DIR` when set, retaining `storage` for local development.
- `ADTOF_WEIGHTS_PATH`: use `APP_MODELS_DIR/adtof`, retaining `models/adtof` locally.
- `OAF_ONNX_PATH`: use `APP_MODELS_DIR/oaf/model.onnx`, retaining the current local default.

Use typed settings/properties, validate absolute managed paths when supplied,
and test both defaults and configured paths. Existing upload/processed/export
properties and `get_job_store()` already derive from `BASE_STORAGE_DIR`, so
they should not need a separate persistence implementation. Neither service
should create, download, modify or delete model checkpoints at runtime.

The API currently permits custom namespaces such as `sessions/take-1` below
its storage root. The managed root is not writable: use `uploads/...` and
`exports/...` (or explicitly declare another writable top-level storage name).
For friendly 422 errors instead of filesystem errors, optionally restrict
caller-selected namespaces to configured writable roots. `models/` must never
be an upload or export namespace; checkpoint writes remain denied by the OS.

Supply `--root-path {base_path}` to Uvicorn. Alternatively, the API can set
FastAPI's `root_path` explicitly from `APP_BASE_PATH`; do not apply the prefix
twice. The current FastAPI constructor does not configure it. Verify Swagger's
OpenAPI URL, `servers`, download URLs and request-size middleware behavior
with the installed FastAPI/Starlette version behind the stripped prefix.
CORS is not authentication.

These are proposed changes only. Ask the owner before editing the separate API
repository. Do not copy model checkpoints, uploads, environment files or secrets
into Git. Add API tests proving manifests/uploads/exports survive a release
switch and both processes resolve identical absolute paths.

## Cockpit configuration

Register the application with framework **python**, disable automatic deployment
while preparing it, and leave **Deploy now** off. Edit **Build and runtime** and
**Native Python services**. The following illustrates the corresponding payload
fields; keep the real Git remote and credentials separate:

```json
{
  "framework": "python",
  "health_check_url": "/api/health",
  "auto_deploy": false,
  "build_config": {
    "python_interpreter": "python3.12",
    "start_command": [
      "{venv}/python", "-m", "uvicorn", "backend.app.main:app",
      "--host", "127.0.0.1", "--port", "{port}",
      "--workers", "1", "--root-path", "{base_path}"
    ]
  },
  "storage_directories": ["uploads", "processed", "exports", "jobs"],
  "required_environment": ["REDIS_URL", "CORS_ORIGINS"],
  "runtime_requirements": {"python3": "3.11"},
  "python_services": {
    "redis": true,
    "drum_prerequisites": true,
    "upload_limit_mb": 101,
    "access_control": "blocked",
    "model_files": [
      "adtof/adtof_frame_rnn_pytorch_weights.pth",
      "oaf/model.onnx"
    ],
    "worker": {
      "command": [
        "{venv}/python", "-m", "celery", "-A",
        "backend.app.workers.celery_app:celery_app", "worker",
        "--pool=prefork", "--concurrency=1", "--loglevel=INFO"
      ],
      "stop_timeout_seconds": 2400,
      "resource_limits": {"memory_mb": 4096, "cpu_percent": 200, "tasks_max": 128}
    }
  }
}
```

Limits above are examples, not measured capacity recommendations. Budget API,
worker, builds, Redis, Nginx and the existing sites together before deploying.
The worker setting is optional; omit it for an API without a background service.
This first worker profile supports Celery prefork with concurrency one, one unit
per app; beat, detach, autoscale and concurrency/pool overrides are rejected.
It does not manage a fleet or a scheduler. There must be no external worker
consuming this app's queue, including an old manually installed drum-worker unit.

Commands are arrays, not shell programs. Only `{port}`, `{venv}` (the release's
`.venv/bin`) and `{base_path}` are substituted, explicitly, for Python commands.
`$PORT`, `${PORT}`, command substitution and shell activation do not work.
Explicit commands skip the default Gunicorn installation. Repository builds,
pip hooks and app modules execute only as the dedicated unprivileged account.

`build_config.python_interpreter` selects the versioned system command used
for preflight, native prerequisite probes and release venv creation. The default
remains `python3`; accepted overrides are names such as `python3.11`, `python3.12`
and `python3.13`, never shell strings or arbitrary executable paths. The selected
command must already be on the isolated service account's system PATH. Missing
interpreters fail without falling back to the system Python. The API and worker
then use the same release venv through `{venv}/python`.

For the inspected drum pins, provision Python 3.11-3.13 alongside Ubuntu's
system Python, preferably 3.12 for the initial validation. Do not change
`/usr/bin/python3`, use alternatives to replace it, or reuse a venv created by
another interpreter. Updating this setting takes effect on the next deployment,
not an app restart. Administrator-provisioned Python must include matching
venv/ensurepip support; App Manager installs neither the interpreter nor OS packages.

If pip downloads `numpy-2.2.6.tar.gz` or `scipy-1.15.3.tar.gz` under Python 3.14,
it is falling back to source builds rather than compatible wheels. A missing
Fortran compiler is then a downstream failure, not a reason to provision a full
scientific build toolchain on the production server. Select a compatible
interpreter first. This does not certify every model package or pin: validate
the complete requirements and real CPU imports on disposable Ubuntu before use.

Save `REDIS_URL` and JSON `CORS_ORIGINS` using Cockpit's environment editor.
Both units read the same protected environment file outside Git and releases.
Do not put credentials in command arrays or configuration JSON. Do not log
environment values in app code. Native runtime log responses redact saved
values and Redis credentials, but arbitrary app-written/raw journal or file
logs cannot be guaranteed secret-free by the deployment manager.

## Administrator prerequisites

The manager never installs OS packages or configures Redis. On a disposable
Ubuntu host, an administrator must review and provision:

- Python 3.11-3.13 with its matching venv/ensurepip package. The package for
  Ubuntu's default interpreter alone may not supply venv support for an additional version.
- FFmpeg, libsndfile (usually `libsndfile1`), Fontconfig and suitable notation fonts.
- A headless-compatible MuseScore executable matching the API's search names:
  `mscore3`, `musescore3`, `drum-musescore`, or `mscore`. Package names and versions
  differ by Ubuntu release. Do not assume a GUI/AppImage works under the sandbox.
- The pinned CPU dependency set in the API's `requirements.txt`, including model
  libraries. App Manager installs Python dependencies into each release as the
  app user; it does not install apt packages or run repository installers as root.
- Trusted, licensed, version-matched checkpoints with verified checksums.
- A private, administrator-provisioned Redis listener. This profile supports
  explicit loopback `redis://` URLs only, with an explicit database number.

For Redis, use a dedicated instance or an explicitly allocated database and ACL
on an existing instance after checking other consumers. Never use an existing
default `/0` database simply because it answers PING. Allocate all Celery queue,
binding, pidbox, result and visibility keys for this app. A database number is
not a security boundary: ACL isolation and a dedicated instance are preferable.
Bind to loopback, retain protected mode/authentication, block public access,
and do not add a Cloudflare/Nginx route. Redis is not installed, reconfigured,
flushed, enumerated or shared automatically. Set the approved URL explicitly
in the environment editor; do not paste it into logs or shell history.

The probe performs only AUTH (when configured), SELECT and PING with three-second
socket timeouts and a 30-second process bound. It cannot prove database exclusivity,
worker connectivity or model correctness. Redis errors do not echo the URL.

The first deployment prepares the managed app account and directories before
checking prerequisites; it fails safely if they are missing. Then provision
models as the administrator under `/opt/webapps/<app-id>/shared/models/`.
The directory is root-owned, group-readable/traversable by `app-<app-id>`;
checkpoint directories should be root-owned mode 0750 and files mode 0640
with that group, never writable by the app. Do not place symlinks in model paths.
Use the exact relative filenames listed in `model_files`, not `.gitkeep` files.
No model download or repository code is run by a prerequisite check.

**Run preflight** for native apps (or CLI `prerequisites <app-id>`) reads only:
Selected Python version (3.11-3.13 for the drum profile) and venv modules, private Redis, executable lookup, libsndfile,
Fontconfig's font listing and nonempty/readable/non-writable artifacts. It does
not clone/pull, import the API, create a venv, initialize storage, or install
packages. Missing checks are reported in Cockpit. Account creation is a separate
deployment setup action. Generic repository/build preflight still runs during
deployment under the app account.

Both services receive `APP_STORAGE_DIR`, named `APP_STORAGE_*` paths and
`APP_MODELS_DIR`. Releases and models are mounted read-only. Only declared
persistent directories, app home, configuration and logs are writable.
`XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR`, `NUMBA_CACHE_DIR`,
`HOME`, and `QT_QPA_PLATFORM=offscreen` are supplied to both services; the Qt
runtime directory is private mode 0700. This supports headless Qt/MuseScore
and Numba without making releases writable. Real rendering still needs a
sandboxed Ubuntu test. Never relax hardening globally to get a render working.

## Routing and access

For a prefix `/drums/`, Nginx strips `/drums/` and proxies to the assigned
`127.0.0.1:8100-8999` listener. Internal API paths remain `/api/...`.
Swagger should be `/drums/docs`, OpenAPI `/drums/openapi.json`, and readiness
`/drums/api/health`. HTTP readiness checks use the direct loopback URL and
require a 2xx response; binding is independently checked for loopback only.
The drum health endpoint can return 200 `degraded` for missing models or
MuseScore, so the separate prerequisite checks are required.

`client_max_body_size 101m` is emitted only inside this app's location, allowing
the API's 100-MiB audio limit plus multipart overhead. The API still enforces
its own request/upload limits. Cloudflare plan/body/timeout limits may be lower;
App Manager does not change them or any unrelated Nginx location.

The default `access_control: blocked` emits `deny all` for the whole app prefix.
Local readiness still works. Do not use a loopback allow rule in Nginx as an
authentication substitute: a local Tunnel/proxy can make public requests look
local. Select `external-auth` **only after** the administrator protects the
entire prefix with authentication (for example a verified Cloudflare Access
policy) and blocks all origin bypasses. This value is an explicit administrator
attestation, not an implementation of authentication. App Manager never changes
the Tunnel or global/root-site access rules. Keep the route blocked otherwise.
Protected public health probes may fail; do not disable authentication to make
the dashboard green.

## Lifecycle, recovery and operations

The units are `cockpit-app-<app-id>.service` (or the existing secondary web slot)
and `cockpit-app-<app-id>@worker.service`. Both use the same unprivileged account,
protected environment and persistent directories. Each has independent limits
and stdout/stderr logs; worker logs are included in log rotation.

The old runtime may serve during a build. Activation deliberately has downtime:
stop the old worker, confirm its cgroup is empty, stop the API, replace both
units with concrete release paths, start/check the new API, then start exactly
one worker. There is no staged second worker and no zero-downtime worker promise.
The worker's systemd active state is checked, not Celery broker/task readiness;
verify a real queued job after deployment.

SIGTERM requests Celery's warm shutdown. `KillMode=mixed` sends TERM to the main
worker; after `TimeoutStopSec`, systemd kills the entire remaining cgroup.
The helper allows that timeout plus 15 seconds and refuses replacement if the
unit or cgroup is still populated/unknown. The example 2400-second bound exceeds
the API's 2100-second task hard limit, but must be confirmed for real workloads.
Late acknowledgements can cause redelivery after forced termination; tasks must
be idempotent and a code update does not guarantee exactly-once execution.

If API readiness or worker startup fails, the manager stops/confirms both new
services, restores the old unit bytes and release pointer, checks the previous
API and restarts its worker if it was previously active. Failed stop/recovery
retains the candidate release and reports a rollback error instead of deleting
files potentially used by a running process. Persistent storage/models are
never pruned during redeployment or rollback. The current release is retained;
successful deployment normally prunes old releases, so an arbitrary historical
rollback is not promised. Rollback of an available release coordinates both
units, but **code rollback cannot undo persistent-data changes**. Back up the
complete storage, models and configuration consistently with both services
stopped before a schema/data change. Explicit destructive app removal still
deletes managed data after stopping its services.

App start/stop/restart coordinates the API and worker. Cockpit also offers worker
status, logs and restart; CLI equivalents are `worker-status`, `worker-logs` and
`worker-restart`, each followed by the app ID. No queue purge is performed.
For host power loss or a killed deployment helper during activation, inspect
both units and retained releases before retrying; exception-based recovery
cannot execute while the helper is dead.

## Disposable Ubuntu acceptance checklist

1. Record hashes/state for the root site, existing Nginx/Tunnel config,
   `stuserver-websites` and the 8080 listener; confirm all remain unchanged.
2. Apply approved API-side settings changes and their tests. Verify both processes
   see the same absolute storage/model paths, never release-relative ones.
3. Check missing Python/venv, Redis refusal/timeout/auth/database errors, missing
   FFmpeg/libsndfile/MuseScore/fonts and missing/unreadable/writable checkpoints
   in Cockpit. Confirm checks cause no installations or Redis writes.
4. Verify generated units with `systemd-analyze verify`, account/cgroup limits,
   writable private caches, read-only releases/models and no public app/Redis
   listeners. Confirm repository/build hooks never execute as root.
5. Upload audio, enqueue one real job and fetch MIDI/MusicXML/PDF through the
   authenticated prefix; verify Swagger/OpenAPI, CORS, body limits and headless
   MuseScore/Numba in the actual sandbox.
6. During a long task, redeploy and restart the worker. Confirm no overlap of
   old/new worker processes and record graceful versus timeout/redelivery results.
7. Force API health failure, worker startup failure, stuck shutdown and interrupted
   activation. Verify both services' recovery, retained release files where
   process state is uncertain, and byte-identical persistent files.
8. Test rollback when a release is available, then a backup restore separately.
   Test app stop/start, logs/status/restart and guarded removal/uninstall on the
   disposable VM only. Do not use a production host for these checks.