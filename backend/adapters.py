from __future__ import annotations

import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


CommandRunner = Callable[[Sequence[str], Path, int], subprocess.CompletedProcess]


def _copy_source(repo_path: Path, release_dir: Path) -> Path:
    app_dir = release_dir / "app"
    shutil.copytree(
        repo_path,
        app_dir,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__", ".pytest_cache"),
    )
    return app_dir


def _run_build(
    runner: Optional[CommandRunner],
    command: Sequence[str],
    cwd: Path,
    timeout: int,
    description: str,
) -> None:
    if runner is None:
        raise RuntimeError("Build commands require an unprivileged application command runner")
    try:
        result = runner(command, cwd, timeout)
    except Exception as exc:
        raise RuntimeError(f"{description} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-4000:]
        raise RuntimeError(f"{description} failed (exit {result.returncode})" + (f": {detail}" if detail else ""))


def _configured_start(app: Dict[str, Any], default: List[str]) -> List[str]:
    configured = app.get("build_config", {}).get("start_command")
    if configured is None:
        return default
    if not isinstance(configured, list) or not configured or not all(isinstance(item, str) and item for item in configured):
        raise ValueError("build_config.start_command must be a non-empty argument array")
    return configured


class FrameworkAdapter(ABC):
    name = "base"

    @abstractmethod
    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class NextJSAdapter(FrameworkAdapter):
    name = "nextjs"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        if any((repo_path / name).is_file() for name in ("next.config.js", "next.config.mjs", "next.config.ts")):
            return True
        package_path = repo_path / "package.json"
        if not package_path.is_file():
            return False
        package = json.loads(package_path.read_text(encoding="utf-8"))
        dependencies = {
            **package.get("dependencies", {}),
            **package.get("devDependencies", {}),
        }
        return "next" in dependencies

    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        package_path = repo_path / "package.json"
        if not package_path.is_file():
            raise FileNotFoundError("package.json not found for Next.js application")
        app_dir = _copy_source(repo_path, release_dir)
        package = json.loads((app_dir / "package.json").read_text(encoding="utf-8"))
        dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        if "next" not in dependencies:
            raise ValueError("Next.js was selected but is not declared in package.json")
        installer = "ci" if (app_dir / "package-lock.json").exists() or (app_dir / "npm-shrinkwrap.json").exists() else "install"
        _run_build(runner, ["npm", installer, "--no-fund", "--no-audit"], app_dir, timeout, "Installing Next.js dependencies")
        _run_build(runner, ["npm", "run", "build", "--if-present"], app_dir, timeout, "Building Next.js application")
        start = [
            str(app_dir / "node_modules" / ".bin" / "next"),
            "start",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(app["port"]),
        ]
        start = _configured_start(app, start)
        return {"status": "success", "release_dir": str(release_dir), "start_command": start}


class GoAdapter(FrameworkAdapter):
    name = "go"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "go.mod").is_file()

    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        if not (repo_path / "go.mod").is_file():
            raise FileNotFoundError("go.mod not found for Go application")
        app_dir = _copy_source(repo_path, release_dir)
        binary_path = release_dir / "server"
        _run_build(runner, ["go", "build", "-o", str(binary_path), "."], app_dir, timeout, "Building Go application")
        start = _configured_start(app, [str(binary_path)])
        return {"status": "success", "release_dir": str(release_dir), "start_command": start}


class NodeAdapter(FrameworkAdapter):
    name = "node"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "package.json").is_file()

    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        package_path = repo_path / "package.json"
        if not package_path.is_file():
            raise FileNotFoundError("package.json not found for Node.js application")
        package = json.loads(package_path.read_text(encoding="utf-8"))
        app_dir = _copy_source(repo_path, release_dir)
        installer = "ci" if (app_dir / "package-lock.json").exists() or (app_dir / "npm-shrinkwrap.json").exists() else "install"
        _run_build(runner, ["npm", installer, "--no-fund", "--no-audit"], app_dir, timeout, "Installing Node.js dependencies")
        if package.get("scripts", {}).get("build"):
            _run_build(runner, ["npm", "run", "build"], app_dir, timeout, "Building Node.js application")
        start = _configured_start(
            app,
            ["npm", "run", "start", "--", "--host", "127.0.0.1", "--port", str(app["port"])],
        )
        return {"status": "success", "release_dir": str(release_dir), "start_command": start}


class PythonAdapter(FrameworkAdapter):
    name = "python"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "requirements.txt").is_file() or (repo_path / "pyproject.toml").is_file()

    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        app_dir = _copy_source(repo_path, release_dir)
        _run_build(runner, ["python3", "-m", "venv", ".venv"], app_dir, timeout, "Creating Python virtual environment")
        python = str(app_dir / ".venv" / "bin" / "python")
        if (app_dir / "requirements.txt").is_file():
            _run_build(
                runner,
                [python, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
                app_dir,
                timeout,
                "Installing Python dependencies",
            )
        elif (app_dir / "pyproject.toml").is_file():
            _run_build(
                runner,
                [python, "-m", "pip", "install", "--disable-pip-version-check", "."],
                app_dir,
                timeout,
                "Installing Python application",
            )
        _run_build(
            runner,
            [python, "-m", "pip", "install", "--disable-pip-version-check", "gunicorn"],
            app_dir,
            timeout,
            "Installing Gunicorn",
        )
        start = _configured_start(
            app,
            [str(app_dir / ".venv" / "bin" / "gunicorn"), "--bind", f"127.0.0.1:{app['port']}", "app:app"],
        )
        return {"status": "success", "release_dir": str(release_dir), "start_command": start}


class StaticAdapter(FrameworkAdapter):
    name = "static"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "index.html").is_file()

    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        if not (repo_path / "index.html").is_file():
            raise FileNotFoundError("index.html not found for static application")
        app_dir = _copy_source(repo_path, release_dir)
        start = _configured_start(
            app,
            ["python3", "-m", "http.server", str(app["port"]), "--bind", "127.0.0.1", "--directory", str(app_dir)],
        )
        return {"status": "success", "release_dir": str(release_dir), "start_command": start}


def detect_framework(repo_path: Path) -> str:
    for adapter_cls in (NextJSAdapter, GoAdapter, PythonAdapter, NodeAdapter, StaticAdapter):
        if adapter_cls.detect(repo_path):
            return adapter_cls.name
    raise ValueError("Could not detect a supported framework in the Git repository")


def get_adapter(name: str) -> FrameworkAdapter:
    registry = {
        "nextjs": NextJSAdapter,
        "go": GoAdapter,
        "node": NodeAdapter,
        "python": PythonAdapter,
        "static": StaticAdapter,
    }
    try:
        return registry[name]()
    except KeyError as exc:
        raise ValueError(f"Unsupported framework: {name}") from exc
