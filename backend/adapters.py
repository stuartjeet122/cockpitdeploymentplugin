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
        output = []
        if result.stdout:
            output.append(f"stdout:\n{result.stdout.rstrip()}")
        if result.stderr:
            output.append(f"stderr:\n{result.stderr.rstrip()}")
        detail = "\n".join(output)
        raise RuntimeError(f"{description} failed (exit {result.returncode})" + (f"\n{detail}" if detail else ""))


def _detect_js_package_manager(project_dir: Path) -> tuple[str, Optional[Path]]:
    lockfiles = {
        "npm": ("npm-shrinkwrap.json", "package-lock.json"),
        "pnpm": ("pnpm-lock.yaml",),
        "yarn": ("yarn.lock",),
        "bun": ("bun.lock", "bun.lockb"),
    }
    found = {
        manager: next(
            (
                project_dir / name
                for name in names
                if (project_dir / name).exists() or (project_dir / name).is_symlink()
            ),
            None,
        )
        for manager, names in lockfiles.items()
    }
    found = {manager: lockfile for manager, lockfile in found.items() if lockfile is not None}
    if len(found) > 1:
        names = ", ".join(sorted(path.name for path in found.values()))
        raise ValueError(f"Conflicting JavaScript package-manager lockfiles found: {names}")
    if not found:
        return "npm", None

    manager, lockfile = next(iter(found.items()))
    if lockfile.is_symlink() or not lockfile.is_file():
        raise ValueError(f"Package-manager lockfile must be a regular file: {lockfile.name}")
    if manager == "npm":
        try:
            lock_data = json.loads(lockfile.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid npm lockfile {lockfile.name}: {exc}") from exc
        if not isinstance(lock_data, dict):
            raise ValueError(f"Invalid npm lockfile {lockfile.name}: expected a JSON object")
    return manager, lockfile


def _js_install_command(manager: str, lockfile: Optional[Path]) -> List[str]:
    has_lockfile = lockfile is not None
    if manager == "npm":
        operation = "ci" if has_lockfile else "install"
        return ["npm", operation, "--include=dev", "--no-fund", "--no-audit"]
    if manager == "pnpm":
        return ["pnpm", "install", "--frozen-lockfile", "--prod=false"]
    if manager == "yarn":
        return ["yarn", "install", "--frozen-lockfile", "--production=false"]
    if manager == "bun":
        return ["bun", "install", "--frozen-lockfile"]
    raise ValueError(f"Unsupported JavaScript package manager: {manager}")


def _js_build_command(manager: str) -> List[str]:
    if manager == "npm":
        return ["npm", "run", "build"]
    if manager == "pnpm":
        return ["pnpm", "run", "build"]
    if manager == "yarn":
        return ["yarn", "run", "build"]
    if manager == "bun":
        return ["bun", "run", "build"]
    raise ValueError(f"Unsupported JavaScript package manager: {manager}")


def _js_production_install_command(manager: str) -> List[str]:
    if manager == "npm":
        return ["npm", "prune", "--omit=dev", "--no-fund", "--no-audit"]
    if manager == "pnpm":
        return ["pnpm", "install", "--frozen-lockfile", "--prod=true"]
    if manager == "yarn":
        return ["yarn", "install", "--frozen-lockfile", "--production=true"]
    if manager == "bun":
        return ["bun", "install", "--frozen-lockfile", "--production"]
    raise ValueError(f"Unsupported JavaScript package manager: {manager}")


def _next_start_command(manager: str, port: Any) -> List[str]:
    next_command = ["next", "start", "--hostname", "127.0.0.1", "--port", str(port)]
    if manager == "npm":
        return ["npm", "exec", "--", *next_command]
    if manager == "pnpm":
        return ["pnpm", "exec", *next_command]
    if manager == "yarn":
        return ["yarn", "run", *next_command]
    if manager == "bun":
        return ["bun", "run", *next_command]
    raise ValueError(f"Unsupported JavaScript package manager: {manager}")


def _npm_lockfile_inconsistent(error: RuntimeError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "eusage",
            "package.json and package-lock.json",
            "package.json and npm-shrinkwrap.json",
            "must be in sync",
            "from lock file",
        )
    )


def _install_js_dependencies(
    manager: str,
    lockfile: Optional[Path],
    app_dir: Path,
    runner: Optional[CommandRunner],
    timeout: int,
    framework: str,
) -> None:
    install_command = _js_install_command(manager, lockfile)
    description = f"Installing {framework} dependencies with {manager}"
    try:
        _run_build(runner, install_command, app_dir, timeout, description)
    except RuntimeError as exc:
        if manager == "npm" and install_command[1] == "ci" and _npm_lockfile_inconsistent(exc):
            raise RuntimeError(
                f"npm ci failed because {lockfile.name} is inconsistent with package.json. "
                "Update and commit the lockfile in the source repository, then deploy again. "
                "The source repository was not modified.\n"
                f"{exc}"
            ) from exc
        raise


def _install_js_production_dependencies(
    manager: str,
    app_dir: Path,
    runner: Optional[CommandRunner],
    timeout: int,
    framework: str,
) -> None:
    _run_build(
        runner,
        _js_production_install_command(manager),
        app_dir,
        timeout,
        f"Preparing {framework} production dependencies with {manager}",
    )


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
        if "next" not in package.get("dependencies", {}):
            raise ValueError("Next.js must be listed in package.json dependencies to run in production")
        scripts = package.get("scripts", {})
        if not isinstance(scripts, dict) or not scripts.get("build"):
            raise ValueError("Next.js package.json must define a build script")
        manager, lockfile = _detect_js_package_manager(app_dir)
        _install_js_dependencies(manager, lockfile, app_dir, runner, timeout, "Next.js")
        _run_build(
            runner,
            _js_build_command(manager),
            app_dir,
            timeout,
            "Building Next.js application",
        )
        _install_js_production_dependencies(manager, app_dir, runner, timeout, "Next.js")
        start = _next_start_command(manager, app["port"])
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
