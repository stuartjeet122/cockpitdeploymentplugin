#!/usr/bin/env python3
from __future__ import annotations

import argparse
import codecs
import contextlib
import json
import os
import re
import shutil
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
    from .adapters import detect_framework, get_adapter
except ImportError:  # pragma: no cover - direct execution after installation
    from adapters import detect_framework, get_adapter

APP_NAME = "app-manager"
DEFAULT_STATE_ROOT = "/var/lib/cockpit/app-manager"
DEFAULT_CONFIG_PATH = f"{DEFAULT_STATE_ROOT}/config.json"
DEFAULT_REGISTRY_PATH = f"{DEFAULT_STATE_ROOT}/apps.json"
DEFAULT_PRODUCTION_ROOT = "/opt/webapps"
DEFAULT_SOURCE_ROOT = f"{DEFAULT_STATE_ROOT}/sources"
DEFAULT_LOG_ROOT = "/var/log/cockpit-app-manager"
DEFAULT_PLUGIN_VERSION_PATH = f"{DEFAULT_STATE_ROOT}/plugin-version.json"
PLUGIN_REPOSITORY_URL = "https://github.com/stuartjeet122/cockpitdeploymentplugin.git"
PLUGIN_REPOSITORY_BRANCH = "main"
PLUGIN_UPDATE_USER = "nobody"
PORT_START = 8100
PORT_END = 8999
RESERVED_PORTS = {8080}
COMMAND_TIMEOUT_SECONDS = 30
MAX_DEPLOYMENT_LOG_BYTES = 5 * 1024 * 1024
MAX_ENV_FILE_BYTES = 64 * 1024
APP_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FRAMEWORKS = {"auto", "nextjs", "go", "node", "python", "static"}


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
        "release_retention": 5,
        "build_timeout_seconds": 900,
        "health_timeout_seconds": 30,
        "command_timeout_seconds": COMMAND_TIMEOUT_SECONDS,
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
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    handle = path.open("r+b")
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        elif msvcrt is not None:  # pragma: no cover - Windows development host
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
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
    retention = config["release_retention"]
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 0:
        raise ValueError("release_retention must be a non-negative integer")
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


def validate_app_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Application payload must be a JSON object")
    name = validate_name(payload.get("name"))
    app_id = payload.get("id", payload.get("slug"))
    if app_id is None or app_id == "":
        app_id = sanitize_slug(name)
    app_id = validate_app_id(app_id)
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
        "build_config": build_config,
        "health_check_url": health_path,
    })
    description = payload.get("description", "")
    if not isinstance(description, str) or len(description) > 2000 or any(ord(char) < 32 and char not in "\t" for char in description):
        raise ValueError("Description must be a string of at most 2000 printable characters")
    result["description"] = description
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


def run_command(
    args: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    env: Optional[Dict[str, str]] = None,
    live_log_path: Optional[Path] = None,
    redact_values: Optional[Sequence[str]] = None,
) -> subprocess.CompletedProcess:
    if not args or not all(isinstance(arg, str) and "\0" not in arg for arg in args):
        raise ValueError("Subprocess arguments must be a non-empty list of strings")
    try:
        if live_log_path is not None:
            values = list(redact_values or ())
            if env:
                values.extend(env.values())
            return _run_command_with_live_log(args, cwd, timeout, env, live_log_path, values)
        result = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1, int(timeout)),
        )
        values = list(redact_values or ())
        if env:
            values.extend(env.values())
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            _redact_text(result.stdout or "", values),
            _redact_text(result.stderr or "", values),
        )
    except subprocess.TimeoutExpired as exc:
        values = list(redact_values or ())
        if env:
            values.extend(env.values())
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
    try:
        returncode = process.wait(timeout=max(1, int(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
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
) -> subprocess.CompletedProcess:
    _require_root()
    if os.name == "nt":
        raise RuntimeError("Running application commands as a separate Linux user requires Ubuntu")
    if not _account_exists(username):
        raise RuntimeError(f"Application service account {username} does not exist")
    home = str(_home_path(username.removeprefix("app-")))
    env_args = [
        "env",
        f"HOME={home}",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ]
    for key, value in (environment or {}).items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or "\0" in value:
            raise ValueError("Invalid application environment variable")
        env_args.append(f"{key}={value}")
    return run_command(
        ["runuser", "--user", username, "--", *env_args, *args],
        cwd=cwd,
        timeout=timeout,
        live_log_path=live_log_path,
        redact_values=list((environment or {}).values()),
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
) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(repo_path), *args]
    if username is None:
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            raise PermissionError("Git must run as the application's unprivileged service account")
        return run_command(command, timeout=timeout)
    return _run_as_user(username, command, timeout=timeout)


def clone_or_update_repo(
    repo_url: str,
    repo_path: Path,
    branch: str,
    username: Optional[str] = None,
    timeout: Optional[int] = None,
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
        top_level = _git(repo_path, username, ["rev-parse", "--show-toplevel"], command_timeout)
        _command_error(top_level, "Checking the source repository")
        if _resolved_path(Path(top_level.stdout.strip())) != repo_path:
            raise ValueError("Source path is not the root of its Git repository")
        status = _git(repo_path, username, ["status", "--porcelain", "--untracked-files=all"], command_timeout)
        _command_error(status, "Checking source changes")
        if status.stdout.strip():
            raise RuntimeError("Repository has local changes; refusing to update it")
        remote = _git(repo_path, username, ["remote", "get-url", "origin"], command_timeout)
        _command_error(remote, "Checking the source remote")
        if remote.stdout.strip() != repo_url:
            raise RuntimeError("Repository origin differs from the configured Git URL; refusing to change it")
        current_branch = _git(repo_path, username, ["symbolic-ref", "--short", "HEAD"], command_timeout)
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
        )
        _command_error(fetch, "Fetching Git updates")
        merge = _git(repo_path, username, ["merge", "--ff-only", f"refs/remotes/origin/{branch}"], command_timeout)
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
            result = run_command(clone_args, timeout=command_timeout)
        else:
            result = _run_as_user(username, clone_args, timeout=command_timeout)
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


def app_health_url(app: Dict[str, Any]) -> str:
    health = app.get("health_check_url") or "/"
    if not isinstance(health, str) or not health.startswith("/") or health.startswith("//"):
        raise ValueError("Health-check URL must be a local path")
    port = app.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_START <= port <= PORT_END or port in RESERVED_PORTS:
        raise ValueError("Application has an invalid port")
    return f"http://127.0.0.1:{port}{health}"


def check_app_health(app: Dict[str, Any], timeout_seconds: int = 30) -> bool:
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


def app_metadata(app: Dict[str, Any]) -> Dict[str, Any]:
    metadata = dict(app)
    source = Path(app.get("source_path", ""))
    if source.is_dir():
        metadata["latest_commit"] = git_output(source, "rev-parse", "HEAD", username=app.get("service_user")) or metadata.get("latest_commit")
    metadata["public_url"] = app.get("public_url") or f"https://stuserver.online/{app['id']}/"
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
        }:
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
    for app in registry["apps"]:
        if not isinstance(app, dict):
            raise ValueError("Each application registry entry must be an object")
        app_id = validate_app_id(app.get("id"))
        if app_id in seen_ids:
            raise ValueError(f"Duplicate application ID in registry: {app_id}")
        seen_ids.add(app_id)
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
        port = normalized.get("port")
        if port is None:
            port = _select_port(registry, config)
        else:
            _clear_expired_port_reservations(registry)
            reservations = registry.get("port_reservations", {})
            if (
                any(app.get("port") == port for app in registry["apps"])
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
            "description": normalized.get("description", ""),
            "health_check_url": normalized["health_check_url"],
            "build_config": normalized["build_config"],
            "port": port,
            "public_url": f"https://stuserver.online/{normalized['id']}/",
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
        deployments.extend(dict(item, app_id=app["id"]) for item in app.get("deployments", []))
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
    )


def _plugin_remote_commit() -> str:
    result = _plugin_git(
        [
            "git",
            "ls-remote",
            "--exit-code",
            PLUGIN_REPOSITORY_URL,
            f"refs/heads/{PLUGIN_REPOSITORY_BRANCH}",
        ]
    )
    _command_error(result, "Checking for App Manager updates")
    match = re.fullmatch(
        rf"([0-9a-f]{{40,64}})\trefs/heads/{re.escape(PLUGIN_REPOSITORY_BRANCH)}\s*",
        result.stdout or "",
    )
    if not match:
        raise RuntimeError("The App Manager update source returned an invalid Git revision")
    return match.group(1)


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
    latest_commit = _plugin_remote_commit()
    return {
        "ok": True,
        "available": current_commit != latest_commit,
        "current_commit": current_commit,
        "latest_commit": latest_commit,
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
                PLUGIN_REPOSITORY_BRANCH,
                "--single-branch",
                "--no-tags",
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
        _seal_plugin_update_checkout(checkout, update_root)
        installer = checkout / "scripts" / "install.sh"
        if installer.is_symlink() or not installer.is_file():
            raise RuntimeError("The App Manager update does not contain a regular installer")
        install_result = run_command(
            ["bash", str(installer)],
            cwd=checkout,
            timeout=300,
            env={"HOME": "/root", "PATH": _plugin_update_environment()["PATH"]},
        )
        _command_error(install_result, "Reinstalling the App Manager plugin")
        return {"ok": True, "updated": True, "commit": match.group(0)}
    finally:
        shutil.rmtree(update_root)


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
    }
    update = dict(payload)
    for key in update:
        if key not in allowed_keys:
            raise ValueError(f"Unsupported configuration key: {key!r}")
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
        update["port_range"] = {"start": start, "end": end, "reserved": sorted(set(RESERVED_PORTS) | set(port_range.get("reserved", [])))}
    merged = config.copy()
    merged.update(update)
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
        port = normalized.get("port", app.get("port"))
        if port != app.get("port"):
            if any(other.get("id") != app_id and other.get("port") == port for other in registry["apps"]):
                raise ValueError(f"Port {port} is already in use")
            if port in get_listening_ports() or not _socket_port_available(port):
                raise ValueError(f"Port {port} is already in use")
        with _registry_transaction() as registry2:
            app = _find_app(registry2, app_id)
            for key in ("name", "repo_url", "branch", "framework", "description", "health_check_url", "build_config", "port", "public_url"):
                if key in normalized:
                    app[key] = normalized[key]
            app["slug"] = app_id
            app["updated_at"] = now_iso()
            return {"ok": True, "app": app}


def _service_path(app: Dict[str, Any]) -> Path:
    validate_app_id(app["id"])
    return Path("/etc/systemd/system") / f"cockpit-app-{app['id']}.service"


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
    writable_paths = (
        paths["home"],
        paths["config"],
        paths["logs"],
    )
    read_write_paths = " ".join(_systemd_quote(str(path)) for path in writable_paths)
    exec_start = " ".join(_systemd_quote(part) for part in start_command)
    node_environment = (
        f"Environment={_systemd_quote('NODE_ENV=production')}\n"
        if app.get("framework") == "nextjs"
        else ""
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
        f"Environment={_systemd_quote('APP_BASE_PATH=/' + app['id'])}\n"
        f"Environment={_systemd_quote('APP_CONFIG_DIR=' + str(paths['config']))}\n"
        f"Environment={_systemd_quote('HOME=' + str(paths['home']))}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
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
) -> str:
    app_id = validate_app_id(app_id)
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
        app_root = _app_paths(app_id, config)["current"] / "app"
        route_location = (
            f"# Managed by Cockpit App Manager: {app_id}\n"
            f"location ^~ /{app_id}/ {{\n"
            f"    alias {_nginx_quote(str(app_root) + os.sep)};\n"
            "    index index.html;\n"
            f"    try_files $uri $uri/ /{app_id}/index.html;\n"
            "}\n"
        )
    else:
        proxy_redirect = (
            "    proxy_redirect off;\n"
            if framework == "nextjs"
            else f"    proxy_redirect / /{app_id}/;\n"
        )
        route_location = (
            f"# Managed by Cockpit App Manager: {app_id}\n"
            f"location ^~ /{app_id}/ {{\n"
            f"    proxy_pass http://127.0.0.1:{port}{proxy_uri};\n"
            "    proxy_http_version 1.1;\n"
            "    proxy_set_header Host $host;\n"
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "    proxy_set_header X-Forwarded-Proto $scheme;\n"
            "    proxy_set_header X-Real-IP $remote_addr;\n"
            f"    proxy_set_header X-Forwarded-Prefix /{app_id};\n"
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


def _write_service(
    app: Dict[str, Any],
    release_dir: Path,
    start_command: Sequence[str],
    config: Dict[str, Any],
    working_directory: Optional[Path] = None,
) -> Path:
    service = _render_service(app, start_command, config, working_directory)
    service_path = _service_path(app)
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
    app.setdefault("deployments", []).append(record)
    app["updated_at"] = now_iso()


def _prune_releases(app: Dict[str, Any], keep_count: int) -> None:
    production = Path(app["production_path"])
    releases_dir = production / "releases"
    if not releases_dir.exists():
        return
    releases = sorted((path for path in releases_dir.iterdir() if path.is_dir()), key=lambda path: path.name)
    current = (production / "current").resolve(strict=False)
    retained = set(releases[-max(0, keep_count):]) if keep_count else set()
    retained.add(current)
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
    public_releases = {
        _resolved_path(Path(item["release"]))
        for item in app.get("deployments", [])
        if isinstance(item, dict)
        and item.get("framework") == "static"
        and isinstance(item.get("release"), str)
    }
    if app.get("framework") == "static":
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


def deploy_app(app_id: str) -> Dict[str, Any]:
    _require_root()
    config = get_config()
    with _app_lock(app_id):
        release_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        started_at = now_iso()
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app_snapshot = dict(app)
        record = {
            "id": release_id,
            "name": app_snapshot["name"],
            "date": started_at,
            "commit": None,
            "release": None,
            "release_available": False,
            "framework": app_snapshot.get("framework", "auto"),
            "build_status": "pending",
            "status": "deploying",
            "error": None,
        }
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app["status"] = "deploying"
            _record_deployment(app, record)

        paths: Optional[Dict[str, Path]] = None
        release_dir: Optional[Path] = None
        current_release: Optional[Path] = None
        service_path: Optional[Path] = None
        previous_service_content: Optional[bytes] = None
        deployment_log: Optional[Path] = None
        unit_written = False
        activation_attempted = False
        try:
            deployment_log = _deployment_log_path(app_id, release_id, create=True)
            _append_deployment_log(deployment_log, "Deployment started")
            username = ensure_app_user(app_id)
            paths = _setup_app_directories(app_snapshot, username, config)
            current_release = paths["current"].resolve(strict=False) if paths["current"].exists() or paths["current"].is_symlink() else None
            source_path = paths["source"]
            _append_deployment_log(deployment_log, "Updating the application source repository")
            clone_or_update_repo(
                app_snapshot["repo_url"],
                source_path,
                app_snapshot["branch"],
                username,
                int(config["command_timeout_seconds"]),
            )
            framework = app_snapshot.get("framework", "auto")
            if framework == "auto":
                framework = detect_framework(source_path, app_snapshot.get("build_config", {}))
            app_snapshot["framework"] = framework
            _append_deployment_log(deployment_log, f"Detected framework: {framework}")
            adapter = get_adapter(framework)
            release_dir = paths["releases"] / release_id
            _prepare_release_directory(release_dir, username)
            record.update({
                "release": str(release_dir),
                "release_available": True,
                "framework": framework,
                "build_status": "running",
            })
            commit = git_output(source_path, "rev-parse", "HEAD", username=username)
            record["commit"] = commit or None
            service_path = _service_path(app_snapshot)
            previous_service_content = service_path.read_bytes() if service_path.exists() else None
            build_environment = _environment_values_app(app_id)
            build_result = adapter.build(
                app_snapshot,
                source_path,
                release_dir,
                runner=lambda args, cwd, timeout: _run_as_user(
                    username,
                    args,
                    cwd,
                    timeout,
                    environment=build_environment,
                    live_log_path=deployment_log,
                ),
                timeout=int(config["build_timeout_seconds"]),
            )
            _append_deployment_log(deployment_log, "Build completed; activating the release")
            _, release_group = _account_ids(username)
            _seal_release(release_dir, release_group, public=framework == "static")
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
            _write_service(app_snapshot, release_dir, command, config, working_directory)
            unit_written = True
            _systemctl("daemon-reload")
            _retire_legacy_root_service(app_snapshot)
            _atomic_symlink(release_dir, paths["current"])
            activation_attempted = True
            _systemctl("enable", service_path.name)
            _systemctl("restart", service_path.name)
            _append_deployment_log(deployment_log, "Waiting for the application health check")
            if not _wait_for_health(app_snapshot, int(config["health_timeout_seconds"])):
                raise RuntimeError("Application did not pass its health check before the timeout")
            _append_deployment_log(deployment_log, "Deployment completed successfully")
        except Exception as exc:
            if deployment_log is not None:
                with contextlib.suppress(OSError):
                    _append_deployment_log(deployment_log, f"Deployment failed: {exc}")
            rollback_error = None
            try:
                if unit_written and service_path is not None:
                    _restore_service_file(service_path, previous_service_content)
                    _systemctl("daemon-reload")
                if activation_attempted and paths is not None and current_release is not None and current_release.is_dir():
                    _atomic_symlink(current_release, paths["current"])
                    if previous_service_content is not None and service_path is not None:
                        if app_snapshot.get("status") == "running":
                            _systemctl("restart", service_path.name)
                            if not _wait_for_health(app_snapshot, int(config["health_timeout_seconds"])):
                                raise RuntimeError("The previous release did not recover its health check")
                        else:
                            _systemctl("stop", service_path.name)
                    else:
                        if service_path is not None:
                            _systemctl("stop", service_path.name)
                elif activation_attempted and previous_service_content is None and service_path is not None:
                    _systemctl("stop", service_path.name)
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
            if activation_attempted and current_release is None and service_path is not None:
                with contextlib.suppress(Exception):
                    _systemctl("disable", "--now", service_path.name)
            record.update({
                "build_status": "failed",
                "status": "failed",
                "release_available": bool(release_dir and release_dir.is_dir()),
                "error": str(exc),
            })
            if rollback_error:
                record["rollback_error"] = rollback_error
            if release_dir is not None:
                with contextlib.suppress(OSError):
                    if release_dir.is_dir() and not release_dir.is_symlink():
                        shutil.rmtree(release_dir)
                        record["release_available"] = False
            with _registry_transaction() as registry:
                app = _find_app(registry, app_id)
                stored_record = next(item for item in app["deployments"] if item.get("id") == release_id)
                stored_record.update(record)
                app["updated_at"] = now_iso()
                app["status"] = (
                    "failed" if rollback_error
                    else app_snapshot.get("status", "stopped") if current_release
                    else "failed"
                )
            raise

        record.update({"build_status": "success", "status": "deployed"})
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app["framework"] = framework
            app["status"] = "running"
            app["last_deploy"] = record["date"]
            app["latest_commit"] = commit or app.get("latest_commit")
            stored_record = next(item for item in app["deployments"] if item.get("id") == release_id)
            stored_record.update(record)
            app["updated_at"] = now_iso()
            _prune_releases(app, int(config["release_retention"]))
        app_snapshot.update({"status": "running", "framework": framework, "latest_commit": commit or app_snapshot.get("latest_commit")})
        return {"ok": True, "app": app_snapshot, "build": build_result, "deployment": record}


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
        service_path = _service_path(app_snapshot)
        if not service_path.exists():
            raise RuntimeError(f"Application service {service_path.name} is not installed; deploy it first")
        _systemctl(action_name, service_path.name)
        if action_name in {"start", "restart"} and not _wait_for_health(app_snapshot, int(config["health_timeout_seconds"])):
            raise RuntimeError(f"Application did not pass its health check after {action_name}")
        status = "stopped" if action_name == "stop" else "running"
        with _registry_transaction() as registry:
            app = _find_app(registry, app_id)
            app["status"] = status
            app["updated_at"] = now_iso()
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


def deployment_logs_app(app_id: str) -> Dict[str, Any]:
    app_id = validate_app_id(app_id)
    app = _find_app(get_registry(), app_id)
    latest = next(
        (
            item for item in reversed(app.get("deployments", []))
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and re.fullmatch(r"\d{14}-[a-f0-9]{8}", item["id"])
        ),
        None,
    )
    if latest is None:
        return {"ok": True, "app_id": app_id, "active": False, "status": None, "logs": ""}
    log_path = _deployment_log_path(app_id, latest["id"])
    return {
        "ok": True,
        "app_id": app_id,
        "deployment_id": latest["id"],
        "active": latest.get("status") == "deploying",
        "status": latest.get("status"),
        "logs": _tail_log(log_path, 1000, max_bytes=1024 * 1024),
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


def health_check_app(app_id: str) -> Dict[str, Any]:
    app = _find_app(get_registry(), app_id)
    url = app_health_url(app)
    healthy = check_app_health(app, int(get_config()["health_timeout_seconds"]))
    return {"ok": True, "app_id": app_id, "healthy": healthy, "url": url}


def remove_app(app_id: str) -> Dict[str, Any]:
    validate_app_id(app_id)
    _require_root()
    with _app_lock(app_id):
        app = _find_app(get_registry(), app_id)
        service_path = _service_path(app)
        _retire_legacy_root_service(app)
        if service_path.exists():
            _systemctl("disable", "--now", service_path.name)
            service_path.unlink()
            _systemctl("daemon-reload")
        with _registry_transaction() as registry:
            _find_app(registry, app_id)
            registry["apps"] = [item for item in registry["apps"] if item.get("id") != app_id]
        return {"ok": True, "removed": app_id, "data_retained": True}


def open_app(app_id: str) -> Dict[str, Any]:
    app = _find_app(get_registry(), app_id)
    return {"ok": True, "app_id": app_id, "url": app.get("public_url", f"https://stuserver.online/{app_id}/")}


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
    add = commands.add_parser("add")
    add.add_argument("payload")
    edit = commands.add_parser("edit")
    edit.add_argument("app_id")
    edit.add_argument("payload")
    for name in ("deploy", "pull", "start", "stop", "restart", "remove", "health", "health-check", "check-health", "open"):
        command = commands.add_parser(name)
        command.add_argument("app_id")
    logs = commands.add_parser("logs")
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
        return deployment_logs_app(args.app_id)
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
