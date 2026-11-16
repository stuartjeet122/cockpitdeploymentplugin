import hashlib
import json
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath


RUNTIME_ROOT = Path("/opt/cockpit-app-manager-python")
BIN_DIRECTORY = Path("/usr/local/bin")
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
RELEASE_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
RELEASE_BASE = "https://github.com/astral-sh/python-build-standalone/releases/download/"


def _download(url, destination, limit, deadline):
    request = urllib.request.Request(url, headers={"User-Agent": "Cockpit-App-Manager", "Accept": "application/vnd.github+json" if url == RELEASE_API else "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("xb") as output:
        size = 0
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("Python download exceeded its time limit; retry the installation")
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise RuntimeError("Python download exceeded its size limit")
            output.write(chunk)


def _release_asset(release, architecture):
    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not re.fullmatch(r"[0-9]{8}", tag):
        raise ValueError("Invalid upstream Python release metadata")
    candidates = []
    pattern = rf"cpython-(3\.12\.[0-9]+)\+{tag}-{re.escape(architecture)}-unknown-linux-gnu-install_only_stripped\.tar\.gz"
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        match = re.fullmatch(pattern, name) if isinstance(name, str) else None
        if match:
            candidates.append((int(match.group(1).split(".")[-1]), asset))
    if not candidates:
        raise RuntimeError("The upstream release has no supported Python 3.12 archive for this host")
    asset = max(candidates, key=lambda item: item[0])[1]
    expected_url = RELEASE_BASE + tag + "/" + asset["name"]
    if asset.get("browser_download_url") != expected_url or not re.fullmatch(r"sha256:[a-f0-9]{64}", asset.get("digest") or ""):
        raise ValueError("Python release must include its official HTTPS URL and SHA-256 digest")
    return asset


def _extract(archive_path, destination):
    if not hasattr(tarfile, "data_filter"):
        raise RuntimeError("The host Python needs tarfile data-filter support for safe runtime installation")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = []
        total = 0
        for member in archive:
            name = PurePosixPath(member.name)
            total += member.size
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "python":
                raise ValueError("Unsafe path in Python archive")
            if len(members) >= 50000 or total > 1024 * 1024 * 1024:
                raise ValueError("Python archive exceeds extraction limits")
            members.append(member)
        archive.extractall(destination, members=members, filter="data")


def _directory(path):
    for parent in reversed([path, *path.parents]):
        if not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o755)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"Runtime installation requires a root-owned, non-writable directory: {parent}")


def _probe(executable, runner):
    script = "import json, sys, venv, ensurepip; print(json.dumps({'version': list(sys.version_info[:3])}))"
    result = runner(["/usr/sbin/runuser", "-u", "nobody", "--", str(executable), "-I", "-c", script], timeout=20,
                    env={"PATH": SYSTEM_PATH, "HOME": "/nonexistent", "LANG": "C.UTF-8"})
    if result.returncode:
        raise RuntimeError("Python 3.12 or its venv/ensurepip modules are inaccessible to an unprivileged account")
    version = json.loads(result.stdout).get("version")
    if not isinstance(version, list) or len(version) != 3 or version[:2] != [3, 12] or not all(type(part) is int for part in version):
        raise RuntimeError("Installed interpreter is not Python 3.12")
    return ".".join(map(str, version))


def runtime_status(runner):
    executable = shutil.which("python3.12", path=SYSTEM_PATH)
    if not executable:
        return {"ok": True, "available": False, "version": None, "executable": "python3.12"}
    try:
        version = _probe(executable, runner)
        return {"ok": True, "available": True, "version": version, "executable": "python3.12"}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": True, "available": False, "version": None, "executable": "python3.12", "error": str(exc)}


def install_runtime(runner):
    if platform.system() != "Linux":
        raise RuntimeError("Shared Python provisioning requires Linux")
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if architecture is None or platform.libc_ver()[0] != "glibc":
        raise RuntimeError("Shared Python provisioning supports glibc x86_64 and aarch64 hosts only")
    status = runtime_status(runner)
    if status["available"]:
        return {**status, "installed": False}
    _directory(RUNTIME_ROOT)
    _directory(BIN_DIRECTORY)
    link = BIN_DIRECTORY / "python3.12"
    if link.exists() or link.is_symlink():
        raise RuntimeError("Refusing to overwrite an existing python3.12 command; administrator inspection is required")
    deadline = time.monotonic() + 300
    with tempfile.TemporaryDirectory(prefix=".install-", dir=RUNTIME_ROOT) as temporary:
        stage = Path(temporary)
        metadata = stage / "release.json"
        _download(RELEASE_API, metadata, 4 * 1024 * 1024, deadline)
        asset = _release_asset(json.loads(metadata.read_text(encoding="utf-8")), architecture)
        archive = stage / "runtime.tar.gz"
        _download(asset["browser_download_url"], archive, 128 * 1024 * 1024, deadline)
        with archive.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(handle.read()).hexdigest()
        if digest != asset["digest"][7:]:
            raise ValueError("Python archive SHA-256 verification failed; nothing was installed")
        extracted = stage / "extracted"
        extracted.mkdir(mode=0o755)
        _extract(archive, extracted)
        if (extracted / "python").is_symlink() or not (extracted / "python").is_dir():
            raise ValueError("Python archive must contain a real python directory")
        for path in [extracted / "python", *(extracted / "python").rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        destination = RUNTIME_ROOT / asset["name"].removesuffix(".tar.gz")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Python installation directory already exists; administrator inspection is required")
        (extracted / "python").rename(destination)
        try:
            version = _probe(destination / "bin" / "python3.12", runner)
            link.symlink_to(destination / "bin" / "python3.12")
        except Exception:
            shutil.rmtree(destination)
            raise
    return {"ok": True, "available": True, "installed": True, "version": version, "executable": "python3.12"}