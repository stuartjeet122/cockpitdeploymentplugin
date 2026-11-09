from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, call as mock_call, patch

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
        cases = (
            ("nextjs-package", {"package.json": '{"dependencies":{"next":"^15"}}'}, "nextjs"),
            ("nextjs-config", {"next.config.mjs": "export default {}"}, "nextjs"),
            ("go", {"go.mod": "module example.com/demo\n"}, "go"),
            ("python-requirements", {"requirements.txt": "flask\n"}, "python"),
            ("python-pyproject", {"pyproject.toml": "[project]\nname = 'demo'\n"}, "python"),
            ("node", {"package.json": '{"scripts":{"start":"node server.js"}}'}, "node"),
            ("static", {"index.html": "<html></html>"}, "static"),
        )
        for name, files, expected in cases:
            with self.subTest(framework=name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                for filename, contents in files.items():
                    (repo / filename).write_text(contents, encoding="utf-8")
                self.assertEqual(detect_framework(repo), expected)

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

    def test_registry_structure_corruption_is_reported_instead_of_reset(self):
        registry_path = Path(manager.DEFAULT_REGISTRY_PATH)
        registry_path.parent.mkdir(parents=True)
        for payload in ([], {}, {"apps": None}, {"apps": [None]}):
            with self.subTest(payload=payload):
                registry_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    manager.get_registry()

    def test_registry_write_is_atomic_and_creates_private_json(self):
        manager.save_registry({"apps": []})
        registry_path = Path(manager.DEFAULT_REGISTRY_PATH)
        self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), {"apps": []})
        self.assertFalse(list(registry_path.parent.glob(".apps.json.*.tmp")))

    @unittest.skipIf(os.name == "nt", "POSIX file modes are not available")
    def test_registry_file_permissions_are_private(self):
        manager.save_registry({"apps": []})
        registry_path = Path(manager.DEFAULT_REGISTRY_PATH)
        self.assertEqual(registry_path.stat().st_mode & 0o777, 0o600)

    def test_failed_atomic_registry_replacement_preserves_existing_json(self):
        registry_path = Path(manager.DEFAULT_REGISTRY_PATH)
        manager.save_registry({"apps": []})
        original_contents = registry_path.read_bytes()

        with patch.object(manager.os, "replace", side_effect=OSError("simulated replacement failure")):
            with self.assertRaisesRegex(OSError, "simulated replacement failure"):
                manager.save_registry({"apps": [{"id": "demo"}]})

        self.assertEqual(registry_path.read_bytes(), original_contents)
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

        app = {"port": 8100, "health_check_url": "/health"}
        with patch(
            "backend.app_manager.urllib.request.OpenerDirector.open",
            return_value=contextlib.nullcontext(Response()),
        ):
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

            runner = Mock(return_value=failed)

            with self.assertRaisesRegex(RuntimeError, "compile failed"):
                NextJSAdapter().build({"port": 8100}, repo, release, runner=runner, timeout=1)
            runner.assert_called_once()


class InfrastructureConfigurationTests(unittest.TestCase):
    def test_generated_service_has_no_network_capability_and_is_hardened(self):
        config = manager._default_config()
        config["production_root"] = "/opt/webapps"
        config["log_root"] = "/var/log/cockpit-app-manager"
        config["source_root"] = "/var/lib/cockpit/app-manager/sources"
        app = {"id": "demo", "service_user": "app-demo", "port": 8100}
        service = manager._render_service(app, ["/opt/webapps/demo/current/app/server"], config)
        paths = manager._app_paths("demo", config)
        self.assertIn("User=app-demo", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ProtectHome=true", service)
        self.assertIn("PrivateDevices=true", service)
        self.assertIn("CapabilityBoundingSet=\n", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", service)
        self.assertIn("Environment=\"APP_BASE_PATH=/demo\"", service)
        self.assertIn(f"ReadOnlyPaths={manager._systemd_quote(str(paths['releases']))}", service)
        template = (Path(__file__).resolve().parent.parent / "templates" / "app.service.tpl").read_text(
            encoding="utf-8"
        )
        self.assertIn("ReadOnlyPaths={{ app_root }}/releases", template)
        self.assertIn("CapabilityBoundingSet=", template)
        self.assertNotIn("CAP_NET_BIND_SERVICE", service)
        self.assertNotIn("AmbientCapabilities=", service)
        expected_write_paths = "ReadWritePaths=" + " ".join(
            manager._systemd_quote(str(path))
            for path in (paths["home"], paths["config"], paths["logs"])
        )
        self.assertIn(expected_write_paths, service)
        read_write_paths = next(line for line in service.splitlines() if line.startswith("ReadWritePaths="))
        self.assertNotIn(manager._systemd_quote(str(paths["current"] / "app")), read_write_paths)
        self.assertNotIn(manager._systemd_quote(str(paths["releases"])), read_write_paths)

    def test_release_sealing_removes_app_write_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "release"
            app_dir = release / "app"
            app_dir.mkdir(parents=True)
            executable = app_dir / "server"
            executable.write_text("binary", encoding="utf-8")
            executable.chmod(0o750)
            private_file = app_dir / "settings.json"
            private_file.write_text("{}", encoding="utf-8")
            private_file.chmod(0o600)

            original_stat = Path.stat

            def posix_stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                modes = {executable: 0o750, private_file: 0o600}
                if path in modes:
                    values = list(result)
                    values[0] = (values[0] & ~0o777) | modes[path]
                    return os.stat_result(values)
                return result

            with patch.object(manager.os, "chown", create=True) as chown, patch.object(
                manager.os, "chmod"
            ) as chmod, patch.object(Path, "stat", new=posix_stat):
                manager._seal_release(release, 4321)

            self.assertIn(mock_call(str(release), 0o550), chmod.call_args_list)
            self.assertIn(mock_call(str(app_dir), 0o550), chmod.call_args_list)
            self.assertIn(mock_call(str(executable), 0o550), chmod.call_args_list)
            self.assertIn(mock_call(str(private_file), 0o440), chmod.call_args_list)
            self.assertTrue(all(call.args[1] == 0 and call.args[2] == 4321 for call in chown.call_args_list))

    def test_static_release_sealing_is_readable_by_nginx_but_not_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "release"
            app_dir = release / "app"
            app_dir.mkdir(parents=True)
            page = app_dir / "index.html"
            page.write_text("page", encoding="utf-8")
            page.chmod(0o644)

            original_stat = Path.stat

            def posix_stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path == page:
                    values = list(result)
                    values[0] = (values[0] & ~0o777) | 0o644
                    return os.stat_result(values)
                return result

            with patch.object(manager.os, "chown", create=True), patch.object(
                manager.os, "chmod"
            ) as chmod, patch.object(Path, "stat", new=posix_stat):
                manager._seal_release(release, 4321, public=True)

            self.assertIn(mock_call(str(release), 0o555), chmod.call_args_list)
            self.assertIn(mock_call(str(app_dir), 0o555), chmod.call_args_list)
            self.assertIn(mock_call(str(page), 0o444), chmod.call_args_list)

    @unittest.skipIf(os.name == "nt", "release symlink validation requires POSIX symlink semantics")
    def test_static_release_sealing_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "release"
            app_dir = release / "app"
            app_dir.mkdir(parents=True)
            (root / "outside").write_text("outside", encoding="utf-8")
            (app_dir / "outside-link").symlink_to(root / "outside")

            with patch.object(manager.os, "chown"):
                with self.assertRaisesRegex(RuntimeError, "symlink escapes"):
                    manager._seal_release(release, 4321, public=True)

    def test_generated_systemd_unit_passes_systemd_analyzer_when_available(self):
        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze is not installed in this development environment")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = manager._default_config()
            config["production_root"] = str(root / "opt" / "webapps")
            config["source_root"] = str(root / "state" / "sources")
            config["log_root"] = str(root / "logs")
            app = {"id": "demo", "service_user": "app-demo", "port": 8100}
            with patch.object(manager, "DEFAULT_STATE_ROOT", str(root / "state")):
                paths = manager._app_paths("demo", config)
                for path in (
                    paths["production"],
                    paths["releases"],
                    paths["current"] / "app",
                    paths["home"],
                    paths["config"],
                    paths["logs"],
                ):
                    path.mkdir(parents=True, exist_ok=True)
                unit_path = root / "cockpit-app-demo.service"
                unit_path.write_text(
                    manager._render_service(app, ["/bin/true"], config),
                    encoding="utf-8",
                )
                generated_unit = unit_path.read_text(encoding="utf-8")
                self.assertIn(
                    f"WorkingDirectory={paths['current'] / 'app'}",
                    generated_unit,
                )
            result = subprocess.run(
                [analyzer, "verify", str(unit_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_nginx_prefix_generation_is_framework_aware_and_handles_websockets(self):
        template = Path(__file__).resolve().parent.parent / "templates" / "nginx.conf.tpl"
        app_id = "website1"
        nextjs = manager._render_nginx_config(app_id, 8123, template, "nextjs")
        request_paths = (
            "/website1/",
            "/website1/about",
            "/website1/api/items?page=1",
            "/website1/_next/static/chunks/app.js",
            "/website1/socket.io/?EIO=4&transport=websocket",
        )
        self.assertIn("return 308 /website1/;", nextjs)
        self.assertIn("location ^~ /website1/ {", nextjs)
        next_proxy_pass = next(line for line in nextjs.splitlines() if "proxy_pass " in line)
        self.assertTrue(next_proxy_pass.endswith("8123;"))
        self.assertIn("proxy_http_version 1.1;", nextjs)
        self.assertIn("proxy_read_timeout 3600s;", nextjs)
        self.assertIn("proxy_send_timeout 3600s;", nextjs)
        self.assertIn("proxy_set_header Host $host;", nextjs)
        self.assertIn("proxy_pass http://127.0.0.1:8123;", nextjs)
        self.assertNotIn("proxy_pass http://127.0.0.1:8123/;", nextjs)
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", nextjs)
        self.assertIn("proxy_set_header Connection $app_manager_connection_upgrade;", nextjs)
        self.assertIn("set $app_manager_connection_upgrade upgrade;", nextjs)
        self.assertIn('if ($http_upgrade = "") {', nextjs)
        self.assertIn('set $app_manager_connection_upgrade "";', nextjs)
        self.assertNotIn("$http_connection", nextjs)
        self.assertIn("proxy_set_header X-Forwarded-Prefix /website1;", nextjs)
        next_proxy_uri = next_proxy_pass.split("8123", 1)[1].rstrip(";")
        for request_path in request_paths:
            upstream_path = (
                request_path
                if not next_proxy_uri
                else next_proxy_uri + request_path[len("/website1/"):]
            )
            self.assertEqual(request_path, upstream_path)

        for framework in ("go", "node", "python"):
            stripped_prefix = manager._render_nginx_config(app_id, 8123, template, framework)
            proxy_pass = next(line for line in stripped_prefix.splitlines() if "proxy_pass " in line)
            proxy_uri = proxy_pass.split("8123", 1)[1].rstrip(";")
            self.assertIn("proxy_pass http://127.0.0.1:8123/;", stripped_prefix)
            self.assertIn("proxy_set_header X-Forwarded-Prefix /website1;", stripped_prefix)
            self.assertIn("proxy_redirect / /website1/;", stripped_prefix)
            self.assertIn("proxy_set_header Connection $app_manager_connection_upgrade;", stripped_prefix)
            self.assertNotIn("$http_connection", stripped_prefix)
            for request_path, expected_upstream_path in (
                ("/website1/", "/"),
                ("/website1/about", "/about"),
                ("/website1/api/items?page=1", "/api/items?page=1"),
                ("/website1/_next/static/chunks/app.js", "/_next/static/chunks/app.js"),
            ):
                self.assertEqual(
                    proxy_uri + request_path[len("/website1/"):],
                    expected_upstream_path,
                )

        config = manager._default_config()
        config["production_root"] = "/opt/webapps"
        static = manager._render_nginx_config(app_id, 8123, template, "static", config=config)
        static_root = manager._app_paths(app_id, config)["current"] / "app"
        self.assertIn(f"alias {manager._nginx_quote(str(static_root) + os.sep)};", static)
        self.assertIn("try_files $uri $uri/ /website1/index.html;", static)
        for request_path, expected_file in (
            ("/website1/", static_root / "index.html"),
            ("/website1/about", static_root / "index.html"),
            ("/website1/css/site.css", static_root / "css" / "site.css"),
            ("/website1/js/app.js", static_root / "js" / "app.js"),
            ("/website1/images/logo.png", static_root / "images" / "logo.png"),
        ):
            request_relative_path = request_path[len("/website1/"):]
            mapped_file = (
                static_root / "index.html"
                if request_relative_path in ("", "about")
                else static_root / request_relative_path
            )
            self.assertEqual(expected_file, mapped_file)
        self.assertNotIn("proxy_pass", static)
        self.assertNotIn("rewrite ", static)
        self.assertNotIn("proxy_pass http://127.0.0.1:8123/;", nextjs)
        with self.assertRaises(ValueError):
            manager._render_nginx_config("../outside", 8123, template, "nextjs")
        with self.assertRaises(ValueError):
            manager._render_nginx_config("demo", 8080, template, "nextjs")

    def test_generated_nginx_config_passes_nginx_parser_when_available(self):
        nginx = shutil.which("nginx")
        if nginx is None:
            self.skipTest("nginx is not installed in this development environment")
        template = Path(__file__).resolve().parent.parent / "templates" / "nginx.conf.tpl"
        snippet = manager._render_nginx_config("website1", 8123, template, "nextjs")
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "nginx.conf"
            config.write_text(
                "pid " + str(Path(tmp) / "nginx.pid") + ";\n"
                "error_log stderr;\n"
                "events { worker_connections 16; }\n"
                "http { access_log off; server { listen 127.0.0.1:8080;\n"
                + snippet
                + "\n} }\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [nginx, "-t", "-c", str(config)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_installer_uses_exact_file_ownership_and_checks_host_and_installed_imports(self):
        project = Path(__file__).resolve().parent.parent
        installer = (project / "scripts" / "install.sh").read_text(encoding="utf-8")
        uninstaller = (project / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
        cockpit_page = (project / "cockpit" / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(installer, r"\bchown\s+-R\b")
        self.assertNotIn('chown root:root "$BACKEND_DEST"', installer)
        self.assertNotIn('chown root:root "$PRODUCTION_ROOT"', installer)
        self.assertIn('BACKEND_DEST="${APP_MANAGER_BACKEND_DEST:-/usr/libexec/cockpit/app-manager}"', installer)
        self.assertIn('mkdir -p -- "$BACKEND_DEST"', installer)
        self.assertIn(
            'install -o root -g root -m 0755 "$APP_DIR/backend/app_manager.py" "$BACKEND_DEST/app-manager"',
            installer,
        )
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/backend/adapters.py" "$BACKEND_DEST/adapters.py"',
            installer,
        )
        self.assertIn(
            'BACKEND_DEST="${APP_MANAGER_BACKEND_DEST:-/usr/libexec/cockpit/app-manager}"',
            uninstaller,
        )
        self.assertIn(
            'cockpit.spawn(["/usr/libexec/cockpit/app-manager/app-manager"',
            cockpit_page,
        )
        self.assertNotIn(
            'cockpit.spawn(["/usr/libexec/cockpit/app-manager",',
            cockpit_page,
        )
        self.assertIn(
            'remove_owned_file "${BACKEND_DEST}/app-manager" \'APP_NAME = "app-manager"\'',
            uninstaller,
        )
        self.assertIn(
            'remove_owned_file "${BACKEND_DEST}/adapters.py" "class NextJSAdapter"',
            uninstaller,
        )
        self.assertIn('remove_empty_directory "$BACKEND_DEST"', uninstaller)
        self.assertIn('if [[ "$BACKEND_DEST" == "/usr/libexec/cockpit" ]]', uninstaller)
        self.assertIn("dpkg --compare-versions", installer)
        self.assertIn("Python 3.9 or newer is required", installer)
        self.assertIn('python3 "$BACKEND_DEST/app-manager" --help', installer)
        self.assertIn('"$STATE_ROOT/apps.json" "$STATE_ROOT/config.json"', installer)
        self.assertIn('chmod 0600 "$STATE_ROOT/apps.json" "$STATE_ROOT/config.json"', installer)
        self.assertIn('if [[ ! -e "$STATE_ROOT/config.json" ]]', installer)
        self.assertIn('superuser: "require"', cockpit_page)

    def test_standalone_frontend_assets_are_referenced_and_installed(self):
        project = Path(__file__).resolve().parent.parent
        cockpit_dir = project / "cockpit"
        cockpit_page = (cockpit_dir / "index.html").read_text(encoding="utf-8")
        stylesheet_path = cockpit_dir / "app-manager.css"
        installer = (project / "scripts" / "install.sh").read_text(encoding="utf-8")
        stylesheet = stylesheet_path.read_text(encoding="utf-8")

        self.assertTrue(stylesheet_path.is_file())
        self.assertIn('<link rel="stylesheet" href="app-manager.css">', cockpit_page)
        self.assertIn('<script src="app-manager.js"></script>', cockpit_page)
        self.assertNotIn("/cockpit/static/desktop.css", cockpit_page)
        self.assertNotIn("<style", cockpit_page)
        self.assertTrue((cockpit_dir / "app-manager.js").is_file())
        self.assertNotIn("@import", stylesheet)
        self.assertNotRegex(stylesheet, r"url\(\s*['\"]?https?://")
        self.assertIn(".am-app-manager", stylesheet)
        self.assertIn("@media (prefers-color-scheme: dark)", stylesheet)
        self.assertIn('[[ -f "$APP_DIR/cockpit/app-manager.css" ]]', installer)
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.css" "$PLUGIN_DEST/app-manager.css"',
            installer,
        )
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.js" "$PLUGIN_DEST/app-manager.js"',
            installer,
        )


class UninstallScriptTests(unittest.TestCase):
    def _write_registry(self, temp_root: Path, app_id: str, production_path=None) -> Path:
        state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
        production_root = temp_root / "opt" / "webapps"
        log_root = temp_root / "var" / "log" / "cockpit-app-manager"
        app = {
            "id": app_id,
            "source_path": str(state_root / "sources" / app_id),
            "production_path": str(production_path or production_root / app_id),
            "log_path": str(log_root / app_id),
        }
        registry_path = state_root / "apps.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps({"apps": [app]}), encoding="utf-8")
        return registry_path

    def _run_uninstall(
        self,
        temp_root: Path,
        *args: str,
        supplementary_group: bool = False,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        fake_bin = temp_root / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$APP_MANAGER_TEST_SYSTEMCTL_LOG\"\nexit 0\n",
            encoding="utf-8",
        )
        getent = fake_bin / "getent"
        getent.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"group\" ] && [ -z \"$2\" ]; then\n"
            "  printf 'app-demo:x:1234:\\n'\n"
            "  if [ -n \"$APP_MANAGER_TEST_SUPPLEMENTARY_GROUP\" ]; then\n"
            "    printf 'unrelated:x:9999:%s\\n' \"$APP_MANAGER_TEST_SUPPLEMENTARY_GROUP\"\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "case \"$1:$2\" in\n"
            "  passwd:app-demo) printf 'app-demo:x:1234:1234::%s/homes/demo:/usr/sbin/nologin\\n' \"$APP_MANAGER_STATE_ROOT\" ;;\n"
            "  group:app-demo) printf 'app-demo:x:1234:\\n' ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        userdel = fake_bin / "userdel"
        userdel.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$APP_MANAGER_TEST_USERDEL_LOG\"\n",
            encoding="utf-8",
        )
        for executable in (systemctl, getent, userdel):
            executable.chmod(0o755)
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        systemctl_log = temp_root / "systemctl.log"
        userdel_log = temp_root / "userdel.log"
        env.update({
            "APP_MANAGER_PLUGIN_DEST": str(temp_root / "usr" / "share" / "cockpit" / "app-manager"),
            "APP_MANAGER_BACKEND_DEST": str(temp_root / "usr" / "libexec" / "cockpit" / "app-manager"),
            "APP_MANAGER_CONFIG_DEST": str(temp_root / "etc" / "cockpit" / "app-manager"),
            "APP_MANAGER_STATE_ROOT": str(temp_root / "var" / "lib" / "cockpit" / "app-manager"),
            "APP_MANAGER_PRODUCTION_ROOT": str(temp_root / "opt" / "webapps"),
            "APP_MANAGER_LOG_ROOT": str(temp_root / "var" / "log" / "cockpit-app-manager"),
            "APP_MANAGER_SYSTEMD_DIR": str(temp_root / "etc" / "systemd" / "system"),
            "APP_MANAGER_NGINX_SNIPPET_DIR": str(temp_root / "etc" / "nginx" / "snippets" / "cockpit-app-manager"),
            "APP_MANAGER_NGINX_CONFIG_ROOT": str(temp_root / "etc" / "nginx"),
            "APP_MANAGER_TEST_SYSTEMCTL_LOG": str(systemctl_log),
            "APP_MANAGER_TEST_USERDEL_LOG": str(userdel_log),
            "APP_MANAGER_TEST_SUPPLEMENTARY_GROUP": (
                "app-demo" if supplementary_group else ""
            ),
        })
        script = Path(__file__).resolve().parent.parent / "scripts" / "uninstall.sh"
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        command = [str(git_bash), str(script), *args] if git_bash.exists() else ["bash", str(script), *args]
        return subprocess.run(command, capture_output=True, text=True, env=env)

    def test_default_uninstall_preserves_app_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            plugin = temp_root / "usr" / "share" / "cockpit" / "app-manager"
            backend = temp_root / "usr" / "libexec" / "cockpit" / "app-manager"
            shared_backend = backend.parent
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            production_root = temp_root / "opt" / "webapps"
            plugin.mkdir(parents=True)
            backend.mkdir(parents=True)
            (plugin / "app-manager.css").write_text(
                "/* Cockpit App Manager standalone stylesheet */\n",
                encoding="utf-8",
            )
            (backend / "app-manager").write_text(
                '#!/usr/bin/env python3\nAPP_NAME = "app-manager"\n',
                encoding="utf-8",
            )
            (backend / "adapters.py").write_text(
                "class NextJSAdapter:\n    pass\n",
                encoding="utf-8",
            )
            config = temp_root / "etc" / "cockpit" / "app-manager"
            config.mkdir(parents=True)
            state_root.mkdir(parents=True)
            self._write_registry(temp_root, "demo")
            (production_root / "demo").mkdir(parents=True)
            result = self._run_uninstall(temp_root)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(plugin.exists())
            self.assertFalse(backend.exists())
            self.assertTrue(shared_backend.exists())
            self.assertFalse((backend / "app-manager").exists())
            self.assertFalse((backend / "adapters.py").exists())
            self.assertFalse(config.exists())
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue((production_root / "demo").exists())

    def test_default_uninstall_preserves_unrecognized_files_in_plugin_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            plugin = temp_root / "usr" / "share" / "cockpit" / "app-manager"
            backend = temp_root / "usr" / "libexec" / "cockpit" / "app-manager"
            shared_backend = backend.parent
            plugin.mkdir(parents=True)
            backend.mkdir(parents=True)
            (plugin / "index.html").write_text("<title>App Manager</title>", encoding="utf-8")
            unrelated_plugin_file = plugin / "local-customization.js"
            unrelated_plugin_file.write_text("custom", encoding="utf-8")
            (backend / "app-manager").write_text(
                '#!/usr/bin/env python3\nAPP_NAME = "app-manager"\n',
                encoding="utf-8",
            )
            unrelated_backend_file = backend / "other-tool"
            unrelated_shared_file = shared_backend / "other-tool"
            unrelated_backend_file.write_text("keep", encoding="utf-8")
            unrelated_shared_file.write_text("keep", encoding="utf-8")

            result = self._run_uninstall(temp_root)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(unrelated_plugin_file.exists())
            self.assertTrue(unrelated_backend_file.exists())
            self.assertTrue(unrelated_shared_file.exists())
            self.assertTrue(plugin.exists())
            self.assertTrue(backend.exists())
            self.assertTrue(shared_backend.exists())
            self.assertFalse((plugin / "index.html").exists())
            self.assertFalse((backend / "app-manager").exists())

    def test_default_uninstall_preserves_unverified_managed_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            plugin = temp_root / "usr" / "share" / "cockpit" / "app-manager"
            plugin.mkdir(parents=True)
            replaced_file = plugin / "index.html"
            replaced_file.write_text("<title>Another plugin</title>", encoding="utf-8")
            stylesheet = plugin / "app-manager.css"
            stylesheet.write_text("body { color: red; }\n", encoding="utf-8")

            result = self._run_uninstall(temp_root)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(replaced_file.exists())
            self.assertTrue(stylesheet.exists())
            self.assertIn("Leaving unverified installation file", result.stderr)

    def test_default_uninstall_dry_run_reports_only_verified_files_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            plugin = temp_root / "usr" / "share" / "cockpit" / "app-manager"
            backend = temp_root / "usr" / "libexec" / "cockpit" / "app-manager"
            shared_backend = backend.parent
            config = temp_root / "etc" / "cockpit" / "app-manager"
            templates = config / "templates"
            plugin.mkdir(parents=True)
            backend.mkdir(parents=True)
            templates.mkdir(parents=True)
            (plugin / "index.html").write_text("<title>App Manager</title>", encoding="utf-8")
            (plugin / "app-manager.css").write_text(
                "/* Cockpit App Manager standalone stylesheet */\n",
                encoding="utf-8",
            )
            unknown_plugin_file = plugin / "local.js"
            unknown_plugin_file.write_text("custom", encoding="utf-8")
            (backend / "app-manager").write_text(
                '#!/usr/bin/env python3\nAPP_NAME = "app-manager"\n',
                encoding="utf-8",
            )
            (backend / "adapters.py").write_text(
                "class NextJSAdapter:\n    pass\n",
                encoding="utf-8",
            )
            (templates / "nginx.conf.tpl").write_text(
                "{{ app_route_location }}\n",
                encoding="utf-8",
            )
            (templates / "app.service.tpl").write_text(
                "# Managed by Cockpit App Manager\n",
                encoding="utf-8",
            )

            result = self._run_uninstall(temp_root, "--dry-run")

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            normalized_output = result.stdout.replace("\\", "/")
            for operation in (
                f"DRY-RUN: remove {plugin / 'index.html'}",
                f"DRY-RUN: remove {plugin / 'app-manager.css'}",
                f"DRY-RUN: remove {backend / 'app-manager'}",
                f"DRY-RUN: remove {backend / 'adapters.py'}",
                f"DRY-RUN: rmdir -- {backend}",
                f"DRY-RUN: remove {templates / 'nginx.conf.tpl'}",
                f"DRY-RUN: rmdir -- {templates}",
            ):
                self.assertIn(operation.replace("\\", "/"), normalized_output)
            self.assertNotIn(str(unknown_plugin_file), result.stdout)
            self.assertTrue((plugin / "index.html").exists())
            self.assertTrue((backend / "app-manager").exists())
            self.assertTrue(shared_backend.exists())
            self.assertTrue((templates / "nginx.conf.tpl").exists())
            self.assertTrue(unknown_plugin_file.exists())

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
            self._write_registry(temp_root, "demo")
            (production_root / "demo").mkdir(parents=True)
            (production_root / "shared").mkdir(parents=True)
            (log_root / "demo").mkdir(parents=True)
            nginx_root = temp_root / "etc" / "nginx"
            snippet_root = nginx_root / "snippets" / "cockpit-app-manager"
            snippet_root.mkdir(parents=True)
            managed_snippet = snippet_root / "demo.conf"
            managed_snippet.write_text(
                "# Managed by Cockpit App Manager: demo\nlocation /demo/ {}\n",
                encoding="utf-8",
            )
            systemd_dir.mkdir(parents=True)
            (systemd_dir / "cockpit-app-demo.service").write_text(
                "[Unit]\n# Managed by Cockpit App Manager\nDescription=Cockpit App Manager - demo\n[Service]\nUser=app-demo\n",
                encoding="utf-8",
            )
            (systemd_dir / "cockpit-app-unregistered.service").write_text(
                "[Unit]\nDescription=Unrelated service\n[Service]\nUser=app-unregistered\n",
                encoding="utf-8",
            )
            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse((production_root / "demo").exists())
            self.assertTrue((production_root / "shared").exists())
            self.assertFalse((state_root / "sources" / "demo").exists())
            self.assertFalse((state_root / "apps.json").exists())
            self.assertFalse((systemd_dir / "cockpit-app-demo.service").exists())
            self.assertTrue((systemd_dir / "cockpit-app-unregistered.service").exists())
            self.assertTrue((state_root / "sources").exists())
            self.assertTrue((log_root).exists())
            self.assertTrue((production_root).exists())
            self.assertFalse(managed_snippet.exists())
            userdel_log = (temp_root / "userdel.log").read_text(encoding="utf-8")
            self.assertIn("app-demo", userdel_log)
            systemctl_log = (temp_root / "systemctl.log").read_text(encoding="utf-8")
            self.assertIn("disable --now cockpit-app-demo.service", systemctl_log)
            self.assertIn("daemon-reload", systemctl_log)

    def test_active_nginx_include_preserves_all_app_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            production_root = temp_root / "opt" / "webapps"
            systemd_dir = temp_root / "etc" / "systemd" / "system"
            nginx_root = temp_root / "etc" / "nginx"
            snippet_root = nginx_root / "snippets" / "cockpit-app-manager"
            app_dir = production_root / "demo"
            app_dir.mkdir(parents=True)
            state_root.mkdir(parents=True)
            self._write_registry(temp_root, "demo")
            snippet_root.mkdir(parents=True)
            snippet = snippet_root / "demo.conf"
            snippet.write_text(
                "# Managed by Cockpit App Manager: demo\nlocation /demo/ {}\n",
                encoding="utf-8",
            )
            (nginx_root / "sites-enabled").mkdir(parents=True)
            (nginx_root / "sites-enabled" / "site.conf").write_text(
                "include snippets/cockpit-app-manager/demo.conf;\n",
                encoding="utf-8",
            )
            systemd_dir.mkdir(parents=True)
            unit = systemd_dir / "cockpit-app-demo.service"
            unit.write_text(
                "[Unit]\n# Managed by Cockpit App Manager\nDescription=Cockpit App Manager - demo\n[Service]\nUser=app-demo\n",
                encoding="utf-8",
            )

            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes")

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue(app_dir.exists())
            self.assertTrue(snippet.exists())
            self.assertTrue(unit.exists())
            self.assertFalse((temp_root / "userdel.log").exists())
            self.assertFalse((temp_root / "systemctl.log").exists())
            self.assertIn("Preserving active Nginx snippet", result.stderr)

    def test_unverified_nginx_snippet_preserves_registered_app_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            app_dir = temp_root / "opt" / "webapps" / "demo"
            snippet_root = temp_root / "etc" / "nginx" / "snippets" / "cockpit-app-manager"
            state_root.mkdir(parents=True)
            app_dir.mkdir(parents=True)
            self._write_registry(temp_root, "demo")
            snippet_root.mkdir(parents=True)
            snippet = snippet_root / "demo.conf"
            snippet.write_text("location /demo/ {}\n", encoding="utf-8")

            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes")

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue(app_dir.exists())
            self.assertTrue(snippet.exists())
            self.assertIn("unverified Nginx snippet", result.stderr)

    def test_unverified_service_preserves_app_data_and_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            app_dir = temp_root / "opt" / "webapps" / "demo"
            systemd_dir = temp_root / "etc" / "systemd" / "system"
            state_root.mkdir(parents=True)
            app_dir.mkdir(parents=True)
            self._write_registry(temp_root, "demo")
            systemd_dir.mkdir(parents=True)
            unit = systemd_dir / "cockpit-app-demo.service"
            unit.write_text(
                "[Unit]\nDescription=Cockpit App Manager - demo\n[Service]\nUser=app-demo\n",
                encoding="utf-8",
            )

            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes")

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue(app_dir.exists())
            self.assertTrue(unit.exists())
            self.assertFalse((temp_root / "userdel.log").exists())

    def test_user_with_unrelated_group_membership_is_not_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            state_root.mkdir(parents=True)
            self._write_registry(temp_root, "demo")
            result = self._run_uninstall(
                temp_root,
                "--remove-managed-data",
                "--yes",
                supplementary_group=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((state_root / "apps.json").exists())
            self.assertFalse((temp_root / "userdel.log").exists())
            self.assertIn("additional group", result.stderr)

    def test_explicit_cleanup_uses_registered_managed_path_not_same_id_at_default_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            production_root = temp_root / "opt" / "webapps"
            custom_app = production_root / "custom-root" / "demo"
            unrelated_app = production_root / "demo"
            custom_app.mkdir(parents=True)
            unrelated_app.mkdir(parents=True)
            self._write_registry(temp_root, "demo", custom_app)

            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes")

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(custom_app.exists())
            self.assertTrue(unrelated_app.exists())

    def test_invalid_registry_ids_abort_destructive_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            production_root = temp_root / "opt" / "webapps"
            state_root.mkdir(parents=True)
            (state_root / "apps.json").write_text(
                '{"apps": [{"id": "../../outside"}]}',
                encoding="utf-8",
            )
            (production_root / "outside").mkdir(parents=True)
            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue((production_root / "outside").exists())

    def test_dry_run_does_not_change_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            state_root = temp_root / "var" / "lib" / "cockpit" / "app-manager"
            production_root = temp_root / "opt" / "webapps"
            state_root.mkdir(parents=True)
            self._write_registry(temp_root, "demo")
            source_path = state_root / "sources" / "demo"
            home_path = state_root / "homes" / "demo"
            log_path = temp_root / "var" / "log" / "cockpit-app-manager" / "demo"
            source_path.mkdir(parents=True)
            home_path.mkdir(parents=True)
            log_path.mkdir(parents=True)
            (production_root / "demo").mkdir(parents=True)
            snippet_root = temp_root / "etc" / "nginx" / "snippets" / "cockpit-app-manager"
            snippet_root.mkdir(parents=True)
            (snippet_root / "demo.conf").write_text(
                "# Managed by Cockpit App Manager: demo\nlocation /demo/ {}\n",
                encoding="utf-8",
            )
            systemd_dir = temp_root / "etc" / "systemd" / "system"
            systemd_dir.mkdir(parents=True)
            (systemd_dir / "cockpit-app-demo.service").write_text(
                "[Unit]\n# Managed by Cockpit App Manager\nDescription=Cockpit App Manager - demo\n[Service]\nUser=app-demo\n",
                encoding="utf-8",
            )
            result = self._run_uninstall(temp_root, "--remove-managed-data", "--yes", "--dry-run")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertIn("userdel -- app-demo", result.stdout)
            self.assertIn("systemctl disable --now cockpit-app-demo.service", result.stdout)
            self.assertIn("systemctl daemon-reload", result.stdout)
            self.assertIn("demo.conf", result.stdout)
            normalized_output = result.stdout.replace("\\", "/")
            for path in (source_path, home_path, log_path, production_root / "demo"):
                self.assertIn(
                    f"DRY-RUN: remove {str(path).replace(chr(92), '/')}",
                    normalized_output,
                )
            self.assertTrue((state_root / "apps.json").exists())
            self.assertTrue((production_root / "demo").exists())
            self.assertTrue((systemd_dir / "cockpit-app-demo.service").exists())
            self.assertTrue((snippet_root / "demo.conf").exists())


if __name__ == "__main__":
    unittest.main()
