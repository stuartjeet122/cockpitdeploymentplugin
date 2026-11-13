from __future__ import annotations

import ctypes
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.parse import unquote, urlsplit


def redis_ping(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "redis" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.query or parsed.fragment:
        raise ValueError("Set an explicit loopback redis:// REDIS_URL; remote Redis is not supported by this profile")
    if not parsed.path.startswith("/") or not parsed.path[1:].isdigit():
        raise ValueError("REDIS_URL must select an administrator-assigned dedicated Redis database")
    commands = []
    if parsed.password is not None:
        credentials = [unquote(parsed.password)]
        if parsed.username:
            credentials.insert(0, unquote(parsed.username))
        commands.append(["AUTH", *credentials])
    commands.extend([["SELECT", parsed.path[1:]], ["PING"]])
    with socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=3) as connection:
        connection.settimeout(3)
        with connection.makefile("rb") as response:
            for command in commands:
                encoded = [part.encode("utf-8") for part in command]
                connection.sendall(b"*" + str(len(encoded)).encode("ascii") + b"\r\n" + b"".join(
                    b"$" + str(len(part)).encode("ascii") + b"\r\n" + part + b"\r\n" for part in encoded
                ))
                expected = b"+PONG\r\n" if command[0] == "PING" else b"+OK\r\n"
                if response.readline(4096) != expected:
                    raise ValueError("Redis refused AUTH, SELECT or PING; verify the dedicated database and ACL")


def check_runtime(settings: dict, models: Path) -> dict:
    checks = []

    def check(name, operation, success, failure):
        try:
            passed = operation() is not False
        except Exception:
            passed = False
        checks.append({"name": name, "passed": passed, "message": success if passed else failure})

    minimum = (3, 11) if settings.get("drum_prerequisites") else (3, 9)
    supported = sys.version_info[:2] >= minimum and (not settings.get("drum_prerequisites") or sys.version_info[:2] <= (3, 13))
    version_requirement = "Python 3.11-3.13 for the pinned drum dependencies" if settings.get("drum_prerequisites") else "Python 3.9 or newer"
    check("Python", lambda: supported, "Supported Python", f"Administrator must provision {version_requirement} and select it with build_config.python_interpreter")
    check("venv", lambda: all(importlib.util.find_spec(name) is not None for name in ("venv", "ensurepip")),
          "venv and ensurepip available", "Administrator must provision matching Python venv/ensurepip support")
    if settings.get("redis"):
        check("Redis", lambda: redis_ping(os.environ.get("REDIS_URL", "")), "Private Redis AUTH/SELECT/PING succeeded",
              "Redis unavailable: explicitly configure REDIS_URL with a loopback host, dedicated database and valid ACL credentials; check listener and permissions (3-second socket timeout)")
    if settings.get("drum_prerequisites"):
        check("FFmpeg", lambda: shutil.which("ffmpeg") is not None, "FFmpeg available", "Administrator must provision FFmpeg")
        check("libsndfile", lambda: bool(ctypes.CDLL(ctypes.util.find_library("sndfile") or "libsndfile.so.1")),
              "libsndfile available", "Administrator must provision libsndfile")
        check("MuseScore", lambda: any(shutil.which(name) for name in ("mscore3", "musescore3", "drum-musescore", "mscore")),
              "MuseScore available", "Administrator must provision a headless-compatible MuseScore executable")
        check("Fonts", lambda: bool(subprocess.run(["fc-list"], capture_output=True, timeout=5, check=True).stdout.strip()),
              "Fontconfig reports installed fonts", "Administrator must provision fontconfig and notation fonts")
    for relative in settings.get("model_files", []):
        def artifact_present():
            artifact = models / relative
            for component in (models, *artifact.relative_to(models).parents, artifact):
                candidate = component if component.is_absolute() else models / component
                if candidate.is_symlink():
                    return False
            if os.access(models, os.W_OK) or os.access(artifact, os.W_OK):
                return False
            with artifact.open("rb") as handle:
                return bool(handle.read(1))
        check("Model: " + relative, artifact_present, "Readable nonempty read-only model: " + relative,
              "Administrator must provision a nonempty readable, non-symlink, runtime-read-only model: " + relative)
    return {"checks": checks, "errors": [item["message"] for item in checks if not item["passed"]], "warnings": [],
            "passed": all(item["passed"] for item in checks)}


if __name__ == "__main__":
    print(json.dumps(check_runtime(json.loads(sys.argv[1]), Path(sys.argv[2]))))