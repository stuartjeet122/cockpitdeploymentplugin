#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


class StaticRequestHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "AppManagerStatic"

    def __init__(self, *args, directory: str, base_path: str, spa_fallback: bool, **kwargs):
        self.root = Path(directory).resolve(strict=True)
        self.base_path = base_path.rstrip("/")
        self.spa_fallback = spa_fallback
        super().__init__(*args, directory=str(self.root), **kwargs)

    def translate_path(self, path: str) -> str:
        url_path = unquote(urlsplit(path).path)
        if "\\" in url_path or any(ord(char) < 32 for char in url_path):
            return str(self.root / ".invalid-path.txt")
        parts = [part for part in url_path.split("/") if part]
        if any(part.startswith(".") or part in {"..", "."} for part in parts):
            return str(self.root / ".invalid-path.txt")
        if self.base_path:
            if url_path == self.base_path:
                parts = []
            elif url_path.startswith(self.base_path + "/"):
                parts = [part for part in url_path[len(self.base_path):].split("/") if part]
        target = self.root.joinpath(*parts)
        try:
            resolved = target.resolve(strict=False)
            relative = resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError):
            return str(self.root / ".invalid-path.txt")
        if any(part.startswith(".") for part in relative.parts):
            return str(self.root / ".invalid-path.txt")
        if self.spa_fallback and not target.exists() and not PurePosixPath(url_path).suffix:
            return str(self.root / "index.html")
        return str(target)

    def log_message(self, format: str, *args) -> None:
        return

    def list_directory(self, path: str):
        self.send_error(404, "Directory listings are disabled")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a deployed static application on loopback.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--base-path", required=True)
    parser.add_argument("--spa-fallback", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve(strict=True)
    if not root.is_dir() or not (root / "index.html").is_file():
        parser.error("root must be a directory containing index.html")
    if not 8100 <= args.port <= 8999 or args.port == 8080:
        parser.error("port must be in the managed application range")
    if not args.base_path.startswith("/") or args.base_path.startswith("//"):
        parser.error("base-path must be a local absolute path")

    handler = lambda *handler_args, **handler_kwargs: StaticRequestHandler(
        *handler_args, directory=str(root), base_path=args.base_path,
        spa_fallback=args.spa_fallback, **handler_kwargs,
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
