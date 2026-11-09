#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .adapters import detect_framework, get_adapter
except ImportError:  # pragma: no cover - direct execution fallback
    from adapters import detect_framework, get_adapter

APP_NAME = "app-manager"
DEFAULT_CONFIG_PATH = "/var/lib/cockpit/app-manager/config.json"
DEFAULT_REGISTRY_PATH = "/var/lib/cockpit/app-manager/apps.json"
DEFAULT_STATE_ROOT = "/var/lib/cockpit/app-manager"
DEFAULT_PRODUCTION_ROOT = "/opt/webapps"
DEFAULT_SOURCE_ROOT = "/var/lib/cockpit/app-manager/sources"
ALLOWED_SOURCE_DIRS = ["/opt", "/home/sjeetoo", "/srv"]
PORT_START = 8100
PORT_END = 8999
RESERVED_PORTS = {8080}


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_runtime_dirs() -> Dict[str, str]:
    source_root = Path(DEFAULT_SOURCE_ROOT)
    production_root = Path(DEFAULT_PRODUCTION_ROOT)
    source_root.mkdir(parents=True, exist_ok=True)
    production_root.mkdir(parents=True, exist_ok=True)
    return {"source_root": str(source_root), "production_root": str(production_root)}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        path.write_text(json.dumps(default, indent=2), encoding="utf-8")
    with path.open("r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError:
            return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def get_config() -> Dict[str, Any]:
    config_path = Path(DEFAULT_CONFIG_PATH)
    default = {
        "port_range": {"start": PORT_START, "end": PORT_END, "reserved": sorted(RESERVED_PORTS)},
        "source_root": DEFAULT_SOURCE_ROOT,
        "production_root": DEFAULT_PRODUCTION_ROOT,
        "registry_path": DEFAULT_REGISTRY_PATH,
        "allowed_source_dirs": ALLOWED_SOURCE_DIRS,
        "default_branch": "main",
        "release_retention": 5,
        "build_timeout_seconds": 900,
        "health_timeout_seconds": 30,
    }
    return load_json(config_path, default)


def get_registry() -> Dict[str, Any]:
    registry_path = Path(get_config().get("registry_path", DEFAULT_REGISTRY_PATH))
    return load_json(registry_path, {"apps": []})


def save_registry(registry: Dict[str, Any]) -> None:
    registry_path = Path(get_config().get("registry_path", DEFAULT_REGISTRY_PATH))
    write_json(registry_path, registry)


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "app"


def validate_git_url(value: str) -> bool:
    return bool(value) and ("github.com" in value or "git@" in value or value.startswith("https://") or value.startswith("ssh://") or value.startswith("git://"))


def validate_app_payload(payload: Dict[str, Any]) -> None:
    if not payload.get("name"):
        raise ValueError("Website name is required")
    if not payload.get("repo_url"):
        raise ValueError("Git repository URL is required")
    if not validate_git_url(payload["repo_url"]):
        raise ValueError("Repository URL must be a valid Git remote")
    if not payload.get("slug"):
        payload["slug"] = sanitize_slug(payload["name"])
    slug = sanitize_slug(payload["slug"])
    payload["slug"] = slug
    if payload.get("framework") == "auto":
        payload["framework"] = "auto"

    if payload.get("source_directory"):
        safe_dir = payload["source_directory"]
        if not any(safe_dir.startswith(prefix) for prefix in ALLOWED_SOURCE_DIRS):
            raise ValueError(f"Source directory must be within: {', '.join(ALLOWED_SOURCE_DIRS)}")


def get_listening_ports() -> set:
    ports = set()
    try:
        result = subprocess.run(["ss", "-lnt"], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 5 and parts[3].endswith(":"):
                    continue
                if len(parts) >= 4:
                    local = parts[3]
                    if ":" in local:
                        try:
                            ports.add(int(local.rsplit(":", 1)[1]))
                        except ValueError:
                            pass
    except FileNotFoundError:
        pass
    return ports


def pick_port() -> int:
    registry = get_registry()
    used = {int(app.get("port", 0)) for app in registry.get("apps", []) if app.get("port")}
    used |= get_listening_ports() | RESERVED_PORTS
    start = int(get_config().get("port_range", {}).get("start", PORT_START))
    end = int(get_config().get("port_range", {}).get("end", PORT_END))
    for port in range(start, end + 1):
        if port not in used:
            return port
    raise RuntimeError("No available ports remaining in the configured range")


def clone_or_update_repo(repo_url: str, repo_path: Path, branch: str) -> Dict[str, Any]:
    if repo_path.exists():
        try:
            subprocess.run(["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True, text=True)
            fetch = subprocess.run(["git", "-C", str(repo_path), "fetch", "--all", "--tags"], check=False, capture_output=True, text=True)
            if fetch.returncode != 0:
                raise RuntimeError(fetch.stderr.strip() or "Failed to fetch repository updates")
            status = subprocess.run(["git", "-C", str(repo_path), "status", "--porcelain"], capture_output=True, text=True, check=False)
            if status.stdout.strip():
                raise RuntimeError("Repository contains uncommitted changes; refusing to pull over local modifications")
            branch_result = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=False)
            if branch_result.returncode == 0 and branch_result.stdout.strip() != branch:
                subprocess.run(["git", "-C", str(repo_path), "checkout", branch], check=False, capture_output=True, text=True)
            pull = subprocess.run(["git", "-C", str(repo_path), "pull", "--ff-only", "origin", branch], capture_output=True, text=True, check=False)
            if pull.returncode != 0:
                raise RuntimeError(pull.stderr.strip() or "Failed to pull updates")
            return {"status": "updated"}
        except RuntimeError:
            raise
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(["git", "clone", "--branch", branch, repo_url, str(repo_path)], capture_output=True, text=True, check=False)
    if clone.returncode != 0:
        raise RuntimeError(clone.stderr.strip() or "Failed to clone repository")
    return {"status": "cloned"}


def git_output(repo_path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def run_command(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False)


def app_health_url(app: Dict[str, Any]) -> str:
    health = app.get("health_check_url") or "/health"
    normalized = health if health.startswith("http") else f"http://127.0.0.1:{app.get('port', 8080)}{health if health.startswith('/') else '/' + health}"
    return normalized


def check_app_health(app: Dict[str, Any], timeout_seconds: int = 30) -> bool:
    try:
        import urllib.request

        request = urllib.request.Request(app_health_url(app), method="GET")
        with urllib.request.urlopen(request, timeout=max(1, timeout_seconds)) as response:
            return 200 <= getattr(response, "status", 200) < 500
    except Exception:
        return False


def prune_releases(app: Dict[str, Any], keep_count: int) -> None:
    production_root = Path(app["production_path"])
    releases = sorted((production_root / "releases").glob("release-*"), key=lambda path: path.name)
    while len(releases) > max(0, int(keep_count)):
        rel = releases.pop(0)
        if rel.exists():
            shutil.rmtree(rel, ignore_errors=True)


def app_metadata(app: Dict[str, Any]) -> Dict[str, Any]:
    repo_path = Path(app.get("source_path") or app.get("source_dir") or str(Path(DEFAULT_SOURCE_ROOT) / app["slug"]))
    metadata = dict(app)
    if repo_path.exists():
        metadata["latest_commit"] = git_output(repo_path, "rev-parse", "HEAD") or metadata.get("latest_commit")
        metadata["branch"] = git_output(repo_path, "rev-parse", "--abbrev-ref", "HEAD") or app.get("branch", "main")
        metadata["status"] = "running" if app.get("status") == "running" else app.get("status", "stopped")
        metadata["public_url"] = app.get("public_url") or f"https://stuserver.online/{app['slug']}/"
    return metadata


def add_app(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_runtime_dirs()
    validate_app_payload(payload)
    registry = get_registry()
    slug = sanitize_slug(payload["slug"])
    if any(app.get("slug") == slug for app in registry.get("apps", [])):
        raise ValueError(f"Application '{slug}' is already registered")

    port = payload.get("port") or pick_port()
    if payload.get("framework") == "auto" or not payload.get("framework"):
        source_dir = Path(payload.get("source_directory") or str(Path(DEFAULT_SOURCE_ROOT) / slug))
        if source_dir.exists() and source_dir.is_dir():
            framework = detect_framework(source_dir)
        else:
            framework = "static"
        payload["framework"] = framework

    app_record = {
        "id": slug,
        "name": payload["name"],
        "slug": slug,
        "repo_url": payload["repo_url"],
        "branch": payload.get("branch") or get_config().get("default_branch", "main"),
        "framework": payload.get("framework", "auto"),
        "description": payload.get("description", ""),
        "health_check_url": payload.get("health_check_url", "/health"),
        "build_config": payload.get("build_config") or {},
        "port": port,
        "public_url": f"https://stuserver.online/{slug}/",
        "source_path": str(Path(DEFAULT_SOURCE_ROOT) / slug),
        "production_path": str(Path(DEFAULT_PRODUCTION_ROOT) / slug),
        "status": "stopped",
        "last_deploy": None,
        "latest_commit": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }

    registry.setdefault("apps", []).append(app_record)
    save_registry(registry)
    return {"ok": True, "app": app_record}


def list_apps() -> Dict[str, Any]:
    registry = get_registry()
    apps = [app_metadata(app) for app in registry.get("apps", [])]
    return {"apps": apps}


def stats_for_apps(apps: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(apps)
    running = sum(1 for app in apps if app.get("status") == "running")
    stopped = sum(1 for app in apps if app.get("status") == "stopped")
    failed = sum(1 for app in apps if app.get("status") == "failed")
    ports = sorted({app.get("port") for app in apps if app.get("port")})
    return {
        "total_apps": total,
        "running_apps": running,
        "stopped_apps": stopped,
        "failed_deployments": failed,
        "available_ports": [port for port in range(PORT_START, PORT_END + 1) if port not in set(ports) and port not in RESERVED_PORTS],
        "recent_activity": [{"name": app.get("name"), "action": "Deployment", "when": app.get("last_deploy") or "never"} for app in apps[:5]],
    }


def overview() -> Dict[str, Any]:
    apps = list_apps()["apps"]
    return stats_for_apps(apps)


def get_deployments() -> Dict[str, Any]:
    registry = get_registry()
    deployments = []
    for app in registry.get("apps", []):
        if app.get("deployments"):
            deployments.extend(app["deployments"])
    return {"deployments": deployments}


def get_settings() -> Dict[str, Any]:
    config = get_config()
    return {"config": config}


def clone_repo_if_needed(app: Dict[str, Any]) -> Path:
    source_path = Path(app["source_path"])
    if not source_path.exists():
        subprocess.run(["git", "clone", "--branch", app["branch"], app["repo_url"], str(source_path)], check=False, capture_output=True, text=True)
    return source_path


def deploy_app(app_id: str) -> Dict[str, Any]:
    registry = get_registry()
    app = next((item for item in registry["apps"] if item["id"] == app_id), None)
    if app is None:
        raise ValueError(f"App '{app_id}' not found")

    source_path = clone_repo_if_needed(app)
    clone_or_update_repo(app["repo_url"], source_path, app["branch"])

    production_root = Path(app["production_path"])
    production_root.mkdir(parents=True, exist_ok=True)
    release_dir = production_root / "releases"
    release_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(release_dir.glob("release-*"), key=lambda path: path.name)
    next_index = len(existing) + 1
    target = release_dir / f"release-{next_index:03d}"
    target.mkdir(parents=True, exist_ok=True)

    current_link = production_root / "current"
    previous_release = current_link.resolve(strict=False) if current_link.exists() or current_link.is_symlink() else None
    adapter = get_adapter(app.get("framework", "static"))
    build_result = adapter.build(app, source_path, target)
    if build_result.get("status") != "success":
        raise RuntimeError("Build failed")

    service_name = f"{app['slug']}.service"
    service_path = Path("/etc/systemd/system") / service_name
    systemd_start = build_result.get("start_command") or f"python3 -m http.server {app['port']} --directory {target}"
    service_path.write_text(
        f"[Unit]\nDescription={app['name']}\nAfter=network.target\n\n[Service]\nType=simple\nWorkingDirectory={target}\nExecStart={systemd_start}\nRestart=on-failure\nRestartSec=5\nUser=root\nGroup=root\nEnvironment=PORT={app['port']}\n\n[Install]\nWantedBy=multi-user.target\n",
        encoding="utf-8",
    )

    subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True, text=True)
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(target)

    try:
        subprocess.run(["systemctl", "restart", service_name], check=False, capture_output=True, text=True)
        if not check_app_health(app):
            raise RuntimeError("Health check failed after deployment")
    except Exception as exc:
        if previous_release is not None and previous_release.exists():
            if current_link.exists() or current_link.is_symlink():
                current_link.unlink()
            current_link.symlink_to(previous_release)
            subprocess.run(["systemctl", "restart", service_name], check=False, capture_output=True, text=True)
        app["status"] = "failed"
        app["updated_at"] = now_iso()
        app.setdefault("deployments", []).append(
            {"name": app["name"], "date": now_iso(), "commit": git_output(source_path, "rev-parse", "HEAD"), "build_status": "failed", "status": "failed", "error": str(exc)}
        )
        save_registry(registry)
        raise RuntimeError(str(exc))

    app["status"] = "running"
    app["last_deploy"] = now_iso()
    app["updated_at"] = now_iso()
    app["latest_commit"] = git_output(source_path, "rev-parse", "HEAD")
    app.setdefault("deployments", []).append(
        {
            "name": app["name"],
            "date": app["last_deploy"],
            "commit": app["latest_commit"],
            "build_status": "success",
            "status": "deployed",
            "error": None,
        }
    )
    keep_count = int(get_config().get("release_retention", 5))
    prune_releases(app, keep_count)
    save_registry(registry)
    return {"ok": True, "app": app, "build": build_result}


def remove_app(app_id: str) -> Dict[str, Any]:
    registry = get_registry()
    apps = registry.get("apps", [])
    filtered = [app for app in apps if app["id"] != app_id]
    if len(filtered) == len(apps):
        raise ValueError(f"App '{app_id}' not found")
    registry["apps"] = filtered
    save_registry(registry)
    return {"ok": True, "removed": app_id}


def action(app_id: str, action_name: str) -> Dict[str, Any]:
    registry = get_registry()
    app = next((item for item in registry["apps"] if item["id"] == app_id), None)
    if app is None:
        raise ValueError(f"App '{app_id}' not found")

    service_name = f"{app['slug']}.service"
    if action_name == "deploy":
        return deploy_app(app_id)
    if action_name == "pull":
        return {"ok": True, "result": clone_or_update_repo(app["repo_url"], Path(app["source_path"]), app["branch"])}
    if action_name == "start":
        subprocess.run(["systemctl", "start", service_name], check=False, capture_output=True, text=True)
        app["status"] = "running"
        save_registry(registry)
        return {"ok": True}
    if action_name == "stop":
        subprocess.run(["systemctl", "stop", service_name], check=False, capture_output=True, text=True)
        app["status"] = "stopped"
        save_registry(registry)
        return {"ok": True}
    if action_name == "restart":
        subprocess.run(["systemctl", "restart", service_name], check=False, capture_output=True, text=True)
        app["status"] = "running"
        save_registry(registry)
        return {"ok": True}
    if action_name == "remove":
        return remove_app(app_id)
    if action_name == "logs":
        proc = subprocess.run(["journalctl", "-u", service_name, "-n", "50", "--no-pager"], capture_output=True, text=True, check=False)
        return {"ok": proc.returncode == 0, "logs": proc.stdout}
    return {"ok": False, "error": f"Action '{action_name}' is not supported"}


def parse_payload(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Cockpit App Manager backend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list")
    subparsers.add_parser("overview")
    subparsers.add_parser("deployments")
    subparsers.add_parser("settings")

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("payload")

    action_parser = subparsers.add_parser("action")
    action_parser.add_argument("action_name")
    action_parser.add_argument("app_id")

    remove_parser = subparsers.add_parser("remove")
    remove_parser.add_argument("app_id")

    args = parser.parse_args()

    try:
        ensure_runtime_dirs()
        if args.command == "list":
            sys.stdout.write(json.dumps(list_apps()))
        elif args.command == "overview":
            sys.stdout.write(json.dumps(overview()))
        elif args.command == "deployments":
            sys.stdout.write(json.dumps(get_deployments()))
        elif args.command == "settings":
            sys.stdout.write(json.dumps(get_settings()))
        elif args.command == "add":
            payload = parse_payload(args.payload)
            result = add_app(payload)
            sys.stdout.write(json.dumps(result))
        elif args.command == "action":
            result = action(args.app_id, args.action_name)
            sys.stdout.write(json.dumps(result))
        elif args.command == "remove":
            result = remove_app(args.app_id)
            sys.stdout.write(json.dumps(result))
        else:
            raise ValueError(f"Unknown command '{args.command}'")
        return 0
    except Exception as exc:  # pragma: no cover - CLI error reporting
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
