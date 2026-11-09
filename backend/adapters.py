from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


class FrameworkAdapter:
    name = "base"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return False

    def build(self, app: Dict[str, Any], repo_path: Path, release_dir: Path) -> Dict[str, Any]:
        return {"status": "success", "release_dir": str(release_dir)}

    def write_service(self, app: Dict[str, Any], release_dir: Path, service_path: Path, start_command: str) -> None:
        service_path.write_text(
            "[Unit]\n"
            "Description=App Manager service for {name}\n"
            "After=network.target\n"
            "[Service]\n"
            "Type=simple\n"
            "WorkingDirectory={release_dir}\n"
            "ExecStart={cmd}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "User=root\n"
            "Group=root\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n".format(name=app.get("name", "app"), release_dir=release_dir, cmd=start_command),
            encoding="utf-8",
        )


class NextJSAdapter(FrameworkAdapter):
    name = "nextjs"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (
            (repo_path / "next.config.js").exists()
            or (repo_path / "next.config.mjs").exists()
            or (repo_path / "next.config.ts").exists()
            or (repo_path / "package.json").exists() and json.loads((repo_path / "package.json").read_text(encoding="utf-8").replace("\n", " ") or "{}", strict=False).get("dependencies", {}).get("next") is not None
        )

    def build(self, app: Dict[str, Any], repo_path: Path, release_dir: Path) -> Dict[str, Any]:
        if not (repo_path / "package.json").exists():
            raise FileNotFoundError("package.json not found for Next.js app")

        app_dir = release_dir / "app"
        shutil.copytree(repo_path, app_dir, dirs_exist_ok=True)
        package_path = app_dir / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8")) if package_path.exists() else {}
        package.setdefault("scripts", {})
        package["scripts"]["start"] = "next start -H 127.0.0.1 -p ${PORT:-" + str(app.get("port", 3000)) + "}"
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        subprocess.run(["npm", "install", "--no-fund", "--no-audit"], cwd=str(app_dir), check=False, capture_output=True, text=True)
        subprocess.run(["npm", "run", "build"], cwd=str(app_dir), check=False, capture_output=True, text=True)

        next_config = app_dir / "next.config.js"
        if next_config.exists():
            content = next_config.read_text(encoding="utf-8")
            if "basePath" not in content:
                config_text = (
                    "\nmodule.exports = {\n"
                    "  basePath: '/{slug}',\n"
                    "  output: 'standalone',\n"
                    "};\n"
                ).format(slug=app.get("slug", "app"))
                next_config.write_text(content + config_text, encoding="utf-8")

        return {"status": "success", "release_dir": str(release_dir), "start_command": f"cd {app_dir} && npx next start -H 127.0.0.1 -p {app.get('port', 3000)}"}


class GoAdapter(FrameworkAdapter):
    name = "go"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "go.mod").exists()

    def build(self, app: Dict[str, Any], repo_path: Path, release_dir: Path) -> Dict[str, Any]:
        if not (repo_path / "go.mod").exists():
            raise FileNotFoundError("go.mod not found for Go app")

        app_dir = release_dir / "app"
        shutil.copytree(repo_path, app_dir, dirs_exist_ok=True)
        binary_path = release_dir / "server"
        subprocess.run(["go", "build", "-o", str(binary_path), "."], cwd=str(app_dir), check=False, capture_output=True, text=True)
        return {"status": "success", "release_dir": str(release_dir), "start_command": f"{binary_path} --port {app.get('port', 3000)}"}


class NodeAdapter(FrameworkAdapter):
    name = "node"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "package.json").exists()

    def build(self, app: Dict[str, Any], repo_path: Path, release_dir: Path) -> Dict[str, Any]:
        if not (repo_path / "package.json").exists():
            raise FileNotFoundError("package.json not found for Node app")

        app_dir = release_dir / "app"
        shutil.copytree(repo_path, app_dir, dirs_exist_ok=True)
        subprocess.run(["npm", "install", "--no-fund", "--no-audit"], cwd=str(app_dir), check=False, capture_output=True, text=True)
        start_script = app.get("build_config", {}).get("start_command") or "npm run start -- --host 127.0.0.1 --port " + str(app.get("port", 3000))
        return {"status": "success", "release_dir": str(release_dir), "start_command": start_script}


class PythonAdapter(FrameworkAdapter):
    name = "python"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "requirements.txt").exists() or (repo_path / "pyproject.toml").exists()

    def build(self, app: Dict[str, Any], repo_path: Path, release_dir: Path) -> Dict[str, Any]:
        app_dir = release_dir / "app"
        shutil.copytree(repo_path, app_dir, dirs_exist_ok=True)
        if (app_dir / "requirements.txt").exists():
            subprocess.run(["python3", "-m", "pip", "install", "-r", "requirements.txt"], cwd=str(app_dir), check=False, capture_output=True, text=True)
        return {"status": "success", "release_dir": str(release_dir), "start_command": f"cd {app_dir} && python3 -m gunicorn --bind 127.0.0.1:{app.get('port', 8000)} app:app"}


class StaticAdapter(FrameworkAdapter):
    name = "static"

    @staticmethod
    def detect(repo_path: Path) -> bool:
        return (repo_path / "index.html").exists()

    def build(self, app: Dict[str, Any], repo_path: Path, release_dir: Path) -> Dict[str, Any]:
        app_dir = release_dir / "app"
        shutil.copytree(repo_path, app_dir, dirs_exist_ok=True)
        return {"status": "success", "release_dir": str(release_dir), "start_command": f"python3 -m http.server {app.get('port', 8081)} --directory {app_dir}"}


def detect_framework(repo_path: Path) -> str:
    for adapter_cls in (NextJSAdapter, GoAdapter, PythonAdapter, NodeAdapter, StaticAdapter):
        if adapter_cls.detect(repo_path):
            return adapter_cls.name
    return "static"


def get_adapter(name: str) -> FrameworkAdapter:
    registry = {
        "nextjs": NextJSAdapter(),
        "go": GoAdapter(),
        "node": NodeAdapter(),
        "python": PythonAdapter(),
        "static": StaticAdapter(),
    }
    return registry.get(name, StaticAdapter())
