from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import backend.app_manager as manager
from backend.adapters import NextJSAdapter, detect_framework


class AppManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        paths = {
            "DEFAULT_STATE_ROOT": root / "state",
            "DEFAULT_CONFIG_PATH": root / "state" / "config.json",
            "DEFAULT_REGISTRY_PATH": root / "state" / "apps.json",
            "DEFAULT_SOURCE_ROOT": root / "state" / "sources",
            "DEFAULT_PRODUCTION_ROOT": root / "webapps",
            "DEFAULT_LOG_ROOT": root / "logs",
        }
        for attribute, path in paths.items():
            active_patch = patch.object(manager, attribute, str(path))
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def add_payload(self, name="Example Site", **overrides):
        payload = {
            "name": name,
            "repo_url": "https://github.com/example/demo.git",
            "branch": "main",
            "framework": "static",
        }
        payload.update(overrides)
        return payload

    def add_app(self, name="Example Site", **overrides):
        with patch.object(manager, "get_listening_ports", return_value=set()), patch.object(manager, "_socket_port_available", return_value=True):
            return manager.add_app(self.add_payload(name, **overrides))

    def test_detect_frameworks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text('{"dependencies":{"next":"^15"}}', encoding="utf-8")
            self.assertEqual(detect_framework(repo), "nextjs")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            self.assertEqual(detect_framework(repo), "go")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "requirements.txt").write_text("flask\n", encoding="utf-8")
            self.assertEqual(detect_framework(repo), "python")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text('{"scripts":{"start":"node server.js"}}', encoding="utf-8")
            self.assertEqual(detect_framework(repo), "node")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "index.html").write_text("<html></html>", encoding="utf-8")
            self.assertEqual(detect_framework(repo), "static")

    def test_unsupported_framework_detection_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Could not detect"):
                detect_framework(Path(tmp))

    def test_add_application_sanitizes_generated_id_and_assigns_supported_port(self):
        result = self.add_app("Example Site!")
        self.assertEqual(result["app"]["id"], "example-site")
        self.assertTrue(8100 <= result["app"]["port"] <= 8999)
        self.assertNotEqual(result["app"]["port"], 8080)
        self.assertEqual(len(manager.get_registry()["apps"]), 1)

    def test_add_application_accepts_explicit_available_port(self):
        result = self.add_app("Fixed Port", slug="fixed-port", port=8123)
        self.assertEqual(result["app"]["port"], 8123)

    def test_edit_application_updates_supported_fields(self):
        app = self.add_app("Demo App")["app"]
        result = manager.edit_app(app["id"], {"name": "Updated Demo", "health_check_url": "/status"})
        self.assertEqual(result["app"]["name"], "Updated Demo")
        self.assertEqual(result["app"]["health_check_url"], "/status")
        stored = manager.get_registry()["apps"][0]
        self.assertEqual(stored["name"], "Updated Demo")

    def test_settings_update_persists_valid_config(self):
        result = manager.update_settings({"release_retention": 9, "health_timeout_seconds": 45})
        self.assertEqual(result["config"]["release_retention"], 9)
        self.assertEqual(result["config"]["health_timeout_seconds"], 45)

    def test_duplicate_ids_and_ports_are_rejected(self):
        self.add_app()
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.add_app()
        with self.assertRaisesRegex(ValueError, "already in use"):
            self.add_app("Other", slug="other", port=8100)

    def test_port_validation_rejects_reserved_and_out_of_range(self):
        for port in (8080, 8099, 9000, True, "8100"):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    manager.validate_app_payload(self.add_payload(port=port))

    def test_invalid_ids_names_branches_and_git_urls_are_rejected(self):
        invalid_payloads = (
            self.add_payload(slug="../bad"),
            self.add_payload(name="Bad\nName"),
            self.add_payload(branch="../main"),
            self.add_payload(branch="-option"),
            self.add_payload(repo_url="https://user:password@example.com/repo.git"),
            self.add_payload(repo_url="file:///etc/passwd"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    manager.validate_app_payload(payload)

    def test_path_validation_resolves_symlinks_and_checks_directory_boundaries(self):
        root = Path(manager.DEFAULT_STATE_ROOT) / "allowed"
        outside = Path(manager.DEFAULT_STATE_ROOT) / "allowed-other"
        root.mkdir(parents=True)
        outside.mkdir(parents=True)
        with self.assertRaises(ValueError):
            manager.validate_allowed_path(str(outside / "source"), [str(root)])
        link = root / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is not permitted on this host")
        with self.assertRaises(ValueError):
            manager.validate_allowed_path(str(link / "source"), [str(root)])

    def test_ss_listener_parser_and_empty_proc_table(self):
        self.assertEqual(manager._parse_ss_ports("LISTEN 0 128 127.0.0.1:8123 0.0.0.0:*\n"), {8123})
        with patch.object(manager.Path, "read_text", return_value=" sl local_address rem_address st\n"):
            self.assertEqual(manager._proc_listening_ports(), set())

    def test_registry_corruption_is_reported_instead_of_reset(self):
        registry_path = Path(manager.DEFAULT_REGISTRY_PATH)
        registry_path.parent.mkdir(parents=True)
        registry_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            manager.get_registry()

    def test_registry_write_is_atomic_and_creates_private_json(self):
        manager.save_registry({"apps": []})
        registry_path = Path(manager.DEFAULT_REGISTRY_PATH)
        self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), {"apps": []})
        self.assertFalse(list(registry_path.parent.glob(".apps.json.*.tmp")))

    def test_concurrent_adds_reserve_unique_ports(self):
        def add(index):
            return self.add_app(f"App {index}", slug=f"app-{index}")["app"]["port"]

        with patch.object(manager, "get_listening_ports", return_value=set()), patch.object(manager, "_socket_port_available", return_value=True):
            with ThreadPoolExecutor(max_workers=12) as executor:
                ports = list(executor.map(add, range(12)))
        self.assertEqual(len(ports), len(set(ports)))
        self.assertTrue(all(8100 <= port <= 8999 and port != 8080 for port in ports))

    def test_concurrent_port_reservations_are_unique_and_consumable(self):
        with patch.object(manager, "get_listening_ports", return_value=set()), patch.object(manager, "_socket_port_available", return_value=True):
            with ThreadPoolExecutor(max_workers=8) as executor:
                ports = list(executor.map(lambda _: manager.pick_port(), range(8)))
            self.assertEqual(len(ports), len(set(ports)))
            self.assertEqual(len(manager.get_registry()["port_reservations"]), 8)
            result = manager.add_app(self.add_payload("Reserved", slug="reserved", port=ports[0]))
        self.assertEqual(result["app"]["port"], ports[0])
        self.assertNotIn(str(ports[0]), manager.get_registry()["port_reservations"])

    def test_missing_application_is_reported(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            manager.open_app("missing")

    def test_frontend_action_convention_supports_open(self):
        self.add_app()
        result = manager.action("example-site", "open")
        self.assertEqual(result["url"], "https://stuserver.online/example-site/")

    def test_cli_errors_go_to_stderr_and_stdout_remains_json(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = manager.main(["health", "not a valid id"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["ok"], False)
        self.assertIn("ValueError", stderr.getvalue())

    def test_cli_returns_json_for_settings_command(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = manager.main(["settings"])
        response = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(response["ok"])
        self.assertEqual(response["config"]["registry_path"], manager.DEFAULT_REGISTRY_PATH)
        self.assertEqual(stderr.getvalue(), "")

    def test_run_command_reports_timeout_and_missing_executable(self):
        with patch.object(manager.subprocess, "run", side_effect=subprocess.TimeoutExpired("sleep", 1)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                manager.run_command(["sleep", "2"], timeout=1)
        with patch.object(manager.subprocess, "run", side_effect=FileNotFoundError("missing")):
            with self.assertRaisesRegex(RuntimeError, "Could not run"):
                manager.run_command(["missing"])

    def test_git_clone_failure_is_reported_and_partial_directory_removed(self):
        source = Path(manager.DEFAULT_SOURCE_ROOT) / "example-site"
        failure = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="remote unavailable")
        with patch.object(manager, "run_command", return_value=failure), patch.object(manager, "get_config", wraps=manager.get_config):
            with self.assertRaisesRegex(RuntimeError, "remote unavailable"):
                manager.clone_or_update_repo(
                    "https://github.com/example/demo.git",
                    source,
                    "main",
                    username=None,
                )
        self.assertFalse(source.exists())

    def test_git_update_fast_forwards_without_switching_branches(self):
        source = Path(manager.DEFAULT_SOURCE_ROOT) / "example-site"
        source.mkdir(parents=True)
        (source / ".git").mkdir()
        responses = [
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout=str(source), stderr=""),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout="https://github.com/example/demo.git", stderr=""),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout="main", stderr=""),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout="abc123", stderr=""),
        ]
        with patch.object(manager, "_git", side_effect=responses) as git:
            result = manager.clone_or_update_repo(
                "https://github.com/example/demo.git",
                source,
                "main",
                username="app-example-site",
            )
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["commit"], "abc123")
        self.assertEqual(git.call_args_list[-2].args[2], ["merge", "--ff-only", "refs/remotes/origin/main"])

    def test_git_update_refuses_uncommitted_changes_before_fetch(self):
        source = Path(manager.DEFAULT_SOURCE_ROOT) / "example-site"
        source.mkdir(parents=True)
        (source / ".git").mkdir()
        responses = [
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout=str(source), stderr=""),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout=" M app.py\n", stderr=""),
        ]
        with patch.object(manager, "_git", side_effect=responses) as git:
            with self.assertRaisesRegex(RuntimeError, "local changes"):
                manager.clone_or_update_repo(
                    "https://github.com/example/demo.git",
                    source,
                    "main",
                    username="app-example-site",
                )
        self.assertEqual(git.call_count, 2)

    def test_failed_clone_is_recorded_in_deployment_history(self):
        app = self.add_app()["app"]
        with patch.object(manager, "_require_root"), patch.object(manager, "ensure_app_user", return_value=app["service_user"]), patch.object(
            manager, "_setup_app_directories", return_value=manager._app_paths(app["id"], manager.get_config())
        ), patch.object(manager, "clone_or_update_repo", side_effect=RuntimeError("remote unavailable")):
            with self.assertRaisesRegex(RuntimeError, "remote unavailable"):
                manager.deploy_app(app["id"])
        stored = manager.get_registry()["apps"][0]
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["deployments"][-1]["status"], "failed")
        self.assertIn("remote unavailable", stored["deployments"][-1]["error"])

    def test_health_check_accepts_only_success_responses(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        app = {"port": 8100, "health_check_url": "/health"}
        with patch("backend.app_manager.urllib.request.OpenerDirector.open", return_value=Response()):
            self.assertTrue(manager.check_app_health(app))
        with patch(
            "backend.app_manager.urllib.request.OpenerDirector.open",
            side_effect=urllib.error.HTTPError("http://127.0.0.1:8100/health", 404, "Not Found", {}, None),
        ):
            self.assertFalse(manager.check_app_health(app))

    def test_logs_returns_recent_app_specific_output(self):
        app = self.add_app()["app"]
        log_dir = Path(app["log_path"])
        log_dir.mkdir(parents=True)
        (log_dir / "stdout.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (log_dir / "stderr.log").write_text("warning\n", encoding="utf-8")
        result = manager.logs_app(app["id"], lines=2)
        self.assertIn("[stdout]\ntwo\nthree", result["logs"])
        self.assertIn("[stderr]\nwarning", result["logs"])

    def test_build_subprocess_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            release = Path(tmp) / "release"
            repo.mkdir()
            release.mkdir()
            (repo / "package.json").write_text('{"dependencies":{"next":"^15"}}', encoding="utf-8")
            failed = subprocess.CompletedProcess(args=["npm"], returncode=2, stdout="", stderr="compile failed")

            def runner(command, cwd, timeout):
                return failed

            with self.assertRaisesRegex(RuntimeError, "compile failed"):
                NextJSAdapter().build({"port": 8100}, repo, release, runner=runner, timeout=1)


class UninstallScriptTests(unittest.TestCase):
    def _run_uninstall(self, temp_root: Path, *args: str) -> subprocess.CompletedProcess:
        env = dict(__import__("os").environ)
        env.update({
            "APP_MANAGER_PLUGIN_DEST": str(temp_root / "usr" / "share" / "cockpit" / "app-manager"),
            "APP_MANAGER_BACKEND_DEST": str(temp_root / "usr" / "libexec" / "cockpit"),
            "APP_MANAGER_CONFIG_DEST": str(temp_root / "etc" / "cockpit" / "app-manager"),
            "APP_MANAGER_STATE_ROOT": str(temp_root / "var" / "lib" / "cockpit" / "app-manager"),
            "APP_MANAGER_PRODUCTION_ROOT": str(temp_root / "opt" / "webapps"),
            "APP_MANAGER_LOG_ROOT": str(temp_root / "var" / "log" / "cockpit-app-manager"),
            "APP_MANAGER_SYSTEMD_DIR": str(temp_root / "etc" / "systemd" / "system"),
        })
        script = Path(__file__).resolve().parent.parent / "scripts" / "uninstall.sh"
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        command = [str(git_bash), str(script), *args] if git_bash.exists() else ["bash", str(script), *args]
        return subprocess.run(command, capture_output=True, text=True, env=env)

    def test_default_uninstall_preserves_app_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            plugin = temp_root / "usr" / "share" / "cockpit" / "app-manager"
            backend = temp_root / "usr" / "libexec" / "cockpit"
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            production_root = temp_root / "opt" / "webapps"
            plugin.mkdir(parents=True)
            backend.mkdir(parents=True)
            (backend / "app-manager").write_text("#!/bin/bash\n", encoding="utf-8")
            (backend / "adapters.py").write_text("print('x')\n", encoding="utf-8")
            config = temp_root / "etc" / "cockpit" / "app-manager"
            config.mkdir(parents=True)
            state_root.mkdir(parents=True)
            (state_root / "apps.json").write_text('{"apps": [{"id": "demo"}]}', encoding="utf-8")
            (production_root / "demo").mkdir(parents=True)
            result = self._run_uninstall(temp_root)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(plugin.exists())
            self.assertFalse((backend / "app-manager").exists())
            self.assertFalse((backend / "adapters.py").exists())
            self.assertFalse(config.exists())
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue((production_root / "demo").exists())

    def test_explicit_data_removal_targets_only_app_manager_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            production_root = temp_root / "opt" / "webapps"
            log_root = temp_root / "var" / "log" / "cockpit-app-manager"
            systemd_dir = temp_root / "etc" / "systemd" / "system"
            state_root.mkdir(parents=True)
            (state_root / "sources").mkdir(parents=True)
            (state_root / "sources" / "demo").mkdir(parents=True)
            (state_root / "homes").mkdir(parents=True)
            (state_root / "homes" / "demo").mkdir(parents=True)
            (state_root / "apps.json").write_text('{"apps": [{"id": "demo"}]}', encoding="utf-8")
            (production_root / "demo").mkdir(parents=True)
            (production_root / "shared").mkdir(parents=True)
            (log_root / "demo").mkdir(parents=True)
            systemd_dir.mkdir(parents=True)
            (systemd_dir / "cockpit-app-demo.service").write_text("[Unit]\n", encoding="utf-8")
            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse((production_root / "demo").exists())
            self.assertTrue((production_root / "shared").exists())
            self.assertFalse((state_root / "sources" / "demo").exists())
            self.assertFalse((state_root / "apps.json").exists())
            self.assertFalse((systemd_dir / "cockpit-app-demo.service").exists())

    def test_dry_run_does_not_change_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            production_root = temp_root / "opt" / "webapps"
            state_root.mkdir(parents=True)
            (state_root / "apps.json").write_text('{"apps": [{"id": "demo"}]}', encoding="utf-8")
            (production_root / "demo").mkdir(parents=True)
            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes", "--dry-run")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue((production_root / "demo").exists())


if __name__ == "__main__":
    unittest.main()
