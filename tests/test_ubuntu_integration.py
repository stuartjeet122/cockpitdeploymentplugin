from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import backend.app_manager as manager


ENABLED = sys.platform == "linux" and os.environ.get("APP_MANAGER_DISPOSABLE_UBUNTU") == "1"


@unittest.skipUnless(ENABLED, "requires an explicitly opted-in disposable Ubuntu VM")
class UbuntuIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or "ID=ubuntu" not in Path("/etc/os-release").read_text():
            raise RuntimeError("Integration tests require root on a disposable Ubuntu VM")
        cls.state = Path("/var/lib/cockpit/app-manager-integration")
        cls.production = Path("/opt/webapps-integration")
        cls.logs = Path("/var/log/cockpit-app-manager-integration")
        cls.site = Path("/etc/nginx/conf.d/cockpit-app-manager-integration.conf")
        cls.snippets = Path("/etc/nginx/snippets/cockpit-app-manager-integration")
        cls.targets = (cls.state, cls.production, cls.logs, cls.site, cls.snippets)
        if any(path.exists() or path.is_symlink() for path in cls.targets):
            raise RuntimeError("Integration paths already exist; refusing to overwrite them")
        if any(Path(f"/etc/systemd/system/cockpit-app-it-{framework}{suffix}.service").exists()
               for framework in ("static", "node", "go", "python", "nextjs") for suffix in ("", "@secondary")):
            raise RuntimeError("Integration application units already exist")
        cls.stack = contextlib.ExitStack()
        cls.addClassCleanup(cls.stack.close)
        constants = {
            "DEFAULT_STATE_ROOT": str(cls.state), "DEFAULT_CONFIG_PATH": str(cls.state / "config.json"),
            "DEFAULT_REGISTRY_PATH": str(cls.state / "apps.json"), "DEFAULT_SOURCE_ROOT": str(cls.state / "sources"),
            "DEFAULT_PRODUCTION_ROOT": str(cls.production), "DEFAULT_LOG_ROOT": str(cls.logs),
            "DEFAULT_NGINX_SNIPPET_DIR": str(cls.snippets),
            "NGINX_MANAGED_INCLUDE": f"include {cls.snippets}/*.conf;",
            "DEFAULT_PUBLIC_ORIGIN": "http://127.0.0.1:18080",
        }
        for name, value in constants.items():
            cls.stack.enter_context(patch.object(manager, name, value))
        cls.original_nginx = cls.nginx_hashes()
        cls.legacy_state = cls.command(["systemctl", "show", "stuserver-websites", "--property=ActiveState", "--value"], check=False).stdout
        cls.addClassCleanup(cls.clean_host)
        cls.site.write_text("server {\n listen 127.0.0.1:18080;\n server_name 127.0.0.1;\n location = / { return 200 'root-site-preserved'; }\n}\n", encoding="utf-8")
        cls.command(["nginx", "-t"])
        cls.command(["systemctl", "reload", "nginx"])
        manager.ensure_runtime_dirs()
        manager.update_settings({"build_timeout_seconds": 600, "health_timeout_seconds": 60})

    @classmethod
    def command(cls, arguments, check=True):
        return subprocess.run(arguments, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    @classmethod
    def nginx_hashes(cls):
        result = {}
        for path in Path("/etc/nginx").rglob("*"):
            if path.is_file() and path != cls.site and cls.snippets not in path.parents:
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    @classmethod
    def clean_host(cls):
        for framework in ("static", "node", "go", "python", "nextjs"):
            for suffix in ("", "@secondary"):
                unit = Path(f"/etc/systemd/system/cockpit-app-it-{framework}{suffix}.service")
                if unit.exists() and "# Managed by Cockpit App Manager" in unit.read_text():
                    cls.command(["systemctl", "disable", "--now", unit.name], check=False)
                    unit.unlink()
            cls.command(["userdel", f"app-it-{framework}"], check=False)
        cls.command(["systemctl", "daemon-reload"])
        cls.site.unlink(missing_ok=True)
        cls.command(["nginx", "-t"])
        cls.command(["systemctl", "reload", "nginx"])
        for path in (cls.snippets, cls.state, cls.production, cls.logs):
            if path.exists():
                shutil.rmtree(path)

    def fetch(self, route):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:18080" + route, timeout=10) as response:
            return response.status, response.read().decode()

    def test_real_framework_deployments_and_recovery(self):
        fixtures = {
            "static": {"index.html": "release-one"},
            "node": {
                "package.json": json.dumps({"scripts": {"start": "node server.js"}}),
                "server.js": "require('http').createServer((req,res)=>{res.end('release-one')}).listen(Number(process.env.PORT),'127.0.0.1');\n",
            },
            "go": {
                "go.mod": "module integration.example/app\n\ngo 1.22\n",
                "main.go": 'package main\nimport("net/http";"os")\nfunc main(){http.HandleFunc("/",func(response http.ResponseWriter,request *http.Request){response.Write([]byte("release-one"))});http.ListenAndServe("127.0.0.1:"+os.Getenv("PORT"),nil)}\n',
            },
            "python": {
                "requirements.txt": "",
                "app.py": 'def app(environ, start_response):\n    start_response("200 OK", [("Content-Type", "text/plain")])\n    return [b"release-one"]\n',
            },
            "nextjs": {
                "package.json": json.dumps({"scripts": {"build": "next build"}, "dependencies": {"next": "^15.5.0", "react": "^19.0.0", "react-dom": "^19.0.0"}}),
                "pages/index.js": "export default function Page() { return <main>release-one</main>; }\n",
            },
        }
        for framework, files in fixtures.items():
            with self.subTest(framework=framework):
                app_id = "it-" + framework
                app = manager.add_app({"id": app_id, "name": "Integration " + framework,
                                       "repo_url": "https://github.com/example/integration.git", "framework": framework,
                                       "resource_limits": {"build_memory_mb": 4096}})["app"]
                def copy_fixture(repo_url, source, branch, username, *args):
                    for filename, contents in files.items():
                        target = source / filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(contents, encoding="utf-8")
                    manager._chown_tree(source, username)
                    return {"status": "cloned"}
                with patch.object(manager, "clone_or_update_repo", side_effect=copy_fixture), patch.object(manager, "git_output", return_value="a" * 40):
                    first = manager.deploy_app(app_id)
                self.assertEqual(self.fetch(app["route"])[0], 200)
                self.assertIn("release-one", self.fetch(app["route"])[1])
                self.assertFalse(Path(app["source_path"]).exists())
                self.assertTrue(manager.health_check_app(app_id)["public_healthy"])
                persistent = manager._app_paths(app_id, manager.get_config())["storage"] / "data" / "saved.txt"
                manager._run_as_user(app["service_user"], ["python3", "-c", "import os,pathlib;pathlib.Path(os.environ['APP_DATA_DIR']).joinpath('saved.txt').write_text('retained')"],
                                     environment={"APP_DATA_DIR": str(persistent.parent)})
                self.assertEqual(persistent.read_text(), "retained")
                metrics = manager.metrics_app(app_id)["metrics"]
                self.assertGreater(metrics["storage_bytes"], 0)
                if framework != "static":
                    self.assertEqual(metrics["service_state"], "active")
                    self.assertIsNotNone(metrics["memory_bytes"])
                if framework != "static":
                    files = {filename: contents.replace("release-one", "release-two") for filename, contents in files.items()}
                    with patch.object(manager, "clone_or_update_repo", side_effect=copy_fixture), patch.object(manager, "git_output", return_value="b" * 40), patch.object(manager, "_assert_deployed_ancestor"):
                        second = manager.deploy_app(app_id)
                    self.assertNotEqual(first["app"]["port"], second["app"]["port"])
                    self.assertIn("release-two", self.fetch(app["route"])[1])
                    self.assertFalse(Path(first["deployment"]["release"]).exists())
                    self.assertEqual(persistent.read_text(), "retained")
                    manager.edit_app(app_id, {"health_check_url": "/never-ready", "build_config": {"start_command": ["/bin/false"]}})
                    with patch.object(manager, "clone_or_update_repo", side_effect=copy_fixture), patch.object(manager, "git_output", return_value="c" * 40), patch.object(manager, "_assert_deployed_ancestor"), patch.object(manager, "_wait_for_health", return_value=False):
                        with self.assertRaises(RuntimeError):
                            manager.deploy_app(app_id)
                    self.assertIn("release-two", self.fetch(app["route"])[1])
                    stored = manager._find_app(manager.get_registry(), app_id)
                    self.assertEqual(stored["deployed_commit"], "b" * 40)
                    manager.edit_app(app_id, {"auto_deploy": False})
                    with patch.object(manager, "_remote_app_commit") as remote:
                        manager.auto_deploy_apps()
                        remote.assert_not_called()
                self.assertEqual(self.fetch("/")[1], "root-site-preserved")
                self.assertEqual(self.nginx_hashes(), self.original_nginx)
                self.assertEqual(self.command(["systemctl", "show", "stuserver-websites", "--property=ActiveState", "--value"], check=False).stdout, self.legacy_state)
                manager.remove_app(app_id)

    def test_real_ssh_release_signature_verification(self):
        workspace = self.state / "signature-test"
        workspace.mkdir()
        key = workspace / "release-key"
        self.command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        trust = workspace / "trusted-signers"
        trust.write_text("release " + key.with_suffix(".pub").read_text(), encoding="utf-8")
        trust.chmod(0o600)
        self.command(["git", "init", str(workspace / "repository")])
        checkout = workspace / "repository"
        for setting, value in (("user.name", "Integration"), ("user.email", "integration@example.invalid"),
                               ("gpg.format", "ssh"), ("user.signingkey", str(key))):
            self.command(["git", "-C", str(checkout), "config", setting, value])
        self.command(["git", "-C", str(checkout), "commit", "--allow-empty", "-m", "Release fixture"])
        self.command(["git", "-C", str(checkout), "tag", "-s", "v1.0.0", "-m", "Signed release"])
        self.command(["git", "-C", str(checkout), "tag", "-a", "v2.0.0", "-m", "Unsigned release"])
        with patch.object(manager, "PLUGIN_TRUSTED_SIGNERS", str(trust)):
            manager._verify_plugin_release(checkout, "v1.0.0")
            with self.assertRaises(RuntimeError):
                manager._verify_plugin_release(checkout, "v2.0.0")
        self.assertEqual(self.nginx_hashes(), self.original_nginx)