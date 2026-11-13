from __future__ import annotations

import ast
import configparser
import contextlib
import hashlib
import json
import os
import re
import shlex
import stat
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit


class CommandRunner(Protocol):
    def __call__(self, command: Sequence[str], cwd: Path, timeout: int, *, preserve_stdout: bool = False) -> subprocess.CompletedProcess: ...


def _copy_source(repo_path: Path, release_dir: Path, group_id: Optional[int] = None, route: Optional[str] = None,
                 excluded_roots: Sequence[str] = ()) -> Path:
    app_dir = release_dir / "app"
    default_ignore = shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__", ".pytest_cache")
    def ignore(directory: str, names: List[str]) -> set:
        excluded = set(default_ignore(directory, names))
        if Path(directory) == repo_path:
            excluded.update(name for name in names if name in excluded_roots or excluded_roots and name.startswith(".env"))
        return excluded
    shutil.copytree(
        repo_path,
        app_dir,
        symlinks=True,
        ignore=ignore,
    )
    _validate_copy_symlinks(app_dir)
    if route:
        _prefix_static_html_assets(app_dir, route)
    if group_id is not None:
        _prepare_build_copy_permissions(app_dir, group_id)
    return app_dir


def _validate_copy_symlinks(app_dir: Path) -> None:
    root = app_dir.resolve(strict=True)
    for current, directories, filenames in os.walk(app_dir, topdown=True, followlinks=False):
        for name in directories + filenames:
            path = Path(current) / name
            if not path.is_symlink():
                continue
            try:
                path.resolve(strict=False).relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Application source symlink escapes the isolated release copy: {path}") from exc


def _prepare_build_copy_permissions(app_dir: Path, group_id: int) -> None:
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 0:
        raise ValueError("Build release group ID is invalid")
    for current, directories, filenames in os.walk(app_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        paths = [current_path]
        paths.extend(current_path / name for name in directories + filenames)
        for path in paths:
            if path.is_symlink():
                continue
            metadata = path.stat(follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            if path.is_dir():
                permissions = (mode & 0o700) | 0o070
            else:
                group_permissions = 0o060 | (0o010 if mode & 0o100 else 0)
                permissions = (mode & 0o700) | group_permissions
            os.chown(str(path), -1, group_id, follow_symlinks=False)
            os.chmod(str(path), permissions, follow_symlinks=False)


def _file_diagnostics(path: Path, *, group_id: Optional[int] = None) -> str:
    resolved_path = path.resolve(strict=False)
    if not path.exists() and not path.is_symlink():
        return f"{path} (resolved: {resolved_path}; exists: no; is_file: no)"
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        return f"{path} (resolved: {resolved_path}; stat error: {exc})"
    regular_file = stat.S_ISREG(metadata.st_mode)
    readable = False
    if regular_file:
        try:
            with path.open("rb") as handle:
                handle.read(1)
            readable = True
        except OSError:
            readable = False
    app_group_readable = (
        group_id is None or metadata.st_gid == group_id and bool(metadata.st_mode & stat.S_IRGRP)
    )
    return (
        f"{path} (resolved: {resolved_path}; exists: yes; is_file: {regular_file}; "
        f"readable: {readable}; app_group_readable: {app_group_readable}; "
        f"size: {metadata.st_size}; mode: {stat.S_IMODE(metadata.st_mode):04o}; "
        f"uid: {metadata.st_uid}; gid: {metadata.st_gid})"
    )


def _readable_regular_file(path: Path, description: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{description} must be a regular file: {_file_diagnostics(path)}")
    try:
        with path.open("rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ValueError(f"{description} is not readable: {path}: {exc}") from exc


def _set_build_file_permissions(path: Path, group_id: int) -> None:
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 0:
        raise ValueError("Build release group ID is invalid")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Generated build file is not a regular file: {path}")
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    if os.name == "nt":
        os.chmod(str(path), (mode & 0o700) | 0o040)
    else:
        os.chown(str(path), -1, group_id, follow_symlinks=False)
        os.chmod(str(path), (mode & 0o700) | 0o040, follow_symlinks=False)


def _run_build(
    runner: Optional[CommandRunner],
    command: Sequence[str],
    cwd: Path,
    timeout: int,
    description: str,
    *,
    preserve_stdout: bool = False,
) -> subprocess.CompletedProcess:
    if runner is None:
        raise RuntimeError("Build commands require an application command runner")
    try:
        result = runner(command, cwd, timeout, preserve_stdout=True) if preserve_stdout else runner(command, cwd, timeout)
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
    return result


def _detect_js_package_manager(project_dir: Path) -> tuple[str, Optional[Path]]:
    lockfiles = {
        "npm": ("npm-shrinkwrap.json", "package-lock.json"),
        "pnpm": ("pnpm-lock.yaml",),
        "yarn": ("yarn.lock",),
        "bun": ("bun.lock", "bun.lockb"),
    }
    found = {}
    for manager, names in lockfiles.items():
        existing = [
            project_dir / name
            for name in names
            if (project_dir / name).exists() or (project_dir / name).is_symlink()
        ]
        if existing:
            found[manager] = existing[0]
    if len(found) > 1:
        names = ", ".join(sorted(path.name for path in found.values()))
        raise ValueError(f"Conflicting JavaScript package-manager lockfiles found: {names}")
    if not found:
        return "npm", None

    manager, lockfile = next(iter(found.items()))
    lock_data_bytes = _readable_regular_file(lockfile, "Package-manager lockfile")
    if manager == "npm":
        try:
            lock_data = json.loads(lock_data_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid npm lockfile {lockfile}: {exc}") from exc
        if not isinstance(lock_data, dict):
            raise ValueError(f"Invalid npm lockfile {lockfile}: expected a JSON object")
        lockfile_version = lock_data.get("lockfileVersion")
        if isinstance(lockfile_version, bool) or not isinstance(lockfile_version, int) or lockfile_version not in {1, 2, 3}:
            raise ValueError(
                f"Unsupported npm lockfile {lockfile}: lockfileVersion must be one of 1, 2, or 3"
            )
        required_section = "dependencies" if lockfile_version == 1 else "packages"
        if not isinstance(lock_data.get(required_section), dict):
            raise ValueError(
                f"Invalid npm lockfile {lockfile}: lockfileVersion {lockfile_version} requires "
                f"a {required_section} object"
            )
    return manager, lockfile


def _npm_lockfile_version(lockfile: Optional[Path]) -> Optional[int]:
    if lockfile is None or lockfile.name not in {"package-lock.json", "npm-shrinkwrap.json"}:
        return None
    try:
        lock_data = json.loads(_readable_regular_file(lockfile, "npm lockfile").decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid npm lockfile {lockfile}: {exc}") from exc
    version = lock_data.get("lockfileVersion") if isinstance(lock_data, dict) else None
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    return None


def _convert_to_pnpm(
    package_dir: Path,
    source_manager: str,
    lockfile: Optional[Path],
    runner: Optional[CommandRunner],
    timeout: int,
    log: Optional[Callable[[str], None]] = None,
    group_id: Optional[int] = None,
) -> Optional[Path]:
    package_path = package_dir / "package.json"
    package = json.loads(_readable_regular_file(package_path, "JavaScript package.json").decode("utf-8"))
    if not isinstance(package, dict):
        raise ValueError("JavaScript package.json must be an object")
    package.pop("packageManager", None)
    if isinstance(package.get("devEngines"), dict):
        package["devEngines"].pop("packageManager", None)
    package_path.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    npmrc = package_dir / ".npmrc"
    contents = _readable_regular_file(npmrc, "Project npm configuration").decode("utf-8") if npmrc.exists() or npmrc.is_symlink() else ""
    settings = configparser.ConfigParser(interpolation=None, delimiters=("=",), strict=False)
    settings.read_string("[npm]\n" + contents)
    for key, value in settings.items("npm"):
        if key in {"_auth", "_authtoken", "username", "_password"}:
            raise ValueError("Registry credentials must be scoped to their registry URL before pnpm conversion")
        if key == "registry" and value.rstrip("/") not in {"https://registry.npmjs.org", "http://127.0.0.1:4873"}:
            raise ValueError("A custom default registry cannot be converted to the public shared cache; configure private registries by package scope")
    npmrc.write_bytes((contents.rstrip() + "\n" + "\n".join([
        "registry=http://127.0.0.1:4873/",
        "enable-pre-post-scripts=true",
        "manage-package-manager-versions=false",
        "package-manager-strict=false",
        "package-import-method=copy",
        "verify-store-integrity=true",
        "strict-dep-builds=true",
    ]) + "\n").encode("utf-8"))
    if group_id is not None:
        _set_build_file_permissions(npmrc, group_id)
    workspace_path = package_dir / "pnpm-workspace.yaml"
    patterns = _workspace_patterns(package)
    if patterns and not workspace_path.exists():
        workspace_path.write_text(json.dumps({"packages": patterns}, indent=2) + "\n", encoding="utf-8")
        if group_id is not None:
            _set_build_file_permissions(workspace_path, group_id)
    if log is not None:
        log(f"Converting release dependencies from {source_manager} to pnpm; shared registry cache: http://127.0.0.1:4873/")
    if source_manager == "pnpm":
        return lockfile
    if lockfile is not None and source_manager in {"npm", "yarn"}:
        if lockfile.name == "npm-shrinkwrap.json":
            (package_dir / "package-lock.json").unlink(missing_ok=True)
        _run_build(runner, ["pnpm", "import"], package_dir, timeout, "Importing dependency lockfile into pnpm")
        imported = package_dir / "pnpm-lock.yaml"
        _readable_regular_file(imported, "Imported pnpm lockfile")
        return imported
    if log is not None:
        log("No pnpm-importable lockfile: resolving package.json ranges into a new release-only pnpm lockfile")
    return None


def _log_npm_preflight(
    app_dir: Path,
    package_dir: Path,
    lockfile: Optional[Path],
    group_id: Optional[int],
    log: Optional[Callable[[str], None]],
) -> None:
    if log is None:
        return
    package_path = package_dir / "package.json"
    lockfile_description = "none"
    if lockfile is not None:
        lockfile_description = (
            f"{lockfile}; lockfileVersion={_npm_lockfile_version(lockfile)}; "
            f"{_file_diagnostics(lockfile, group_id=group_id)}"
        )
    lockfile_candidates = (
        package_dir / "npm-shrinkwrap.json",
        package_dir / "package-lock.json",
    )
    log(
        "Next.js npm preflight: "
        f"working_directory={app_dir}; resolved_working_directory={app_dir.resolve(strict=True)}; "
        f"package_directory={package_dir}; resolved_package_directory={package_dir.resolve(strict=True)}; "
        f"package_json={_file_diagnostics(package_path, group_id=group_id)}; "
        f"npm_shrinkwrap={_file_diagnostics(lockfile_candidates[0], group_id=group_id)}; "
        f"package_lock={_file_diagnostics(lockfile_candidates[1], group_id=group_id)}; "
        f"selected_lockfile={lockfile_description}"
    )


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
        return ["pnpm", *prefix, "install", "--frozen-lockfile" if has_lockfile else "--no-frozen-lockfile", "--prod=false"]
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
    log: Optional[Callable[[str], None]] = None,
) -> None:
    package_dir = package_dir or app_dir
    install_command = _js_install_command(manager, lockfile, app_dir, package_dir)
    description = f"Installing {framework} dependencies with {manager}"
    if log is not None:
        log(
            f"{description}: working_directory={app_dir}; "
            f"resolved_working_directory={app_dir.resolve(strict=True)}; "
            f"package_manager={manager}; lockfile={lockfile if lockfile is not None else 'none'}; "
            f"command={shlex.join(install_command)}"
        )
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
    log: Optional[Callable[[str], None]] = None,
) -> None:
    command = _js_production_install_command(manager, app_dir, package_dir or app_dir)
    if log is not None:
        log(
            f"Preparing {framework} production dependencies: "
            f"working_directory={app_dir}; package_manager={manager}; command={shlex.join(command)}"
        )
    _run_build(
        runner,
        command,
        app_dir,
        timeout,
        f"Preparing {framework} production dependencies with {manager}",
    )


_NEXT_CONFIG_NAMES = (
    "next.config.js",
    "next.config.mjs",
    "next.config.cjs",
    "next.config.ts",
    "next.config.mts",
    "next.config.cts",
)
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


def _set_next_config_permissions(path: Path, group_id: int) -> None:
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 0:
        raise ValueError("Build release group ID is invalid")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Next.js config is not a regular file: {path}")
    metadata = path.stat(follow_symlinks=False)
    mode = stat.S_IMODE(metadata.st_mode)
    permissions = (mode & 0o700) | 0o060 | (0o010 if mode & 0o100 else 0)
    if os.name == "nt":
        os.chmod(str(path), permissions)
    else:
        os.chown(str(path), -1, group_id, follow_symlinks=False)
        os.chmod(str(path), permissions, follow_symlinks=False)


def _write_next_config(path: Path, contents: str, group_id: Optional[int]) -> None:
    path.write_text(contents, encoding="utf-8")
    if group_id is not None:
        _set_next_config_permissions(path, group_id)


def _next_route_config_paths(project_dir: Path) -> List[Path]:
    config_paths = [project_dir / name for name in _NEXT_CONFIG_NAMES]
    config_paths.extend(project_dir.glob("next.config.app-manager-original.*"))
    return config_paths


def _prepare_next_config_permissions(project_dir: Path, group_id: Optional[int]) -> None:
    if group_id is None:
        return
    for path in _next_route_config_paths(project_dir):
        if path.exists() or path.is_symlink():
            _set_next_config_permissions(path, group_id)


def _configure_next_public_route(project_dir: Path, group_id: Optional[int] = None) -> None:
    config_path = next(
        (project_dir / name for name in _NEXT_CONFIG_NAMES if (project_dir / name).is_file()),
        None,
    )
    if config_path is None:
        _write_next_config(
            project_dir / "next.config.mjs",
            'export default { basePath: process.env.APP_BASE_PATH || "", trailingSlash: true };\n',
            group_id,
        )
        return

    original_path = config_path.with_name(
        f"next.config.app-manager-original{config_path.suffix}"
    )
    if original_path.exists() or original_path.is_symlink():
        raise ValueError(f"Reserved Next.js config backup path already exists: {original_path}")
    config_path.rename(original_path)

    import_path = "./" + original_path.name
    if config_path.suffix in {".ts", ".mts", ".cts"}:
        wrapper = (
            "import { createRequire } from 'node:module';\n"
            "import { dirname } from 'node:path';\n"
            "import { fileURLToPath } from 'node:url';\n"
            "const require = createRequire(import.meta.url);\n"
            "const nextRequire = createRequire(require.resolve('next'));\n"
            "const { transpileConfig } = nextRequire('next/dist/build/next-config-ts/transpile-config');\n"
            f"const originalPath = fileURLToPath(new URL({json.dumps(import_path)}, import.meta.url));\n"
            "export default async function appManagerConfig(phase, context) {\n"
            "  const importedConfig = await transpileConfig({ nextConfigPath: originalPath, dir: dirname(originalPath) });\n"
            "  const originalConfig = importedConfig.default ?? importedConfig;\n"
            "  const config = typeof originalConfig === 'function'\n"
            "    ? await originalConfig(phase, context)\n"
            "    : originalConfig;\n"
            "  return { ...config, basePath: process.env.APP_BASE_PATH || '', trailingSlash: true };\n"
            "}\n"
        )
        _write_next_config(project_dir / "next.config.mjs", wrapper, group_id)
        return

    package_path = project_dir / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read Next.js package manifest {package_path}: {exc}") from exc
    uses_esm = config_path.suffix in {".mjs", ".mts"} or (
        config_path.suffix == ".js" and package.get("type") == "module"
    )
    if uses_esm:
        wrapper = (
            f"import originalConfig from {json.dumps(import_path)};\n"
            "export default async function appManagerConfig(phase, context) {\n"
            "  const config = typeof originalConfig === 'function'\n"
            "    ? await originalConfig(phase, context)\n"
            "    : originalConfig;\n"
            "  return { ...config, basePath: process.env.APP_BASE_PATH || '', trailingSlash: true };\n"
            "}\n"
        )
    else:
        wrapper = (
            f"const importedConfig = require({json.dumps(import_path)});\n"
            "const originalConfig = importedConfig.default ?? importedConfig;\n"
            "module.exports = async function appManagerConfig(phase, context) {\n"
            "  const config = typeof originalConfig === 'function'\n"
            "    ? await originalConfig(phase, context)\n"
            "    : originalConfig;\n"
            "  return { ...config, basePath: process.env.APP_BASE_PATH || '', trailingSlash: true };\n"
            "};\n"
        )
    _write_next_config(config_path, wrapper, group_id)


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
        group_id: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError


DOCKER_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def _validate_docker_relative_path(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\0" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"{field} must be a relative path without '.', '..', or empty components")


def docker_deployment_mode(build_config: Dict[str, Any], repo_path: Optional[Path] = None) -> str:
    mode = build_config.get("docker_mode")
    if mode is not None:
        if not isinstance(mode, str) or mode not in {"dockerfile", "compose"}:
            raise ValueError("build_config.docker_mode must be 'dockerfile' or 'compose'")
        return mode
    if "dockerfile" in build_config:
        return "dockerfile"
    if "compose_file" in build_config:
        return "compose"
    if repo_path is not None and any((repo_path / filename).is_file() for filename in DOCKER_COMPOSE_FILES):
        return "compose"
    return "dockerfile"


def validate_docker_build_config(build_config: Dict[str, Any], repo_path: Optional[Path] = None) -> None:
    mode = docker_deployment_mode(build_config, repo_path)
    if mode == "compose":
        _validate_docker_relative_path(
            build_config.get("compose_file", DOCKER_COMPOSE_FILES[0]),
            "build_config.compose_file",
        )
        service = build_config.get("compose_service", "web")
        if not isinstance(service, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", service):
            raise ValueError("build_config.compose_service must be a lowercase Compose service name")
    else:
        _validate_docker_relative_path(
            build_config.get("dockerfile", "Dockerfile"),
            "build_config.dockerfile",
        )
    port = build_config.get("container_port", 3000)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("build_config.container_port must be an integer from 1 to 65535")
    app_directory = build_config.get("app_directory")
    if app_directory is not None and (
        not isinstance(app_directory, str)
        or not app_directory
        or "\\" in app_directory
        or "\0" in app_directory
        or any(part in {"", ".", ".."} for part in app_directory.split("/"))
    ):
        raise ValueError("build_config.app_directory must be a relative path without '.', '..', or empty components")
    if "start_command" in build_config:
        raise ValueError("Docker applications use their Dockerfile CMD/ENTRYPOINT or Compose command; start_command is not supported")


def _validate_compose_config(
    config: Any,
    project_dir: Path,
    web_service: str,
    project_name: str,
    allowed_env_files: Sequence[str] = (),
    allowed_web_port: Optional[Dict[str, Any]] = None,
    require_managed_security: bool = False,
) -> Dict[str, Any]:
    project_root = project_dir.resolve(strict=True)

    def resolve_project_path(value: str, root: Path = project_root) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve(strict=True)

    if not isinstance(config, dict) or not isinstance(config.get("services"), dict) or not config["services"]:
        raise ValueError("Compose configuration must define one or more services")
    services = config["services"]
    if web_service not in services:
        raise ValueError(f"Compose service {web_service!r} was not found")
    for service_name, service in services.items():
        if not isinstance(service, dict):
            raise ValueError(f"Compose service {service_name!r} must be an object")
        ports = service.get("ports", [])
        if ports:
            managed_port = (
                service_name == web_service
                and len(ports) == 1
                and isinstance(ports[0], dict)
                and allowed_web_port is not None
                and ports[0].get("host_ip") == "127.0.0.1"
                and str(ports[0].get("published")) == str(allowed_web_port["published"])
                and int(ports[0].get("target", 0)) == int(allowed_web_port["target"])
                and ports[0].get("protocol", "tcp") == "tcp"
                and ports[0].get("mode") in {None, "ingress"}
            )
            if not managed_port:
                raise ValueError(f"Compose service {service_name!r} must not publish host ports; App Manager publishes only the selected service on loopback")
        if service.get("container_name"):
            raise ValueError(f"Compose service {service_name!r} must not set container_name")
        if service.get("privileged") or service.get("devices") or service.get("device_cgroup_rules"):
            raise ValueError(f"Compose service {service_name!r} requests privileged host capabilities")
        if service.get("cap_add") and not (
            require_managed_security
            and service_name == web_service
            and allowed_web_port is not None
            and int(allowed_web_port["target"]) < 1024
            and service.get("cap_add") == ["NET_BIND_SERVICE"]
        ):
            raise ValueError(f"Compose service {service_name!r} must not add container capabilities")
        if service.get("security_opt") and not (
            require_managed_security and "no-new-privileges:true" in service.get("security_opt", [])
        ):
            raise ValueError(f"Compose service {service_name!r} must not override container security options")
        if require_managed_security and "ALL" not in service.get("cap_drop", []):
            raise ValueError(f"Compose service {service_name!r} is missing the managed capability restrictions")
        if any(service.get(key) == "host" for key in ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup")):
            raise ValueError(f"Compose service {service_name!r} must not use a host namespace")
        if service.get("network_mode") or service.get("volumes_from"):
            raise ValueError(f"Compose service {service_name!r} must not join an unmanaged network or container")
        if service.get("profiles"):
            raise ValueError(f"Compose service {service_name!r} must not require an opt-in profile")
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict) or volume.get("type") == "bind":
                raise ValueError(f"Compose service {service_name!r} must not mount host filesystem paths")
            if volume.get("type") not in {"volume", "tmpfs"}:
                raise ValueError(f"Compose service {service_name!r} uses an unsupported volume type")
        build = service.get("build")
        if build:
            if not isinstance(build, dict):
                raise ValueError(f"Compose build for service {service_name!r} must be an object")
            if any(key in build for key in ("additional_contexts", "secrets", "ssh")):
                raise ValueError(f"Compose build for service {service_name!r} requests external build access")
            context = build.get("context")
            if not isinstance(context, str):
                raise ValueError(f"Compose build for service {service_name!r} has an invalid context")
            context_path = resolve_project_path(context)
            try:
                context_path.relative_to(project_root)
            except ValueError as exc:
                raise ValueError(f"Compose build context for service {service_name!r} escapes the managed project") from exc
            dockerfile = build.get("dockerfile")
            if dockerfile:
                dockerfile_path = resolve_project_path(dockerfile, context_path)
                try:
                    dockerfile_path.relative_to(context_path)
                except ValueError as exc:
                    raise ValueError(f"Compose Dockerfile for service {service_name!r} escapes its build context") from exc
        env_files = service.get("env_file", [])
        if isinstance(env_files, (str, dict)):
            env_files = [env_files]
        for env_file in env_files:
            path = env_file.get("path") if isinstance(env_file, dict) else env_file
            if not isinstance(path, str):
                raise ValueError(f"Compose env_file for service {service_name!r} is invalid")
            env_path = resolve_project_path(path)
            if str(env_path) in allowed_env_files:
                continue
            try:
                env_path.relative_to(project_root)
            except ValueError as exc:
                raise ValueError(f"Compose env_file for service {service_name!r} escapes the managed project") from exc
    for section in ("volumes", "networks", "configs", "secrets"):
        entries = config.get(section, {})
        if not isinstance(entries, dict):
            raise ValueError(f"Compose {section} must be an object")
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                raise ValueError(f"Compose {section[:-1]} {name!r} must be an object")
            configured_name = entry.get("name")
            if entry.get("external") or (
                configured_name
                and (
                    not isinstance(configured_name, str)
                    or not configured_name.startswith(project_name + "_")
                )
            ):
                raise ValueError(f"Compose {section[:-1]} {name!r} must remain scoped to this application")
            if section == "volumes" and (entry.get("driver") or entry.get("driver_opts")):
                raise ValueError(f"Compose volume {name!r} must use Docker's app-scoped local volume storage")
            if section == "networks" and (
                (entry.get("driver") is not None and entry.get("driver") != "bridge") or entry.get("driver_opts")
            ):
                raise ValueError(f"Compose network {name!r} must use the app-scoped bridge network")
            file_path = entry.get("file")
            if file_path:
                if not isinstance(file_path, str):
                    raise ValueError(f"Compose {section[:-1]} {name!r} has an invalid file path")
                resolved_file = resolve_project_path(file_path)
                try:
                    resolved_file.relative_to(project_root)
                except ValueError as exc:
                    raise ValueError(f"Compose {section[:-1]} {name!r} file escapes the managed project") from exc
    return services


class DockerAdapter(FrameworkAdapter):
    name = "docker"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "Dockerfile").is_file() or any(
            (repo_path / filename).is_file() for filename in DOCKER_COMPOSE_FILES
        )

    def build(
        self,
        app: Dict[str, Any],
        repo_path: Path,
        release_dir: Path,
        *,
        runner: Optional[CommandRunner] = None,
        timeout: int = 900,
        group_id: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        build_config = app.get("build_config", {})
        if not isinstance(build_config, dict):
            raise ValueError("build_config must be a JSON object")
        validate_docker_build_config(build_config, repo_path)
        app_dir = _copy_source(repo_path, release_dir, group_id, app.get("route"))
        context_dir = app_dir
        configured_directory = build_config.get("app_directory")
        if configured_directory:
            current = app_dir
            for part in configured_directory.split("/"):
                current = current / part
                if current.is_symlink():
                    raise ValueError(f"Docker build context must not contain symlinks: {current}")
            if not current.is_dir():
                raise ValueError(f"Docker build context directory does not exist: {configured_directory}")
            context_dir = current
        if docker_deployment_mode(build_config, repo_path) == "compose":
            compose_file_name = build_config.get("compose_file")
            if compose_file_name is None:
                compose_file_name = next(
                    (filename for filename in DOCKER_COMPOSE_FILES if (context_dir / filename).is_file()),
                    DOCKER_COMPOSE_FILES[0],
                )
            compose_file = context_dir / compose_file_name
            current = context_dir
            for part in compose_file_name.split("/"):
                current = current / part
                if current.is_symlink():
                    raise ValueError(f"Compose file path must not contain symlinks: {current}")
            if not compose_file.is_file():
                raise FileNotFoundError(f"Compose file not found: {compose_file}")
            service_name = build_config.get("compose_service", "web")
            project = f"cockpit-app-manager-{app['id']}"
            route = app.get("route", "/").rstrip("/")
            port = build_config.get("container_port", 3000)
            tag = app["docker_tag"]
            compose_prefix = [
                "docker", "compose", "--project-name", project,
                "--project-directory", str(context_dir),
                "--env-file", app["docker_environment_file"],
                "-f", str(compose_file),
            ]
            config_command = [*compose_prefix, "config", "--format", "json"]
            config_result = _run_build(
                runner, config_command, context_dir, timeout, "Validating Docker Compose project",
                preserve_stdout=True,
            )
            try:
                compose_config = json.loads(config_result.stdout or "")
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError(f"Docker Compose returned invalid normalized configuration: {exc}") from exc
            services = _validate_compose_config(compose_config, context_dir, service_name, project)
            managed_images = {}
            for name, service in services.items():
                if service.get("build"):
                    image_service = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
                    if not image_service:
                        raise ValueError(f"Compose service name {name!r} cannot be used for a managed image tag")
                    image_service += "-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
                    managed_images[name] = f"cockpit-app-manager-{app['id']}-{image_service}:{tag}"
            limits = app.get("resource_limits", {})
            override_services = {
                name: {
                    "image": image,
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                }
                for name, image in managed_images.items()
            }
            for name in services:
                override_services.setdefault(name, {}).update({
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                })
            override_services.setdefault(service_name, {}).update({
                "ports": [{
                    "target": port,
                    "published": str(app["port"]),
                    "protocol": "tcp",
                    "host_ip": "127.0.0.1",
                }],
                "env_file": [app["docker_environment_file"]],
                "environment": {
                    "PORT": str(port),
                    "HOST": "0.0.0.0",
                    "APP_BASE_PATH": route,
                },
                "pids_limit": limits.get("tasks_max", 256),
                "deploy": {
                    "resources": {
                        "limits": {
                            "memory": f"{limits.get('memory_mb', 1024)}M",
                            "cpus": str(limits.get("cpu_percent", 100) / 100),
                            "pids": limits.get("tasks_max", 256),
                        }
                    }
                },
            })
            if port < 1024:
                override_services[service_name]["cap_add"] = ["NET_BIND_SERVICE"]
            override_file = release_dir / ".app-manager-compose.override.json"
            descriptor, temporary_name = tempfile.mkstemp(prefix=".compose-override.", dir=str(release_dir))
            temporary_file = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps({"services": override_services}, indent=2) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(str(temporary_file), str(override_file))
            except Exception:
                with contextlib.suppress(FileNotFoundError):
                    temporary_file.unlink()
                raise
            compose_files = [*compose_prefix, "-f", str(override_file)]
            normalized_command = [*compose_files, "config", "--format", "json"]
            normalized_result = _run_build(
                runner, normalized_command, context_dir, timeout, "Validating managed Docker Compose configuration",
                preserve_stdout=True,
            )
            try:
                normalized = json.loads(normalized_result.stdout or "")
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError(f"Docker Compose returned invalid managed configuration: {exc}") from exc
            _validate_compose_config(
                normalized, context_dir, service_name, project,
                allowed_env_files=[str(Path(app["docker_environment_file"]).resolve(strict=True))],
                allowed_web_port={"target": port, "published": app["port"]},
                require_managed_security=True,
            )
            build_command = [*compose_files, "build", "--pull"]
            if log is not None:
                log(f"Building Docker Compose project {project}")
            _run_build(runner, build_command, context_dir, timeout, "Building Docker Compose project")
            start = [*compose_files, "up", "--no-build", "--remove-orphans"]
            stop = [*compose_files, "down", "--timeout", "10", "--remove-orphans"]
            return {
                "status": "success",
                "release_dir": str(release_dir),
                "working_directory": str(context_dir),
                "start_command": start,
                "stop_command": stop,
                "compose_project": project,
                "images": list(managed_images.values()),
                "container_port": port,
            }
        dockerfile = context_dir / build_config.get("dockerfile", "Dockerfile")
        current = context_dir
        for part in build_config.get("dockerfile", "Dockerfile").split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Dockerfile path must not contain symlinks: {current}")
        if not dockerfile.is_file():
            raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")
        image = f"cockpit-app-manager-{app['id']}:{app['docker_tag']}"
        build_limits = app.get("resource_limits", {})
        route = app.get("route", "/").rstrip("/")
        container_port = build_config.get("container_port", 3000)
        command = [
            "docker", "build", "--pull", "--tag", image,
            "--memory", f"{build_limits.get('build_memory_mb', 2048)}m",
            "--cpu-period", "100000",
            "--cpu-quota", str(build_limits.get("build_cpu_percent", 100) * 1000),
            "--build-arg", f"APP_BASE_PATH={route}",
            "--build-arg", f"PORT={container_port}",
            "--file", str(dockerfile), str(context_dir),
        ]
        if log is not None:
            log(f"Building Docker image {image} from {dockerfile}")
        _run_build(runner, command, context_dir, timeout, "Building Docker image")
        container_name = f"cockpit-app-manager-{app['id']}-{app.get('service_slot', 'primary')}"
        limits = app.get("resource_limits", {})
        low_port_capability = ["--cap-add=NET_BIND_SERVICE"] if container_port < 1024 else []
        start = [
            "docker", "run", "--rm", "--name", container_name,
            "--publish", f"127.0.0.1:{app['port']}:{container_port}",
            "--env-file", app["docker_environment_file"],
            "--env", f"PORT={container_port}",
            "--env", "HOST=0.0.0.0",
            "--env", f"APP_BASE_PATH={route}",
            "--cap-drop=ALL",
            *low_port_capability,
            "--security-opt=no-new-privileges",
            "--pids-limit", str(limits.get("tasks_max", 256)),
            "--memory", f"{limits.get('memory_mb', 1024)}m",
            "--cpus", str(limits.get("cpu_percent", 100) / 100),
            image,
        ]
        return {
            "status": "success",
            "release_dir": str(release_dir),
            "start_command": start,
            "image": image,
            "container_port": container_port,
        }


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
        group_id: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        build_config = app.get("build_config", {})
        if not isinstance(build_config, dict):
            raise ValueError("build_config must be a JSON object")
        source_app_dir, source_package_dir = _next_project_directories(repo_path, build_config)
        app_relative_path = source_app_dir.relative_to(repo_path)
        package_relative_path = source_package_dir.relative_to(repo_path)
        source_package_path = source_app_dir / "package.json"
        source_package_contents = _readable_regular_file(source_package_path, "Next.js package.json")
        app_dir = _copy_source(repo_path, release_dir, group_id, app.get("route"))
        project_dir = app_dir / app_relative_path
        package_dir = app_dir / package_relative_path
        copied_package_path = project_dir / "package.json"
        copied_package_contents = _readable_regular_file(copied_package_path, "Copied Next.js package.json")
        if copied_package_contents != source_package_contents:
            raise RuntimeError(
                f"Next.js package.json changed while copying source: source={source_package_path}; "
                f"release={copied_package_path}"
            )
        try:
            package = json.loads(copied_package_contents.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid Next.js package.json {copied_package_path}: {exc}") from exc
        dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        if "next" not in dependencies:
            raise ValueError(f"Next.js was selected but is not declared in {project_dir / 'package.json'}")
        if "next" not in package.get("dependencies", {}):
            raise ValueError(f"Next.js must be listed in dependencies in {project_dir / 'package.json'} to run in production")
        scripts = package.get("scripts", {})
        if not isinstance(scripts, dict) or not scripts.get("build"):
            raise ValueError(f"Next.js package.json must define a build script: {project_dir / 'package.json'}")
        route = app.get("route")
        static_export = False
        if route:
            _configure_next_public_route(project_dir, group_id)
        manager, lockfile = _detect_js_package_manager(package_dir)
        if manager == "npm":
            source_lockfile = source_package_dir / lockfile.name if lockfile is not None else None
            if lockfile is not None:
                source_lockfile_contents = _readable_regular_file(source_lockfile, "Source npm lockfile")
                copied_lockfile_contents = _readable_regular_file(lockfile, "Copied npm lockfile")
                if source_lockfile_contents != copied_lockfile_contents:
                    raise RuntimeError(
                        f"npm lockfile changed while copying source: source={source_lockfile}; release={lockfile}"
                    )
            _log_npm_preflight(project_dir, package_dir, lockfile, group_id, log)
        if app.get("_managed_pnpm"):
            lockfile = _convert_to_pnpm(
                package_dir, manager, lockfile, runner, timeout, log, group_id=group_id
            )
            manager = "pnpm"
        _install_js_dependencies(
            manager,
            lockfile,
            project_dir,
            runner,
            timeout,
            "Next.js",
            package_dir,
            log,
        )
        if route:
            _prepare_next_config_permissions(project_dir, group_id)
            if log is not None and group_id is not None:
                for path in _next_route_config_paths(project_dir):
                    if path.exists() or path.is_symlink():
                        log(
                            "Next.js route config permissions: "
                            + _file_diagnostics(path, group_id=group_id)
                        )
            config_check = (
                "const load = require('next/dist/server/config').default;"
                "const { PHASE_PRODUCTION_BUILD } = require('next/constants');"
                "load(PHASE_PRODUCTION_BUILD, process.cwd()).then(config => {"
                "if (config.basePath !== process.argv[1] || config.trailingSlash !== true) {"
                "throw new Error('Next.js basePath/trailingSlash could not be configured for the public route.');"
                "}"
                "if (config.output === 'export') process.stdout.write('APP_MANAGER_NEXT_STATIC_EXPORT');"
                "}).catch(error => { console.error(error.message); process.exit(1); });"
            )
            config_result = _run_build(
                runner, ["node", "--eval", config_check, route.rstrip("/")],
                project_dir, timeout, "Checking Next.js public route configuration",
            )
            static_export = (config_result.stdout or "").strip() == "APP_MANAGER_NEXT_STATIC_EXPORT"
            _run_build(
                runner,
                ["node", str(Path(__file__).with_name("prepare_next_assets.cjs")), route.rstrip("/"),
                 "--install-hook", manager, str(timeout)],
                project_dir, timeout, "Preparing Next.js local asset/API URLs for the public route",
            )
        _run_build(
            runner,
            _js_build_command(manager),
            project_dir,
            timeout,
            "Building Next.js application",
        )
        if static_export:
            export_dir = project_dir / "out"
            if export_dir.is_symlink() or not export_dir.is_dir():
                raise RuntimeError(f"Next.js static export did not create an output directory: {export_dir}")
            try:
                export_root = export_dir.resolve(strict=True)
                export_root.relative_to(app_dir.resolve(strict=True))
            except ValueError as exc:
                raise RuntimeError("Next.js static export output escapes the isolated release copy") from exc
            for current, directories, filenames in os.walk(export_dir, followlinks=False):
                for name in directories + filenames:
                    path = Path(current) / name
                    if path.is_symlink():
                        try:
                            path.resolve(strict=False).relative_to(export_root)
                        except ValueError as exc:
                            raise RuntimeError(
                                f"Next.js static export symlink escapes its output directory: {path}"
                            ) from exc
            index_path = export_dir / "index.html"
            if index_path.is_symlink() or not index_path.is_file():
                raise RuntimeError(f"Next.js static export is missing a regular index.html: {index_path}")
            return {
                "status": "success",
                "release_dir": str(release_dir),
                "start_command": [],
                "runtime_required": False,
                "static_root": export_dir.relative_to(app_dir).as_posix(),
                "working_directory": str(project_dir),
            }
        _install_js_production_dependencies(manager, project_dir, runner, timeout, "Next.js", package_dir, log)
        start = _next_start_command(manager, app["port"])
        start = _configured_start(app, start)
        return {
            "status": "success",
            "release_dir": str(release_dir),
            "start_command": start,
            "working_directory": str(project_dir),
        }


def validate_go_package(value: Any) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not re.fullmatch(r"\.(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*", value)
    ):
        raise ValueError("build_config.go_package must be '.' or a relative package path such as './cmd/server'")


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
        group_id: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if not (repo_path / "go.mod").is_file():
            raise FileNotFoundError("go.mod not found for Go application")
        app_dir = _copy_source(repo_path, release_dir, group_id, app.get("route"))
        binary_path = release_dir / "server"
        target_result = _run_build(
            runner, ["go", "env", "-json", "GOHOSTOS", "GOHOSTARCH", "GOOS", "GOARCH"],
            app_dir, timeout, "Inspecting Go deployment target",
            preserve_stdout=True,
        )
        target = json.loads(target_result.stdout)
        if not isinstance(target, dict) or target.get("GOHOSTOS") != "linux" or not isinstance(target.get("GOHOSTARCH"), str) or not re.fullmatch(r"[a-z0-9]+", target["GOHOSTARCH"]):
            raise ValueError("Go deployment requires a Linux host toolchain with a valid GOHOSTARCH")
        native_go = ["env", "GOOS=linux", "GOARCH=" + target["GOHOSTARCH"], "go"]
        configured_package = app.get("build_config", {}).get("go_package")
        validate_go_package(configured_package)
        listed = _run_build(
            runner, [*native_go, "list", "-json=Name,Dir", configured_package or "./..."],
            app_dir, timeout, "Finding Go executable packages",
            preserve_stdout=True,
        )
        remaining = listed.stdout.strip()
        decoder = json.JSONDecoder()
        packages = []
        while remaining:
            package, end = decoder.raw_decode(remaining)
            remaining = remaining[end:].lstrip()
            if not isinstance(package, dict) or not isinstance(package.get("Dir"), str):
                raise ValueError("Go package metadata must include a package directory")
            relative = Path(package["Dir"]).resolve().relative_to(app_dir.resolve())
            if package.get("Name") == "main":
                packages.append("." if not relative.parts else "./" + relative.as_posix())
        if configured_package is None and "." in packages:
            selected_package = "."
        elif len(packages) == 1:
            selected_package = packages[0]
        elif not packages:
            raise ValueError("No executable Go main package found. A library cannot be deployed as a web service.")
        else:
            raise ValueError("Multiple Go main packages found: " + ", ".join(packages) + ". Set build_config.go_package to the web server package.")
        if log is not None:
            log(f"Go deployment target: linux/{target['GOHOSTARCH']}; configured target={target.get('GOOS')}/{target.get('GOARCH')}; package={selected_package}")
        _run_build(runner, [*native_go, "build", "-o", str(binary_path), selected_package], app_dir, timeout, "Building Go application")
        if binary_path.is_symlink() or not binary_path.is_file():
            raise RuntimeError("Go build did not create a regular server executable")
        with binary_path.open("rb") as binary:
            signature = binary.read(8)
        if not signature.startswith(b"\x7fELF"):
            artifact = "Go library archive" if signature.startswith(b"!<arch>\n") else "non-ELF artifact"
            raise RuntimeError(
                f"Go build output is not a Linux ELF executable ({artifact}). "
                "Check GOFLAGS/buildmode settings; deployment requires a native executable, not an archive or cross-platform binary."
            )
        if os.name != "nt" and not binary_path.stat().st_mode & 0o111:
            raise RuntimeError("Go server has no execute permission; check the build umask and release filesystem permissions")
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
        group_id: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        package_path = repo_path / "package.json"
        try:
            package = json.loads(_readable_regular_file(package_path, "Node.js package.json").decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid Node.js package.json: {exc}") from exc
        if not isinstance(package, dict) or not isinstance(package.get("scripts", {}), dict):
            raise ValueError("Node.js package.json must be an object with a scripts object")
        scripts = package.get("scripts", {})
        if not scripts.get("start") and not app.get("build_config", {}).get("start_command"):
            raise ValueError("Node.js applications need a start script or build_config.start_command")
        app_dir = _copy_source(repo_path, release_dir, group_id, app.get("route"))
        manager, lockfile = _detect_js_package_manager(app_dir)
        if app.get("_managed_pnpm"):
            lockfile = _convert_to_pnpm(
                app_dir, manager, lockfile, runner, timeout, log, group_id=group_id
            )
            manager = "pnpm"
        _install_js_dependencies(manager, lockfile, app_dir, runner, timeout, "Node.js", log=log)
        if scripts.get("build"):
            build_command = _js_build_command(manager)
            if app.get("route") and scripts["build"].strip() == "vite build":
                build_command.extend((["--"] if manager == "npm" else []) + ["--base", app["route"]])
            _run_build(runner, build_command, app_dir, timeout, "Building Node.js application")
            if app.get("route"):
                _prefix_static_html_assets(app_dir, app["route"])
        _install_js_production_dependencies(manager, app_dir, runner, timeout, "Node.js", log=log)
        start = _configured_start(app, [manager, "run", "start"])
        return {"status": "success", "release_dir": str(release_dir), "start_command": start}


def python_interpreter(build_config: Dict[str, Any]) -> str:
    executable = build_config.get("python_interpreter", "python3")
    if not isinstance(executable, str) or not re.fullmatch(r"python3(?:\.(?:9|[1-9][0-9]))?", executable):
        raise ValueError("build_config.python_interpreter must be python3 or a versioned system command such as python3.12")
    return executable


def python_command(command: Sequence[str], app: Dict[str, Any], app_dir: Path) -> List[str]:
    replacements = {
        "{port}": str(app["port"]),
        "{venv}": str(app_dir / ".venv" / "bin"),
        "{base_path}": app.get("route", "/").rstrip("/"),
    }
    resolved = []
    for argument in command:
        for token, value in replacements.items():
            argument = argument.replace(token, value)
        if "{" in argument or "}" in argument or "$" in argument:
            raise ValueError("Python commands accept only {port}, {venv}, and {base_path} substitutions; not shell expansion")
        resolved.append(argument)
    if "/" not in resolved[0] and "\\" not in resolved[0]:
        executable = app_dir / ".venv" / "bin" / resolved[0]
        if executable.is_file():
            resolved[0] = str(executable)
    return resolved


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
        group_id: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        del log
        app_dir = _copy_source(repo_path, release_dir, group_id, app.get("route"),
                       excluded_roots=("storage", "models") if app.get("python_services") else ())
        interpreter = python_interpreter(app.get("build_config", {}))
        _run_build(runner, [interpreter, "-m", "venv", ".venv"], app_dir, timeout, "Creating Python virtual environment")
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
        if not app.get("build_config", {}).get("start_command"):
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
        start = python_command(start, app, app_dir)
        return {"status": "success", "release_dir": str(release_dir), "start_command": start}


def _scoped_file_url(value: str, root: Path, route: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith(("//", route)):
        return value
    segments = unquote(parsed.path).lstrip("/").split("/")
    if any(not segment or segment.startswith(".") or "\\" in segment for segment in segments):
        return value
    for candidate_root in (root, root / "public", root / "static", root / "dist", root / "build"):
        candidate = candidate_root.joinpath(*segments)
        try:
            candidate.resolve(strict=True).relative_to(root)
            if candidate.is_file():
                return urlunsplit(("", "", route.rstrip("/") + parsed.path, parsed.query, parsed.fragment))
        except (OSError, ValueError):
            continue
    return value


def _prefix_web_manifest(path: Path, root: Path, route: str) -> None:
    contents = path.read_text(encoding="utf-8")
    try:
        manifest = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid web manifest in release: {path.relative_to(root)}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"Web manifest must be a JSON object: {path.relative_to(root)}")
    original = json.dumps(manifest)
    if path.suffix != ".webmanifest" and not any(key in manifest for key in ("start_url", "scope")):
        return
    for key in ("id", "scope", "start_url"):
        value = manifest.get(key)
        if isinstance(value, str) and value.startswith("/") and not value.startswith(("//", route)) and value != route.rstrip("/"):
            manifest[key] = route.rstrip("/") + value
    icons = manifest.get("icons", [])
    if isinstance(icons, list):
        for icon in icons:
            if isinstance(icon, dict) and isinstance(icon.get("src"), str):
                icon["src"] = _scoped_file_url(icon["src"], root, route)
    if json.dumps(manifest) != original:
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


class _StaticAssetParser(HTMLParser):
    def __init__(self, content: str, root: Path, route: str):
        super().__init__(convert_charrefs=False)
        self.content = content
        self.root = root.resolve(strict=True)
        self.route = route
        self.offsets = [0]
        for line in content.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.replacements = []

    def handle_starttag(self, tag, attrs):
        updated = []
        changed = False
        for name, value in attrs:
            if tag == "base" and name == "href" and value == "/":
                value = self.route
                changed = True
            elif tag != "base" and name in {"src", "href", "poster", "data"} and value:
                scoped = _scoped_file_url(value, self.root, self.route)
                changed = changed or scoped != value
                value = scoped
            updated.append(name if value is None else f'{name}="{escape(value, quote=True)}"')
        if changed:
            original = self.get_starttag_text()
            ending = " />" if original.endswith("/>") else ">"
            line, column = self.getpos()
            start = self.offsets[line - 1] + column
            self.replacements.append((start, start + len(original), "<" + tag + " " + " ".join(updated) + ending))

    handle_startendtag = handle_starttag


def _prefix_static_html_assets(app_dir: Path, route: str) -> None:
    for current, directories, filenames in os.walk(app_dir, followlinks=False):
        directories[:] = [name for name in directories if name not in {
            "node_modules", ".venv", ".git", ".next", "__pycache__",
        } and not (Path(current) / name).is_symlink()]
        for name in filenames:
            path = Path(current) / name
            if path.is_symlink():
                continue
            if path.suffix == ".webmanifest" or name == "manifest.json":
                _prefix_web_manifest(path, app_dir.resolve(strict=True), route)
            elif path.suffix == ".html":
                content = path.read_text(encoding="utf-8")
                parser = _StaticAssetParser(content, app_dir, route)
                parser.feed(content)
                parser.close()
                if parser.replacements:
                    for start, end, replacement in reversed(parser.replacements):
                        content = content[:start] + replacement + content[end:]
                    path.write_text(content, encoding="utf-8")


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
        group_id: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        del log
        if not (repo_path / "index.html").is_file():
            raise FileNotFoundError("index.html not found for static application")
        app_dir = _copy_source(repo_path, release_dir, group_id, app.get("route"))
        start = _configured_start(app, [])
        return {
            "status": "success",
            "release_dir": str(release_dir),
            "start_command": start,
            "runtime_required": bool(start),
        }


def detect_framework(repo_path: Path, build_config: Optional[Dict[str, Any]] = None) -> str:
    if DockerAdapter.detect(repo_path):
        return DockerAdapter.name
    if NextJSAdapter.detect(repo_path, build_config):
        return NextJSAdapter.name
    for adapter_cls in (GoAdapter, PythonAdapter, NodeAdapter, StaticAdapter):
        if adapter_cls.detect(repo_path):
            return adapter_cls.name
    raise ValueError("Could not detect a supported framework in the Git repository")


def inspect_project(repo_path: Path, framework: str, build_config: Dict[str, Any]) -> Dict[str, Any]:
    errors = []
    warnings = []
    tools = {}
    project = repo_path
    explicit_start = bool(build_config.get("start_command"))
    try:
        _validate_copy_symlinks(repo_path)
        if framework == "docker":
            validate_docker_build_config(build_config, repo_path)
            context_dir = repo_path / build_config["app_directory"] if build_config.get("app_directory") else repo_path
            current = repo_path
            for part in build_config["app_directory"].split("/") if build_config.get("app_directory") else []:
                current = current / part
                if current.is_symlink():
                    raise ValueError(f"Docker build context must not contain symlinks: {current}")
            if not context_dir.is_dir():
                errors.append(f"Docker build context directory does not exist: {context_dir}")
            elif docker_deployment_mode(build_config, repo_path) == "compose":
                compose_file = build_config.get("compose_file")
                if compose_file is None:
                    compose_file = next(
                        (filename for filename in DOCKER_COMPOSE_FILES if (context_dir / filename).is_file()),
                        DOCKER_COMPOSE_FILES[0],
                    )
                current = context_dir
                for part in compose_file.split("/"):
                    current = current / part
                    if current.is_symlink():
                        raise ValueError(f"Compose file path must not contain symlinks: {current}")
                if not (context_dir / compose_file).is_file():
                    errors.append(f"Compose file not found: {context_dir / compose_file}")
                warnings.append(
                    "Compose starts all project services but publishes only the selected service on loopback; "
                    "host ports, host filesystem mounts, external volumes and host namespaces are rejected"
                )
                warnings.append("Compose builds execute through the host Docker daemon; deploy only trusted repositories")
            else:
                dockerfile = build_config.get("dockerfile", "Dockerfile")
                current = context_dir
                for part in dockerfile.split("/"):
                    current = current / part
                    if current.is_symlink():
                        raise ValueError(f"Dockerfile path must not contain symlinks: {current}")
                if not (context_dir / dockerfile).is_file():
                    errors.append(f"Dockerfile not found: {context_dir / dockerfile}")
                warnings.append("Docker builds execute through the host Docker daemon; deploy only trusted repositories")
            tools["docker"] = None
        elif framework in {"node", "nextjs"}:
            package_root = repo_path
            if framework == "nextjs":
                project, package_root = _next_project_directories(repo_path, build_config)
            package = json.loads(_readable_regular_file(project / "package.json", "package.json").decode("utf-8"))
            if not isinstance(package, dict) or not isinstance(package.get("scripts", {}), dict):
                raise ValueError("package.json must contain a scripts object")
            scripts = package.get("scripts", {})
            if any(not isinstance(value, str) or not value.strip() for value in scripts.values()):
                errors.append("Package scripts must be non-empty strings")
            if framework == "nextjs" and not scripts.get("build"):
                errors.append("Next.js requires a build script")
            if framework == "node" and not scripts.get("start") and not explicit_start:
                errors.append("Node.js requires a start script or an explicit start command")
            manager, _ = _detect_js_package_manager(package_root)
            tools.update({"node": "18.18.0", "pnpm": "10.3.0"})
            warnings.append(f"Managed builds convert {manager} dependencies to pnpm in the release copy and reuse the loopback registry cache")
            dependencies = {}
            for field in ("devDependencies", "dependencies"):
                if not isinstance(package.get(field, {}), dict):
                    errors.append(f"package.json {field} must be an object")
                else:
                    dependencies.update(package.get(field, {}))
            if framework == "nextjs":
                next_version = re.search(r"[0-9]+", str(dependencies.get("next", "")))
                if next_version and int(next_version.group(0)) >= 16:
                    tools["node"] = "20.9.0"
                warnings.append("Existing Next.js basePath, trailingSlash and output settings are verified by Next.js before the build")
            if not isinstance(package.get("engines", {}), dict):
                errors.append("package.json engines must be an object")
            elif package.get("engines", {}).get("node"):
                warnings.append("Declared Node engine: " + str(package["engines"]["node"]) + "; configure a minimum runtime version when needed")
        elif framework == "go":
            validate_go_package(build_config.get("go_package"))
            if not (project / "go.mod").is_file():
                errors.append("Go requires go.mod")
            tools["go"] = None
            if (project / "go.mod").is_file():
                directive = re.search(r"(?m)^go\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\s*$", _readable_regular_file(project / "go.mod", "go.mod").decode("utf-8"))
                if directive:
                    tools["go"] = directive.group(1)
            warnings.append("Go main packages are discovered before build; deployment targets the Linux host architecture")
        elif framework == "python":
            if not any((project / filename).is_file() for filename in ("requirements.txt", "pyproject.toml")):
                errors.append("Python requires requirements.txt or pyproject.toml")
            if not explicit_start and not ((project / "app.py").is_file() or (project / "app" / "__init__.py").is_file()):
                errors.append("Default Python startup requires app:app; configure an explicit WSGI/ASGI/Django start command")
            if not explicit_start and (project / "manage.py").is_file():
                errors.append("Django requires an explicit compatible start command")
            entrypoint = project / "app.py" if (project / "app.py").is_file() else project / "app" / "__init__.py"
            if not explicit_start and entrypoint.is_file():
                syntax = ast.parse(_readable_regular_file(entrypoint, "Python entry point").decode("utf-8"))
                declared = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "app"
                               or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets)
                               or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "app"
                               or isinstance(node, ast.ImportFrom) and any((alias.asname or alias.name) == "app" for alias in node.names)
                               for node in syntax.body)
                if not declared:
                    errors.append("Default Python startup requires an app symbol in app:app")
                asgi = any(isinstance(node, ast.AsyncFunctionDef) and node.name == "app"
                           or isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id in {"FastAPI", "Starlette"}
                           or isinstance(node.func, ast.Attribute) and node.func.attr in {"FastAPI", "Starlette"}) for node in ast.walk(syntax))
                if asgi:
                    errors.append("ASGI applications require an explicit compatible start command and dependencies")
            tools["python3"] = "3.9.0"
        elif framework == "static":
            if not (project / "index.html").is_file():
                errors.append("Static applications require index.html")
        else:
            errors.append("Unsupported framework")
    except (OSError, ValueError, UnicodeError, SyntaxError) as exc:
        errors.append(str(exc))
    return {"framework": framework, "project_directory": str(project), "tools": tools,
            "errors": errors, "warnings": warnings}


def get_adapter(name: str) -> FrameworkAdapter:
    registry = {
        "docker": DockerAdapter,
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
