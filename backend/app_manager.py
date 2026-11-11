#!/usr/bin/env python3
from __future__ import annotations

import argparse
import codecs
import contextlib
import errno
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - used on Windows development hosts
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - used on POSIX hosts
    msvcrt = None

try:
    import pwd
except ImportError:  # pragma: no cover - Windows has no POSIX account database
    pwd = None

try:
    import grp
except ImportError:  # pragma: no cover - Windows has no POSIX group database
    grp = None

try:
    from .adapters import detect_framework, get_adapter, inspect_project
except ImportError:  # pragma: no cover - direct execution after installation
    from adapters import detect_framework, get_adapter, inspect_project

APP_NAME = "app-manager"
DEFAULT_STATE_ROOT = "/var/lib/cockpit/app-manager"
DEFAULT_CONFIG_PATH = f"{DEFAULT_STATE_ROOT}/config.json"
DEFAULT_REGISTRY_PATH = f"{DEFAULT_STATE_ROOT}/apps.json"
DEFAULT_PRODUCTION_ROOT = "/opt/webapps"
DEFAULT_SOURCE_ROOT = f"{DEFAULT_STATE_ROOT}/sources"
DEFAULT_LOG_ROOT = "/var/log/cockpit-app-manager"
DEFAULT_NGINX_SNIPPET_DIR = "/etc/nginx/snippets/cockpit-app-manager"
DEFAULT_NGINX_CONFIG_ROOT = "/etc/nginx"
NGINX_MANAGED_INCLUDE = f"include {DEFAULT_NGINX_SNIPPET_DIR}/*.conf;"
DEFAULT_PUBLIC_ORIGIN = "https://stuserver.online"
DEFAULT_PLUGIN_VERSION_PATH = f"{DEFAULT_STATE_ROOT}/plugin-version.json"
PLUGIN_REPOSITORY_URL = "https://github.com/stuartjeet122/cockpitdeploymentplugin.git"
PLUGIN_REPOSITORY_BRANCH = "main"
PLUGIN_UPDATE_USER = "nobody"
PLUGIN_TRUSTED_SIGNERS = "/etc/cockpit/app-manager/trusted-signers"
PORT_START = 8100
PORT_END = 8999
RESERVED_PORTS = {8080}
COMMAND_TIMEOUT_SECONDS = 30
MAX_DEPLOYMENT_LOG_BYTES = 5 * 1024 * 1024
MAX_ENV_FILE_BYTES = 64 * 1024
APP_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
APP_ROUTE_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
FRAMEWORKS = {"auto", "nextjs", "go", "node", "python", "static"}
RESOURCE_DEFAULTS = {
    "memory_mb": 1024, "cpu_percent": 100, "tasks_max": 256,
    "build_memory_mb": 2048, "build_cpu_percent": 100, "build_tasks_max": 512,
}


def _resource_limits(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict) or set(value) - RESOURCE_DEFAULTS.keys():
        raise ValueError("Resource limits must be an object with supported limit names")
    limits = {**RESOURCE_DEFAULTS, **value}
    for key, limit in limits.items():
        maximum = 1048576 if key.endswith("memory_mb") else 10000
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"Resource limit {key} must be an integer from 1 to {maximum}")
    return limits


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_config() -> Dict[str, Any]:
    return {
        "port_range": {"start": PORT_START, "end": PORT_END, "reserved": sorted(RESERVED_PORTS)},
        "source_root": DEFAULT_SOURCE_ROOT,
        "production_root": DEFAULT_PRODUCTION_ROOT,
        "registry_path": DEFAULT_REGISTRY_PATH,
        "allowed_source_dirs": ["/opt", "/home/stuserver", "/srv", DEFAULT_SOURCE_ROOT],
        "default_branch": "main",
        "release_retention": 1,
        "build_timeout_seconds": 900,
        "health_timeout_seconds": 30,
        "command_timeout_seconds": COMMAND_TIMEOUT_SECONDS,
        "min_free_disk_mb": 512,
        "max_concurrent_builds": 1,
        "log_root": DEFAULT_LOG_ROOT,
        "nginx_template": "/etc/cockpit/app-manager/templates/nginx.conf.tpl",
        "service_template": "/etc/cockpit/app-manager/templates/app.service.tpl",
    }


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
        raise


@contextlib.contextmanager
def _file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    _assert_no_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "r+b")
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            locked = True
        elif msvcrt is not None:  # pragma: no cover - Windows development host
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    if not blocking or exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.01)
        else:  # pragma: no cover - unsupported platform
            raise RuntimeError("No supported file-locking implementation is available")
        yield
    finally:
        if locked and fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif locked and msvcrt is not None:  # pragma: no cover - Windows development host
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        _atomic_json_write(path, default)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    _atomic_json_write(path, payload)


def get_config() -> Dict[str, Any]:
    config_path = Path(DEFAULT_CONFIG_PATH)
    with _file_lock(config_path.with_name(config_path.name + ".lock")):
        loaded = load_json(config_path, _default_config())
    if not isinstance(loaded, dict):
        raise ValueError("Configuration must contain a JSON object")
    config = _default_config()
    config.update(loaded)
    config["release_retention"] = 1
    # Registry location is fixed so configuration cannot redirect privileged writes.
    config["registry_path"] = DEFAULT_REGISTRY_PATH
    configured_roots = (
        ("source_root", DEFAULT_SOURCE_ROOT),
        ("production_root", DEFAULT_PRODUCTION_ROOT),
        ("log_root", DEFAULT_LOG_ROOT),
    )
    for key, allowed_root in configured_roots:
        candidate = _resolved_path(Path(config[key]))
        if not _path_is_within(candidate, Path(allowed_root)):
            raise ValueError(f"Configured {key} resolves outside its managed directory")
        config[key] = str(candidate)
    if not isinstance(config["allowed_source_dirs"], list) or not all(
        isinstance(root, str) and root and "\0" not in root for root in config["allowed_source_dirs"]
    ):
        raise ValueError("allowed_source_dirs must be an array of paths")
    port_range = config.get("port_range")
    if not isinstance(port_range, dict):
        raise ValueError("port_range must be a JSON object")
    start, end = port_range.get("start"), port_range.get("end")
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or start < PORT_START or end > PORT_END or start > end
    ):
        raise ValueError("Configured port range must stay within 8100-8999")
    for key in ("build_timeout_seconds", "health_timeout_seconds", "command_timeout_seconds"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
            _validate_capacity_settings(config)
    return config


def get_registry() -> Dict[str, Any]:
    registry_path = Path(DEFAULT_REGISTRY_PATH)
    with _file_lock(registry_path.with_name(registry_path.name + ".lock")):
        registry = load_json(registry_path, {"apps": []})
    if not isinstance(registry, dict) or not isinstance(registry.get("apps"), list):
        raise ValueError("Application registry must contain an 'apps' array")
    _validate_registry(registry)
    return registry


def save_registry(registry: Dict[str, Any]) -> None:
    if not isinstance(registry, dict) or not isinstance(registry.get("apps"), list):
        raise ValueError("Application registry must contain an 'apps' array")
    registry_path = Path(DEFAULT_REGISTRY_PATH)
    with _file_lock(registry_path.with_name(registry_path.name + ".lock")):
        _atomic_json_write(registry_path, registry)


@contextlib.contextmanager
def _registry_transaction() -> Iterator[Dict[str, Any]]:
    registry_path = Path(DEFAULT_REGISTRY_PATH)
    with _file_lock(registry_path.with_name(registry_path.name + ".lock")):
        registry = load_json(registry_path, {"apps": []})
        if not isinstance(registry, dict) or not isinstance(registry.get("apps"), list):
            raise ValueError("Application registry must contain an 'apps' array")
        _validate_registry(registry)
        yield registry
        _atomic_json_write(registry_path, registry)


def ensure_runtime_dirs() -> Dict[str, str]:
    config = get_config()
    state_root = Path(DEFAULT_STATE_ROOT)
    source_root = Path(config["source_root"])
    production_root = Path(config["production_root"])
    log_root = Path(config["log_root"])
    for path in (state_root, source_root, production_root, log_root):
        path.mkdir(parents=True, exist_ok=True, mode=0o711 if path in (state_root, source_root) else 0o755)
    os.chmod(state_root, 0o711)
    os.chmod(source_root, 0o711)
    return {
        "source_root": str(source_root),
        "production_root": str(production_root),
        "log_root": str(log_root),
    }


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:24].strip("-") or "app"


def validate_app_id(value: Any) -> str:
    if not isinstance(value, str) or not APP_ID_RE.fullmatch(value):
        raise ValueError("Application ID must be 1-24 lowercase letters, digits, or internal hyphens")
    return value


def validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Website name must be a string")
    name = value.strip()
    if not name or len(name) > 100 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("Website name must be 1-100 printable characters")
    return name


def validate_git_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(char.isspace() or ord(char) < 32 for char in value):
        return False
    if value.startswith("git@"):
        match = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_./-]+)", value)
        if not match:
            return False
        host, path = match.groups()
        return (
            bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host))
            and ".." not in host
            and not path.startswith("-")
            and ".." not in path.split("/")
        )
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path:
            return False
        if parsed.password is not None or (parsed.scheme == "https" and parsed.username is not None):
            return False
        if (
            parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", parsed.hostname)
            or ".." in parsed.hostname
            or ".." in parsed.path.split("/")
        ):
            return False
        if parsed.username is not None and (
            not re.fullmatch(r"[A-Za-z0-9._-]+", parsed.username) or parsed.username.startswith("-")
        ):
            return False
        path_pattern = r"/?[A-Za-z0-9._/-]+" if parsed.scheme == "ssh" else r"/[A-Za-z0-9._~/%+-]+"
        if not re.fullmatch(path_pattern, parsed.path) or parsed.path.startswith("-"):
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        return True
    except ValueError:
        return False


def validate_branch(value: Any) -> str:
    if not isinstance(value, str) or not BRANCH_RE.fullmatch(value):
        raise ValueError("Branch must be a valid Git branch name")
    components = value.split("/")
    if (
        value.startswith("-") or value.endswith(("/", ".", ".lock"))
        or ".." in value or "@{" in value or "//" in value
        or any(component.startswith(".") or component.endswith(".") or component.endswith(".lock") for component in components)
    ):
        raise ValueError("Branch must be a valid Git branch name")
    return value


def _resolved_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        _resolved_path(path).relative_to(_resolved_path(root))
        return True
    except ValueError:
        return False


def validate_allowed_path(value: Any, allowed_roots: Sequence[str]) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Path must be a non-empty string")
    candidate = _resolved_path(Path(value))
    if not any(_path_is_within(candidate, Path(root)) for root in allowed_roots):
        raise ValueError("Path resolves outside the configured allowed directories")
    return candidate


def _validate_app_directory_setting(value: Any) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\0" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(
            "build_config.app_directory must be a non-empty relative path without '.', '..', or empty components"
        )


def validate_app_route(value: Any, app_id: str) -> str:
    if value is None:
        value = f"/{app_id}/"
    if (
        not isinstance(value, str)
        or len(value) > 256
        or not value.startswith("/")
        or "//" in value
    ):
        raise ValueError(
            "Route must be a URL path prefix such as /my-application/ "
            "using lowercase letters, numbers, and hyphens"
        )
    route_parts = value.strip("/").split("/")
    if (
        not route_parts
        or any(not APP_ROUTE_SEGMENT_RE.fullmatch(part) for part in route_parts)
    ):
        raise ValueError(
            "Route must be a URL path prefix such as /my-application/ "
            "using lowercase letters, numbers, and hyphens"
        )
    return value.rstrip("/") + "/"


def _public_url_for_route(route: str) -> str:
    return f"{DEFAULT_PUBLIC_ORIGIN}{route}"


def validate_app_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Application payload must be a JSON object")
    name = validate_name(payload.get("name"))
    app_id = payload.get("id", payload.get("slug"))
    if app_id is None or app_id == "":
        app_id = sanitize_slug(name)
    app_id = validate_app_id(app_id)
    route = validate_app_route(payload.get("route"), app_id)
    repo_url = payload.get("repo_url")
    if not validate_git_url(repo_url):
        raise ValueError("Repository URL must be a valid HTTPS or SSH Git remote")
    branch = validate_branch(payload.get("branch") or get_config().get("default_branch", "main"))
    framework = payload.get("framework") or "auto"
    if not isinstance(framework, str) or framework not in FRAMEWORKS:
        raise ValueError(f"Framework must be one of: {', '.join(sorted(FRAMEWORKS))}")
    build_config = payload.get("build_config") or {}
    if not isinstance(build_config, dict):
        raise ValueError("Build configuration must be a JSON object")
    _validate_app_directory_setting(build_config.get("app_directory"))
    if "start_command" in build_config:
        command = build_config["start_command"]
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part and not any(ord(char) < 32 or ord(char) == 127 for char in part)
            for part in command
        ):
            raise ValueError("build_config.start_command must be a non-empty array of command arguments")
    health_path = payload.get("health_check_url") or "/"
    if not isinstance(health_path, str) or not health_path.startswith("/") or health_path.startswith("//"):
        raise ValueError("Health-check URL must be a path beginning with one slash")
    if any(ord(char) < 32 for char in health_path):
        raise ValueError("Health-check URL contains control characters")
    result: Dict[str, Any] = dict(payload)
    result.update({
        "id": app_id,
        "slug": app_id,
        "name": name,
        "repo_url": repo_url,
        "branch": branch,
        "framework": framework,
        "route": route,
        "build_config": build_config,
        "health_check_url": health_path,
    })
    description = payload.get("description", "")
    if not isinstance(description, str) or len(description) > 2000 or any(ord(char) < 32 and char not in "\t" for char in description):
        raise ValueError("Description must be a string of at most 2000 printable characters")
    result["description"] = description
    result["resource_limits"] = _resource_limits(payload.get("resource_limits", {}))
    enabled = payload.get("auto_deploy", True)
    if not isinstance(enabled, bool):
        raise ValueError("auto_deploy must be a boolean")
    window = payload.get("deployment_window", {})
    if not isinstance(window, dict) or set(window) - {"start", "end"}:
        raise ValueError("Deployment window must contain start and end UTC times")
    if window and (set(window) != {"start", "end"} or any(
        not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
        for value in window.values()
    )):
        raise ValueError("Deployment window requires start and end in HH:MM UTC")
    result["auto_deploy"] = enabled
    result["deployment_window"] = window
    required = payload.get("required_environment", [])
    if not isinstance(required, list) or len(required) > 256 or any(not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key) for key in required):
        raise ValueError("required_environment must be an array of environment variable names")
    requirements = payload.get("runtime_requirements", {})
    if not isinstance(requirements, dict) or set(requirements) - {"node", "npm", "pnpm", "yarn", "bun", "go", "python3"} or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){0,2}", value) for value in requirements.values()
    ):
        raise ValueError("runtime_requirements must map supported tools to minimum numeric versions")
    storage = payload.get("storage_directories", ["data"])
    if not isinstance(storage, list) or len(storage) > 16 or any(not isinstance(name, str) or not APP_ROUTE_SEGMENT_RE.fullmatch(name) for name in storage):
        raise ValueError("storage_directories must contain at most 16 lowercase directory names")
    environment_names = ["APP_STORAGE_" + name.upper().replace("-", "_") for name in dict.fromkeys(storage)]
    if "APP_STORAGE_DIR" in environment_names or len(set(environment_names)) != len(environment_names):
        raise ValueError("Persistent directory names conflict with managed environment names; dir is reserved")
    mode = payload.get("deployment_mode", "automatic")
    if not isinstance(mode, str) or mode not in {"automatic", "approval"}:
        raise ValueError("deployment_mode must be automatic or approval")
    result.update(required_environment=list(dict.fromkeys(required)), runtime_requirements=requirements,
                  storage_directories=list(dict.fromkeys(["data", *storage])), deployment_mode=mode)
    if len(result["storage_directories"]) > 16:
        raise ValueError("storage_directories allows at most 16 names including data")
    if "port" in payload and payload["port"] is not None:
        port = payload["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not PORT_START <= port <= PORT_END or port in RESERVED_PORTS:
            raise ValueError(f"Port must be an integer from {PORT_START} through {PORT_END}, excluding reserved ports")
        result["port"] = port
    if payload.get("source_directory"):
        result["source_directory"] = str(validate_allowed_path(
            payload["source_directory"],
            get_config().get("allowed_source_dirs", _default_config()["allowed_source_dirs"]),
        ))
    return result


def _parse_ss_ports(output: str) -> Set[int]:
    ports: Set[int] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        address = fields[3]
        match = re.search(r":(\d+)$", address)
        if match:
            ports.add(int(match.group(1)))
    return ports


def _proc_listening_ports() -> Optional[Set[int]]:
    ports: Set[int] = set()
    readable = False
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        readable = True
        for line in lines:
            fields = line.split()
            if len(fields) > 3 and fields[3] == "0A":
                try:
                    ports.add(int(fields[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
    return ports if readable else None


def get_listening_ports() -> Set[int]:
    try:
        result = subprocess.run(
            ["ss", "-H", "-ltn"],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return _parse_ss_ports(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    proc_ports = _proc_listening_ports()
    if proc_ports is not None:
        return proc_ports
    if os.name == "nt":
        return set()
    raise RuntimeError("Unable to inspect listening TCP sockets (ss and /proc unavailable)")


def _socket_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def _select_port(registry: Dict[str, Any], config: Dict[str, Any]) -> int:
    used = {app.get("port") for app in registry["apps"]}
    used.update(app.get("candidate_port") for app in registry["apps"])
    _clear_expired_port_reservations(registry)
    reservations = {int(port) for port in registry.get("port_reservations", {})}
    used.update(reservations)
    listening = get_listening_ports()
    port_range = config.get("port_range", {})
    start = int(port_range.get("start", PORT_START))
    end = int(port_range.get("end", PORT_END))
    if start < PORT_START or end > PORT_END or start > end:
        raise ValueError("Configured port range must stay within 8100-8999")
    reserved = set(RESERVED_PORTS)
    reserved.update(port_range.get("reserved", []))
    for port in range(start, end + 1):
        if port not in reserved and port not in used and port not in listening and _socket_port_available(port):
            return port
    raise RuntimeError("No available ports remaining in the configured range")


def pick_port() -> int:
    config = get_config()
    with _registry_transaction() as registry:
        port = _select_port(registry, config)
        reservations = registry.setdefault("port_reservations", {})
        reservations[str(port)] = time.time() + 600
        return port


def _clear_expired_port_reservations(registry: Dict[str, Any]) -> None:
    reservations = registry.setdefault("port_reservations", {})
    if not isinstance(reservations, dict):
        raise ValueError("Registry port_reservations must be a JSON object")
    now = time.time()
    for port, expiry in list(reservations.items()):
        try:
            valid_port = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError("Registry contains an invalid port reservation") from exc
        if valid_port < PORT_START or valid_port > PORT_END or valid_port in RESERVED_PORTS:
            raise ValueError("Registry contains an invalid port reservation")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            raise ValueError("Registry contains an invalid port reservation expiry")
        if expiry <= now:
            del reservations[port]


class DeploymentAborted(RuntimeError):
    pass


def _terminate_process(process: subprocess.Popen) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        if process.poll() is None:
            process.wait()
    elif process.poll() is None:  # pragma: no cover - deployment cancellation is used on Ubuntu
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _run_cancellable_command(
    args: Sequence[str],
    cwd: Optional[Path],
    timeout: int,
    env: Optional[Dict[str, str]],
    cancel_file: Path,
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=os.name != "nt",
    )
    deadline = time.monotonic() + max(1, int(timeout))
    while True:
        if cancel_file.exists():
            _terminate_process(process)
            raise DeploymentAborted("Deployment aborted by request")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process)
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"Command timed out after {timeout} seconds: {args[0]}"
                + (f"\nstdout:\n{stdout.decode('utf-8', errors='replace').rstrip()}" if stdout else "")
                + (f"\nstderr:\n{stderr.decode('utf-8', errors='replace').rstrip()}" if stderr else "")
            )
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return subprocess.CompletedProcess(
                list(args),
                process.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        except subprocess.TimeoutExpired:
            continue


def run_command(
    args: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    env: Optional[Dict[str, str]] = None,
    live_log_path: Optional[Path] = None,
    redact_values: Optional[Sequence[str]] = None,
    cancel_file: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    if not args or not all(isinstance(arg, str) and "\0" not in arg for arg in args):
        raise ValueError("Subprocess arguments must be a non-empty list of strings")
    values = list(env.values()) if redact_values is None and env else list(redact_values or ())
    try:
        if live_log_path is not None:
            return _run_command_with_live_log(
                args, cwd, timeout, env, live_log_path, values, cancel_file
            )
        if cancel_file is not None:
            result = _run_cancellable_command(args, cwd, timeout, env, cancel_file)
            return subprocess.CompletedProcess(
                result.args,
                result.returncode,
                _redact_text(result.stdout or "", values),
                _redact_text(result.stderr or "", values),
            )
        result = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1, int(timeout)),
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            _redact_text(result.stdout or "", values),
            _redact_text(result.stderr or "", values),
        )
    except subprocess.TimeoutExpired as exc:
        details = []
        for label, output in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            if output:
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                details.append(f"{label}:\n{_redact_text(output.rstrip(), values)}")
        detail = "\n".join(details)
        raise RuntimeError(
            f"Command timed out after {timeout} seconds: {args[0]}"
            + (f"\n{detail}" if detail else "")
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run {args[0]}: {exc}") from exc


def _run_command_with_live_log(
    args: Sequence[str],
    cwd: Optional[Path],
    timeout: int,
    env: Optional[Dict[str, str]],
    live_log_path: Path,
    redact_values: Optional[Sequence[str]] = None,
    cancel_file: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    live_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if live_log_path.is_symlink() or (live_log_path.exists() and not live_log_path.is_file()):
        raise RuntimeError("Refusing to write a deployment log through a non-regular file")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    reader_errors = []
    write_lock = threading.Lock()
    log_truncated = False
    sensitive_values = sorted(
        {value for value in (redact_values or ()) if isinstance(value, str) and value},
        key=len,
        reverse=True,
    )

    def append_live_text(channel: str, text: str) -> None:
        nonlocal log_truncated
        if not text or log_truncated:
            return
        marker = b"\n[deployment output truncated at 5 MiB]\n"
        with write_lock:
            current_size = live_log_path.stat().st_size if live_log_path.exists() else 0
            write_limit = MAX_DEPLOYMENT_LOG_BYTES - len(marker)
            remaining = max(0, write_limit - current_size)
            encoded = f"[{channel}] {text}".encode("utf-8")
            with live_log_path.open("ab") as handle:
                if remaining:
                    handle.write(encoded[:remaining])
                if len(encoded) > remaining:
                    handle.write(marker)
                    log_truncated = True
                handle.flush()
    process = subprocess.Popen(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=os.name != "nt",
    )

    def consume(channel: str, stream: Any) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        log_write_failed = False
        pending_text = ""

        def emit_redacted(text: str, final: bool = False) -> None:
            nonlocal log_write_failed, pending_text
            pending_text += text
            if final:
                safe_end = len(pending_text)
            elif sensitive_values:
                safe_end = max(0, len(pending_text) - len(sensitive_values[0]) + 1)
            else:
                safe_end = len(pending_text)
            while safe_end > 0 and not final:
                crossing = [
                    index
                    for secret in sensitive_values
                    for index in range(max(0, safe_end - len(secret) + 1), safe_end)
                    if pending_text.startswith(secret, index) and index + len(secret) > safe_end
                ]
                if not crossing:
                    break
                safe_end = min(crossing)
            if safe_end:
                safe_text = _redact_text(pending_text[:safe_end], sensitive_values)
                pending_text = pending_text[safe_end:]
                if not log_write_failed:
                    try:
                        append_live_text(channel, safe_text)
                    except Exception as exc:
                        reader_errors.append(exc)
                        log_write_failed = True

        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                output[channel].extend(chunk)
                emit_redacted(decoder.decode(chunk))
            remaining = decoder.decode(b"", final=True)
            emit_redacted(remaining, final=True)
        except Exception as exc:
            reader_errors.append(exc)
            while True:
                try:
                    if not os.read(stream.fileno(), 4096):
                        break
                except OSError:
                    break

    threads = [
        threading.Thread(target=consume, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=consume, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + max(1, int(timeout))
    try:
        while process.poll() is None:
            if cancel_file is not None and cancel_file.exists():
                cancelled = True
                _terminate_process(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process(process)
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        returncode = process.wait()
    finally:
        for thread in threads:
            thread.join()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    stdout = _redact_text(output["stdout"].decode("utf-8", errors="replace"), sensitive_values)
    stderr = _redact_text(output["stderr"].decode("utf-8", errors="replace"), sensitive_values)
    if timed_out:
        details = []
        if stdout:
            details.append(f"stdout:\n{stdout.rstrip()}")
        if stderr:
            details.append(f"stderr:\n{stderr.rstrip()}")
        detail = "\n".join(details)
        raise RuntimeError(
            f"Command timed out after {timeout} seconds: {args[0]}"
            + (f"\n{detail}" if detail else "")
        )
    if cancelled:
        raise DeploymentAborted("Deployment aborted by request")
    if reader_errors:
        raise RuntimeError(f"Could not write live deployment output: {reader_errors[0]}")
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def _redact_text(text: str, values: Sequence[str]) -> str:
    for value in sorted({item for item in values if isinstance(item, str) and item}, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def _command_error(result: subprocess.CompletedProcess, description: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-4000:]
        raise RuntimeError(f"{description} failed (exit {result.returncode})" + (f": {detail}" if detail else ""))


def _require_root() -> None:
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise PermissionError("This operation requires the privileged Cockpit backend")


def _account_name(app_id: str) -> str:
    return f"app-{validate_app_id(app_id)}"


def _home_path(app_id: str) -> Path:
    return Path(DEFAULT_STATE_ROOT) / "homes" / validate_app_id(app_id)


def _account_exists(username: str) -> bool:
    if pwd is None:
        return False
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def ensure_app_user(app_id: str) -> str:
    _require_root()
    username = _account_name(app_id)
    home = _home_path(app_id)
    home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _account_exists(username):
        result = run_command(
            ["useradd", "--system", "--user-group", "--home-dir", str(home), "--create-home", "--shell", "/usr/sbin/nologin", username],
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 and not _account_exists(username):
            _command_error(result, "Creating the application service account")
    if pwd is not None:
        account = pwd.getpwnam(username)
        if Path(account.pw_dir).resolve() != home.resolve():
            raise RuntimeError(f"Existing account {username} has an unexpected home directory")
        if account.pw_uid == 0 or account.pw_shell != "/usr/sbin/nologin":
            raise RuntimeError(f"Existing account {username} is not a dedicated unprivileged service account")
        if grp is not None and grp.getgrgid(account.pw_gid).gr_name != username:
            raise RuntimeError(f"Existing account {username} does not have its dedicated group")
        os.chown(str(home), account.pw_uid, account.pw_gid)
    return username


def _account_ids(username: str) -> Tuple[int, int]:
    if pwd is None:
        raise RuntimeError("Application accounts are only supported on POSIX systems")
    try:
        account = pwd.getpwnam(username)
    except KeyError as exc:
        raise RuntimeError(f"Application service account {username} does not exist") from exc
    return account.pw_uid, account.pw_gid


def _chown_tree(path: Path, username: str) -> None:
    uid, gid = _account_ids(username)
    if path.is_symlink():
        os.chown(str(path), uid, gid, follow_symlinks=False)
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        os.chown(str(root_path), uid, gid, follow_symlinks=False)
        for name in dirs + files:
            child = root_path / name
            os.chown(str(child), uid, gid, follow_symlinks=False)


def _seal_release(release_dir: Path, group_id: int, *, public: bool = False) -> None:
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise RuntimeError(f"Cannot seal invalid application release {release_dir}")
    for root, dirs, files in os.walk(release_dir, topdown=True, followlinks=False):
        root_path = Path(root)
        os.chown(str(root_path), 0, group_id)
        os.chmod(str(root_path), 0o555 if public else 0o550)
        for entries, is_directory in ((dirs, True), (files, False)):
            for name in entries:
                child = root_path / name
                if child.is_symlink():
                    if public and not _path_is_within(child.resolve(strict=False), release_dir.resolve(strict=False)):
                        raise RuntimeError(f"Static release symlink escapes the application release: {child}")
                    os.chown(str(child), 0, group_id, follow_symlinks=False)
                    continue
                if is_directory:
                    permissions = 0o555 if public else 0o550
                else:
                    mode = child.stat(follow_symlinks=False).st_mode
                    executable = bool(mode & 0o111)
                    execute_bits = 0o111 if public else 0o110
                    permissions = (0o444 if public else 0o440) | (execute_bits if executable else 0)
                os.chown(str(child), 0, group_id)
                os.chmod(str(child), permissions)


def _run_as_user(
    username: str,
    args: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    environment: Optional[Dict[str, str]] = None,
    live_log_path: Optional[Path] = None,
    cancel_file: Optional[Path] = None,
    resource_limits: Optional[Dict[str, int]] = None,
) -> subprocess.CompletedProcess:
    _require_root()
    if os.name == "nt":
        raise RuntimeError("Running application commands as a separate Linux user requires Ubuntu")
    if not _account_exists(username):
        raise RuntimeError(f"Application service account {username} does not exist")
    home = str(_home_path(username.removeprefix("app-")))
    env_args = [
        "env",
        "-i",
        f"HOME={home}",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_TERMINAL_PROMPT=0",
    ]
    for key, value in (environment or {}).items():
        if not ENV_KEY_RE.fullmatch(key) or not isinstance(value, str) or "\0" in value:
            raise ValueError("Invalid application environment variable")
        env_args.append(f"{key}={value}")
    command = ["runuser", "--user", username, "--", *env_args, *args]
    if resource_limits is not None:
        limits = _resource_limits(resource_limits)
        command = [
            "systemd-run", "--scope", "--quiet", "--collect",
            f"--property=MemoryMax={limits['build_memory_mb']}M",
            f"--property=CPUQuota={limits['build_cpu_percent']}%",
            f"--property=TasksMax={limits['build_tasks_max']}",
            "--", *command,
        ]
    return run_command(
        command,
        cwd=cwd,
        timeout=timeout,
        live_log_path=live_log_path,
        redact_values=list((environment or {}).values()),
        cancel_file=cancel_file,
    )


def _run_checked_as_user(
    username: str,
    args: Sequence[str],
    cwd: Path,
    timeout: int,
    description: str,
    environment: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    result = _run_as_user(username, args, cwd, timeout, environment)
    _command_error(result, description)
    return result


def _git(
    repo_path: Path,
    username: Optional[str],
    args: Sequence[str],
    timeout: int,
    cancel_file: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(repo_path), *args]
    if username is None:
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            raise PermissionError("Git must run as the application's unprivileged service account")
        return run_command(command, timeout=timeout, cancel_file=cancel_file)
    return _run_as_user(username, command, timeout=timeout, cancel_file=cancel_file)


def clone_or_update_repo(
    repo_url: str,
    repo_path: Path,
    branch: str,
    username: Optional[str] = None,
    timeout: Optional[int] = None,
    cancel_file: Optional[Path] = None,
) -> Dict[str, Any]:
    if not validate_git_url(repo_url):
        raise ValueError("Repository URL must be a valid HTTPS or SSH Git remote")
    validate_branch(branch)
    command_timeout = int(timeout or get_config().get("command_timeout_seconds", COMMAND_TIMEOUT_SECONDS))
    repo_path = _resolved_path(repo_path)
    config = get_config()
    allowed = [config["source_root"]]
    if not any(_path_is_within(repo_path, Path(root)) and repo_path != _resolved_path(Path(root)) for root in allowed):
        raise ValueError("Repository path must be a child of the configured source root")

    if repo_path.exists() and not (repo_path.is_dir() and not any(repo_path.iterdir())):
        if not repo_path.is_dir():
            raise ValueError("Repository path exists and is not a directory")
        top_level = _git(repo_path, username, ["rev-parse", "--show-toplevel"], command_timeout, cancel_file)
        _command_error(top_level, "Checking the source repository")
        if _resolved_path(Path(top_level.stdout.strip())) != repo_path:
            raise ValueError("Source path is not the root of its Git repository")
        status = _git(repo_path, username, ["status", "--porcelain", "--untracked-files=all"], command_timeout, cancel_file)
        _command_error(status, "Checking source changes")
        if status.stdout.strip():
            raise RuntimeError("Repository has local changes; refusing to update it")
        remote = _git(repo_path, username, ["remote", "get-url", "origin"], command_timeout, cancel_file)
        _command_error(remote, "Checking the source remote")
        if remote.stdout.strip() != repo_url:
            raise RuntimeError("Repository origin differs from the configured Git URL; refusing to change it")
        current_branch = _git(repo_path, username, ["symbolic-ref", "--short", "HEAD"], command_timeout, cancel_file)
        _command_error(current_branch, "Checking the current Git branch")
        if current_branch.stdout.strip() != branch:
            raise RuntimeError(
                f"Repository is on branch {current_branch.stdout.strip()!r}, not {branch!r}; refusing to switch branches"
            )
        fetch = _git(
            repo_path,
            username,
            ["fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            command_timeout,
            cancel_file,
        )
        _command_error(fetch, "Fetching Git updates")
        merge = _git(repo_path, username, ["merge", "--ff-only", f"refs/remotes/origin/{branch}"], command_timeout, cancel_file)
        _command_error(merge, "Fast-forwarding the source repository")
        return {"status": "updated", "branch": branch, "commit": git_output(repo_path, "rev-parse", "HEAD", username=username, timeout=command_timeout)}

    repo_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if repo_path.exists():
        repo_path.rmdir()
    repo_path.mkdir(mode=0o750)
    if username:
        _chown_tree(repo_path, username)
    clone_args = ["git", "clone", "--branch", branch, "--single-branch", repo_url, str(repo_path)]
    try:
        if username is None:
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                raise PermissionError("Git must run as the application's unprivileged service account")
            result = run_command(clone_args, timeout=command_timeout, cancel_file=cancel_file)
        else:
            result = _run_as_user(username, clone_args, timeout=command_timeout, cancel_file=cancel_file)
    except Exception:
        shutil.rmtree(repo_path, ignore_errors=True)
        raise
    if result.returncode != 0:
        shutil.rmtree(repo_path)
        _command_error(result, "Cloning the Git repository")
    return {"status": "cloned", "branch": branch, "commit": git_output(repo_path, "rev-parse", "HEAD", username=username, timeout=command_timeout)}


def git_output(
    repo_path: Path,
    *args: str,
    username: Optional[str] = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> str:
    try:
        result = _git(repo_path, username, args, timeout)
    except (RuntimeError, PermissionError, ValueError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _routed_health_path(app: Dict[str, Any]) -> str:
    health = app.get("health_check_url") or "/"
    if not isinstance(health, str) or not health.startswith("/") or health.startswith("//"):
        raise ValueError("Health-check URL must be a local path")
    route = validate_app_route(app.get("active_route") or app.get("route"), app["id"])
    if app.get("framework") == "nextjs" and (health == route.rstrip("/") or health.startswith(route)):
        return health
    return route + health.lstrip("/")


def _is_static_app(app: Dict[str, Any]) -> bool:
    if app.get("static_export") is True:
        return True
    return app.get("framework") == "static" and not app.get("build_config", {}).get("start_command")


def _static_root_relative_path(value: Any) -> Path:
    if value in (None, ""):
        return Path(".")
    if not isinstance(value, str) or "\\" in value or "\0" in value or value.startswith("/"):
        raise ValueError("Static output directory must be a relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Static output directory contains an invalid path component")
    return Path(*parts)


def _static_content_root(app: Dict[str, Any], config: Dict[str, Any]) -> Path:
    app_root = _app_paths(app["id"], config)["current"] / "app"
    content_root = app_root / _static_root_relative_path(app.get("static_root"))
    try:
        content_root.resolve(strict=True).relative_to(app_root.resolve(strict=True))
    except FileNotFoundError:
        return content_root
    except (OSError, ValueError) as exc:
        raise RuntimeError("Static output directory is missing or escapes the application release") from exc
    return content_root


def app_health_url(app: Dict[str, Any]) -> str:
    health = app.get("health_check_url") or "/"
    if not isinstance(health, str) or not health.startswith("/") or health.startswith("//"):
        raise ValueError("Health-check URL must be a local path")
    if _is_static_app(app):
        return DEFAULT_PUBLIC_ORIGIN + _routed_health_path(app)
    if app.get("framework") == "nextjs":
        health = _routed_health_path(dict(app, active_route=app.get("route")))
    port = app.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_START <= port <= PORT_END or port in RESERVED_PORTS:
        raise ValueError("Application has an invalid port")
    return f"http://127.0.0.1:{port}{health}"


def check_app_health(app: Dict[str, Any], timeout_seconds: int = 30) -> bool:
    if _is_static_app(app):
        index = _static_content_root(app, get_config()) / "index.html"
        return bool(app.get("hosting_active") and app.get("status") == "running" and index.is_file())
    request = urllib.request.Request(app_health_url(app), method="GET")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=max(1, min(int(timeout_seconds), 10))) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new_url):
        return None


def _wait_for_health(app: Dict[str, Any], timeout_seconds: int) -> bool:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while True:
        if check_app_health(app, min(5, max(1, int(deadline - time.monotonic())))):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _assert_loopback_binding(app: Dict[str, Any], timeout: int) -> None:
    port = app["port"]
    result = run_command(["ss", "-H", "-ltn", f"sport = :{port}"], timeout=timeout)
    _command_error(result, "Checking application listener isolation")
    listeners = []
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 4:
            raise RuntimeError("Could not verify the application TCP listener")
        address, _, listed_port = fields[3].rpartition(":")
        if listed_port != str(port):
            raise RuntimeError("Listener inspection returned an unexpected port")
        try:
            is_loopback = ipaddress.ip_address(address.strip("[]")).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise RuntimeError(f"Application port {port} is exposed on {address}; configure the app to bind to 127.0.0.1")
        listeners.append(address)
    if not listeners:
        raise RuntimeError(f"Application is not listening on its assigned port {port}")


def app_metadata(app: Dict[str, Any]) -> Dict[str, Any]:
    metadata = dict(app)
    source = Path(app.get("source_path", ""))
    if source.is_dir():
        metadata["latest_commit"] = git_output(source, "rev-parse", "HEAD", username=app.get("service_user")) or metadata.get("latest_commit")
    route = validate_app_route(app.get("route"), app["id"])
    active_route = app.get("active_route")
    if active_route is not None:
        active_route = validate_app_route(active_route, app["id"])
    hosting_active = bool(app.get("hosting_active", False) and active_route)
    metadata["route"] = route
    metadata["hosting_active"] = hosting_active
    metadata["active_route"] = active_route
    metadata["public_url"] = _public_url_for_route(active_route) if hosting_active else None
    return metadata


def _app_paths(app_id: str, config: Dict[str, Any]) -> Dict[str, Path]:
    app_id = validate_app_id(app_id)
    source_root = _resolved_path(Path(config["source_root"]))
    production_root = _resolved_path(Path(config["production_root"]))
    source = source_root / app_id
    production = production_root / app_id
    log_root = _resolved_path(Path(config["log_root"]))
    state_root = _resolved_path(Path(DEFAULT_STATE_ROOT))
    home_root = state_root / "homes"
    environment_root = state_root / "environment"
    paths = {
        "source": source,
        "production": production,
        "releases": production / "releases",
        "current": production / "current",
        "config": production / "config",
        "logs": log_root / app_id,
        "home": home_root / app_id,
        "environment": environment_root / f"{app_id}.env",
        "storage": production / "shared",
    }
    if (
        not _path_is_within(source, source_root)
        or not _path_is_within(production, production_root)
        or not _path_is_within(paths["releases"], production)
        or not _path_is_within(paths["current"], production)
        or not _path_is_within(paths["config"], production)
        or not _path_is_within(paths["logs"], log_root)
        or not _path_is_within(paths["home"], home_root)
        or not _path_is_within(paths["environment"], environment_root)
        or not _path_is_within(paths["storage"], production)
    ):
        raise ValueError("Application paths resolve outside their managed roots")
    return paths


def _environment_file_path(
    app: Dict[str, Any],
    config: Dict[str, Any],
    *,
    create_directory: bool = False,
) -> Path:
    paths = _app_paths(app["id"], config)
    state_root = Path(DEFAULT_STATE_ROOT)
    environment_root = paths["environment"].parent
    env_path = paths["environment"]
    if state_root.is_symlink() or environment_root.is_symlink():
        raise RuntimeError("Application environment directory must not be a symlink")
    if create_directory:
        username = ensure_app_user(app["id"])
        environment_root.mkdir(parents=True, exist_ok=True, mode=0o711)
        if environment_root.is_symlink():
            raise RuntimeError("Application environment directory must not be a symlink")
        if hasattr(os, "chown"):
            os.chown(str(environment_root), 0, 0)
        os.chmod(str(environment_root), 0o711)
    if env_path.is_symlink() or (env_path.exists() and not env_path.is_file()):
        raise RuntimeError("Application environment file must be a regular file")
    return env_path


def _parse_environment_data(contents: str) -> Tuple[str, Dict[str, str]]:
    if not isinstance(contents, str):
        raise ValueError("Environment file content must be text")
    if "\0" in contents or len(contents.encode("utf-8")) > MAX_ENV_FILE_BYTES:
        raise ValueError(f"Environment file must be smaller than {MAX_ENV_FILE_BYTES} bytes and contain no NUL bytes")
    normalized_lines = []
    values = {}
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            normalized_lines.append(raw_line)
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            raise ValueError(f"Invalid environment assignment on line {line_number}")
        key, raw_value = match.groups()
        if key in values:
            raise ValueError(f"Duplicate environment key {key!r} on line {line_number}")
        if key in {
            "HOME",
            "PATH",
            "PORT",
            "HOST",
            "NODE_ENV",
            "APP_BASE_PATH",
            "APP_CONFIG_DIR",
            "APP_DATA_DIR",
            "APP_STORAGE_DIR",
        }:
            raise ValueError(f"Environment key {key!r} is reserved by App Manager")
        if key.startswith("APP_STORAGE_"):
            raise ValueError(f"Environment key {key!r} is reserved by App Manager")
        raw_value = raw_value.strip()
        if raw_value.startswith(("'", '"')):
            quote = raw_value[0]
            value_chars = []
            index = 1
            while index < len(raw_value):
                char = raw_value[index]
                if quote == '"' and char == "\\" and index + 1 < len(raw_value):
                    index += 1
                    escaped = raw_value[index]
                    if escaped in {'"', "\\", "$", "`"}:
                        value_chars.append(escaped)
                    else:
                        value_chars.extend(("\\", escaped))
                elif char == quote:
                    suffix = raw_value[index + 1:].strip()
                    if suffix and not suffix.startswith("#"):
                        raise ValueError(f"Unexpected text after quoted value on line {line_number}")
                    break
                else:
                    value_chars.append(char)
                index += 1
            else:
                raise ValueError(f"Unclosed quoted value on line {line_number}")
            value = "".join(value_chars)
        else:
            value = re.split(r"\s+#", raw_value, maxsplit=1)[0].strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"Multiline environment values are not supported (line {line_number})")
        values[key] = value
        escaped_value = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
        )
        normalized_lines.append(f'{key}="{escaped_value}"')
    normalized = "\n".join(normalized_lines)
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    if len(normalized.encode("utf-8")) > MAX_ENV_FILE_BYTES:
        raise ValueError(f"Normalized environment file exceeds {MAX_ENV_FILE_BYTES} bytes")
    return normalized, values


def _parse_environment_file(contents: str) -> str:
    return _parse_environment_data(contents)[0]


def get_environment_app(app_id: str) -> Dict[str, Any]:
    app_id = validate_app_id(app_id)
    app = _find_app(get_registry(), app_id)
    path = _environment_file_path(app, get_config())
    if not path.exists():
        return {"ok": True, "app_id": app_id, "content": ""}
    if path.stat().st_size > MAX_ENV_FILE_BYTES:
        raise RuntimeError(f"Application environment file exceeds {MAX_ENV_FILE_BYTES} bytes")
    _validate_environment_file_permissions(path, app)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Could not read application environment file: {exc}") from exc
    return {"ok": True, "app_id": app_id, "content": content}


def _environment_values_app(app_id: str) -> Dict[str, str]:
    content = get_environment_app(app_id)["content"]
    return _parse_environment_data(content)[1] if content else {}


def _validate_environment_file_permissions(path: Path, app: Dict[str, Any]) -> None:
    if os.name == "nt":
        return
    _, gid = _account_ids(app["service_user"])
    metadata = path.stat()
    if (
        stat.S_IMODE(metadata.st_mode) != 0o640
        or metadata.st_uid != 0
        or metadata.st_gid != gid
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("Application environment file has unsafe ownership or permissions")


def save_environment_app(app_id: str, contents: str) -> Dict[str, Any]:
    app_id = validate_app_id(app_id)
    normalized, values = _parse_environment_data(contents)
    with _app_lock(app_id):
        app = _find_app(get_registry(), app_id)
        path = _environment_file_path(app, get_config(), create_directory=True)
        username = app["service_user"]
        _, gid = _account_ids(username)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".env.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary_path = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = -1
                handle.write(normalized)
                handle.flush()
                os.fsync(handle.fileno())
            if hasattr(os, "chown"):
                os.chown(str(temporary_path), 0, gid)
            temporary_path.chmod(0o640)
            os.replace(str(temporary_path), str(path))
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()
            raise
    return {"ok": True, "app_id": app_id, "key_count": len(values), "restart_required": app.get("status") == "running"}


def _validate_registry(registry: Dict[str, Any]) -> None:
    config = get_config()
    seen_ids: Set[str] = set()
    seen_ports: Set[int] = set()
    seen_routes: Dict[str, str] = {}
    for app in registry["apps"]:
        if not isinstance(app, dict):
            raise ValueError("Each application registry entry must be an object")
        app_id = validate_app_id(app.get("id"))
        for field in ("metrics", "pending_update"):
            if field in app and not isinstance(app[field], dict):
                raise ValueError(f"Application {field} must be an object")
        if app_id in seen_ids:
            raise ValueError(f"Duplicate application ID in registry: {app_id}")
        seen_ids.add(app_id)
        route = validate_app_route(app.get("route"), app_id)
        app["route"] = route
        route_values = {route}
        active_route = app.get("active_route")
        if active_route is not None:
            active_route = validate_app_route(active_route, app_id)
            app["active_route"] = active_route
            route_values.add(active_route)
        app.setdefault("hosting_active", False)
        if not isinstance(app["hosting_active"], bool):
            raise ValueError(f"Application {app_id!r} has an invalid Nginx hosting status")
        for route_value in route_values:
            owner = seen_routes.get(route_value)
            if owner is not None and owner != app_id:
                raise ValueError(f"Duplicate application route in registry: {route_value}")
            seen_routes[route_value] = app_id
        validate_name(app.get("name"))
        if not validate_git_url(app.get("repo_url")):
            raise ValueError(f"Application {app_id!r} has an invalid Git URL")
        validate_branch(app.get("branch"))
        if app.get("framework") not in FRAMEWORKS:
            raise ValueError(f"Application {app_id!r} has an unsupported framework")
        port = app.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not PORT_START <= port <= PORT_END or port in RESERVED_PORTS:
            raise ValueError(f"Application {app_id!r} has an invalid port")
        if port in seen_ports:
            raise ValueError(f"Duplicate application port in registry: {port}")
        seen_ports.add(port)
        candidate_port = app.get("candidate_port")
        if candidate_port is not None:
            if isinstance(candidate_port, bool) or not isinstance(candidate_port, int) or not PORT_START <= candidate_port <= PORT_END or candidate_port in RESERVED_PORTS or candidate_port in seen_ports:
                raise ValueError("Invalid or conflicting candidate port in registry")
            seen_ports.add(candidate_port)
        if app.get("service_slot", "primary") not in {"primary", "secondary"}:
            raise ValueError("Invalid managed service slot in registry")
        normalized_policy = validate_app_payload(app)
        for key in ("auto_deploy", "deployment_window", "resource_limits", "required_environment", "runtime_requirements", "storage_directories", "deployment_mode"):
            app[key] = normalized_policy[key]
        expected_paths = _app_paths(app_id, config)
        expected_user = _account_name(app_id)
        fields = {
            "source_path": expected_paths["source"],
            "production_path": expected_paths["production"],
            "config_path": expected_paths["config"],
            "log_path": expected_paths["logs"],
        }
        for field, expected in fields.items():
            if field in app and _resolved_path(Path(app[field])) != _resolved_path(expected):
                raise ValueError(f"Application {app_id!r} has an invalid {field}")
            app[field] = str(expected)
        if app.get("service_user", expected_user) != expected_user:
            raise ValueError(f"Application {app_id!r} has an invalid service account")
        app["service_user"] = expected_user
        if "deployed_commit" not in app:
            successful = next((record for record in app.get("deployments", [])
                               if isinstance(record, dict) and record.get("status") in {"deployed", "rolled_back"}), None)
            app["deployed_commit"] = successful.get("commit") if successful else None
        if app.get("slug", app_id) != app_id:
            raise ValueError(f"Application {app_id!r} has an invalid slug")
        build_config = app.get("build_config", {})
        if not isinstance(build_config, dict):
            raise ValueError(f"Application {app_id!r} has an invalid build configuration")
        try:
            _validate_app_directory_setting(build_config.get("app_directory"))
        except ValueError as exc:
            raise ValueError(f"Application {app_id!r} has an invalid build configuration") from exc
        try:
            app_health_url(app)
        except ValueError as exc:
            raise ValueError(f"Application {app_id!r} has an invalid health-check configuration") from exc
    _clear_expired_port_reservations(registry)
    if seen_ports & {int(port) for port in registry["port_reservations"]}:
        raise ValueError("Registry port reservations conflict with registered applications")


def _find_app(registry: Dict[str, Any], app_id: str) -> Dict[str, Any]:
    validate_app_id(app_id)
    app = next((item for item in registry["apps"] if item.get("id") == app_id), None)
    if app is None:
        raise ValueError(f"Application {app_id!r} not found")
    return app


def add_app(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_runtime_dirs()
    normalized = validate_app_payload(payload)
    config = get_config()
    with _registry_transaction() as registry:
        if any(app.get("id") == normalized["id"] for app in registry["apps"]):
            raise ValueError(f"Application {normalized['id']!r} is already registered")
        for app in registry["apps"]:
            existing_routes = {
                validate_app_route(app.get("route"), app["id"]),
                *(
                    [validate_app_route(app["active_route"], app["id"])]
                    if app.get("active_route") is not None
                    else []
                ),
            }
            if normalized["route"] in existing_routes:
                raise ValueError(f"Route {normalized['route']} is already assigned to another application")
        port = normalized.get("port")
        if port is None:
            port = _select_port(registry, config)
        else:
            _clear_expired_port_reservations(registry)
            reservations = registry.get("port_reservations", {})
            if (
                any(port in {app.get("port"), app.get("candidate_port")} for app in registry["apps"])
                or port in get_listening_ports()
                or not _socket_port_available(port)
            ):
                raise ValueError(f"Port {port} is already in use")
            reservations.pop(str(port), None)
        paths = _app_paths(normalized["id"], config)
        app_record = {
            "id": normalized["id"],
            "name": normalized["name"],
            "slug": normalized["id"],
            "repo_url": normalized["repo_url"],
            "branch": normalized["branch"],
            "framework": normalized["framework"],
            "route": normalized["route"],
            "active_route": None,
            "hosting_active": False,
            "description": normalized.get("description", ""),
            "health_check_url": normalized["health_check_url"],
            "build_config": normalized["build_config"],
            "resource_limits": normalized["resource_limits"],
            "auto_deploy": normalized["auto_deploy"],
            "deployment_window": normalized["deployment_window"],
            "required_environment": normalized["required_environment"],
            "runtime_requirements": normalized["runtime_requirements"],
            "storage_directories": normalized["storage_directories"],
            "deployment_mode": normalized["deployment_mode"],
            "port": port,
            "public_url": None,
            "source_path": str(paths["source"]),
            "production_path": str(paths["production"]),
            "config_path": str(paths["config"]),
            "log_path": str(paths["logs"]),
            "service_user": _account_name(normalized["id"]),
            "status": "stopped",
            "last_deploy": None,
            "latest_commit": None,
            "deployments": [],
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        registry["apps"].append(app_record)
    return {"ok": True, "app": app_record}


def list_apps() -> Dict[str, Any]:
    return {"apps": [app_metadata(app) for app in get_registry()["apps"]]}


def stats_for_apps(apps: List[Dict[str, Any]]) -> Dict[str, Any]:
    ports = {app.get("port") for app in apps}
    return {
        "total_apps": len(apps),
        "running_apps": sum(app.get("status") == "running" for app in apps),
        "stopped_apps": sum(app.get("status") == "stopped" for app in apps),
        "failed_deployments": sum(app.get("status") == "failed" for app in apps),
        "available_ports": [
            port for port in range(PORT_START, PORT_END + 1)
            if port not in ports and port not in RESERVED_PORTS
        ],
        "recent_activity": [
            {"name": app.get("name"), "action": "Deployment", "when": app.get("last_deploy") or "never"}
            for app in sorted(apps, key=lambda item: item.get("updated_at", ""), reverse=True)[:5]
        ],
    }


def overview() -> Dict[str, Any]:
    return stats_for_apps(list_apps()["apps"])


def get_deployments() -> Dict[str, Any]:
    deployments = []
    for app in get_registry()["apps"]:
        latest = app.get("deployments", [])[-1:] if app.get("deployments") else []
        deployments.extend(dict(item, app_id=app["id"]) for item in latest)
    return {"deployments": sorted(deployments, key=lambda item: item.get("date", ""), reverse=True)}


def get_settings() -> Dict[str, Any]:
    return {"config": get_config()}


def _plugin_update_environment() -> Dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _plugin_git(args: Sequence[str], timeout: int = 120) -> subprocess.CompletedProcess:
    _require_root()
    if not _account_exists(PLUGIN_UPDATE_USER):
        raise RuntimeError(f"The unprivileged update account {PLUGIN_UPDATE_USER} is unavailable")
    env_args = [
        "env",
        "-i",
        *[f"{key}={value}" for key, value in _plugin_update_environment().items()],
        *args,
    ]
    return run_command(
        ["runuser", "--user", PLUGIN_UPDATE_USER, "--", *env_args],
        timeout=timeout,
        env={"HOME": "/root", "PATH": _plugin_update_environment()["PATH"]},
        redact_values=[],
    )


def _plugin_remote_release() -> Dict[str, str]:
    result = _plugin_git(
        [
            "git",
            "ls-remote",
            "--exit-code",
            PLUGIN_REPOSITORY_URL,
            "refs/tags/v*",
        ]
    )
    if result.returncode == 2:
        raise RuntimeError(
            "No versioned App Manager release tags were found on GitHub. "
            "The update checker requires an annotated, signed vMAJOR.MINOR.PATCH tag."
        )
    _command_error(result, "Checking for App Manager updates")
    releases = {}
    for line in (result.stdout or "").splitlines():
        match = re.fullmatch(r"([0-9a-f]{40,64})\trefs/tags/(v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))\^\{\}", line)
        if match:
            releases[tuple(int(part) for part in match.groups()[2:])] = {
                "version": match.group(2), "commit": match.group(1),
            }
    if not releases:
        raise RuntimeError("No annotated versioned App Manager release is available")
    return releases[max(releases)]


def _verify_plugin_release(checkout: Path, version: str) -> None:
    trust = Path(PLUGIN_TRUSTED_SIGNERS)
    _assert_no_symlink_components(trust)
    if not trust.is_file():
        raise RuntimeError("Provision a trusted release signing key in " + str(trust))
    metadata = trust.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError("Trusted release signers must be root-owned and not group/world writable")
    result = run_command([
        "git", "-c", f"safe.directory={checkout}",
        "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "gpg.format=ssh", "-c", "gpg.ssh.program=/usr/bin/ssh-keygen",
        "-c", f"gpg.ssh.allowedSignersFile={trust}",
        "-C", str(checkout), "verify-tag", version,
    ], timeout=30, env=_plugin_update_environment(), redact_values=[])
    _command_error(result, "Verifying the trusted App Manager release signature")


def _verify_plugin_checkout(checkout: Path, version: str, commit: str) -> None:
    prefix = ["git", "-c", f"safe.directory={checkout}", "-c", "core.fsmonitor=false",
              "-c", "core.hooksPath=/dev/null", "-C", str(checkout)]
    revision = run_command([*prefix, "rev-parse", f"{version}^{{commit}}"], timeout=30, env=_plugin_update_environment(), redact_values=[])
    _command_error(revision, "Resolving the signed release commit")
    if (revision.stdout or "").strip() != commit:
        raise RuntimeError("The signed release tag does not match the selected commit")
    listing = run_command([*prefix, "ls-tree", "-r", "-z", "--full-tree", commit], timeout=30, env=_plugin_update_environment(), redact_values=[])
    _command_error(listing, "Reading the signed release file manifest")
    expected = set()
    for entry in (listing.stdout or "").split("\0"):
        if not entry:
            continue
        metadata, separator, name = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[0] not in {"100644", "100755"} or fields[1] != "blob":
            raise RuntimeError("Unsupported entry in the signed release manifest")
        if not name or Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name or name.split("/")[0] == ".git":
            raise RuntimeError("Unsafe path in the signed release manifest")
        path = checkout / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Missing or unsafe file in the release checkout")
        blob = path.read_bytes()
        digest = hashlib.sha1() if len(fields[2]) == 40 else hashlib.sha256()
        digest.update(f"blob {len(blob)}\0".encode("ascii"))
        digest.update(blob)
        if digest.hexdigest() != fields[2]:
            raise RuntimeError("Release checkout differs from the signed tree: " + name)
        expected.add(name)
    actual = set()
    for root, directories, filenames in os.walk(checkout, followlinks=False):
        if Path(root) == checkout:
            directories[:] = [name for name in directories if name != ".git"]
        for name in filenames:
            actual.add((Path(root) / name).relative_to(checkout).as_posix())
    if not expected or actual != expected:
        raise RuntimeError("Release checkout contains unexpected files or an empty manifest")


def _installed_plugin_commit() -> Optional[str]:
    version_path = Path(DEFAULT_PLUGIN_VERSION_PATH)
    if version_path.is_symlink():
        raise RuntimeError("Installed plugin version metadata must not be a symlink")
    if not version_path.exists():
        return None
    try:
        payload = json.loads(version_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Installed plugin version metadata is invalid: {exc}") from exc
    commit = payload.get("commit") if isinstance(payload, dict) else None
    if commit is not None and (
        not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit)
    ):
        raise RuntimeError("Installed plugin version metadata contains an invalid Git revision")
    return commit


def plugin_update_check() -> Dict[str, Any]:
    current_commit = _installed_plugin_commit()
    release = _plugin_remote_release()
    latest_commit = release["commit"]
    installed = load_json(Path(DEFAULT_PLUGIN_VERSION_PATH), {})
    installed_version = installed.get("version") if isinstance(installed, dict) else None
    if installed_version is not None:
        if not isinstance(installed_version, str) or not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", installed_version):
            raise RuntimeError("Installed plugin release version is invalid")
        current_version = tuple(int(part) for part in installed_version[1:].split("."))
        remote_version = tuple(int(part) for part in release["version"][1:].split("."))
        if remote_version < current_version or (remote_version == current_version and current_commit != latest_commit):
            raise RuntimeError("Refusing an older or rewritten plugin release")
    return {
        "ok": True,
        "available": current_commit != latest_commit,
        "current_commit": current_commit,
        "latest_commit": latest_commit,
        "version": release["version"],
        "release_notes_url": "https://github.com/stuartjeet122/cockpitdeploymentplugin/releases/tag/" + release["version"],
    }


def _seal_plugin_update_checkout(checkout: Path, update_root: Path) -> None:
    for root, dirs, files in os.walk(checkout, topdown=True, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink():
            raise RuntimeError("Update checkout contains a symlinked directory")
        os.chown(str(root_path), 0, 0)
        os.chmod(str(root_path), 0o500)
        for name in dirs + files:
            path = root_path / name
            if path.is_symlink():
                raise RuntimeError("Update checkout contains a symlink")
            os.chown(str(path), 0, 0)
            os.chmod(str(path), 0o400 if path.is_file() else 0o500)
    os.chown(str(update_root), 0, 0)
    os.chmod(str(update_root), 0o700)


def plugin_update() -> Dict[str, Any]:
    _require_root()
    check = plugin_update_check()
    if not check["available"]:
        return {"ok": True, "updated": False, "commit": check["current_commit"]}

    update_parent = Path(DEFAULT_STATE_ROOT) / "plugin-updates"
    if Path(DEFAULT_STATE_ROOT).is_symlink() or update_parent.is_symlink():
        raise RuntimeError("Plugin update staging directory must not be a symlink")
    update_parent.mkdir(parents=True, exist_ok=True, mode=0o711)
    os.chmod(str(update_parent), 0o711)
    update_root = Path(tempfile.mkdtemp(prefix="app-manager-update-", dir=str(update_parent)))
    checkout = update_root / "checkout"
    verified_root: Optional[Path] = None
    try:
        uid, gid = _account_ids(PLUGIN_UPDATE_USER)
        os.chown(str(update_root), uid, gid)
        result = _plugin_git(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                check["version"],
                "--single-branch",
                PLUGIN_REPOSITORY_URL,
                str(checkout),
            ],
            timeout=300,
        )
        _command_error(result, "Downloading the App Manager update")
        if not checkout.is_dir() or checkout.is_symlink():
            raise RuntimeError("The App Manager update checkout is invalid")
        commit_result = _plugin_git(["git", "-C", str(checkout), "rev-parse", "HEAD"])
        _command_error(commit_result, "Verifying the App Manager update")
        match = re.fullmatch(r"[0-9a-f]{40,64}", (commit_result.stdout or "").strip())
        if not match:
            raise RuntimeError("The downloaded App Manager update has an invalid Git revision")
        if match.group(0) != check["latest_commit"]:
            raise RuntimeError("The release changed after the update check; check again")
        verified_root = Path(tempfile.mkdtemp(prefix="app-manager-verified-", dir=str(update_parent)))
        os.chmod(str(verified_root), 0o700)
        verified_checkout = verified_root / "checkout"
        shutil.copytree(checkout, verified_checkout, symlinks=True)
        checkout = verified_checkout
        _seal_plugin_update_checkout(checkout, verified_root)
        _verify_plugin_release(checkout, check["version"])
        _verify_plugin_checkout(checkout, check["version"], check["latest_commit"])
        installer = checkout / "scripts" / "install.sh"
        if installer.is_symlink() or not installer.is_file():
            raise RuntimeError("The App Manager update does not contain a regular installer")
        install_result = run_command(
            ["bash", str(installer)],
            cwd=checkout,
            timeout=300,
            env={"HOME": "/root", "PATH": _plugin_update_environment()["PATH"], "APP_MANAGER_RELEASE_VERSION": check["version"]},
        )
        _command_error(install_result, "Reinstalling the App Manager plugin")
        result = {"ok": True, "updated": True, "commit": match.group(0)}
        try:
            restart = run_command(
                [
                    "systemd-run",
                    "--quiet",
                    "--collect",
                    f"--unit=cockpit-app-manager-restart-{os.getpid()}",
                    "--on-active=5s",
                    "--timer-property=AccuracySec=1s",
                    "systemctl",
                    "restart",
                    "cockpit.service",
                ],
                timeout=30,
                env={"HOME": "/root", "PATH": _plugin_update_environment()["PATH"]},
            )
            _command_error(restart, "Scheduling the Cockpit restart")
            result["restart_scheduled"] = True
            result["restart_delay_seconds"] = 5
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            result["restart_scheduled"] = False
            result["restart_error"] = str(exc)
        return result
    finally:
        try:
            if verified_root is not None:
                shutil.rmtree(verified_root)
        finally:
            shutil.rmtree(update_root)


def _validate_capacity_settings(config: Dict[str, Any]) -> None:
    for key, maximum in (("max_concurrent_builds", 8), ("min_free_disk_mb", 1048576)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{key} must be an integer from 1 to {maximum}")


def update_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Settings payload must be a JSON object")
    config = get_config()
    allowed_keys = {
        "port_range",
        "default_branch",
        "source_root",
        "production_root",
        "log_root",
        "allowed_source_dirs",
        "release_retention",
        "build_timeout_seconds",
        "health_timeout_seconds",
        "command_timeout_seconds",
        "max_concurrent_builds",
        "min_free_disk_mb",
    }
    update = dict(payload)
    for key in update:
        if key not in allowed_keys:
            raise ValueError(f"Unsupported configuration key: {key!r}")
    for key in ("build_timeout_seconds", "health_timeout_seconds", "command_timeout_seconds"):
        if key in update:
            value = update[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key} must be a positive integer")
    if "default_branch" in update:
        update["default_branch"] = validate_branch(update["default_branch"])
    if "source_root" in update:
        update["source_root"] = str(validate_allowed_path(update["source_root"], [DEFAULT_SOURCE_ROOT]))
    if "production_root" in update:
        update["production_root"] = str(validate_allowed_path(update["production_root"], [DEFAULT_PRODUCTION_ROOT]))
    if "log_root" in update:
        update["log_root"] = str(validate_allowed_path(update["log_root"], [DEFAULT_LOG_ROOT]))
    if "allowed_source_dirs" in update:
        if not isinstance(update["allowed_source_dirs"], list) or not all(
            isinstance(root, str) and root and "\0" not in root for root in update["allowed_source_dirs"]
        ):
            raise ValueError("allowed_source_dirs must be an array of paths")
        update["allowed_source_dirs"] = [
            str(validate_allowed_path(root, [DEFAULT_SOURCE_ROOT, "/opt", "/srv", "/home"])) for root in update["allowed_source_dirs"]
        ]
    if "port_range" in update:
        port_range = update["port_range"]
        if not isinstance(port_range, dict):
            raise ValueError("port_range must be a JSON object")
        start = port_range.get("start")
        end = port_range.get("end")
        if (
            isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or start < PORT_START or end > PORT_END or start > end
        ):
            raise ValueError("Configured port range must stay within 8100-8999")
        reserved = port_range.get("reserved", [])
        if not isinstance(reserved, list) or any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in reserved
        ):
            raise ValueError("Reserved ports must be an array of integer port numbers")
        update["port_range"] = {"start": start, "end": end, "reserved": sorted(set(RESERVED_PORTS) | set(reserved))}
    if "release_retention" in update:
        retention = update["release_retention"]
        if isinstance(retention, bool) or not isinstance(retention, int) or retention != 1:
            raise ValueError("App Manager retains only the current deployment release")
        update.pop("release_retention")
    merged = config.copy()
    merged.update(update)
    _validate_capacity_settings(merged)
    path = Path(DEFAULT_CONFIG_PATH)
    with _file_lock(path.with_name(path.name + ".lock")):
        _atomic_json_write(path, merged)
    return {"ok": True, "config": get_config()}


def edit_app(app_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    validate_app_id(app_id)
    if not isinstance(payload, dict):
        raise ValueError("Application payload must be a JSON object")
    if "id" in payload or "slug" in payload:
        raise ValueError("Application ID and slug cannot be changed")
    with _app_lock(app_id):
        registry = get_registry()
        app = _find_app(registry, app_id)
        current = {key: value for key, value in app.items() if key not in {"id", "slug", "status", "last_deploy", "latest_commit", "deployments", "created_at", "updated_at", "source_path", "production_path", "config_path", "log_path", "service_user", "public_url"}}
        current["id"] = app_id
        current["slug"] = app_id
        candidate = {**current, **payload}
        normalized = validate_app_payload(candidate)
        for other in registry["apps"]:
            if other.get("id") == app_id:
                continue
            other_routes = {
                validate_app_route(other.get("route"), other["id"]),
                *(
                    [validate_app_route(other["active_route"], other["id"])]
                    if other.get("active_route") is not None
                    else []
                ),
            }
            if normalized["route"] in other_routes:
                raise ValueError(f"Route {normalized['route']} is already assigned to another application")
        port = normalized.get("port", app.get("port"))
        if port != app.get("port"):
            if any(other.get("id") != app_id and port in {other.get("port"), other.get("candidate_port")} for other in registry["apps"]):
                raise ValueError(f"Port {port} is already in use")
            if port in get_listening_ports() or not _socket_port_available(port):
                raise ValueError(f"Port {port} is already in use")
        with _registry_transaction() as registry2:
            app = _find_app(registry2, app_id)
            for key in ("name", "repo_url", "branch", "framework", "route", "description", "health_check_url", "build_config", "resource_limits", "auto_deploy", "deployment_window", "required_environment", "runtime_requirements", "storage_directories", "deployment_mode", "port", "public_url"):
                if key in normalized:
                    app[key] = normalized[key]
            app["slug"] = app_id
            app["public_url"] = _public_url_for_route(app.get("active_route") or app["route"])
            app["updated_at"] = now_iso()
            return {"ok": True, "app": app}


def _service_path(app: Dict[str, Any]) -> Path:
    validate_app_id(app["id"])
    slot = app.get("service_slot", "primary")
    if slot not in {"primary", "secondary"}:
        raise ValueError("Invalid managed service slot")
    suffix = "@secondary" if slot == "secondary" else ""
    return Path("/etc/systemd/system") / f"cockpit-app-{app['id']}{suffix}.service"


def _verified_service_bytes(app: Dict[str, Any], service_path: Path) -> Optional[bytes]:
    _assert_no_symlink_components(service_path)
    if not service_path.exists():
        return None
    contents = service_path.read_bytes()
    required = {"# Managed by Cockpit App Manager", f"Description=Cockpit App Manager - {app['id']}", f"User={app['service_user']}"}
    if not required.issubset(set(contents.decode("utf-8").splitlines())):
        raise RuntimeError("Refusing to modify an unverified managed service unit")
    return contents


def _legacy_service_path(app: Dict[str, Any]) -> Path:
    validate_app_id(app["id"])
    return Path("/etc/systemd/system") / f"{app['id']}.service"


def _retire_legacy_root_service(app: Dict[str, Any]) -> None:
    legacy_path = _legacy_service_path(app)
    if not legacy_path.exists() and not legacy_path.is_symlink():
        return
    if legacy_path.is_symlink():
        raise RuntimeError(f"Refusing to manage symlinked legacy service {legacy_path.name}")
    try:
        content = legacy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not inspect legacy service {legacy_path.name}: {exc}") from exc
    required_lines = {
        f"Description={app['name']}",
        "User=root",
        "Group=root",
        f"Environment=PORT={app['port']}",
    }
    if "[Service]" not in content or not required_lines.issubset(set(content.splitlines())):
        raise RuntimeError(f"Legacy unit {legacy_path.name} is not a recognized App Manager root service")
    _systemctl("disable", "--now", legacy_path.name)
    legacy_path.unlink()
    _systemctl("daemon-reload")


def _systemd_quote(argument: str) -> str:
    return '"' + argument.replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_service(
    app: Dict[str, Any],
    start_command: Sequence[str],
    config: Dict[str, Any],
    working_directory: Optional[Path] = None,
    release_directory: Optional[Path] = None,
) -> str:
    if not start_command:
        raise ValueError("Application start command cannot be empty")
    paths = _app_paths(app["id"], config)
    app_working_directory = paths["current"] / "app"
    if working_directory is None:
        working_directory = app_working_directory
    try:
        relative_working_directory = working_directory.relative_to(app_working_directory)
    except ValueError as exc:
        raise ValueError("Application working directory must be inside its managed release") from exc
    if ".." in relative_working_directory.parts:
        raise ValueError("Application working directory must not traverse outside its managed release")
    if release_directory is not None:
        if not _path_is_within(release_directory, paths["releases"]) or release_directory == paths["releases"]:
            raise ValueError("Service release must be inside managed releases")
        working_directory = release_directory / "app" / relative_working_directory
        start_command = _release_command_path(start_command, paths["current"], release_directory)
    writable_paths = (
        paths["home"],
        paths["config"],
        paths["logs"],
        *(paths["storage"] / name for name in app.get("storage_directories", ["data"])),
    )
    read_write_paths = " ".join(_systemd_quote(str(path)) for path in writable_paths)
    exec_start = " ".join(_systemd_quote(part) for part in start_command)
    node_environment = (
        f"Environment={_systemd_quote('NODE_ENV=production')}\n"
        if app.get("framework") in {"nextjs", "node"}
        else ""
    )
    limits = _resource_limits(app.get("resource_limits", {}))
    storage_environment = "".join(
        f"Environment={_systemd_quote('APP_STORAGE_' + name.upper().replace('-', '_') + '=' + str(paths['storage'] / name))}\n"
        for name in app.get("storage_directories", ["data"])
    )
    return (
        "[Unit]\n"
        "# Managed by Cockpit App Manager\n"
        f"Description=Cockpit App Manager - {app['id']}\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={app['service_user']}\n"
        f"Group={app['service_user']}\n"
        f"WorkingDirectory={working_directory}\n"
        f"ExecStart={exec_start}\n"
        f"EnvironmentFile=-{_systemd_quote(str(paths['environment']))}\n"
        f"Environment={_systemd_quote('PORT=' + str(app['port']))}\n"
        f"Environment={_systemd_quote('HOST=127.0.0.1')}\n"
        f"{node_environment}"
        f"Environment={_systemd_quote('APP_BASE_PATH=' + validate_app_route(app.get('route'), app['id']).rstrip('/'))}\n"
        f"Environment={_systemd_quote('APP_CONFIG_DIR=' + str(paths['config']))}\n"
        f"Environment={_systemd_quote('APP_DATA_DIR=' + str(paths['storage'] / 'data'))}\n"
        f"Environment={_systemd_quote('APP_STORAGE_DIR=' + str(paths['storage']))}\n"
        f"{storage_environment}"
        f"Environment={_systemd_quote('HOME=' + str(paths['home']))}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "CPUAccounting=true\n"
        "MemoryAccounting=true\n"
        "TasksAccounting=true\n"
        f"MemoryMax={limits['memory_mb']}M\n"
        f"CPUQuota={limits['cpu_percent']}%\n"
        f"TasksMax={limits['tasks_max']}\n"
        f"StandardOutput=append:{paths['logs'] / 'stdout.log'}\n"
        f"StandardError=append:{paths['logs'] / 'stderr.log'}\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "PrivateDevices=true\n"
        "ProtectSystem=strict\n"
        "ProtectHome=true\n"
        f"ReadOnlyPaths={_systemd_quote(str(paths['releases']))}\n"
        "ProtectKernelTunables=true\n"
        "ProtectKernelModules=true\n"
        "ProtectControlGroups=true\n"
        "LockPersonality=true\n"
        "RestrictSUIDSGID=true\n"
        "RestrictRealtime=true\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
        "CapabilityBoundingSet=\n"
        "UMask=0027\n"
        f"ReadWritePaths={read_write_paths}\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _render_nginx_config(
    app_id: str,
    port: int,
    template_path: Optional[Path] = None,
    framework: str = "nextjs",
    config: Optional[Dict[str, Any]] = None,
    route: Optional[str] = None,
    static_root: Optional[str] = None,
) -> str:
    app_id = validate_app_id(app_id)
    route = validate_app_route(route, app_id)
    route_prefix = route.rstrip("/")
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_START <= port <= PORT_END or port in RESERVED_PORTS:
        raise ValueError("Nginx upstream port must be an available managed application port")
    if framework not in FRAMEWORKS - {"auto"}:
        raise ValueError("Nginx framework must be a detected, supported application framework")
    if template_path is None:
        config = config or get_config()
        template_path = Path(config["nginx_template"])
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read Nginx template {template_path}: {exc}") from exc
    proxy_uri = "" if framework == "nextjs" else "/"
    if framework == "static":
        config = config or get_config()
        app_root = _app_paths(app_id, config)["current"] / "app" / _static_root_relative_path(static_root)
        route_location = (
            f"# Managed by Cockpit App Manager: {app_id}\n"
            f"location ^~ {route} {{\n"
            f"    alias {_nginx_quote(str(app_root) + os.sep)};\n"
            "    index index.html;\n"
            f"    try_files $uri $uri/ {route}index.html;\n"
            "}\n"
        )
    else:
        proxy_redirect = (
            "    proxy_redirect off;\n"
            if framework == "nextjs"
            else (
                f"    proxy_redirect http://127.0.0.1:{port}/ {route};\n"
                f"    proxy_redirect / {route};\n"
                f"    proxy_cookie_path / {route};\n"
            )
        )
        route_location = (
            f"# Managed by Cockpit App Manager: {app_id}\n"
            f"location ^~ {route} {{\n"
            f"    proxy_pass http://127.0.0.1:{port}{proxy_uri};\n"
            "    proxy_http_version 1.1;\n"
            "    proxy_set_header Host $host;\n"
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "    proxy_set_header X-Forwarded-Proto $scheme;\n"
            "    proxy_set_header X-Real-IP $remote_addr;\n"
            f"    proxy_set_header X-Forwarded-Prefix {route_prefix};\n"
            "    proxy_set_header Upgrade $http_upgrade;\n"
            "    proxy_read_timeout 3600s;\n"
            "    proxy_send_timeout 3600s;\n"
            "    set $app_manager_connection_upgrade upgrade;\n"
            "    if ($http_upgrade = \"\") {\n"
            "        set $app_manager_connection_upgrade \"\";\n"
            "    }\n"
            "    proxy_set_header Connection $app_manager_connection_upgrade;\n"
            + proxy_redirect
            + "}\n"
        )
    rendered = (
        template.replace("{{ app_slug }}", app_id)
        .replace("{{ app_route }}", route_prefix)
        .replace("{{ app_route_slash }}", route)
        .replace("{{ app_port }}", str(port))
        .replace("{{ app_proxy_uri }}", proxy_uri)
        .replace("{{ app_route_location }}", route_location.rstrip())
    )
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("Nginx template contains an unresolved placeholder")
    return rendered


def _nginx_quote(path: str) -> str:
    if any(char in path for char in ('"', "\r", "\n", ";", "{", "}")):
        raise ValueError("Path cannot be safely represented in an Nginx configuration")
    return '"' + path.replace("\\", "\\\\") + '"'


def _nginx_snippet_path(app_id: str) -> Path:
    return Path(DEFAULT_NGINX_SNIPPET_DIR) / f"{validate_app_id(app_id)}.conf"


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        if current.is_symlink():
            raise RuntimeError(f"Refusing to manage an Nginx path through a symlink: {current}")


def _write_nginx_snippet(path: Path, content: str) -> Optional[bytes]:
    _assert_no_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    _assert_no_symlink_components(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Refusing to replace a non-regular Nginx snippet: {path}")
    previous_content = path.read_bytes() if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o644)
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
        raise
    return previous_content


def _restore_nginx_snippet(path: Path, previous_content: Optional[bytes]) -> None:
    if previous_content is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o644)
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(previous_content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
        raise


def _nginx_tokens(source: str) -> List[Tuple[str, int, int]]:
    tokens: List[Tuple[str, int, int]] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if character == "#":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline + 1
            continue
        if character in "{};":
            tokens.append((character, index, index + 1))
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            start = index
            index += 1
            value: List[str] = []
            while index < len(source) and source[index] != quote:
                if source[index] == "\\" and index + 1 < len(source):
                    index += 1
                value.append(source[index])
                index += 1
            if index >= len(source):
                raise ValueError("Nginx configuration contains an unterminated quoted string")
            index += 1
            tokens.append(("".join(value), start, index))
            continue
        start = index
        while index < len(source) and not source[index].isspace() and source[index] not in "{};#":
            index += 1
        tokens.append((source[start:index], start, index))
    return tokens


def _nginx_server_blocks(source: str) -> List[Dict[str, Any]]:
    tokens = _nginx_tokens(source)
    blocks: List[Dict[str, Any]] = []
    for index, token in enumerate(tokens[:-1]):
        if token[0] != "server" or tokens[index + 1][0] != "{":
            continue
        depth = 1
        statement: List[str] = []
        directives: List[Tuple[str, List[str]]] = []
        cursor = index + 2
        closing_index = None
        while cursor < len(tokens):
            value = tokens[cursor][0]
            if value == "{":
                depth += 1
                statement.clear()
            elif value == "}":
                depth -= 1
                if depth == 0:
                    closing_index = cursor
                    break
                if depth == 1:
                    statement.clear()
            elif depth == 1 and value == ";":
                if statement:
                    directives.append((statement[0], statement[1:]))
                statement = []
            elif depth == 1:
                statement.append(value)
            cursor += 1
        if closing_index is None:
            raise ValueError("Nginx configuration contains an unterminated server block")
        blocks.append({
            "open_end": tokens[index + 1][2],
            "close_start": tokens[closing_index][1],
            "directives": directives,
        })
    return blocks


def _nginx_config_sections(output: str) -> List[Tuple[Path, str]]:
    sections: List[Tuple[Path, str]] = []
    current_path: Optional[Path] = None
    current_lines: List[str] = []
    header = re.compile(r"^# configuration file (.+):\s*$")
    for line in output.splitlines(keepends=True):
        match = header.match(line.rstrip("\r\n"))
        if match:
            if current_path is not None:
                sections.append((current_path, "".join(current_lines)))
            current_path = Path(match.group(1))
            current_lines = []
        elif current_path is not None:
            current_lines.append(line)
    if current_path is not None:
        sections.append((current_path, "".join(current_lines)))
    return sections


def _nginx_includes_path(output: str, path: Path) -> bool:
    expected = path.resolve(strict=False)
    return any(
        listed_path.resolve(strict=False) == expected
        for listed_path, _ in _nginx_config_sections(output)
    )


def _select_nginx_site_server(output: str, hostname: str) -> Tuple[Path, str, Dict[str, Any]]:
    matches: List[Tuple[Path, str, Dict[str, Any], bool]] = []
    config_root = Path(DEFAULT_NGINX_CONFIG_ROOT).resolve(strict=False)
    for listed_path, source in _nginx_config_sections(output):
        try:
            resolved_path = listed_path.resolve(strict=True)
            resolved_path.relative_to(config_root)
        except (OSError, ValueError):
            continue
        if not resolved_path.is_file():
            continue
        for block in _nginx_server_blocks(source):
            directives = block["directives"]
            names = [
                name
                for directive, values in directives
                if directive == "server_name"
                for name in values
            ]
            if hostname not in names:
                continue
            listeners = [
                values
                for directive, values in directives
                if directive == "listen"
            ]
            is_https = any(
                "ssl" in values
                or any(re.search(r":443$", value) or value == "443" for value in values)
                for values in listeners
            )
            matches.append((resolved_path, source, block, is_https))
    https_matches = [item for item in matches if item[3]]
    candidates = https_matches or matches
    unique = {(path, block["open_end"]): (path, source, block) for path, source, block, _ in candidates}
    if len(unique) != 1:
        if not unique:
            raise RuntimeError(
                f"Could not find an Nginx server block with the exact server_name {hostname!r}; "
                "no site configuration was changed."
            )
        raise RuntimeError(
            f"Found multiple Nginx server blocks for {hostname!r}; refusing to modify an ambiguous site configuration."
        )
    return next(iter(unique.values()))


def _write_nginx_site_file(path: Path, source: str) -> None:
    _assert_no_symlink_components(path.parent)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Refusing to edit a non-regular Nginx site configuration: {path}")
    metadata = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.app-manager.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        if hasattr(os, "fchown") and os.name != "nt":
            os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
        raise


def _backup_nginx_site_file(path: Path, contents: bytes) -> Path:
    backup_root = Path(DEFAULT_STATE_ROOT) / "nginx-backups"
    if backup_root.is_symlink():
        raise RuntimeError("Nginx backup directory must not be a symlink")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    backup_path = backup_root / f"{path.name}.{digest}.before-app-manager"
    if backup_path.exists() or backup_path.is_symlink():
        if backup_path.is_symlink() or not backup_path.is_file():
            raise RuntimeError("Existing Nginx site backup is not a regular file")
        return backup_path
    descriptor = os.open(
        str(backup_path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
    return backup_path


def _ensure_nginx_site_include(output: str) -> Tuple[Path, Optional[str]]:
    hostname = urlsplit(DEFAULT_PUBLIC_ORIGIN).hostname
    if not hostname:
        raise RuntimeError("App Manager public origin does not contain a valid hostname")
    site_path, source, block = _select_nginx_site_server(output, hostname)
    wanted = NGINX_MANAGED_INCLUDE
    if any(
        directive == "include"
        and len(values) == 1
        and values[0] == f"{DEFAULT_NGINX_SNIPPET_DIR}/*.conf"
        for directive, values in block["directives"]
    ):
        return site_path, None
    original_bytes = site_path.read_bytes()
    current_source = original_bytes.decode("utf-8")
    _, _, current_block = _select_nginx_site_server(
        f"# configuration file {site_path}:\n{current_source}",
        hostname,
    )
    if any(
        directive == "include"
        and len(values) == 1
        and values[0] == f"{DEFAULT_NGINX_SNIPPET_DIR}/*.conf"
        for directive, values in current_block["directives"]
    ):
        return site_path, None
    insert_at = current_block["open_end"]
    newline = "\r\n" if "\r\n" in current_source else "\n"
    addition = (
        f"{newline}    # Managed by Cockpit App Manager{newline}"
        f"    {wanted}{newline}"
    )
    updated_source = current_source[:insert_at] + addition + current_source[insert_at:]
    _backup_nginx_site_file(site_path, original_bytes)
    _write_nginx_site_file(site_path, updated_source)
    return site_path, current_source


def _activate_nginx_config(
    app: Dict[str, Any],
    framework: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    with _file_lock(Path(DEFAULT_STATE_ROOT) / "locks" / "nginx.lock"):
        return _activate_nginx_config_locked(app, framework, config)


def _activate_nginx_config_locked(
    app: Dict[str, Any],
    framework: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    app_id = validate_app_id(app["id"])
    snippet_path = _nginx_snippet_path(app_id)
    rendered = _render_nginx_config(
        app_id,
        app["port"],
        framework=framework,
        config=config,
        route=app.get("route"),
        static_root=app.get("static_root"),
    )
    previous_content = _write_nginx_snippet(snippet_path, rendered)
    site_path: Optional[Path] = None
    previous_site_source: Optional[str] = None
    try:
        config_listing = run_command(["nginx", "-T"], timeout=int(config["command_timeout_seconds"]))
        _command_error(config_listing, "nginx -T")
        site_path, previous_site_source = _ensure_nginx_site_include(
            (config_listing.stdout or "") + "\n" + (config_listing.stderr or "")
        )
        test_result = run_command(["nginx", "-t"], timeout=int(config["command_timeout_seconds"]))
        _command_error(test_result, "nginx -t")
        verification = run_command(["nginx", "-T"], timeout=int(config["command_timeout_seconds"]))
        _command_error(verification, "nginx -T after managed include")
        if not _nginx_includes_path(
            (verification.stdout or "") + "\n" + (verification.stderr or ""), snippet_path
        ):
            raise RuntimeError(
                f"Nginx did not load the generated application snippet {snippet_path}"
            )
        _systemctl("reload", "nginx", timeout=int(config["command_timeout_seconds"]))
    except Exception as exc:
        rollback_errors: List[str] = []
        if site_path is not None and previous_site_source is not None:
            try:
                _write_nginx_site_file(site_path, previous_site_source)
            except Exception as rollback_exc:
                rollback_errors.append(f"site configuration restore failed: {rollback_exc}")
        try:
            _restore_nginx_snippet(snippet_path, previous_content)
        except Exception as rollback_exc:
            rollback_errors.append(f"snippet restore failed: {rollback_exc}")
        rollback_detail = f"; {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise RuntimeError(
            f"Nginx route activation failed: {exc}{rollback_detail}"
        ) from exc
    return {
        "active": True,
        "route": app.get("route"),
        "config_path": str(snippet_path),
        "message": (
            f"Nginx is serving {app.get('route')} from the managed application configuration. "
            f"Added the managed include to {site_path}."
            if previous_site_source is not None
            else f"Nginx is serving {app.get('route')} from the managed application configuration."
        ),
    }


def _remove_nginx_config(app_id: str, config: Dict[str, Any]) -> None:
    with _file_lock(Path(DEFAULT_STATE_ROOT) / "locks" / "nginx.lock"):
        _remove_nginx_config_locked(app_id, config)


def _remove_nginx_config_locked(app_id: str, config: Dict[str, Any]) -> None:
    snippet_path = _nginx_snippet_path(app_id)
    if not snippet_path.exists() and not snippet_path.is_symlink():
        return
    _assert_no_symlink_components(snippet_path.parent)
    if snippet_path.is_symlink() or not snippet_path.is_file():
        raise RuntimeError(f"Refusing to remove a non-regular Nginx snippet: {snippet_path}")
    previous_content = snippet_path.read_bytes()
    if f"# Managed by Cockpit App Manager: {app_id}".encode("utf-8") not in previous_content:
        raise RuntimeError(f"Refusing to remove an unverified Nginx snippet: {snippet_path}")
    config_listing = run_command(["nginx", "-T"], timeout=int(config["command_timeout_seconds"]))
    if config_listing.returncode != 0:
        detail = (config_listing.stderr or config_listing.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Could not verify whether the Nginx snippet is active: {detail}")
    snippet_is_active = _nginx_includes_path(
        (config_listing.stdout or "") + "\n" + (config_listing.stderr or ""), snippet_path
    )
    snippet_path.unlink()
    if not snippet_is_active:
        return
    try:
        test_result = run_command(["nginx", "-t"], timeout=int(config["command_timeout_seconds"]))
        _command_error(test_result, "nginx -t")
        _systemctl("reload", "nginx", timeout=int(config["command_timeout_seconds"]))
    except Exception as exc:
        _restore_nginx_snippet(snippet_path, previous_content)
        raise RuntimeError(f"Could not deactivate the App Manager Nginx route; the snippet was restored: {exc}") from exc


def _write_service(
    app: Dict[str, Any],
    release_dir: Path,
    start_command: Sequence[str],
    config: Dict[str, Any],
    working_directory: Optional[Path] = None,
) -> Path:
    service = _render_service(app, start_command, config, working_directory, release_dir if "service_slot" in app else None)
    service_path = _service_path(app)
    _verified_service_bytes(app, service_path)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{service_path.name}.", dir=str(service_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(service)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, service_path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    return service_path


def _restore_service_file(service_path: Path, previous_content: Optional[bytes]) -> None:
    if previous_content is None:
        with contextlib.suppress(FileNotFoundError):
            service_path.unlink()
        return
    fd, temporary_name = tempfile.mkstemp(prefix=f".{service_path.name}.", dir=str(service_path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(previous_content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, service_path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def _systemctl(*args: str, timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    result = run_command(["systemctl", *args], timeout=timeout)
    _command_error(result, f"systemctl {' '.join(args)}")
    return result


def _atomic_symlink(target: Path, link_path: Path) -> None:
    temp_link = link_path.with_name(f".{link_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_link.symlink_to(target, target_is_directory=True)
        os.replace(str(temp_link), str(link_path))
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp_link.unlink()
        raise


def _release_command_path(command: Sequence[str], release_dir: Path, current_path: Path) -> List[str]:
    result = []
    release = str(release_dir)
    current = str(current_path)
    for part in command:
        if part == release:
            result.append(current)
        elif part.startswith(release + os.sep):
            result.append(current + part[len(release):])
        else:
            result.append(part)
    return result


def _record_deployment(app: Dict[str, Any], record: Dict[str, Any]) -> None:
    app["deployments"] = [dict(record)]
    app["updated_at"] = now_iso()


def _prune_releases(app: Dict[str, Any]) -> None:
    production = Path(app["production_path"])
    releases_dir = production / "releases"
    if not releases_dir.exists():
        return
    releases = sorted((path for path in releases_dir.iterdir() if path.is_dir()), key=lambda path: path.name)
    current = (production / "current").resolve(strict=False)
    retained = {current} if current.exists() else set()
    for release in releases:
        if release not in retained:
            shutil.rmtree(release)
            for deployment in app.get("deployments", []):
                if deployment.get("release") == str(release):
                    deployment["release_available"] = False


def _app_lock(app_id: str) -> contextlib.AbstractContextManager:
    return _file_lock(Path(DEFAULT_STATE_ROOT) / "locks" / f"{validate_app_id(app_id)}.lock")


def _deployment_log_path(app_id: str, deployment_id: str, *, create: bool = False) -> Path:
    app_id = validate_app_id(app_id)
    if not re.fullmatch(r"\d{14}-[a-f0-9]{8}", deployment_id):
        raise ValueError("Invalid deployment log identifier")
    state_root = Path(DEFAULT_STATE_ROOT)
    log_root = state_root / "deployment-logs"
    if state_root.is_symlink() or log_root.is_symlink():
        raise RuntimeError("Deployment log directory must not be a symlink")
    log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(log_root), 0o700)
    path = log_root / f"{app_id}-{deployment_id}.log"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("Deployment log must be a regular file")
    if create:
        descriptor = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    return path


def _deployment_cancel_path(app_id: str, deployment_id: str) -> Path:
    _deployment_log_path(app_id, deployment_id)
    return Path(DEFAULT_STATE_ROOT) / "deployment-logs" / f"{app_id}-{deployment_id}.cancel"


def _prune_deployment_logs(app_id: str, keep_deployment_id: str) -> None:
    app_id = validate_app_id(app_id)
    _deployment_log_path(app_id, keep_deployment_id)
    log_root = Path(DEFAULT_STATE_ROOT) / "deployment-logs"
    prefix = f"{app_id}-"
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d{14}-[a-f0-9]{8})(?:\.log|\.cancel)$")
    for path in log_root.iterdir():
        match = pattern.fullmatch(path.name)
        if match is None or match.group(1) == keep_deployment_id:
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Refusing to remove non-regular deployment log entry: {path.name}")
        path.unlink()


def _raise_if_deployment_cancelled(cancel_file: Path) -> None:
    if cancel_file.exists():
        raise DeploymentAborted("Deployment aborted by request")


def _append_deployment_log(path: Path, message: str) -> None:
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(f"{now_iso()} {message.rstrip()}\n")
            handle.flush()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _setup_persistent_storage(app: Dict[str, Any], username: str, config: Dict[str, Any]) -> None:
    paths = _app_paths(app["id"], config)
    storage = paths["storage"]
    _assert_no_symlink_components(storage)
    storage.mkdir(mode=0o755, exist_ok=True)
    os.chown(str(storage), 0, 0)
    os.chmod(str(storage), 0o755)
    uid, group_id = _account_ids(username)
    for name in app.get("storage_directories", ["data"]):
        if not isinstance(name, str) or not APP_ROUTE_SEGMENT_RE.fullmatch(name):
            raise ValueError("Invalid persistent directory name")
        directory = storage / name
        _assert_no_symlink_components(directory)
        directory.mkdir(mode=0o750, exist_ok=True)
        os.chown(str(directory), uid, group_id, follow_symlinks=False)
        os.chmod(str(directory), 0o750)


def _setup_app_directories(app: Dict[str, Any], username: str, config: Dict[str, Any]) -> Dict[str, Path]:
    paths = _app_paths(app["id"], config)
    paths["home"].parent.mkdir(parents=True, exist_ok=True, mode=0o711)
    os.chmod(paths["home"].parent, 0o711)
    if paths["production"].is_symlink() or paths["releases"].is_symlink():
        raise RuntimeError("Application production directories must not be symlinks")
    for key in ("source", "config", "logs", "home"):
        if paths[key].is_symlink():
            raise RuntimeError(f"Application {key} directory must not be a symlink")
    for key in ("production", "releases"):
        paths[key].mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chown(str(paths[key]), 0, 0)
        os.chmod(str(paths[key]), 0o755)
    for key in ("source", "config", "logs", "home"):
        paths[key].mkdir(parents=True, exist_ok=True, mode=0o750)
        _chown_tree(paths[key], username)

    _, group_id = _account_ids(username)
    _setup_persistent_storage(app, username, config)
    public_releases = {
        _resolved_path(Path(item["release"]))
        for item in app.get("deployments", [])
        if isinstance(item, dict)
        and (item.get("framework") == "static" or item.get("static_export") is True)
        and isinstance(item.get("release"), str)
    }
    if _is_static_app(app):
        current = paths["current"].resolve(strict=False)
        if current.exists():
            public_releases.add(current)
    for release in paths["releases"].iterdir():
        if release.is_symlink() or not release.is_dir():
            raise RuntimeError(f"Unexpected entry in application releases directory: {release}")
        _seal_release(release, group_id, public=_resolved_path(release) in public_releases)
    return paths


def _prepare_release_directory(release_dir: Path, username: str) -> None:
    if release_dir.exists() or release_dir.is_symlink():
        raise RuntimeError("Generated release path already exists")
    uid, group_id = _account_ids(username)
    release_dir.mkdir(mode=0o750)
    os.chown(str(release_dir), uid, group_id)
    os.chmod(str(release_dir), 0o750)


def pull_app(app_id: str) -> Dict[str, Any]:
    config = get_config()
    with _app_lock(app_id):
        app = _find_app(get_registry(), app_id)
        username = ensure_app_user(app_id)
        paths = _setup_app_directories(app, username, config)
        result = clone_or_update_repo(app["repo_url"], paths["source"], app["branch"], username, int(config["command_timeout_seconds"]))
        app["latest_commit"] = result.get("commit") or git_output(paths["source"], "rev-parse", "HEAD", username=username)
        with _registry_transaction() as registry:
            stored = _find_app(registry, app_id)
            stored["latest_commit"] = app["latest_commit"]
            stored["updated_at"] = now_iso()
        return {"ok": True, "result": result, "app": app}


def _remote_app_commit(app: Dict[str, Any]) -> str:
    username = ensure_app_user(app["id"])
    branch = validate_branch(app["branch"])
    if not validate_git_url(app["repo_url"]):
        raise ValueError("Invalid repository URL")
    result = _run_as_user(username, ["git", "ls-remote", "--exit-code", app["repo_url"], f"refs/heads/{branch}"],
                          timeout=int(get_config()["command_timeout_seconds"]))
    _command_error(result, "Checking application repository")
    match = re.fullmatch(r"([0-9a-f]{40,64})\t" + re.escape(f"refs/heads/{branch}") + r"\s*", result.stdout or "")
    if not match:
        raise RuntimeError("Repository returned an invalid branch revision")
    return match.group(1)


def _in_deployment_window(window: Dict[str, str]) -> bool:
    if not window or window["start"] == window["end"]:
        return True
    current = datetime.now(timezone.utc).strftime("%H:%M")
    if window["start"] < window["end"]:
        return window["start"] <= current < window["end"]
    return current >= window["start"] or current < window["end"]


def review_update_app(app_id: str) -> Dict[str, Any]:
    _require_root()
    config = get_config()
    with _app_lock(app_id):
        app = _find_app(get_registry(), app_id)
        username = ensure_app_user(app_id)
        paths = _setup_app_directories(app, username, config)
        clone_or_update_repo(app["repo_url"], paths["source"], app["branch"], username, int(config["command_timeout_seconds"]))
        _assert_deployed_ancestor(app, paths["source"], username, int(config["command_timeout_seconds"]))
        commit = git_output(paths["source"], "rev-parse", "HEAD", username=username)
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise RuntimeError("Invalid candidate revision")
        base = app.get("deployed_commit")
        revision_range = f"{base}..{commit}" if base else commit
        commits = _git(paths["source"], username, ["log", "--format=%h %s", "--max-count=20", revision_range], int(config["command_timeout_seconds"]))
        _command_error(commits, "Reading changes for approval")
        arguments = ["diff", "--stat", base, commit] if base else ["show", "--stat", "--format=", commit]
        difference = _git(paths["source"], username, arguments, int(config["command_timeout_seconds"]))
        _command_error(difference, "Reading changed file summary")
        pending = {"commit": commit, "base_commit": base, "repo_url": app["repo_url"], "branch": app["branch"],
                   "checked_at": now_iso(), "commits": (commits.stdout or "")[:8192], "diffstat": (difference.stdout or "")[:8192]}
        with _registry_transaction() as registry:
            stored = _find_app(registry, app_id)
            stored["pending_update"] = pending
            stored["latest_commit"] = commit
        return {"ok": True, "app_id": app_id, "pending_update": pending}


def _validate_approval(app: Dict[str, Any], commit: Optional[str]) -> None:
    if app.get("deployment_mode", "automatic") != "approval":
        return
    pending = app.get("pending_update", {})
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit) or commit != pending.get("commit"):
        raise RuntimeError("Review and approve the exact candidate commit before deploying")
    if pending.get("repo_url") != app["repo_url"] or pending.get("branch") != app["branch"] or pending.get("base_commit") != app.get("deployed_commit"):
        raise RuntimeError("Application changed since review; review the update again")


def approve_deployment_app(app_id: str, commit: str) -> Dict[str, Any]:
    app = _find_app(get_registry(), app_id)
    if app.get("deployment_mode") != "approval":
        raise ValueError("Application is not in approval mode")
    _validate_approval(app, commit)
    return deployment_start_app(app_id, approved_commit=commit)


def auto_deploy_apps() -> Dict[str, Any]:
    _require_root()
    app_ids = [app["id"] for app in get_registry()["apps"]]
    outcomes = []
    errors = []
    for app_id in app_ids:
        try:
            app = _find_app(get_registry(), app_id)
            if app.get("last_deploy"):
                with contextlib.suppress(OSError, RuntimeError, ValueError):
                    health_check_app(app_id)
                with contextlib.suppress(OSError, RuntimeError, ValueError):
                    metrics_app(app_id)
            if not app.get("auto_deploy", True):
                outcomes.append({"app_id": app_id, "status": "paused"})
                continue
            if not _in_deployment_window(app.get("deployment_window", {})):
                outcomes.append({"app_id": app_id, "status": "outside-deployment-window"})
                continue
            if app.get("auto_deploy_state", {}).get("retry_after", 0) > time.time():
                outcomes.append({"app_id": app_id, "status": "retry-backoff"})
                continue
            latest = app.get("deployments", [])[-1] if app.get("deployments") else None
            if isinstance(latest, dict) and latest.get("status") == "deploying":
                outcomes.append({"app_id": app_id, "status": "deployment-in-progress"})
                continue
            if latest is None:
                outcomes.append({"app_id": app_id, "status": "awaiting-first-deployment"})
                continue
            commit = _remote_app_commit(app)
            current = _find_app(get_registry(), app_id)
            deployment = current.get("deployments", [])[-1] if current.get("deployments") else None
            live_commit = current.get("deployed_commit") or (deployment or {}).get("commit")
            if live_commit != commit:
                if current.get("deployment_mode") == "approval":
                    pending = current.get("pending_update", {})
                    if pending.get("commit") != commit or pending.get("base_commit") != live_commit:
                        review_update_app(app_id)
                    outcomes.append({"app_id": app_id, "status": "awaiting-approval", "commit": commit})
                else:
                    result = deploy_app(app_id)
                    outcomes.append({"app_id": app_id, "status": "deployed", "commit": result["deployment"].get("commit")})
            else:
                outcomes.append({"app_id": app_id, "status": "up-to-date", "commit": commit})
            with _registry_transaction() as registry:
                stored = _find_app(registry, app_id)
                stored["latest_commit"] = commit
                stored["auto_deploy_state"] = {"checked_at": now_iso(), "error": None, "failures": 0, "retry_after": 0}
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{app_id}: {exc}")
            outcomes.append({"app_id": app_id, "status": "failed", "error": str(exc)})
            with _registry_transaction() as registry:
                stored = _find_app(registry, app_id)
                failures = min(stored.get("auto_deploy_state", {}).get("failures", 0) + 1, 8)
                stored["auto_deploy_state"] = {
                    "checked_at": now_iso(), "error": str(exc), "failures": failures,
                    "retry_after": time.time() + min(3600, 300 * 2 ** (failures - 1)),
                }
    if errors:
        raise RuntimeError("Automatic deployment errors: " + "; ".join(errors))
    return {"ok": True, "results": outcomes}


def deployment_start_app(app_id: str, approved_commit: Optional[str] = None) -> Dict[str, Any]:
    _require_root()
    app_id = validate_app_id(app_id)
    _find_app(get_registry(), app_id)
    ensure_app_user(app_id)
    release_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    started_at = now_iso()
    with _registry_transaction() as registry:
        app = _find_app(registry, app_id)
        _validate_approval(app, approved_commit)
        current = app.get("deployments", [])[-1] if app.get("deployments") else None
        if isinstance(current, dict) and current.get("status") == "deploying":
            return {
                "ok": True,
                "app_id": app_id,
                "deployment_id": current.get("id"),
                "active": True,
            }
        record = {
            "id": release_id,
            "name": app["name"],
            "date": started_at,
            "previous_status": app.get("status", "stopped"),
            "approved_commit": approved_commit,
            "commit": None,
            "release": None,
            "release_available": False,
            "framework": app.get("framework", "auto"),
            "build_status": "pending",
            "status": "deploying",
            "abortable": True,
            "error": None,
        }
        app["status"] = "deploying"
        _record_deployment(app, record)

    log_path = None
    unit_name = f"cockpit-app-manager-deploy-{app_id}-{release_id}.service"
    try:
        log_path = _deployment_log_path(app_id, release_id, create=True)
        _prune_deployment_logs(app_id, release_id)
        _append_deployment_log(log_path, "Deployment queued")
        command = [
            "systemd-run",
            "--quiet",
            f"--unit={unit_name}",
            "--collect",
            "--property=NoNewPrivileges=true",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=true",
            "--property=ProtectKernelTunables=true",
            "--property=ProtectKernelModules=true",
            "--property=ProtectControlGroups=true",
            "--property=PrivateTmp=true",
            "--property=PrivateDevices=true",
            "--property=LockPersonality=true",
            "--property=RestrictRealtime=true",
            "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "--property=CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SETGID CAP_SETUID CAP_NET_BIND_SERVICE CAP_KILL",
            "--property=UMask=0027",
            f"--property=ReadWritePaths={DEFAULT_STATE_ROOT} {DEFAULT_PRODUCTION_ROOT} {DEFAULT_LOG_ROOT} /etc/systemd/system /etc/nginx",
            "--property=StandardOutput=null",
            "--property=StandardError=journal",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "deploy",
            app_id,
            "--deployment-id",
            release_id,
        ]
        result = run_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
        _command_error(result, "Starting the detached deployment service")
    except (OSError, RuntimeError) as exc:
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            record = app["deployments"][0]
            record.update({"status": "failed", "error": str(exc)})
            app["status"] = "failed"
        if log_path is not None:
            _append_deployment_log(log_path, f"Could not start deployment: {exc}")
        raise RuntimeError(f"Could not start deployment: {exc}") from exc
    with _registry_transaction() as registry:
        app = _find_app(registry, app_id)
        if app["deployments"][0].get("id") == release_id:
            app["deployments"][0]["systemd_unit"] = unit_name
    return {
        "ok": True,
        "app_id": app_id,
        "deployment_id": release_id,
        "active": True,
    }


def abort_deployment_app(app_id: str, deployment_id: str) -> Dict[str, Any]:
    _require_root()
    app_id = validate_app_id(app_id)
    if not isinstance(deployment_id, str) or not re.fullmatch(r"\d{14}-[a-f0-9]{8}", deployment_id):
        raise ValueError("Invalid deployment identifier")
    with _registry_transaction() as registry:
        app = _find_app(registry, app_id)
        latest = app.get("deployments", [])[-1] if app.get("deployments") else None
        if (
            not isinstance(latest, dict)
            or latest.get("id") != deployment_id
            or latest.get("status") != "deploying"
            or latest.get("abortable", True) is False
        ):
            raise ValueError("That deployment is no longer active or abortable")
        cancel_file = _deployment_cancel_path(app_id, deployment_id)
        if not cancel_file.exists():
            try:
                descriptor = os.open(
                    str(cancel_file),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
    log_path = _deployment_log_path(app_id, deployment_id)
    _append_deployment_log(log_path, "Abort requested")
    return {"ok": True, "app_id": app_id, "deployment_id": deployment_id, "aborting": True}


@contextlib.contextmanager
def _build_slot(cancel_file: Path, on_wait: Optional[Callable[[], None]] = None) -> Iterator[None]:
    root = Path(DEFAULT_STATE_ROOT) / "build-slots"
    _assert_no_symlink_components(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + 3600
    waiting = False
    while True:
        _raise_if_deployment_cancelled(cancel_file)
        if time.monotonic() > deadline:
            raise RuntimeError("Timed out waiting for a build slot")
        with contextlib.ExitStack() as slot:
            selected = False
            occupied = 0
            with _file_lock(root / "admission.lock"):
                for index in range(8):
                    with contextlib.ExitStack() as probe:
                        try:
                            probe.enter_context(_file_lock(root / f"{index}.lock", blocking=False))
                        except OSError as exc:
                            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                                raise
                            occupied += 1
                        else:
                            if not selected:
                                slot.enter_context(probe.pop_all())
                                selected = True
                admitted = selected and occupied < get_config()["max_concurrent_builds"]
            if admitted:
                _raise_if_deployment_cancelled(cancel_file)
                yield
                return
        if not waiting and on_wait is not None:
            on_wait()
        waiting = True
        time.sleep(0.1)


def _host_preflight(app: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    checks = []
    environment = _environment_values_app(app["id"])
    missing = [key for key in app.get("required_environment", []) if not environment.get(key)]
    checks.append({"name": "Environment", "passed": not missing,
                   "message": "Missing required variables: " + ", ".join(missing) if missing else "Required variable names are present"})
    for key in ("source_root", "production_root"):
        path = Path(config[key])
        while not path.exists():
            path = path.parent
        free = shutil.disk_usage(path).free
        checks.append({"name": key, "passed": free >= config.get("min_free_disk_mb", 512) * 1024 * 1024,
                       "message": f"{free // (1024 * 1024)} MiB free; minimum {config.get('min_free_disk_mb', 512)} MiB"})
    return {"checks": checks, "errors": [check["message"] for check in checks if not check["passed"]], "warnings": []}


def _deployment_preflight(app: Dict[str, Any], source: Path, username: str, config: Dict[str, Any]) -> Dict[str, Any]:
    report = _host_preflight(app, config)
    project = inspect_project(source, app["framework"], app.get("build_config", {}))
    report["errors"].extend(project["errors"])
    report["warnings"].extend(project["warnings"])
    report["framework"] = app["framework"]
    report["project_directory"] = project["project_directory"]
    for tool, minimum in {**project["tools"], **app.get("runtime_requirements", {})}.items():
        declared_minimum = project["tools"].get(tool)
        if declared_minimum and minimum:
            minimum = max((minimum, declared_minimum), key=lambda version: tuple(int(part) for part in version.split(".")) + (0,) * (3 - len(version.split("."))))
        try:
            result = _run_as_user(username, [tool, "version" if tool == "go" else "--version"], timeout=10)
            output = (result.stdout or "") + " " + (result.stderr or "")
            match = re.search(r"(?:v|go)?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", output)
            actual = tuple(int(part or 0) for part in match.groups()) if match else None
            needed = tuple(int(part) for part in minimum.split(".")) if minimum else None
            if needed:
                needed += (0,) * (3 - len(needed))
            passed = result.returncode == 0 and (needed is None or actual is not None and actual >= needed)
            message = f"{tool}: " + (".".join(map(str, actual)) if actual else "version unavailable")
            if minimum:
                message += f"; minimum {minimum}"
        except (OSError, RuntimeError) as exc:
            passed, message = False, f"{tool}: {type(exc).__name__}; runtime is unavailable"
        report["checks"].append({"name": tool, "passed": passed, "message": message})
        if not passed:
            report["errors"].append(message)
    report["passed"] = not report["errors"]
    report["checked_at"] = now_iso()
    return report


def preflight_app(app_id: str) -> Dict[str, Any]:
    _require_root()
    config = get_config()
    with _app_lock(app_id):
        app = dict(_find_app(get_registry(), app_id))
        host = _host_preflight(app, config)
        if host["errors"]:
            return {"ok": True, "passed": False, "checked_at": now_iso(), **host}
        username = ensure_app_user(app_id)
        paths = _setup_app_directories(app, username, config)
        clone_or_update_repo(app["repo_url"], paths["source"], app["branch"], username, int(config["command_timeout_seconds"]))
        _assert_deployed_ancestor(app, paths["source"], username, int(config["command_timeout_seconds"]))
        if app["framework"] == "auto":
            app["framework"] = detect_framework(paths["source"], app.get("build_config", {}))
        report = _deployment_preflight(app, paths["source"], username, config)
        report["checks"].insert(0, {"name": "Repository", "passed": True, "message": "Repository access and fast-forward history verified"})
        return {"ok": True, **report}


def _assert_deployed_ancestor(app: Dict[str, Any], source: Path, username: str, timeout: int,
                              cancel_file: Optional[Path] = None) -> None:
    commit = app.get("deployed_commit")
    if commit is None:
        return
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("Invalid deployed Git revision")
    result = _git(source, username, ["merge-base", "--is-ancestor", commit, "HEAD"], timeout, cancel_file)
    if result.returncode != 0:
        raise RuntimeError("Repository history does not fast-forward the deployed commit; refusing rewritten history")


def _remove_deployed_source(app_id: str, config: Dict[str, Any]) -> None:
    source = _app_paths(validate_app_id(app_id), config)["source"]
    _assert_no_symlink_components(source)
    if source.exists():
        if not source.is_dir():
            raise RuntimeError("Managed source checkout is not a directory")
        shutil.rmtree(source)


def _build_release(app: Dict[str, Any], source: Path, release: Path, adapter: Any, username: str,
                   config: Dict[str, Any], release_id: str, cancel_file: Path, deployment_log: Path,
                   **build_arguments: Any) -> Dict[str, Any]:
    def set_phase(phase: str) -> None:
        with _registry_transaction() as registry:
            record = _find_app(registry, app["id"])["deployments"][0]
            record["build_status"] = phase
        _append_deployment_log(deployment_log, "Build phase: " + phase)
    set_phase("queued")
    with _build_slot(cancel_file):
        _raise_if_deployment_cancelled(cancel_file)
        set_phase("preflight")
        report = _deployment_preflight(app, source, username, config)
        for check in report.get("checks", []):
            _append_deployment_log(deployment_log, ("PASS " if check["passed"] else "FAIL ") + check["name"] + ": " + check["message"])
        for warning in report.get("warnings", []):
            _append_deployment_log(deployment_log, "Preflight warning: " + warning)
        if not report["passed"]:
            raise RuntimeError("Deployment preflight failed: " + "; ".join(report["errors"]))
        set_phase("running")
        return adapter.build(app, source, release, **build_arguments)


def deploy_app(app_id: str, deployment_id: Optional[str] = None, approved_commit: Optional[str] = None) -> Dict[str, Any]:
    _require_root()
    config = get_config()
    with _app_lock(app_id):
        release_id = deployment_id or f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        cancel_file = _deployment_cancel_path(app_id, release_id)
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app_snapshot = dict(app)
            existing = next(
                (item for item in app.get("deployments", []) if item.get("id") == release_id),
                None,
            )
            if deployment_id is not None and (existing is None or existing.get("status") != "deploying"):
                raise ValueError("That deployment is not active")
            if existing is None and any(item.get("status") == "deploying" for item in app.get("deployments", [])):
                raise ValueError("Another deployment is already active")
            if existing is not None:
                app_snapshot["status"] = existing.get("previous_status", app_snapshot["status"])
                approved_commit = existing.get("approved_commit")
            else:
                _validate_approval(app_snapshot, approved_commit)
        record = dict(existing) if existing else {
            "id": release_id,
            "name": app_snapshot["name"],
            "date": now_iso(),
            "previous_status": app_snapshot.get("status", "stopped"),
            "approved_commit": approved_commit,
            "commit": None,
            "release": None,
            "release_available": False,
            "framework": app_snapshot.get("framework", "auto"),
            "build_status": "pending",
            "status": "deploying",
            "abortable": True,
            "error": None,
        }
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app["status"] = "deploying"
            if not any(item.get("id") == release_id for item in app.get("deployments", [])):
                _record_deployment(app, record)

        paths: Optional[Dict[str, Path]] = None
        release_dir: Optional[Path] = None
        release_prepared = False
        current_release: Optional[Path] = None
        service_path: Optional[Path] = None
        previous_service_content: Optional[bytes] = None
        deployment_log: Optional[Path] = None
        unit_written = False
        activation_attempted = False
        runtime_required = True
        staged = False
        previous_app = dict(app_snapshot)
        previous_unit_path = _service_path(previous_app)
        old_service_retired = True
        route_activated = False
        try:
            deployment_log = _deployment_log_path(app_id, release_id, create=True)
            _prune_deployment_logs(app_id, release_id)
            _append_deployment_log(deployment_log, "Deployment started")
            _raise_if_deployment_cancelled(cancel_file)
            username = ensure_app_user(app_id)
            paths = _setup_app_directories(app_snapshot, username, config)
            current_release = paths["current"].resolve(strict=False) if paths["current"].exists() or paths["current"].is_symlink() else None
            _validate_approval(app_snapshot, approved_commit)
            host_report = _host_preflight(app_snapshot, config)
            if host_report["errors"]:
                raise RuntimeError("Deployment preflight failed: " + "; ".join(host_report["errors"]))
            source_path = paths["source"]
            _append_deployment_log(deployment_log, "Updating the application source repository")
            clone_or_update_repo(
                app_snapshot["repo_url"],
                source_path,
                app_snapshot["branch"],
                username,
                int(config["command_timeout_seconds"]),
                cancel_file,
            )
            _assert_deployed_ancestor(app_snapshot, source_path, username, int(config["command_timeout_seconds"]), cancel_file)
            _raise_if_deployment_cancelled(cancel_file)
            framework = app_snapshot.get("framework", "auto")
            if framework == "auto":
                framework = detect_framework(source_path, app_snapshot.get("build_config", {}))
            app_snapshot["framework"] = framework
            _append_deployment_log(deployment_log, f"Detected framework: {framework}")
            adapter = get_adapter(framework)
            release_dir = paths["releases"] / release_id
            _prepare_release_directory(release_dir, username)
            release_prepared = True
            record.update({
                "release": str(release_dir),
                "release_available": True,
                "framework": framework,
                "build_status": "running",
            })
            commit = git_output(source_path, "rev-parse", "HEAD", username=username)
            if app_snapshot.get("deployment_mode") == "approval" and commit != approved_commit:
                raise RuntimeError("Repository changed after approval; review and approve the new commit")
            record["commit"] = commit or None
            service_path = _service_path(app_snapshot)
            previous_service_content = _verified_service_bytes(app_snapshot, service_path)
            expects_runtime = framework != "static" or bool(app_snapshot.get("build_config", {}).get("start_command"))
            if expects_runtime and current_release is not None and previous_service_content is not None and previous_app.get("status") == "running":
                staged = True
                old_service_retired = False
                with _registry_transaction() as registry:
                    candidate_port = _select_port(registry, config)
                    _find_app(registry, app_id)["candidate_port"] = candidate_port
                app_snapshot = dict(app_snapshot, port=candidate_port,
                                    service_slot="secondary" if previous_app.get("service_slot", "primary") == "primary" else "primary")
                service_path = _service_path(app_snapshot)
                if service_path.exists():
                    raise RuntimeError("Inactive service slot is occupied; administrator cleanup is required")
                previous_service_content = None
            build_environment = _environment_values_app(app_id)
            build_environment["APP_BASE_PATH"] = validate_app_route(
                app_snapshot.get("route"),
                app_id,
            ).rstrip("/")
            _, release_group = _account_ids(username)
            build_result = _build_release(
                app_snapshot,
                source_path,
                release_dir,
                adapter,
                username,
                config,
                release_id,
                cancel_file,
                deployment_log,
                runner=lambda args, cwd, timeout: _run_as_user(
                    username,
                    args,
                    cwd,
                    timeout,
                    environment=build_environment,
                    live_log_path=deployment_log,
                    cancel_file=cancel_file,
                    resource_limits=app_snapshot.get("resource_limits", {}),
                ),
                timeout=int(config["build_timeout_seconds"]),
                group_id=release_group,
                log=lambda message: _append_deployment_log(deployment_log, message),
            )
            _append_deployment_log(deployment_log, "Build completed; activating the release")
            _raise_if_deployment_cancelled(cancel_file)
            runtime_required = build_result.get("runtime_required", True)
            static_root = build_result.get("static_root")
            static_export = bool(static_root)
            if static_export:
                _static_root_relative_path(static_root)
                app_snapshot["static_export"] = True
                app_snapshot["static_root"] = static_root
            else:
                app_snapshot.pop("static_export", None)
                app_snapshot.pop("static_root", None)
            hosting_framework = "static" if framework == "static" or static_export else framework
            record["static_export"] = static_export
            _seal_release(release_dir, release_group, public=hosting_framework == "static")
            command = _release_command_path(build_result["start_command"], release_dir, paths["current"])
            working_directory = None
            build_working_directory = build_result.get("working_directory")
            if build_working_directory is not None:
                release_app_dir = (release_dir / "app").resolve(strict=True)
                actual_build_directory = Path(build_working_directory).resolve(strict=True)
                try:
                    relative_build_directory = actual_build_directory.relative_to(release_app_dir)
                except ValueError as exc:
                    raise RuntimeError("Framework adapter returned a working directory outside its release") from exc
                if not actual_build_directory.is_dir():
                    raise RuntimeError("Framework adapter returned a non-directory working directory")
                working_directory = paths["current"] / "app" / relative_build_directory
            if runtime_required:
                _write_service(app_snapshot, release_dir, command, config, working_directory)
                unit_written = True
                _systemctl("daemon-reload")
            elif previous_service_content is not None:
                activation_attempted = True
                unit_written = True
                _systemctl("disable", "--now", service_path.name)
                _restore_service_file(service_path, None)
                _systemctl("daemon-reload")
            _retire_legacy_root_service(app_snapshot)
            if not staged:
                _atomic_symlink(release_dir, paths["current"])
            activation_attempted = True
            if runtime_required:
                _systemctl("enable", service_path.name)
                _systemctl("restart", service_path.name)
                _raise_if_deployment_cancelled(cancel_file)
                _append_deployment_log(deployment_log, "Waiting for the application health check")
                if not _wait_for_health(app_snapshot, int(config["health_timeout_seconds"])):
                    raise RuntimeError("Application did not pass its health check before the timeout")
                _assert_loopback_binding(app_snapshot, int(config["command_timeout_seconds"]))
            elif hosting_framework == "static":
                static_release_root = release_dir / "app" / _static_root_relative_path(static_root)
                if not (static_release_root / "index.html").is_file():
                    raise RuntimeError("Static release is missing index.html")
            _raise_if_deployment_cancelled(cancel_file)
            if staged:
                _atomic_symlink(release_dir, paths["current"])
            _append_deployment_log(deployment_log, "Generating and activating the application Nginx route")
            with _registry_transaction() as registry:
                _raise_if_deployment_cancelled(cancel_file)
                latest = _find_app(registry, app_id)["deployments"][0]
                latest["abortable"] = False
            hosting_result = _activate_nginx_config(app_snapshot, hosting_framework, config)
            route_activated = True
            record["hosting"] = hosting_result
            record["abortable"] = False
            _append_deployment_log(deployment_log, hosting_result["message"])
            _append_deployment_log(deployment_log, "Deployment completed successfully")
            record.update({"build_status": "success", "status": "deployed"})
            with _registry_transaction() as registry:
                app = _find_app(registry, app_id)
                app["framework"] = framework
                app["static_export"] = static_export
                if static_export:
                    app["static_root"] = static_root
                else:
                    app.pop("static_root", None)
                app["status"] = "running"
                app["hosting_active"] = (
                    True if hosting_result["active"] else bool(app_snapshot.get("hosting_active", False))
                )
                if hosting_result["active"]:
                    app["active_route"] = app_snapshot["route"]
                app["public_url"] = _public_url_for_route(
                    app.get("active_route") or app["route"]
                )
                app["last_deploy"] = record["date"]
                app["deployed_commit"] = commit or None
                app.pop("pending_update", None)
                app["port"] = app_snapshot["port"]
                if staged:
                    app["service_slot"] = app_snapshot["service_slot"]
                app.pop("candidate_port", None)
                app["health"] = {
                    "checked_at": now_iso(), "service_state": "active" if runtime_required else "not-required",
                    "service_error": None, "local_healthy": True, "local_error": None,
                    "public_healthy": None, "public_error": "Public reachability has not been checked",
                }
                app["latest_commit"] = commit or app.get("latest_commit")
                stored_record = next(item for item in app["deployments"] if item.get("id") == release_id)
                stored_record.update(record)
                app["updated_at"] = now_iso()
        except Exception as exc:
            if deployment_log is not None:
                with contextlib.suppress(OSError):
                    message = "Deployment aborted" if isinstance(exc, DeploymentAborted) else f"Deployment failed: {exc}"
                    _append_deployment_log(deployment_log, message)
            rollback_error = None
            try:
                if staged:
                    if route_activated:
                        previous_route_app = dict(previous_app, route=previous_app.get("active_route") or previous_app["route"])
                        previous_framework = "static" if _is_static_app(previous_app) else previous_app["framework"]
                        _activate_nginx_config(previous_route_app, previous_framework, config)
                    if unit_written and service_path is not None:
                        _systemctl("disable", "--now", service_path.name)
                        _restore_service_file(service_path, None)
                        _systemctl("daemon-reload")
                    if paths is not None and current_release is not None:
                        _atomic_symlink(current_release, paths["current"])
                elif unit_written and service_path is not None:
                    _restore_service_file(service_path, previous_service_content)
                    _systemctl("daemon-reload")
                    if not runtime_required and previous_service_content is not None:
                        _systemctl("enable", service_path.name)
                if not staged and activation_attempted and paths is not None and current_release is not None and current_release.is_dir():
                    _atomic_symlink(current_release, paths["current"])
                    if previous_service_content is not None and service_path is not None:
                        if app_snapshot.get("status") == "running":
                            _systemctl("restart", service_path.name)
                            previous_app = dict(app_snapshot, route=app_snapshot.get("active_route") or app_snapshot["route"])
                            if not _wait_for_health(previous_app, int(config["health_timeout_seconds"])):
                                raise RuntimeError("The previous release did not recover its health check")
                        else:
                            _systemctl("stop", service_path.name)
                    else:
                        if runtime_required and service_path is not None:
                            _systemctl("stop", service_path.name)
                elif not staged and activation_attempted and previous_service_content is None:
                    if paths is not None and paths["current"].is_symlink():
                        paths["current"].unlink()
                    if runtime_required and service_path is not None:
                        _systemctl("stop", service_path.name)
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
            if activation_attempted and current_release is None and runtime_required and service_path is not None:
                with contextlib.suppress(Exception):
                    _systemctl("disable", "--now", service_path.name)
            record.update({
                "build_status": "aborted" if isinstance(exc, DeploymentAborted) else "failed",
                "status": "aborted" if isinstance(exc, DeploymentAborted) else "failed",
                "release_available": bool(release_prepared and release_dir and release_dir.is_dir()),
                "error": str(exc),
            })
            if rollback_error:
                record["rollback_error"] = rollback_error
            if release_prepared and release_dir is not None and not rollback_error:
                with contextlib.suppress(OSError):
                    if release_dir.is_dir() and not release_dir.is_symlink():
                        shutil.rmtree(release_dir)
                        record["release_available"] = False
            with _registry_transaction() as registry:
                app = _find_app(registry, app_id)
                stored_record = next(item for item in app["deployments"] if item.get("id") == release_id)
                stored_record.update(record)
                if not rollback_error:
                    app.pop("candidate_port", None)
                app["updated_at"] = now_iso()
                app["status"] = (
                    "failed" if rollback_error
                    else app_snapshot.get("status", "stopped") if current_release or isinstance(exc, DeploymentAborted)
                    else "failed"
                )
            with contextlib.suppress(FileNotFoundError):
                cancel_file.unlink()
            raise

        cleanup_errors = []
        try:
            _remove_deployed_source(app_id, config)
            _append_deployment_log(deployment_log, "Removed the managed source checkout; runtime release retained")
        except (OSError, RuntimeError) as exc:
            cleanup_errors.append("Source cleanup: " + str(exc))
        try:
            if staged:
                _verified_service_bytes(previous_app, previous_unit_path)
                _systemctl("disable", "--now", previous_unit_path.name)
                _restore_service_file(previous_unit_path, None)
                _systemctl("daemon-reload")
                old_service_retired = True
            if old_service_retired:
                _prune_releases(app)
        except (OSError, RuntimeError) as exc:
            cleanup_errors.append("Previous release cleanup: " + str(exc))
        if cleanup_errors:
            record["cleanup_error"] = "; ".join(cleanup_errors)
            _append_deployment_log(
                _deployment_log_path(app_id, release_id),
                record["cleanup_error"],
            )
            with _registry_transaction() as registry:
                stored = _find_app(registry, app_id)
                stored["deployments"][0]["cleanup_error"] = record["cleanup_error"]
        with contextlib.suppress(FileNotFoundError):
            cancel_file.unlink()
        app_snapshot.update({"status": "running", "framework": framework, "latest_commit": commit or app_snapshot.get("latest_commit")})
        return {
            "ok": True,
            "app": app_metadata(app),
            "build": build_result,
            "deployment": record,
            "hosting": hosting_result,
        }


def rollback_app(app_id: str, release_id: Optional[str] = None) -> Dict[str, Any]:
    _require_root()
    config = get_config()
    with _app_lock(app_id):
        registry = get_registry()
        app = _find_app(registry, app_id)
        deployments = app.get("deployments", [])
        paths = _app_paths(app_id, config)
        current_release = paths["current"].resolve(strict=False) if paths["current"].exists() or paths["current"].is_symlink() else None
        target_record = None
        for item in reversed(deployments):
            if item.get("status") == "deployed" and item.get("release_available", True):
                if release_id is not None and item.get("id") == release_id:
                    target_record = item
                    break
                candidate = item.get("release")
                if release_id is None and candidate and _resolved_path(Path(candidate)) != current_release:
                    target_record = item
                    break
        if target_record is None:
            raise ValueError("No requested rollback deployment is available")
        release = _resolved_path(Path(target_record["release"]))
        if not _path_is_within(release, paths["releases"]) or not release.is_dir():
            raise ValueError("Rollback release is outside the managed releases directory or missing")
        username = app["service_user"]
        service_path = _service_path(app)
        if not service_path.exists():
            raise RuntimeError("Application service is not installed")
        previous_running = app.get("status") == "running"
        try:
            _atomic_symlink(release, paths["current"])
            _systemctl("restart", service_path.name)
            if not _wait_for_health(app, int(config["health_timeout_seconds"])):
                raise RuntimeError("Rolled-back application did not pass its health check")
        except Exception as exc:
            recovery_error = None
            try:
                if current_release is not None and current_release.is_dir():
                    _atomic_symlink(current_release, paths["current"])
                    if previous_running:
                        _systemctl("restart", service_path.name)
                        if not _wait_for_health(app, int(config["health_timeout_seconds"])):
                            raise RuntimeError("The previous release did not recover its health check")
                    else:
                        _systemctl("stop", service_path.name)
                else:
                    if paths["current"].is_symlink():
                        paths["current"].unlink()
                    _systemctl("stop", service_path.name)
            except Exception as recovery_exc:
                recovery_error = str(recovery_exc)
            message = f"Rollback failed: {exc}"
            if recovery_error:
                message += f"; restoring the previous release also failed: {recovery_error}"
            raise RuntimeError(message) from exc
        with _registry_transaction() as current_registry:
            stored = _find_app(current_registry, app_id)
            stored["status"] = "running"
            stored["updated_at"] = now_iso()
            _record_deployment(stored, {
                "id": f"rollback-{uuid.uuid4().hex[:12]}",
                "name": stored["name"],
                "date": now_iso(),
                "commit": target_record.get("commit"),
                "release": str(release),
                "release_available": True,
                "build_status": "skipped",
                "status": "rolled_back",
                "error": None,
            })
        return {"ok": True, "app_id": app_id, "release": str(release), "service_user": username}


def _service_action(app_id: str, action_name: str) -> Dict[str, Any]:
    _require_root()
    config = get_config()
    with _app_lock(app_id):
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app_snapshot = dict(app)
        if _is_static_app(app_snapshot):
            route = app_snapshot.get("active_route") or app_snapshot["route"]
            if action_name == "stop":
                _remove_nginx_config(app_id, config)
            else:
                index = _static_content_root(app_snapshot, config) / "index.html"
                if not index.is_file():
                    raise RuntimeError("Static application has no deployed index.html; deploy it first")
                _activate_nginx_config(dict(app_snapshot, route=route), "static", config)
            status = "stopped" if action_name == "stop" else "running"
            with _registry_transaction() as registry:
                app = _find_app(registry, app_id)
                app["status"] = status
                app["hosting_active"] = status == "running"
                app["active_route"] = route
                app["updated_at"] = now_iso()
                app.pop("health", None)
            return {"ok": True, "app_id": app_id, "status": status}
        service_path = _service_path(app_snapshot)
        if not service_path.exists():
            raise RuntimeError(f"Application service {service_path.name} is not installed; deploy it first")
        _systemctl(action_name, service_path.name)
        active_app = dict(app_snapshot, route=app_snapshot.get("active_route") or app_snapshot["route"])
        if action_name in {"start", "restart"}:
            try:
                if not _wait_for_health(active_app, int(config["health_timeout_seconds"])):
                    raise RuntimeError(f"Application did not pass its health check after {action_name}")
                _assert_loopback_binding(active_app, int(config["command_timeout_seconds"]))
            except RuntimeError:
                try:
                    _systemctl("stop", service_path.name)
                finally:
                    with _registry_transaction() as registry:
                        stored = _find_app(registry, app_id)
                        stored["status"] = "failed"
                        stored["updated_at"] = now_iso()
                        stored.pop("health", None)
                raise
        status = "stopped" if action_name == "stop" else "running"
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app["status"] = status
            app["updated_at"] = now_iso()
            app.pop("health", None)
        return {"ok": True, "app_id": app_id, "status": status}


def start_app(app_id: str) -> Dict[str, Any]:
    return _service_action(app_id, "start")


def stop_app(app_id: str) -> Dict[str, Any]:
    return _service_action(app_id, "stop")


def restart_app(app_id: str) -> Dict[str, Any]:
    return _service_action(app_id, "restart")


def logs_app(app_id: str, lines: int = 100) -> Dict[str, Any]:
    validate_app_id(app_id)
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 1000:
        raise ValueError("Log line count must be between 1 and 1000")
    app = _find_app(get_registry(), app_id)
    config = get_config()
    paths = _app_paths(app_id, config)
    stdout_log = paths["logs"] / "stdout.log"
    stderr_log = paths["logs"] / "stderr.log"
    output = _tail_log(stdout_log, lines)
    errors = _tail_log(stderr_log, lines)
    combined = []
    if output:
        combined.append("[stdout]\n" + output)
    if errors:
        combined.append("[stderr]\n" + errors)
    latest = next(
        (
            item for item in reversed(app.get("deployments", []))
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and re.fullmatch(r"\d{14}-[a-f0-9]{8}", item["id"])
        ),
        None,
    )
    if latest is not None:
        deployment_path = _deployment_log_path(app_id, latest["id"])
        deployment_output = _tail_log(deployment_path, lines)
        if deployment_output:
            combined.append("[latest deployment]\n" + deployment_output)
    return {
        "ok": True,
        "app_id": app_id,
        "logs": "\n".join(combined),
        "message": None if combined else "No application logs are available",
    }


def deployment_logs_app(app_id: str, deployment_id: Optional[str] = None, *, status_only: bool = False) -> Dict[str, Any]:
    app_id = validate_app_id(app_id)
    app = _find_app(get_registry(), app_id)
    latest = next(
        (
            item for item in reversed(app.get("deployments", []))
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and re.fullmatch(r"\d{14}-[a-f0-9]{8}", item["id"])
            and (deployment_id is None or item.get("id") == deployment_id)
        ),
        None,
    )
    if latest is None:
        return {"ok": True, "app_id": app_id, "active": False, "status": None, "logs": ""}
    log_path = _deployment_log_path(app_id, latest["id"])
    cancel_file = _deployment_cancel_path(app_id, latest["id"])
    return {
        "ok": True,
        "app_id": app_id,
        "deployment_id": latest["id"],
        "active": latest.get("status") == "deploying",
        "aborting": latest.get("status") == "deploying" and cancel_file.exists(),
        "build_status": latest.get("build_status", "pending"),
        "abortable": latest.get("abortable", True) is not False,
        "status": latest.get("status"),
        **({} if status_only else {"logs": _tail_log(log_path, 1000, max_bytes=1024 * 1024)}),
    }


def _tail_log(path: Path, lines: int, max_bytes: int = 1024 * 1024) -> str:
    if not path.exists():
        return ""
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Refusing to read non-regular application log: {path.name}")
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        start = max(0, end - max_bytes)
        handle.seek(start)
        data = handle.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    if start:
        text = text.split("\n", 1)[-1]
    return "\n".join(text.splitlines()[-lines:])


def _directory_usage(path: Path, max_entries: int = 10000) -> Dict[str, Any]:
    size = 0
    entries = 0
    deadline = time.monotonic() + 1
    truncated = False
    def scan_error(error: OSError) -> None:
        nonlocal truncated
        if error.errno != errno.ENOENT:
            truncated = True
    if path.is_symlink():
        return {"bytes": 0, "truncated": True}
    for root, directories, filenames in os.walk(path, followlinks=False, onerror=scan_error):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in directories + filenames:
            entries += 1
            if entries > max_entries or time.monotonic() > deadline:
                return {"bytes": size, "truncated": True}
            try:
                metadata = (Path(root) / name).lstat()
                if stat.S_ISREG(metadata.st_mode):
                    size += metadata.st_size
            except OSError:
                truncated = True
    return {"bytes": size, "truncated": truncated}


def metrics_app(app_id: str) -> Dict[str, Any]:
    app = _find_app(get_registry(), app_id)
    paths = _app_paths(app_id, get_config())
    metrics = {"checked_at": now_iso(), "sample_time": time.monotonic(), "cpu_percent": None,
               "memory_bytes": None, "peak_memory_bytes": None, "tasks": None, "restarts": None,
               "uptime_seconds": None, "service_state": "not-required", "errors": []}
    if not _is_static_app(app):
        try:
            result = _systemctl("show", _service_path(app).name,
                                "--property=ActiveState,CPUUsageNSec,MemoryCurrent,MemoryPeak,TasksCurrent,NRestarts,ActiveEnterTimestampMonotonic,InvocationID", timeout=5)
            properties = dict(line.split("=", 1) for line in (result.stdout or "").splitlines() if "=" in line)
            metrics["service_state"] = properties.get("ActiveState", "unknown")
            for name, property_name in (("memory_bytes", "MemoryCurrent"), ("peak_memory_bytes", "MemoryPeak"),
                                        ("tasks", "TasksCurrent"), ("restarts", "NRestarts"), ("cpu_usage_nsec", "CPUUsageNSec")):
                value = properties.get(property_name, "")
                metrics[name] = int(value) if re.fullmatch(r"[0-9]{1,20}", value) and int(value) < 2 ** 64 - 1 else None
            metrics["invocation_id"] = properties.get("InvocationID")
            entered = properties.get("ActiveEnterTimestampMonotonic", "")
            if metrics["service_state"] == "active" and entered.isdigit() and int(entered) > 0:
                metrics["uptime_seconds"] = max(0, metrics["sample_time"] - int(entered) / 1000000)
            previous = app.get("metrics", {})
            previous_time = previous.get("sample_time")
            elapsed = metrics["sample_time"] - previous_time if type(previous_time) in (int, float) and 0 <= previous_time < metrics["sample_time"] else 0
            previous_cpu = previous.get("cpu_usage_nsec")
            if elapsed > 0 and metrics["invocation_id"] and metrics["invocation_id"] == previous.get("invocation_id") and metrics.get("cpu_usage_nsec") is not None and type(previous_cpu) is int and 0 <= previous_cpu < 2 ** 64 - 1:
                difference = metrics["cpu_usage_nsec"] - previous_cpu
                if difference >= 0:
                    metrics["cpu_percent"] = round(difference / elapsed / 10000000, 1)
        except (OSError, RuntimeError) as exc:
            metrics["service_state"] = "unknown"
            metrics["errors"].append(str(exc))
    for name, directory in (("disk", paths["production"]), ("storage", paths["storage"]),
                             ("logs", paths["logs"]), ("source", paths["source"])):
        usage = _directory_usage(directory)
        metrics[name + "_bytes"] = usage["bytes"]
        metrics[name + "_truncated"] = usage["truncated"]
    with _registry_transaction() as registry:
        _find_app(registry, app_id)["metrics"] = metrics
    return {"ok": True, "app_id": app_id, "metrics": metrics}


def health_check_app(app_id: str) -> Dict[str, Any]:
    app = _find_app(get_registry(), app_id)
    app = dict(app, route=app.get("active_route") or app["route"])
    url = app_health_url(app)
    static = _is_static_app(app)
    service_state = "not-required" if static else "unknown"
    service_error = None
    if not static:
        try:
            result = _systemctl("show", "--property=ActiveState", "--value", _service_path(app).name, timeout=5)
            service_state = (result.stdout or "").strip() or "unknown"
        except (OSError, RuntimeError) as exc:
            service_error = str(exc)
    healthy = check_app_health(app, 3)
    public_healthy = None
    public_error = "Route is not active" if not app.get("hosting_active") else None
    if app.get("hosting_active"):
        public_url = DEFAULT_PUBLIC_ORIGIN + _routed_health_path(app)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
            with opener.open(urllib.request.Request(public_url, method="GET"), timeout=3) as response:
                public_healthy = 200 <= response.status < 300
                if not public_healthy:
                    public_error = f"Public route returned HTTP {response.status}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            public_healthy = False
            public_error = str(exc)
    health = {"checked_at": now_iso(), "service_state": service_state, "service_error": service_error,
              "local_healthy": healthy, "local_error": None if healthy else "Local readiness check failed",
              "public_healthy": public_healthy, "public_error": public_error}
    with _registry_transaction() as registry:
        stored = _find_app(registry, app_id)
        if stored.get("status") == app.get("status") and stored.get("updated_at") == app.get("updated_at"):
            stored["health"] = health
    return {"ok": True, "app_id": app_id, "healthy": healthy, "url": url, **health}


def remove_app(app_id: str) -> Dict[str, Any]:
    validate_app_id(app_id)
    _require_root()
    with _app_lock(app_id):
        app = _find_app(get_registry(), app_id)
        _remove_nginx_config(app_id, get_config())
        service_path = _service_path(app)
        _retire_legacy_root_service(app)
        for slot in ("primary", "secondary"):
            service_path = _service_path(dict(app, service_slot=slot))
            if service_path.exists():
                _verified_service_bytes(app, service_path)
                _systemctl("disable", "--now", service_path.name)
                service_path.unlink()
                _systemctl("daemon-reload")
        with _registry_transaction() as registry:
            _find_app(registry, app_id)
            registry["apps"] = [item for item in registry["apps"] if item.get("id") != app_id]
        return {"ok": True, "removed": app_id, "data_retained": True}


def open_app(app_id: str) -> Dict[str, Any]:
    app = _find_app(get_registry(), app_id)
    route = app.get("active_route")
    if not app.get("hosting_active") or not route:
        raise RuntimeError(
            "Application route is not active in Nginx. Add the App Manager snippets include "
            "inside the intended site's server block, run nginx -t and reload Nginx, then deploy again."
        )
    route = validate_app_route(route, app_id)
    return {"ok": True, "app_id": app_id, "url": _public_url_for_route(route)}


def action(app_id: str, action_name: str) -> Dict[str, Any]:
    operations = {
        "deploy": lambda: deploy_app(app_id),
        "pull": lambda: pull_app(app_id),
        "start": lambda: start_app(app_id),
        "stop": lambda: stop_app(app_id),
        "restart": lambda: restart_app(app_id),
        "logs": lambda: logs_app(app_id),
        "remove": lambda: remove_app(app_id),
        "open": lambda: open_app(app_id),
        "health": lambda: health_check_app(app_id),
        "health-check": lambda: health_check_app(app_id),
        "rollback": lambda: rollback_app(app_id),
        "edit": lambda: edit_app(app_id, {}),
    }
    if action_name not in operations:
        raise ValueError(f"Action {action_name!r} is not supported")
    return operations[action_name]()


def parse_payload(raw: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Application payload must be a JSON object")
    return payload


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="Cockpit App Manager backend")
    commands = parser.add_subparsers(dest="command", required=True)
    settings = commands.add_parser("settings")
    settings.add_argument("payload", nargs="?")
    for name in ("list", "overview", "deployments"):
        commands.add_parser(name)
    commands.add_parser("plugin-update-check")
    commands.add_parser("plugin-update")
    deployment_logs = commands.add_parser("deployment-logs")
    deployment_logs.add_argument("app_id")
    deployment_logs.add_argument("deployment_id", nargs="?")
    deployment_logs.add_argument("--status-only", action="store_true")
    deployment_abort = commands.add_parser("deployment-abort")
    deployment_abort.add_argument("app_id")
    deployment_abort.add_argument("deployment_id")
    commands.add_parser("auto-deploy")
    deployment_start = commands.add_parser("deployment-start")
    deployment_start.add_argument("app_id")
    add = commands.add_parser("add")
    add.add_argument("payload")
    edit = commands.add_parser("edit")
    edit.add_argument("app_id")
    edit.add_argument("payload")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("app_id")
    deploy.add_argument("--deployment-id")
    for name in ("pull", "start", "stop", "restart", "remove", "health", "health-check", "check-health", "open", "preflight", "metrics", "review-update"):
        command = commands.add_parser(name)
        command.add_argument("app_id")
    logs = commands.add_parser("logs")
    approve = commands.add_parser("approve")
    approve.add_argument("app_id")
    approve.add_argument("commit")
    logs.add_argument("app_id")
    logs.add_argument("--lines", type=int, default=100)
    env_get = commands.add_parser("env-get")
    env_get.add_argument("app_id")
    env_save = commands.add_parser("env-save")
    env_save.add_argument("app_id")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("app_id")
    rollback.add_argument("release_id", nargs="?")
    act = commands.add_parser("action")
    act.add_argument("action_name")
    act.add_argument("app_id")
    return parser


def _dispatch(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "list":
        return list_apps()
    if args.command == "overview":
        return overview()
    if args.command == "deployments":
        return get_deployments()
    if args.command == "deployment-logs":
        return deployment_logs_app(args.app_id, args.deployment_id, status_only=args.status_only)
    if args.command == "deployment-abort":
        return abort_deployment_app(args.app_id, args.deployment_id)
    if args.command == "deployment-start":
        return deployment_start_app(args.app_id)
    if args.command == "auto-deploy":
        return auto_deploy_apps()
    if args.command == "deploy":
        return deploy_app(args.app_id, args.deployment_id)
    if args.command == "plugin-update-check":
        return plugin_update_check()
    if args.command == "plugin-update":
        return plugin_update()
    if args.command == "settings":
        if getattr(args, "payload", None) is None:
            return get_settings()
        return update_settings(parse_payload(args.payload))
    if args.command == "add":
        return add_app(parse_payload(args.payload))
    if args.command == "edit":
        return edit_app(args.app_id, parse_payload(args.payload))
    if args.command == "action":
        return action(args.app_id, args.action_name)
    if args.command == "logs":
        return logs_app(args.app_id, args.lines)
    if args.command == "rollback":
        return rollback_app(args.app_id, args.release_id)
    if args.command in {"health", "health-check", "check-health"}:
        return health_check_app(args.app_id)
    if args.command == "open":
        return open_app(args.app_id)
    if args.command == "preflight":
        return preflight_app(args.app_id)
    if args.command == "metrics":
        return metrics_app(args.app_id)
    if args.command == "review-update":
        return review_update_app(args.app_id)
    if args.command == "approve":
        return approve_deployment_app(args.app_id, args.commit)
    if args.command == "env-get":
        return get_environment_app(args.app_id)
    if args.command == "env-save":
        contents = sys.stdin.read(MAX_ENV_FILE_BYTES + 1)
        if len(contents.encode("utf-8")) > MAX_ENV_FILE_BYTES:
            raise ValueError(f"Environment file must be smaller than {MAX_ENV_FILE_BYTES} bytes")
        return save_environment_app(args.app_id, contents)
    operations = {
        "deploy": deploy_app,
        "pull": pull_app,
        "start": start_app,
        "stop": stop_app,
        "restart": restart_app,
        "remove": remove_app,
    }
    if args.command in operations:
        return operations[args.command](args.app_id)
    raise ValueError(f"Unsupported command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        result = _dispatch(args)
        if not isinstance(result, dict):
            raise RuntimeError("Backend command returned an invalid response")
        response = dict(result)
        response.setdefault("ok", True)
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0
    except SystemExit as exc:  # argparse help
        return int(exc.code or 0)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
