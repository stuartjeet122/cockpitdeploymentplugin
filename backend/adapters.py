from __future__ import annotations

import json
import os
import shlex
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
        raise ValueError(f"Package-manager lockfile must be a regular file: {lockfile}")
    if manager == "npm":
        try:
            lock_data = json.loads(lockfile.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid npm lockfile {lockfile}: {exc}") from exc
        if not isinstance(lock_data, dict):
            raise ValueError(f"Invalid npm lockfile {lockfile}: expected a JSON object")
        lockfile_version = lock_data.get("lockfileVersion")
        if (
            isinstance(lockfile_version, bool)
            or not isinstance(lockfile_version, int)
            or lockfile_version < 1
        ):
            raise ValueError(
                f"Unsupported npm lockfile {lockfile}: lockfileVersion must be a number greater than or equal to 1"
            )
    return manager, lockfile


def _package_manager_prefix(manager: str, app_dir: Path, package_dir: Path) -> List[str]:
    if app_dir == package_dir:
        return []
    if manager == "npm":
        return ["--prefix", str(package_dir)]
    if manager == "pnpm":
        return ["--dir", str(package_dir)]
    if manager == "yarn":
        return ["--cwd", str(package_dir)]
    if manager == "bun":
        return ["--cwd", str(package_dir)]
    raise ValueError(f"Unsupported JavaScript package manager: {manager}")


def _js_install_command(
    manager: str,
    lockfile: Optional[Path],
    app_dir: Optional[Path] = None,
    package_dir: Optional[Path] = None,
) -> List[str]:
    has_lockfile = lockfile is not None
    prefix = (
        _package_manager_prefix(manager, app_dir, package_dir)
        if app_dir is not None and package_dir is not None
        else []
    )
    if manager == "npm":
        operation = "ci" if has_lockfile else "install"
        return ["npm", *prefix, operation, "--include=dev", "--no-fund", "--no-audit"]
    if manager == "pnpm":
        return ["pnpm", *prefix, "install", "--frozen-lockfile", "--prod=false"]
    if manager == "yarn":
        return ["yarn", *prefix, "install", "--frozen-lockfile", "--production=false"]
    if manager == "bun":
        return ["bun", *prefix, "install", "--frozen-lockfile"]
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


def _js_production_install_command(
    manager: str,
    app_dir: Optional[Path] = None,
    package_dir: Optional[Path] = None,
) -> List[str]:
    prefix = (
        _package_manager_prefix(manager, app_dir, package_dir)
        if app_dir is not None and package_dir is not None
        else []
    )
    if manager == "npm":
        return ["npm", *prefix, "prune", "--omit=dev", "--no-fund", "--no-audit"]
    if manager == "pnpm":
        return ["pnpm", *prefix, "install", "--frozen-lockfile", "--prod=true"]
    if manager == "yarn":
        return ["yarn", *prefix, "install", "--frozen-lockfile", "--production=true"]
    if manager == "bun":
        return ["bun", *prefix, "install", "--frozen-lockfile", "--production"]
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


def _npm_lockfile_inconsistent(output: str) -> bool:
    message = output.lower()
    return any(
        marker in message
        for marker in (
            "package.json and package-lock.json",
            "package.json and npm-shrinkwrap.json",
            "must be in sync",
            "is not in sync",
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
    package_dir: Optional[Path] = None,
) -> None:
    package_dir = package_dir or app_dir
    install_command = _js_install_command(manager, lockfile, app_dir, package_dir)
    description = f"Installing {framework} dependencies with {manager}"
    if runner is None:
        raise RuntimeError("Build commands require an unprivileged application command runner")
    try:
        result = runner(install_command, app_dir, timeout)
    except Exception as exc:
        raise RuntimeError(
            f"{description} could not run\n"
            f"Application working directory: {app_dir}\n"
            f"Package manager: {manager}\n"
            f"Lockfile: {lockfile if lockfile is not None else 'none'}\n"
            f"Command: {shlex.join(install_command)}\n"
            f"Error: {exc}"
        ) from exc
    if result.returncode == 0:
        return

    output = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part)
    message = (
        f"{description} failed\n"
        f"Application working directory: {app_dir}\n"
        f"Package manager: {manager}\n"
        f"Lockfile: {lockfile if lockfile is not None else 'none'}\n"
        f"Command: {shlex.join(install_command)}\n"
        f"Exit code: {result.returncode}"
    )
    if manager == "npm" and lockfile is not None and _npm_lockfile_inconsistent(output):
        message += (
            f"\nThe npm lockfile {lockfile} is inconsistent with package.json. "
            "Update the lockfile in the source repository and deploy again."
        )
    if result.stdout:
        message += f"\nstdout:\n{result.stdout.rstrip()}"
    if result.stderr:
        message += f"\nstderr:\n{result.stderr.rstrip()}"
    raise RuntimeError(message)


def _install_js_production_dependencies(
    manager: str,
    app_dir: Path,
    runner: Optional[CommandRunner],
    timeout: int,
    framework: str,
    package_dir: Optional[Path] = None,
) -> None:
    _run_build(
        runner,
        _js_production_install_command(manager, app_dir, package_dir or app_dir),
        app_dir,
        timeout,
        f"Preparing {framework} production dependencies with {manager}",
    )


_NEXT_CONFIG_NAMES = ("next.config.js", "next.config.mjs", "next.config.ts")
_IGNORED_PACKAGE_DIRS = {".git", "node_modules", ".next", ".venv", "__pycache__"}


def _package_has_next(package_path: Path) -> bool:
    if package_path.is_symlink() or not package_path.is_file():
        return False
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JavaScript package manifest {package_path}: {exc}") from exc
    if not isinstance(package, dict):
        raise ValueError(f"Invalid JavaScript package manifest {package_path}: expected a JSON object")
    dependencies = package.get("dependencies", {})
    dev_dependencies = package.get("devDependencies", {})
    if not isinstance(dependencies, dict) or not isinstance(dev_dependencies, dict):
        raise ValueError(f"Invalid dependency sections in JavaScript package manifest {package_path}")
    return "next" in dependencies or "next" in dev_dependencies


def _next_config_present(directory: Path) -> bool:
    return any((directory / name).is_file() for name in _NEXT_CONFIG_NAMES)


def _validate_app_directory(repo_path: Path, configured: Any) -> Path:
    if not isinstance(configured, str) or not configured or "\\" in configured or "\0" in configured:
        raise ValueError("build_config.app_directory must be a non-empty relative POSIX path")
    parts = configured.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("build_config.app_directory must not contain empty, '.' or '..' path components")
    directory = repo_path.joinpath(*parts)
    current = repo_path
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Next.js application directory must not contain symlinks: {current}")
    if not directory.is_dir():
        raise ValueError(f"Configured Next.js application directory does not exist: {directory}")
    package_path = directory / "package.json"
    if package_path.is_symlink() or not package_path.is_file():
        raise ValueError(f"Configured Next.js application directory has no regular package.json: {package_path}")
    return directory


def _next_project_candidates(repo_path: Path) -> List[Path]:
    candidates: List[Path] = []
    for current, directories, filenames in os.walk(repo_path, topdown=True, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if name not in _IGNORED_PACKAGE_DIRS and not (Path(current) / name).is_symlink()
        )
        current_path = Path(current)
        package_path = current_path / "package.json"
        has_config = _next_config_present(current_path)
        try:
            has_next_package = package_path.is_file() and _package_has_next(package_path)
        except ValueError:
            if current_path == repo_path or has_config:
                raise
            continue
        if has_next_package or has_config:
            candidates.append(current_path)
    return candidates


def _workspace_patterns(package: Dict[str, Any]) -> List[str]:
    workspaces = package.get("workspaces", [])
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages", [])
    if not isinstance(workspaces, list) or not all(isinstance(item, str) for item in workspaces):
        return []
    return workspaces


def _is_npm_workspace(package_dir: Path, workspace_root: Path) -> bool:
    package_path = workspace_root / "package.json"
    if not package_path.is_file() or package_path.is_symlink():
        return False
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read workspace package manifest {package_path}: {exc}") from exc
    if not isinstance(package, dict):
        return False
    relative = package_dir.relative_to(workspace_root).as_posix()
    included = False
    for pattern in _workspace_patterns(package):
        if pattern.startswith("!"):
            matches = workspace_root.glob(pattern[1:])
            if any(path.resolve() == package_dir.resolve() for path in matches):
                return False
        elif not Path(pattern).is_absolute() and ".." not in Path(pattern).parts:
            if any(path.resolve() == package_dir.resolve() for path in workspace_root.glob(pattern)):
                included = True
    return included


def _next_project_directories(repo_path: Path, build_config: Dict[str, Any]) -> tuple[Path, Path]:
    configured = build_config.get("app_directory")
    if configured is not None:
        app_dir = _validate_app_directory(repo_path, configured)
        if not _package_has_next(app_dir / "package.json") and not _next_config_present(app_dir):
            raise ValueError(f"Configured Next.js application directory does not declare Next.js: {app_dir}")
    else:
        candidates = _next_project_candidates(repo_path)
        if not candidates:
            raise FileNotFoundError(f"No Next.js package.json was found under {repo_path}")
        nested_candidates = [path for path in candidates if path != repo_path]
        root_is_workspace_only = (
            repo_path in candidates
            and bool(nested_candidates)
            and not _next_config_present(repo_path)
            and bool(_workspace_patterns(
                json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
            ))
        )
        if repo_path in candidates and not root_is_workspace_only:
            app_dir = repo_path
        elif len(nested_candidates) == 1:
            app_dir = nested_candidates[0]
        elif not nested_candidates and repo_path in candidates:
            app_dir = repo_path
        else:
            paths = ", ".join(str(path.relative_to(repo_path)) for path in nested_candidates)
            raise ValueError(
                f"Multiple Next.js applications were found ({paths}); set build_config.app_directory explicitly"
            )
        package_path = app_dir / "package.json"
        if package_path.is_symlink() or not package_path.is_file():
            raise FileNotFoundError(f"package.json not found for Next.js application in {app_dir}")

    workspace_root = app_dir
    for ancestor in app_dir.parents:
        try:
            ancestor.relative_to(repo_path)
        except ValueError:
            break
        if _is_npm_workspace(app_dir, ancestor):
            workspace_root = ancestor
            break
    return app_dir, workspace_root


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
    def detect(repo_path: Path, build_config: Optional[Dict[str, Any]] = None) -> bool:
        try:
            _next_project_directories(repo_path, build_config or {})
        except FileNotFoundError:
            return _next_config_present(repo_path)
        return True

    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        build_config = app.get("build_config", {})
        if not isinstance(build_config, dict):
            raise ValueError("build_config must be a JSON object")
        source_app_dir, source_package_dir = _next_project_directories(repo_path, build_config)
        app_relative_path = source_app_dir.relative_to(repo_path)
        package_relative_path = source_package_dir.relative_to(repo_path)
        app_dir = _copy_source(repo_path, release_dir)
        project_dir = app_dir / app_relative_path
        package_dir = app_dir / package_relative_path
        package = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
        dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        if "next" not in dependencies:
            raise ValueError(f"Next.js was selected but is not declared in {project_dir / 'package.json'}")
        if "next" not in package.get("dependencies", {}):
            raise ValueError(f"Next.js must be listed in dependencies in {project_dir / 'package.json'} to run in production")
        scripts = package.get("scripts", {})
        if not isinstance(scripts, dict) or not scripts.get("build"):
            raise ValueError(f"Next.js package.json must define a build script: {project_dir / 'package.json'}")
        manager, lockfile = _detect_js_package_manager(package_dir)
        _install_js_dependencies(manager, lockfile, project_dir, runner, timeout, "Next.js", package_dir)
        _run_build(
            runner,
            _js_build_command(manager),
            project_dir,
            timeout,
            "Building Next.js application",
        )
        _install_js_production_dependencies(manager, project_dir, runner, timeout, "Next.js", package_dir)
        start = _next_start_command(manager, app["port"])
        start = _configured_start(app, start)
        return {
            "status": "success",
            "release_dir": str(release_dir),
            "start_command": start,
            "working_directory": str(project_dir),
        }


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


def detect_framework(repo_path: Path, build_config: Optional[Dict[str, Any]] = None) -> str:
    if NextJSAdapter.detect(repo_path, build_config):
        return NextJSAdapter.name
    for adapter_cls in (GoAdapter, PythonAdapter, NodeAdapter, StaticAdapter):
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
