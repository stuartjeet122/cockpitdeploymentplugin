from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, call as mock_call, patch

import backend.app_manager as manager
from backend.adapters import NextJSAdapter, detect_framework, get_adapter, inspect_project


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
            "DEFAULT_PLUGIN_VERSION_PATH": root / "state" / "plugin-version.json",
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
            ("nextjs-config-js", {"next.config.js": "module.exports = {}"}, "nextjs"),
            ("nextjs-config-mjs", {"next.config.mjs": "export default {}"}, "nextjs"),
            ("nextjs-config-cjs", {"next.config.cjs": "module.exports = {}"}, "nextjs"),
            ("nextjs-config-ts", {"next.config.ts": "export default {}"}, "nextjs"),
            ("nextjs-config-mts", {"next.config.mts": "export default {}"}, "nextjs"),
            ("nextjs-config-cts", {"next.config.cts": "export = {}"}, "nextjs"),
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

    def test_invalid_settings_never_replace_the_working_configuration(self):
        original = manager.get_config()
        path = Path(manager.DEFAULT_CONFIG_PATH)
        contents = path.read_bytes()
        invalid = [
            {key: value}
            for key in ("build_timeout_seconds", "health_timeout_seconds", "command_timeout_seconds")
            for value in (0, -1, True, "30", None)
        ] + [
            {"default_branch": "../unsafe"},
            {"port_range": {"start": 8100, "end": 8999, "reserved": [True]}},
            {"port_range": {"start": 8100, "end": 8999, "reserved": "8080"}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                manager.update_settings(payload)
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(manager.get_config(), original)

    def test_application_commands_use_an_isolated_environment_and_accept_mixed_case_keys(self):
        with patch.object(manager, "_require_root"), patch.object(manager.os, "name", "posix"), patch.object(
            manager, "_account_exists", return_value=True
        ), patch.object(manager, "_home_path", return_value="/managed/home"), patch.object(
            manager, "run_command", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ) as run:
            manager._run_as_user("app-demo", ["node", "build.js"], environment={"Database_URL": "private"})
        command = run.call_args.args[0]
        self.assertIn("-i", command)
        self.assertIn("GIT_TERMINAL_PROMPT=0", command)
        self.assertIn("Database_URL=private", command)
        self.assertEqual(command[-2:], ["node", "build.js"])

    def test_unsupported_framework_detection_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Could not detect"):
                detect_framework(Path(tmp))

    def test_detects_nextjs_inside_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            project = repo / "apps" / "web"
            project.mkdir(parents=True)
            (project / "package.json").write_text(
                '{"dependencies":{"next":"^15"}}',
                encoding="utf-8",
            )

            self.assertEqual(detect_framework(repo), "nextjs")

    def test_add_application_sanitizes_generated_id_and_assigns_supported_port(self):
        result = self.add_app("Example Site!")
        self.assertEqual(result["app"]["id"], "example-site")
        self.assertTrue(8100 <= result["app"]["port"] <= 8999)
        self.assertNotEqual(result["app"]["port"], 8080)
        self.assertEqual(len(manager.get_registry()["apps"]), 1)

    def test_add_application_accepts_explicit_available_port(self):
        result = self.add_app("Fixed Port", slug="fixed-port", port=8123)
        self.assertEqual(result["app"]["port"], 8123)

    def test_application_route_is_validated_normalized_and_unique(self):
        created = self.add_app("Public Site", route="/public-site")
        self.assertEqual(created["app"]["route"], "/public-site/")
        self.assertIsNone(created["app"]["public_url"])

        with self.assertRaisesRegex(ValueError, "already assigned"):
            self.add_app("Other Site", slug="other-site", route="/public-site/")

        for route in (
            "/",
            "public-site/",
            "//public-site/",
            "/Upper/",
            "/query/?x=1",
            "/../outside/",
            "/ends-with-./",
        ):
            with self.subTest(route=route), self.assertRaisesRegex(ValueError, "Route must be"):
                manager.validate_app_payload(self.add_payload("Bad Route", route=route))
        self.assertEqual(
            manager.validate_app_payload(
            self.add_payload("Nested Route", route="/apps/customer-portal")
            )["route"],
            "/apps/customer-portal/",
        )

    def test_edit_application_changes_route_without_changing_active_route(self):
        created = self.add_app("Route App", route="/old-route")["app"]
        with manager._registry_transaction() as registry:
            app = manager._find_app(registry, created["id"])
            app["active_route"] = "/old-route/"
            app["hosting_active"] = True

        updated = manager.edit_app(created["id"], {"route": "/new-route"})
        self.assertEqual(updated["app"]["route"], "/new-route/")
        self.assertEqual(updated["app"]["active_route"], "/old-route/")
        metadata = manager.list_apps()["apps"][0]
        self.assertEqual(metadata["public_url"], f"{manager.DEFAULT_PUBLIC_ORIGIN}/old-route/")

    def test_public_url_is_withheld_until_the_route_is_active(self):
        created = self.add_app("Pending Site", route="/pending-site")["app"]
        metadata = manager.app_metadata(created)
        self.assertFalse(metadata["hosting_active"])
        self.assertIsNone(metadata["public_url"])

        with manager._registry_transaction() as registry:
            app = manager._find_app(registry, created["id"])
            app["active_route"] = "/pending-site/"
            app["hosting_active"] = True
        active = manager.app_metadata(manager.get_registry()["apps"][0])
        self.assertTrue(active["hosting_active"])
        self.assertEqual(active["public_url"], f"{manager.DEFAULT_PUBLIC_ORIGIN}/pending-site/")

    def test_open_app_refuses_to_return_a_url_for_an_inactive_nginx_route(self):
        app = self.add_app("Pending Site", route="/pending-site")["app"]
        with self.assertRaisesRegex(RuntimeError, "route is not active in Nginx"):
            manager.open_app(app["id"])

        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored["active_route"] = stored["route"]
            stored["hosting_active"] = True
        result = manager.open_app(app["id"])
        self.assertEqual(result["url"], f"{manager.DEFAULT_PUBLIC_ORIGIN}/pending-site/")

    def test_build_config_rejects_unsafe_go_package(self):
        for package in ("", "../outside", "./cmd/../server", "/absolute", "example.com/remote", "./...", ["./cmd/server"]):
            with self.subTest(package=package), self.assertRaisesRegex(ValueError, "go_package"):
                self.add_app("Unsafe Go package", framework="go", build_config={"go_package": package})

    def test_build_config_rejects_unsafe_application_directory(self):
        for app_directory in ("../outside", "/absolute/path", "apps//web", "apps/./web", "apps\\web"):
            with self.subTest(app_directory=app_directory):
                with self.assertRaisesRegex(ValueError, "app_directory"):
                    manager.validate_app_payload(
                        self.add_payload(
                            framework="nextjs",
                            build_config={"app_directory": app_directory},
                        )
                    )

    def test_edit_application_updates_supported_fields(self):
        app = self.add_app("Demo App")["app"]
        result = manager.edit_app(app["id"], {"name": "Updated Demo", "health_check_url": "/status"})
        self.assertEqual(result["app"]["name"], "Updated Demo")
        self.assertEqual(result["app"]["health_check_url"], "/status")
        stored = manager.get_registry()["apps"][0]
        self.assertEqual(stored["name"], "Updated Demo")

    def test_settings_update_persists_valid_config(self):
        result = manager.update_settings({"release_retention": 1, "health_timeout_seconds": 45})
        self.assertEqual(result["config"]["release_retention"], 1)
        self.assertEqual(result["config"]["health_timeout_seconds"], 45)
        with self.assertRaisesRegex(ValueError, "retains only the current deployment"):
            manager.update_settings({"release_retention": 2})

    def test_environment_file_normalizes_supported_dotenv_values(self):
        normalized = manager._parse_environment_file(
            "# Runtime secrets\n"
            "export API_KEY=abc123\n"
            "DATABASE_URL=\"postgres://user:pass@localhost/db\"\n"
            "FEATURE_LABEL='beta release'\n"
            "HASH_VALUE=part # trailing comment\n"
        )

        self.assertIn("# Runtime secrets\n", normalized)
        self.assertIn('API_KEY="abc123"\n', normalized)
        self.assertIn('DATABASE_URL="postgres://user:pass@localhost/db"\n', normalized)
        self.assertIn('FEATURE_LABEL="beta release"\n', normalized)
        self.assertIn('HASH_VALUE="part"\n', normalized)

    def test_environment_file_rejects_malformed_or_ambiguous_values(self):
        invalid_contents = (
            "not an assignment",
            "KEY=one\nKEY=two",
            'KEY="unclosed',
            "KEY=value\nNEXT=bad\x00value",
            "KEY=value\n" + ("LARGE=" + "x" * manager.MAX_ENV_FILE_BYTES),
            "PORT=8080",
        )
        for contents in invalid_contents:
            with self.subTest(contents=contents[:40]):
                with self.assertRaises(ValueError):
                    manager._parse_environment_file(contents)

    def test_environment_file_permission_validation_rejects_public_or_unowned_files(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and permission checks are unavailable")
        app = {"service_user": "app-demo"}
        path = Mock()
        with patch.object(manager, "_account_ids", return_value=(1001, 1001)):
            path.stat.return_value = Mock(
                st_mode=0o100640,
                st_uid=0,
                st_gid=1001,
                st_nlink=1,
            )
            manager._validate_environment_file_permissions(path, app)
            path.stat.return_value = Mock(
                st_mode=0o100644,
                st_uid=0,
                st_gid=1001,
                st_nlink=1,
            )
            with self.assertRaisesRegex(RuntimeError, "unsafe ownership or permissions"):
                manager._validate_environment_file_permissions(path, app)

    def test_environment_values_are_available_to_build_commands(self):
        contents = '# Build variables\nAPI_TOKEN="secret token"\nAPP_MODE=production\n'
        with patch.object(
            manager,
            "get_environment_app",
            return_value={"content": contents},
        ):
            values = manager._environment_values_app("demo")

        self.assertEqual(values, {"API_TOKEN": "secret token", "APP_MODE": "production"})

    def test_environment_file_is_saved_privately_and_returned_for_export(self):
        app = self.add_app()["app"]
        with patch.object(manager, "ensure_app_user", return_value=app["service_user"]), patch.object(
            manager, "_account_ids", return_value=(1001, 1001)
        ), patch.object(manager.os, "chown", create=True), patch.object(
            manager, "_validate_environment_file_permissions"
        ):
            result = manager.save_environment_app(
                app["id"],
                "# app config\nAPI_KEY=\"secret-value\"\n",
            )
            saved = manager.get_environment_app(app["id"])

        self.assertEqual(result["key_count"], 1)
        self.assertFalse(result["restart_required"])
        self.assertEqual(saved["content"], '# app config\nAPI_KEY="secret-value"\n')
        env_path = manager._app_paths(app["id"], manager.get_config())["environment"]
        self.assertTrue(env_path.is_file())
        if os.name != "nt":
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o640)

    def test_environment_file_rejects_symlink_before_read_or_write(self):
        app = self.add_app()["app"]
        env_path = manager._app_paths(app["id"], manager.get_config())["environment"]
        env_path.parent.mkdir(parents=True)
        outside = env_path.parent / "other.env"
        outside.write_text("SECRET=keep\n", encoding="utf-8")
        try:
            env_path.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation is not permitted on this host")

        with self.assertRaisesRegex(RuntimeError, "must be a regular file"):
            manager.get_environment_app(app["id"])

    def test_environment_save_cli_reads_secrets_from_stdin(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(manager.sys, "stdin", io.StringIO("API_TOKEN=private\n")), patch.object(
            manager, "save_environment_app", return_value={"ok": True, "key_count": 1}
        ) as save, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = manager.main(["env-save", "demo"])

        self.assertEqual(code, 0)
        self.assertEqual(save.call_args.args, ("demo", "API_TOKEN=private\n"))
        self.assertNotIn("private", str(save.call_args_list[0].args[0]))
        self.assertEqual(json.loads(stdout.getvalue())["key_count"], 1)
        self.assertEqual(stderr.getvalue(), "")

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
        app = self.add_app()["app"]
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored["active_route"] = stored["route"]
            stored["hosting_active"] = True
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

    def test_machine_stdout_is_preserved_without_leaking_to_logs_or_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "build.log"
            metadata = '{"Dir":"/opt/webapps/website-list/app","Name":"main"}'
            for live in (False, True):
                for cancellable in (False, True):
                    for exit_code in (0, 1):
                        with self.subTest(live=live, cancellable=cancellable, exit_code=exit_code):
                            command = [sys.executable, "-c", "import sys; print(sys.argv[1]); print(sys.argv[1], file=sys.stderr); sys.exit(int(sys.argv[2]))", metadata, str(exit_code)]
                            result = manager.run_command(
                                command, live_log_path=log if live else None, redact_values=["/website-list"],
                                cancel_file=Path(temporary) / "cancel" if cancellable else None, preserve_stdout=True,
                            )
                            self.assertEqual(result.returncode, exit_code)
                            if exit_code == 0:
                                self.assertEqual(json.loads(result.stdout)["Dir"], "/opt/webapps/website-list/app")
                            else:
                                self.assertNotIn("/website-list", result.stdout)
                                self.assertIn("[REDACTED]", result.stdout)
                            self.assertNotIn("/website-list", result.stderr)
                            if live:
                                self.assertNotIn("/website-list", log.read_text(encoding="utf-8"))
                                self.assertIn("[REDACTED]", log.read_text(encoding="utf-8"))

    def test_machine_stdout_timeout_errors_remain_redacted(self):
        with patch.object(manager.subprocess, "run", side_effect=subprocess.TimeoutExpired(
            "metadata", 1, output="secret-metadata", stderr="secret-metadata"
        )):
            with self.assertRaises(RuntimeError) as failure:
                manager.run_command(["metadata"], redact_values=["secret-metadata"], preserve_stdout=True)
        self.assertNotIn("secret-metadata", str(failure.exception))
        self.assertIn("[REDACTED]", str(failure.exception))

    def test_run_command_reports_timeout_and_missing_executable(self):
        with patch.object(manager.subprocess, "run", side_effect=subprocess.TimeoutExpired("sleep", 1)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                manager.run_command(["sleep", "2"], timeout=1)
        with patch.object(
            manager.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(
                "npm",
                1,
                output=b"install progress",
                stderr=b"registry request failed",
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                manager.run_command(["npm", "ci"], timeout=1)
        self.assertIn("stdout:\ninstall progress", str(raised.exception))
        self.assertIn("stderr:\nregistry request failed", str(raised.exception))
        with patch.object(manager.subprocess, "run", side_effect=FileNotFoundError("missing")):
            with self.assertRaisesRegex(RuntimeError, "Could not run"):
                manager.run_command(["missing"])

    def test_run_command_streams_live_output_and_preserves_both_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "deployment.log"
            result = manager.run_command(
                [
                    manager.sys.executable,
                    "-c",
                    "import sys; print('build output'); print('build warning', file=sys.stderr)",
                ],
                timeout=10,
                live_log_path=log_path,
            )
            live_log = log_path.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0)
        self.assertIn("build output", result.stdout)
        self.assertIn("build warning", result.stderr)
        self.assertIn("[stdout] build output", live_log)
        self.assertIn("[stderr] build warning", live_log)

    def test_run_command_redacts_environment_values_from_live_and_captured_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "deployment.log"
            result = manager.run_command(
                [
                    manager.sys.executable,
                    "-c",
                    "import sys,time; sys.stdout.write('private-'); sys.stdout.flush(); time.sleep(.05); print('token')",
                ],
                timeout=10,
                live_log_path=log_path,
                redact_values=["private-token"],
            )
            live_log = log_path.read_text(encoding="utf-8")

        self.assertNotIn("private-token", result.stdout)
        self.assertNotIn("private-token", live_log)
        self.assertIn("[REDACTED]", result.stdout)
        self.assertIn("[REDACTED]", live_log)

    def test_run_command_explicit_redaction_preserves_nonsecret_environment_flags(self):
        root = Path(self.temp_dir.name)
        command = [manager.sys.executable, "-c", "print('100644 private-token')"]
        environment = {"FEATURE_FLAG": "1", "SECRET": "private-token"}
        for options in ({}, {"live_log_path": root / "redaction.log"}, {"cancel_file": root / "cancel"}):
            with self.subTest(options=options):
                result = manager.run_command(command, env=environment, redact_values=["private-token"], timeout=10, **options)
                self.assertEqual(result.stdout.strip(), "100644 [REDACTED]")
        default = manager.run_command(command, env={"SECRET": "private-token"}, timeout=10)
        self.assertNotIn("private-token", default.stdout)
        with patch.object(manager.subprocess, "run", side_effect=subprocess.TimeoutExpired(command, 1, output=b"100644 private-token")):
            with self.assertRaises(RuntimeError) as raised:
                manager.run_command(command, env=environment, redact_values=["private-token"])
        self.assertIn("100644 [REDACTED]", str(raised.exception))
        self.assertNotIn("private-token", str(raised.exception))

    def test_live_command_timeout_keeps_captured_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError) as raised:
                manager.run_command(
                    [
                        manager.sys.executable,
                        "-c",
                        "import time; print('started', flush=True); time.sleep(2)",
                    ],
                    timeout=1,
                    live_log_path=Path(temporary) / "deployment.log",
                )

        self.assertIn("timed out", str(raised.exception))
        self.assertIn("started", str(raised.exception))

    def test_live_log_is_capped_without_truncating_command_output(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            manager, "MAX_DEPLOYMENT_LOG_BYTES", 1024
        ):
            log_path = Path(temporary) / "deployment.log"
            result = manager.run_command(
                [
                    manager.sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush()",
                ],
                timeout=10,
                live_log_path=log_path,
            )
            log_content = log_path.read_bytes()

        self.assertEqual(len(result.stdout), 4096)
        self.assertLessEqual(len(log_content), 1024)
        self.assertIn(b"output truncated", log_content)

    def test_deployment_logs_returns_tail_and_running_state(self):
        app = self.add_app()["app"]
        deployment_id = "20261109220923-abcdef12"
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            manager._record_deployment(stored, {
                "id": deployment_id,
                "name": app["name"],
                "date": "2026-11-09T22:09:23Z",
                "release": None,
                "release_available": False,
                "status": "deploying",
            })
        log_path = manager._deployment_log_path(app["id"], deployment_id, create=True)
        manager._append_deployment_log(log_path, "Build started")

        result = manager.deployment_logs_app(app["id"])

        self.assertTrue(result["active"])
        self.assertEqual(result["status"], "deploying")
        self.assertIn("Build started", result["logs"])
        self.assertIn("Build started", manager.logs_app(app["id"])["logs"])
        with patch.object(manager, "_tail_log", side_effect=AssertionError("Status polling must not read logs")):
            args = manager._build_parser().parse_args(["deployment-logs", app["id"], deployment_id, "--status-only"])
            status = manager._dispatch(args)
        self.assertTrue(status["active"])
        self.assertEqual(status["status"], "deploying")
        self.assertNotIn("logs", status)

    def test_deployment_records_and_releases_keep_only_the_current_version(self):
        app = self.add_app()["app"]
        record = {"id": "20261109220923-abcdef12", "status": "deployed", "date": "2026-11-09"}
        manager._record_deployment(app, record)
        manager._record_deployment(app, {"id": "20261110220923-abcdef12", "status": "deploying"})
        self.assertEqual(len(app["deployments"]), 1)
        self.assertEqual(app["deployments"][0]["status"], "deploying")

        releases = Path(app["production_path"]) / "releases"
        old_release = releases / "20261109220923-abcdef12"
        current_release = releases / "20261110220923-abcdef12"
        old_release.mkdir(parents=True)
        current_release.mkdir()
        try:
            (Path(app["production_path"]) / "current").symlink_to(current_release)
        except OSError:
            self.skipTest("Symlink creation is not permitted on this host")
        manager._prune_releases(app)
        self.assertFalse(old_release.exists())
        self.assertTrue(current_release.exists())

    def test_deployment_api_exposes_only_the_latest_record(self):
        app = self.add_app()["app"]
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored["deployments"] = [
                {"id": "20261108220923-abcdef12", "date": "2026-11-08"},
                {"id": "20261109220923-abcdef12", "date": "2026-11-09"},
            ]
        deployments = manager.get_deployments()["deployments"]
        self.assertEqual(len(deployments), 1)
        self.assertEqual(deployments[0]["id"], "20261109220923-abcdef12")

    def test_deployment_log_pruning_keeps_only_the_current_attempt(self):
        app_id = "demo"
        previous_id = "20261108220923-abcdef12"
        current_id = "20261109220923-abcdef12"
        previous_log = manager._deployment_log_path(app_id, previous_id, create=True)
        manager._append_deployment_log(previous_log, "previous")
        current_log = manager._deployment_log_path(app_id, current_id, create=True)
        manager._prune_deployment_logs(app_id, current_id)
        self.assertFalse(previous_log.exists())
        self.assertTrue(current_log.exists())

    def test_abort_marks_active_deployment_and_exposes_aborting_status(self):
        app = self.add_app()["app"]
        deployment_id = "20261109220923-abcdef12"
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            manager._record_deployment(stored, {
                "id": deployment_id,
                "name": app["name"],
                "date": "2026-11-09T22:09:23Z",
                "status": "deploying",
            })
        log_path = manager._deployment_log_path(app["id"], deployment_id, create=True)
        with patch.object(manager, "_require_root"):
            result = manager.abort_deployment_app(app["id"], deployment_id)
        self.assertTrue(result["aborting"])
        self.assertTrue(manager.deployment_logs_app(app["id"])["aborting"])
        self.assertIn("Abort requested", log_path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "no longer active"):
            with manager._registry_transaction() as registry:
                manager._find_app(registry, app["id"])["deployments"][0]["status"] = "deployed"
            manager.abort_deployment_app(app["id"], deployment_id)

    def test_abort_immediately_after_publication_survives_worker_start(self):
        app = self.add_app()["app"]
        transaction = manager._registry_transaction
        aborted = False

        @contextlib.contextmanager
        def publish_then_abort():
            nonlocal aborted
            with transaction() as registry:
                yield registry
            if not aborted:
                aborted = True
                active = manager._find_app(manager.get_registry(), app["id"])["deployments"][0]
                manager.abort_deployment_app(app["id"], active["id"])

        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_registry_transaction", publish_then_abort), patch.object(
            manager, "run_command", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ):
            result = manager.deployment_start_app(app["id"])

        self.assertTrue(aborted)
        self.assertTrue(manager.deployment_logs_app(app["id"], result["deployment_id"])["aborting"])

    def test_cancellable_command_terminates_its_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            cancel_file = Path(temporary) / "cancel"
            timer = threading.Timer(0.1, cancel_file.touch)
            timer.start()
            try:
                with self.assertRaisesRegex(manager.DeploymentAborted, "aborted by request"):
                    manager.run_command(
                        [manager.sys.executable, "-c", "import time; time.sleep(30)"],
                        timeout=10,
                        cancel_file=cancel_file,
                    )
            finally:
                timer.cancel()

    def test_deployment_start_detaches_worker_and_returns_identifier(self):
        app = self.add_app()["app"]
        with patch.object(manager, "_require_root"), patch.object(manager, "ensure_app_user") as ensure_user, patch.object(
            manager,
            "run_command",
            return_value=subprocess.CompletedProcess(args=["systemd-run"], returncode=0, stdout="", stderr=""),
        ) as run:
            result = manager.deployment_start_app(app["id"])
        self.assertTrue(result["active"])
        self.assertRegex(result["deployment_id"], r"^\d{14}-[a-f0-9]{8}$")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--property=ProtectSystem=strict", command)
        read_write_paths = next(
            argument for argument in command if argument.startswith("--property=ReadWritePaths=")
        )
        self.assertIn(manager.DEFAULT_STATE_ROOT, read_write_paths)
        self.assertIn("/etc/nginx", read_write_paths)
        self.assertIn("/var/log/nginx", read_write_paths)
        ensure_user.assert_called_once_with(app["id"])
        self.assertFalse(any("/etc/passwd" in argument for argument in command))
        self.assertEqual(command[-4:], ["deploy", app["id"], "--deployment-id", result["deployment_id"]])
        stored = manager.get_registry()["apps"][0]["deployments"]
        self.assertEqual(len(stored), 1)
        self.assertIn(f"cockpit-app-manager-deploy-{app['id']}-{result['deployment_id']}.service", stored[0]["systemd_unit"])

    def test_auto_deploy_pulls_and_deploys_only_when_commit_changes(self):
        app = self.add_app()["app"]
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            manager._record_deployment(stored, {
                "id": "20261109220923-abcdef12",
                "name": app["name"],
                "date": "2026-11-09T22:09:23Z",
                "commit": "a" * 40,
                "status": "deployed",
            })
        deployed = {"deployment": {"commit": "b" * 40}}
        with patch.object(manager, "_require_root"), patch.object(
            manager, "_remote_app_commit", return_value="b" * 40
        ) as remote, patch.object(manager, "deploy_app", return_value=deployed) as deploy:
            result = manager.auto_deploy_apps()
        self.assertEqual(result["results"][0]["status"], "deployed")
        remote.assert_called_once()
        deploy.assert_called_once_with(app["id"])

    def test_auto_deploy_pause_window_and_backoff_skip_repository(self):
        app = self.add_app(auto_deploy=False)["app"]
        with patch.object(manager, "_require_root"), patch.object(manager, "_remote_app_commit") as remote:
            self.assertEqual(manager.auto_deploy_apps()["results"][0]["status"], "paused")
            manager.edit_app(app["id"], {"auto_deploy": True, "deployment_window": {"start": "01:00", "end": "02:00"}})
            with patch.object(manager, "_in_deployment_window", return_value=False):
                self.assertEqual(manager.auto_deploy_apps()["results"][0]["status"], "outside-deployment-window")
            remote.assert_not_called()

    def test_remote_revision_check_does_not_require_a_checkout(self):
        app = self.add_app()["app"]
        with patch.object(manager, "ensure_app_user", return_value=app["service_user"]), patch.object(
            manager, "_run_as_user", return_value=subprocess.CompletedProcess([], 0, "a" * 40 + "\trefs/heads/main\n", "")
        ) as run:
            self.assertEqual(manager._remote_app_commit(app), "a" * 40)
        self.assertEqual(run.call_args.args[0], app["service_user"])
        self.assertEqual(run.call_args.args[1], ["git", "ls-remote", "--exit-code", app["repo_url"], "refs/heads/main"])
        self.assertFalse(Path(app["source_path"]).exists())

    def test_preflight_validates_environment_disk_and_runtime_minimums(self):
        app = self.add_app(framework="node", required_environment=["DATABASE_URL"], runtime_requirements={"node": "20.9"})["app"]
        source = Path(app["source_path"])
        source.mkdir(parents=True)
        (source / "package.json").write_text('{"scripts":{"start":"node server.js"}}', encoding="utf-8")
        with patch.object(manager, "_environment_values_app", return_value={}), patch.object(manager, "_run_as_user", return_value=subprocess.CompletedProcess([], 0, "v18.20.0", "")):
            report = manager._deployment_preflight(app, source, app["service_user"], manager.get_config())
        self.assertFalse(report["passed"])
        self.assertTrue(any("DATABASE_URL" in error for error in report["errors"]))
        self.assertTrue(any("minimum 20.9" in error for error in report["errors"]))
        with patch.object(manager, "_environment_values_app", return_value={"DATABASE_URL": "secret-must-not-appear"}), patch.object(manager, "_run_as_user", return_value=subprocess.CompletedProcess([], 0, "v22.1.0", "")):
            report = manager._deployment_preflight(app, source, app["service_user"], manager.get_config())
        self.assertTrue(report["passed"])
        self.assertNotIn("secret-must-not-appear", json.dumps(report))

    def test_approval_is_bound_to_commit_repository_branch_and_live_revision(self):
        app = self.add_app(deployment_mode="approval")["app"]
        commit = "b" * 40
        with self.assertRaisesRegex(RuntimeError, "Review and approve"):
            manager._validate_approval(app, commit)
        app["pending_update"] = {"commit": commit, "repo_url": app["repo_url"], "branch": app["branch"], "base_commit": None}
        manager._validate_approval(app, commit)
        for mutation in ({"branch": "changed"}, {"deployed_commit": "a" * 40}):
            with self.assertRaisesRegex(RuntimeError, "changed since review"):
                manager._validate_approval({**app, **mutation}, commit)

    def test_capacity_settings_and_new_payload_fields_reject_unsafe_values(self):
        for payload in ({"max_concurrent_builds": 0}, {"max_concurrent_builds": 9}, {"max_concurrent_builds": True}, {"min_free_disk_mb": -1}):
            with self.assertRaises(ValueError):
                manager.update_settings(payload)
        for payload in ({"deployment_mode": []}, {"required_environment": ["bad-key"]}, {"runtime_requirements": {"bash": "1"}}, {"runtime_requirements": {"node": ">=20"}}, {"storage_directories": ["data/escape"]}):
            with self.assertRaises(ValueError):
                self.add_app(**payload)
        for key in ("APP_DATA_DIR", "APP_STORAGE_DIR", "APP_STORAGE_UPLOADS"):
            with self.assertRaisesRegex(ValueError, "reserved"):
                manager._parse_environment_data(f"{key}=outside\n")
        with self.assertRaisesRegex(ValueError, "dir is reserved"):
            self.add_app(storage_directories=["dir"])

    def test_failed_build_preflight_never_invokes_adapter(self):
        app = self.add_app(framework="node")["app"]
        with manager._registry_transaction() as registry:
            manager._record_deployment(manager._find_app(registry, app["id"]), {"id": "preflight", "status": "deploying"})
        root = Path(self.temp_dir.name)
        adapter = Mock()
        with patch.object(manager, "_deployment_preflight", return_value={"passed": False, "checks": [], "errors": ["Missing API_KEY"]}):
            with self.assertRaisesRegex(RuntimeError, "Missing API_KEY"):
                manager._build_release(app, root / "source", root / "release", adapter, app["service_user"], manager.get_config(), "preflight", root / "cancel", root / "deployment.log")
        adapter.build.assert_not_called()
        self.assertEqual(manager._find_app(manager.get_registry(), app["id"])["deployments"][0]["build_status"], "preflight")

    def test_approval_policy_blocks_direct_deploy_and_stale_review_before_build(self):
        app = self.add_app(framework="node", deployment_mode="approval")["app"]
        commit = "b" * 40
        paths = manager._app_paths(app["id"], manager.get_config())
        with patch.object(manager, "_require_root"):
            with self.assertRaisesRegex(RuntimeError, "Review and approve"):
                manager.deploy_app(app["id"])
        with manager._registry_transaction() as registry:
            manager._find_app(registry, app["id"])["pending_update"] = {
                "commit": commit, "base_commit": None, "repo_url": app["repo_url"], "branch": app["branch"],
            }
        paths["source"].mkdir(parents=True)
        paths["releases"].mkdir(parents=True)
        with patch.object(manager, "_require_root"), patch.object(manager, "ensure_app_user", return_value=app["service_user"]), patch.object(
            manager, "_setup_app_directories", return_value=paths
        ), patch.object(manager, "clone_or_update_repo"), patch.object(manager, "_environment_values_app", return_value={}), patch.object(
            manager, "git_output", return_value="c" * 40
        ), patch.object(manager, "get_adapter"), patch.object(manager, "_account_ids", return_value=(1001, 1001)), patch.object(
            manager, "_prepare_release_directory", side_effect=lambda path, user: path.mkdir()
        ), patch.object(manager, "_build_release") as build:
            with self.assertRaisesRegex(RuntimeError, "changed after approval"):
                manager.deploy_app(app["id"], approved_commit=commit)
            build.assert_not_called()

    def test_stale_queued_approval_is_recorded_as_failed_before_fetch_or_build(self):
        app = self.add_app(deployment_mode="approval")["app"]
        deployment_id = "20261109220923-abcdef12"
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored["status"] = "deploying"
            stored["pending_update"] = {
                "commit": "c" * 40, "repo_url": app["repo_url"], "branch": app["branch"], "base_commit": None,
            }
            manager._record_deployment(stored, {
                "id": deployment_id, "status": "deploying", "previous_status": "stopped", "approved_commit": "b" * 40,
            })
        paths = manager._app_paths(app["id"], manager.get_config())
        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo"
        ) as fetch, patch.object(manager, "_build_release") as build:
            with self.assertRaisesRegex(RuntimeError, "Review and approve"):
                manager.deploy_app(app["id"], deployment_id=deployment_id)
        fetch.assert_not_called()
        build.assert_not_called()
        result = manager.deployment_logs_app(app["id"], deployment_id)
        self.assertFalse(result["active"])
        self.assertEqual(result["status"], "failed")

    def test_deployment_worker_rejects_replayed_missing_and_competing_jobs(self):
        app = self.add_app()["app"]
        deployment_id = "20261109220923-abcdef12"
        for status in ("deployed", "failed", "aborted", None, "deploying"):
            with self.subTest(status=status):
                with manager._registry_transaction() as registry:
                    stored = manager._find_app(registry, app["id"])
                    stored["deployments"] = [] if status is None else [{"id": deployment_id, "status": status}]
                before = manager.get_registry()
                with patch.object(manager, "_require_root"), patch.object(manager, "_setup_app_directories") as setup:
                    with self.assertRaisesRegex(ValueError, "not active|already active"):
                        manager.deploy_app(app["id"], deployment_id=None if status == "deploying" else deployment_id)
                setup.assert_not_called()
                self.assertEqual(manager.get_registry(), before)

    def test_failed_release_preparation_never_removes_an_existing_directory(self):
        app = self.add_app()["app"]
        deployment_id = "20261109220923-abcdef12"
        paths = manager._app_paths(app["id"], manager.get_config())
        release = paths["releases"] / deployment_id
        release.mkdir(parents=True)
        sentinel = release / "keep.txt"
        sentinel.write_text("existing release", encoding="utf-8")
        with manager._registry_transaction() as registry:
            manager._record_deployment(manager._find_app(registry, app["id"]), {
                "id": deployment_id, "status": "deploying", "previous_status": "stopped",
            })
        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo"
        ), patch.object(manager, "_build_release") as build:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                manager.deploy_app(app["id"], deployment_id=deployment_id)
        build.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing release")
        self.assertEqual(manager.deployment_logs_app(app["id"], deployment_id)["status"], "failed")

    def test_auto_deploy_approval_only_records_pending_changes(self):
        app = self.add_app(deployment_mode="approval")["app"]
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored["deployed_commit"] = "a" * 40
            manager._record_deployment(stored, {"id": "previous", "status": "deployed", "commit": "a" * 40})
        with patch.object(manager, "_require_root"), patch.object(manager, "_remote_app_commit", return_value="b" * 40), patch.object(
            manager, "review_update_app"
        ) as review, patch.object(manager, "deploy_app") as deploy:
            result = manager.auto_deploy_apps()
        self.assertEqual(result["results"][0]["status"], "awaiting-approval")
        review.assert_called_once_with(app["id"])
        deploy.assert_not_called()

    def test_build_slots_wait_cancel_and_release_after_errors(self):
        cancel = Path(self.temp_dir.name) / "cancel"
        waited = threading.Event()
        def wait_for_slot():
            with manager._build_slot(cancel, on_wait=waited.set):
                self.fail("Cancelled worker acquired a slot")
        with manager._build_slot(cancel):
            with ThreadPoolExecutor(max_workers=1) as executor:
                worker = executor.submit(wait_for_slot)
                self.assertTrue(waited.wait(5))
                cancel.touch()
                with self.assertRaises(manager.DeploymentAborted):
                    worker.result(timeout=5)
        cancel.unlink()
        with self.assertRaisesRegex(RuntimeError, "build failure"):
            with manager._build_slot(cancel):
                raise RuntimeError("build failure")
        with manager._build_slot(cancel):
            pass

    def test_two_build_slots_allow_two_workers_but_not_a_third(self):
        manager.update_settings({"max_concurrent_builds": 2})
        cancel = Path(self.temp_dir.name) / "cancel"
        waiting = threading.Event()
        def wait_for_slot():
            with manager._build_slot(cancel, on_wait=waiting.set):
                self.fail("Third worker exceeded capacity")
        with manager._build_slot(cancel), manager._build_slot(cancel):
            with ThreadPoolExecutor(max_workers=1) as executor:
                worker = executor.submit(wait_for_slot)
                self.assertTrue(waiting.wait(5))
                cancel.touch()
                with self.assertRaises(manager.DeploymentAborted):
                    worker.result(timeout=5)

    def test_build_slots_reduction_counts_active_higher_numbered_slots(self):
        manager.update_settings({"max_concurrent_builds": 2})
        cancel = Path(self.temp_dir.name) / "cancel"
        waiting = threading.Event()
        acquired = threading.Event()
        def wait_for_slot():
            with manager._build_slot(cancel, on_wait=waiting.set):
                acquired.set()
        with contextlib.ExitStack() as first, contextlib.ExitStack() as second:
            first.enter_context(manager._build_slot(cancel))
            second.enter_context(manager._build_slot(cancel))
            manager.update_settings({"max_concurrent_builds": 1})
            first.close()
            with ThreadPoolExecutor(max_workers=1) as executor:
                worker = executor.submit(wait_for_slot)
                try:
                    self.assertTrue(waiting.wait(5))
                    self.assertFalse(acquired.is_set())
                    second.close()
                    worker.result(timeout=5)
                    self.assertTrue(acquired.is_set())
                finally:
                    cancel.touch()

    def test_metrics_use_systemd_counters_and_reset_cpu_after_restart(self):
        app = self.add_app(framework="node")["app"]
        properties = "ActiveState=active\nMemoryCurrent=1024\nMemoryPeak=2048\nCPUUsageNSec=1000000000\nTasksCurrent=2\nNRestarts=3\nInvocationID=first\nActiveEnterTimestampMonotonic=1000000\n"
        with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, properties, "")):
            first = manager.metrics_app(app["id"])["metrics"]
        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(first["memory_bytes"], 1024)
        with manager._registry_transaction() as registry:
            manager._find_app(registry, app["id"])["metrics"]["sample_time"] -= 10
        with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, properties.replace("1000000000", "2000000000"), "")):
            second = manager.metrics_app(app["id"])["metrics"]
        self.assertAlmostEqual(second["cpu_percent"], 10, delta=0.2)
        with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, properties.replace("InvocationID=first", "InvocationID=second"), "")):
            self.assertIsNone(manager.metrics_app(app["id"])["metrics"]["cpu_percent"])

    def test_persistent_storage_is_named_isolated_and_not_pruned(self):
        app = self.add_app(storage_directories=["uploads"])["app"]
        config = manager.get_config()
        paths = manager._app_paths(app["id"], config)
        paths["production"].mkdir(parents=True)
        with patch.object(manager.os, "chown", create=True), patch.object(manager, "_account_ids", return_value=(1001, 1001)):
            manager._setup_persistent_storage(app, app["service_user"], config)
        saved = paths["storage"] / "uploads" / "saved.txt"
        saved.write_text("retained", encoding="utf-8")
        manager._prune_releases(app)
        self.assertEqual(saved.read_text(), "retained")
        service = manager._render_service(app, ["node", "server.js"], config)
        self.assertIn("APP_STORAGE_UPLOADS=", service)
        self.assertIn("APP_DATA_DIR=", service)
        self.assertIn(manager._systemd_quote(str(paths["storage"] / "uploads")), service)
        with self.assertRaises(ValueError):
            self.add_app(storage_directories=["../outside"])

    def test_fresh_clone_must_descend_from_deployed_commit(self):
        app = {"deployed_commit": "a" * 40}
        for returncode in (0, 1, 128):
            with patch.object(manager, "_git", return_value=subprocess.CompletedProcess([], returncode, "", "")) as git:
                if returncode == 0:
                    manager._assert_deployed_ancestor(app, Path(self.temp_dir.name), "app-demo", 30)
                else:
                    with self.assertRaisesRegex(RuntimeError, "rewritten history"):
                        manager._assert_deployed_ancestor(app, Path(self.temp_dir.name), "app-demo", 30)
                self.assertEqual(git.call_args.args[2], ["merge-base", "--is-ancestor", "a" * 40, "HEAD"])

    def test_auto_deploy_validation_and_failure_backoff(self):
        for payload in ({"auto_deploy": "yes"}, {"deployment_window": {"start": "25:00", "end": "01:00"}}):
            with self.assertRaises(ValueError):
                self.add_app(**payload)
        app = self.add_app()["app"]
        with manager._registry_transaction() as registry:
            manager._record_deployment(manager._find_app(registry, app["id"]), {"id": "first", "status": "failed"})
        with patch.object(manager, "_require_root"), patch.object(manager, "_remote_app_commit", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                manager.auto_deploy_apps()
            self.assertEqual(manager.auto_deploy_apps()["results"][0]["status"], "retry-backoff")
        self.assertIn("offline", manager.get_registry()["apps"][0]["auto_deploy_state"]["error"])

    def test_health_reports_service_local_and_public_independently(self):
        app = self.add_app(framework="node")["app"]
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored.update(hosting_active=True, active_route=stored["route"])
        with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, "active\n", "")), patch.object(
            manager, "check_app_health", return_value=True
        ), patch.object(manager.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.URLError("public DNS unavailable")
            result = manager.health_check_app(app["id"])
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(result["service_state"], "active")
        self.assertTrue(result["local_healthy"])
        self.assertFalse(result["public_healthy"])
        self.assertIn("DNS", result["public_error"])
        self.assertEqual(request.get_header("User-agent"), "Cockpit-App-Manager/1.0")
        self.assertEqual(manager.get_registry()["apps"][0]["health"]["checked_at"], result["checked_at"])

    def test_public_health_explains_forbidden_health_paths(self):
        app = self.add_app(framework="node")["app"]
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored.update(hosting_active=True, active_route=stored["route"])
        forbidden = urllib.error.HTTPError(
            manager.DEFAULT_PUBLIC_ORIGIN + app["route"], 403, "Forbidden", {}, None
        )
        with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, "active\n", "")), patch.object(
            manager, "check_app_health", return_value=True
        ), patch.object(manager.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = forbidden
            report = manager.health_check_app(app["id"])
        self.assertFalse(report["public_healthy"])
        self.assertEqual(report["public_status"], 403)
        self.assertIn("HTTP 403 Forbidden", report["public_error"])
        self.assertIn("Nginx error logs", report["public_error"])
        self.assertIn("health-check path", report["public_error"])

    def test_public_health_identifies_a_cloudflare_forbidden_probe(self):
        app = self.add_app(framework="go")["app"]
        with manager._registry_transaction() as registry:
            stored = manager._find_app(registry, app["id"])
            stored.update(hosting_active=True, active_route=stored["route"])
        forbidden = urllib.error.HTTPError(
            manager.DEFAULT_PUBLIC_ORIGIN + app["route"], 403, "Forbidden", {"Server": "cloudflare"}, None
        )
        with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, "active\n", "")), patch.object(
            manager, "check_app_health", return_value=True
        ), patch.object(manager.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = forbidden
            report = manager.health_check_app(app["id"])
        self.assertTrue(report["local_healthy"])
        self.assertEqual(report["service_state"], "active")
        self.assertEqual(report["public_status"], 403)
        self.assertIn("Cloudflare edge protection", report["public_error"])

    def test_nextjs_public_health_preserves_an_existing_route_prefix(self):
        app = self.add_app(framework="nextjs")["app"]
        route = app["route"]
        for health in ("/health", route + "health", route.rstrip("/"), "/"):
            with self.subTest(health=health):
                with manager._registry_transaction() as registry:
                    stored = manager._find_app(registry, app["id"])
                    stored.update(hosting_active=True, active_route=route, health_check_url=health)
                with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, "active\n", "")), patch.object(
                    manager, "check_app_health", return_value=True
                ), patch.object(manager.urllib.request, "build_opener") as opener:
                    opener.return_value.open.return_value.__enter__.return_value.status = 200
                    report = manager.health_check_app(app["id"])
                local_path = report["url"].split(":" + str(app["port"]), 1)[1]
                self.assertEqual(opener.return_value.open.call_args.args[0].full_url, manager.DEFAULT_PUBLIC_ORIGIN + local_path)
                self.assertNotIn(route.rstrip("/") + route, local_path)

    def test_health_started_before_stop_cannot_restore_an_active_observation(self):
        app = self.add_app(framework="node")["app"]
        def stop_during_check(*arguments):
            with manager._registry_transaction() as registry:
                stored = manager._find_app(registry, app["id"])
                stored["status"] = "stopped"
                stored.pop("health", None)
            return True
        with manager._registry_transaction() as registry:
            manager._find_app(registry, app["id"])["status"] = "running"
        with patch.object(manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, "active\n", "")), patch.object(
            manager, "check_app_health", side_effect=stop_during_check
        ):
            manager.health_check_app(app["id"])
        stored = manager._find_app(manager.get_registry(), app["id"])
        self.assertEqual(stored["status"], "stopped")
        self.assertNotIn("health", stored)

    def test_service_transitions_discard_previous_health_observations(self):
        app = self.add_app(framework="node")["app"]
        service = Path(self.temp_dir.name) / "service"
        service.touch()
        for operation, healthy, expected in (("stop", True, "stopped"), ("start", True, "running"), ("restart", False, "failed")):
            with self.subTest(operation=operation):
                with manager._registry_transaction() as registry:
                    manager._find_app(registry, app["id"])["health"] = {"service_state": "active", "checked_at": manager.now_iso()}
                with patch.object(manager, "_require_root"), patch.object(manager, "_service_path", return_value=service), patch.object(
                    manager, "_systemctl"
                ), patch.object(manager, "_wait_for_health", return_value=healthy), patch.object(manager, "_assert_loopback_binding"):
                    if healthy:
                        manager._service_action(app["id"], operation)
                    else:
                        with self.assertRaises(RuntimeError):
                            manager._service_action(app["id"], operation)
                stored = manager._find_app(manager.get_registry(), app["id"])
                self.assertEqual(stored["status"], expected)
                self.assertNotIn("health", stored)

    def test_detached_deployment_failure_preserves_previous_running_status(self):
        app = self.add_app()["app"]
        paths = manager._app_paths(app["id"], manager.get_config())
        paths["current"].mkdir(parents=True)
        with manager._registry_transaction() as registry:
            manager._find_app(registry, app["id"])["status"] = "running"
        with patch.object(manager, "_require_root"), patch.object(
            manager, "run_command", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ):
            queued = manager.deployment_start_app(app["id"])
        self.assertEqual(manager.get_registry()["apps"][0]["deployments"][0]["previous_status"], "running")
        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo", side_effect=RuntimeError("repository unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "repository unavailable"):
                manager.deploy_app(app["id"], queued["deployment_id"])
        stored = manager.get_registry()["apps"][0]
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["deployments"][0]["status"], "failed")

    def test_plugin_update_check_compares_installed_and_remote_revisions(self):
        version_path = Path(manager.DEFAULT_PLUGIN_VERSION_PATH)
        version_path.parent.mkdir(parents=True)
        version_path.write_text(
            json.dumps({"commit": "a" * 40, "version": "v99.0.0"}),
            encoding="utf-8",
        )
        with patch.object(manager, "_plugin_remote_commit", return_value={"commit": "b" * 40, "branch": "main"}):
            result = manager.plugin_update_check()

        self.assertTrue(result["available"])
        self.assertEqual(result["current_commit"], "a" * 40)
        self.assertEqual(result["latest_commit"], "b" * 40)
        self.assertEqual(result["branch"], "main")

    def test_plugin_update_rejects_non_fast_forward_branch(self):
        failure = subprocess.CompletedProcess(["git", "merge-base"], 1, stdout="", stderr="")
        with patch.object(manager, "_plugin_git", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "not a fast-forward"):
                manager._verify_plugin_update_is_fast_forward(
                    Path(self.temp_dir.name), "a" * 40, "b" * 40
                )

    def test_plugin_update_downloads_fixed_upstream_and_runs_its_installer(self):
        commit = "b" * 40

        def fake_git(args, timeout=120):
            if "clone" in args:
                checkout = Path(args[-1])
                (checkout / "scripts").mkdir(parents=True)
                (checkout / "scripts" / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=commit + "\n", stderr="")

        with patch.object(manager, "plugin_update_check", return_value={"available": True, "branch": "main", "current_commit": "a" * 40, "latest_commit": commit}), patch.object(
            manager, "_account_ids", return_value=(65534, 65534)
        ), patch.object(manager, "_plugin_git", side_effect=fake_git) as plugin_git, patch.object(
            manager, "_seal_plugin_update_checkout"
        ), patch.object(manager, "_verify_plugin_checkout") as verify_checkout, patch.object(
            manager, "_require_root"
        ), patch.object(manager.os, "chown", create=True), patch.object(
            manager.os, "chmod"
        ), patch.object(
            manager, "run_command", return_value=subprocess.CompletedProcess([], 0, stdout="installed", stderr="")
        ) as install:
            result = manager.plugin_update()

        self.assertTrue(result["updated"])
        verify_checkout.assert_called_once()
        self.assertEqual(result["commit"], commit)
        clone_command = plugin_git.call_args_list[0].args[0]
        self.assertIn(manager.PLUGIN_REPOSITORY_URL, clone_command)
        self.assertIn("--branch", clone_command)
        self.assertIn("main", clone_command)
        self.assertNotIn("--depth", clone_command)
        self.assertNotEqual(Path(clone_command[-1]), verify_checkout.call_args.args[0])
        self.assertEqual(install.call_args_list[0].args[0][0], "bash")
        installer_path = install.call_args_list[0].args[0][1].replace("\\", "/")
        self.assertTrue(installer_path.endswith("scripts/install.sh"))
        self.assertNotIn("APP_MANAGER_RELEASE_VERSION", install.call_args_list[0].kwargs["env"])
        self.assertTrue(result["restart_scheduled"])
        restart_command = install.call_args_list[1].args[0]
        self.assertEqual(restart_command[0], "systemd-run")
        self.assertIn("--on-active=5s", restart_command)
        self.assertEqual(restart_command[-3:], ["systemctl", "restart", "cockpit.service"])

    def test_plugin_update_never_installs_a_moved_or_non_fast_forward_branch(self):
        commit = "b" * 40
        def fake_git(args, timeout=120):
            if "clone" in args:
                checkout = Path(args[-1])
                (checkout / "scripts").mkdir(parents=True)
                (checkout / "scripts" / "install.sh").write_bytes(b"#!/bin/sh\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, commit + "\n", "")
            return subprocess.CompletedProcess(args, 1, "", "")

        with patch.object(manager, "plugin_update_check", return_value={
            "available": True, "branch": "main", "current_commit": "a" * 40, "latest_commit": commit,
        }), patch.object(manager, "_account_ids", return_value=(65534, 65534)), patch.object(
            manager, "_plugin_git", side_effect=fake_git
        ), patch.object(manager, "_seal_plugin_update_checkout"), patch.object(
            manager.os, "chown", create=True
        ), patch.object(manager.os, "chmod"), patch.object(manager, "run_command") as install:
            with self.assertRaisesRegex(RuntimeError, "not a fast-forward"):
                manager.plugin_update()
            install.assert_not_called()
            self.assertFalse(list((Path(manager.DEFAULT_STATE_ROOT) / "plugin-updates").iterdir()))

        with patch.object(manager, "plugin_update_check", return_value={
            "available": True, "branch": "main", "current_commit": "a" * 40, "latest_commit": "c" * 40,
        }), patch.object(manager, "_account_ids", return_value=(65534, 65534)), patch.object(
            manager, "_plugin_git", side_effect=fake_git
        ), patch.object(manager, "_seal_plugin_update_checkout"), patch.object(
            manager.os, "chown", create=True
        ), patch.object(manager.os, "chmod"), patch.object(manager, "run_command") as install:
            with self.assertRaisesRegex(RuntimeError, "branch changed"):
                manager.plugin_update()
            install.assert_not_called()
            self.assertFalse(list((Path(manager.DEFAULT_STATE_ROOT) / "plugin-updates").iterdir()))

    def test_plugin_update_does_not_restart_when_already_current(self):
        with patch.object(manager, "plugin_update_check", return_value={
            "available": False, "current_commit": "a" * 40,
        }), patch.object(manager, "run_command") as run:
            result = manager.plugin_update()
        self.assertFalse(result["updated"])
        run.assert_not_called()

    def test_plugin_update_reports_install_and_restart_failures_separately(self):
        def fake_git(args, timeout=120):
            if "clone" in args:
                checkout = Path(args[-1])
                (checkout / "scripts").mkdir(parents=True)
                (checkout / "scripts" / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="b" * 40 + "\n", stderr="")

        for install_succeeds in (False, True):
            commands = [
                subprocess.CompletedProcess([], 0 if install_succeeds else 1, stdout="", stderr="installer failure"),
                subprocess.CompletedProcess([], 1, stdout="", stderr="restart scheduler unavailable"),
            ]
            with self.subTest(install_succeeds=install_succeeds), patch.object(
                manager, "plugin_update_check", return_value={"available": True, "branch": "main", "current_commit": None, "latest_commit": "b" * 40}
            ), patch.object(manager, "_account_ids", return_value=(65534, 65534)), patch.object(
                manager, "_plugin_git", side_effect=fake_git
            ), patch.object(manager, "_seal_plugin_update_checkout"), patch.object(manager, "_verify_plugin_checkout"), patch.object(
                manager.os, "chown", create=True
            ), patch.object(manager.os, "chmod"), patch.object(manager, "run_command", side_effect=commands) as run:
                if install_succeeds:
                    result = manager.plugin_update()
                    self.assertTrue(result["updated"])
                    self.assertFalse(result["restart_scheduled"])
                    self.assertIn("restart scheduler unavailable", result["restart_error"])
                else:
                    with self.assertRaisesRegex(RuntimeError, "installer failure"):
                        manager.plugin_update()
                self.assertEqual(run.call_count, 2 if install_succeeds else 1)
                self.assertFalse(list((Path(manager.DEFAULT_STATE_ROOT) / "plugin-updates").iterdir()))

    def test_plugin_update_selects_latest_main_branch_commit(self):
        output = "b" * 40 + "\trefs/heads/main\n"
        with patch.object(manager, "_plugin_git", return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
            self.assertEqual(manager._plugin_remote_commit(), {"branch": "main", "commit": "b" * 40})
        self.assertEqual(run.call_args.args[0][-2:], [manager.PLUGIN_REPOSITORY_URL, "refs/heads/main"])

    def test_plugin_update_reports_when_main_branch_is_missing(self):
        with patch.object(
            manager,
            "_plugin_git",
            return_value=subprocess.CompletedProcess([], 2, "", ""),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "update branch 'main' was not found",
            ):
                manager._plugin_remote_commit()

    def test_plugin_tree_rejects_modified_and_unexpected_files(self):
        import hashlib
        checkout = Path(self.temp_dir.name) / "checkout"
        checkout.mkdir()
        installer = checkout / "install.sh"
        installer.write_bytes(b"trusted")
        digest = hashlib.sha1(b"blob 7\0trusted").hexdigest()
        commit = "a" * 40
        responses = [subprocess.CompletedProcess([], 0, commit + "\n", ""),
                     subprocess.CompletedProcess([], 0, f"100644 blob {digest}\tinstall.sh\0", "")]
        with patch.object(manager, "run_command", side_effect=responses):
            manager._verify_plugin_checkout(checkout, commit)
        for modified in (True, False):
            if modified:
                installer.write_bytes(b"tampered")
            else:
                installer.write_bytes(b"trusted")
                (checkout / "json.py").write_bytes(b"injected")
            with patch.object(manager, "run_command", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "selected Git tree|unexpected files"):
                    manager._verify_plugin_checkout(checkout, commit)

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_plugin_tree_uses_real_git_and_rejects_replacement_refs(self):
        checkout = Path(self.temp_dir.name) / "verified-checkout"
        checkout.mkdir()
        environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                           GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                           GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        def git(*arguments, content=None):
            result = subprocess.run(["git", "-C", str(checkout), *arguments], input=content,
                                    capture_output=True, check=True, timeout=10, env=environment)
            return result.stdout.decode("utf-8").strip()
        git("init", "-q")
        original = b"trusted installer\n"
        installer = checkout / "install.sh"
        installer.write_bytes(original)
        git("add", "install.sh")
        git("commit", "-m", "Initial main commit")
        commit = git("rev-parse", "HEAD")
        manager._verify_plugin_checkout(checkout, commit)
        changed = b"untrusted installer\n"
        blob = git("hash-object", "-w", "--stdin", content=changed)
        tree = git("mktree", content=f"100644 blob {blob}\tinstall.sh\n".encode("ascii"))
        replacement = git("commit-tree", tree, content=b"Replacement\n")
        git("replace", commit, replacement)
        manager._verify_plugin_checkout(checkout, commit)
        installer.write_bytes(changed)
        with self.assertRaisesRegex(RuntimeError, "differs from the selected Git tree"):
            manager._verify_plugin_checkout(checkout, commit)

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

    def test_deployment_uses_custom_route_and_activates_nginx_after_health_check(self):
        app = self.add_app("Custom Route", route="/custom-route")["app"]
        config = manager.get_config()
        paths = manager._app_paths(app["id"], config)
        paths["releases"].mkdir(parents=True)
        unit_path = Path(self.temp_dir.name) / "cockpit-app-custom-route.service"
        activation = {
            "active": True,
            "route": "/custom-route/",
            "config_path": "/etc/nginx/snippets/cockpit-app-manager/custom-route.conf",
            "message": "Nginx is serving /custom-route/ from the managed application configuration.",
        }

        class Adapter:
            def build(self, app_record, source_path, release_dir, **kwargs):
                kwargs["runner"](["true"], source_path, 1)
                self.route = app_record["route"]
                return {"start_command": ["/bin/true"], "working_directory": None}

        adapter = Adapter()

        def prepare_release(release_dir, username):
            release_dir.mkdir()

        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo", return_value={"status": "updated"}
        ), patch.object(manager, "git_output", return_value="a" * 40), patch.object(
            manager, "_prepare_release_directory", side_effect=prepare_release
        ), patch.object(manager, "_account_ids", return_value=(1001, 1001)), patch.object(
            manager, "_environment_values_app", return_value={}
        ), patch.object(manager, "_service_path", return_value=unit_path), patch.object(
            manager, "_write_service"
        ), patch.object(manager, "_seal_release"), patch.object(
            manager, "_systemctl"
        ), patch.object(manager, "_retire_legacy_root_service"), patch.object(
            manager, "_atomic_symlink"
        ), patch.object(manager, "_wait_for_health", return_value=True), patch.object(
            manager, "_assert_loopback_binding"
        ) as binding, patch.object(
            manager, "_activate_nginx_config", return_value=activation
        ) as activate, patch.object(
            manager, "_run_as_user", return_value=subprocess.CompletedProcess(
                args=["true"], returncode=0, stdout="", stderr=""
            )
        ) as run_as_user, patch.object(manager, "get_adapter", return_value=adapter), patch.object(
            manager, "_deployment_preflight", return_value={"passed": True}
        ):
            result = manager.deploy_app(app["id"])

        self.assertEqual(adapter.route, "/custom-route/")
        binding.assert_called_once()
        self.assertEqual(run_as_user.call_args.kwargs["environment"]["APP_BASE_PATH"], "/custom-route")
        activate.assert_called_once()
        self.assertEqual(activate.call_args.args[0]["route"], "/custom-route/")
        self.assertTrue(result["hosting"]["active"])
        stored = manager.get_registry()["apps"][0]
        self.assertEqual(stored["active_route"], "/custom-route/")
        self.assertTrue(stored["hosting_active"])

    def test_failed_nextjs_deployment_retains_preflight_and_npm_diagnostics(self):
        app = self.add_app("Next Site", framework="nextjs")["app"]
        paths = manager._app_paths(app["id"], manager.get_config())
        paths["source"].mkdir(parents=True)
        (paths["source"] / "package.json").write_text(
            json.dumps({
                "dependencies": {"next": "^15.0.0"},
                "scripts": {"build": "next build"},
            }),
            encoding="utf-8",
        )
        (paths["source"] / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": {}}),
            encoding="utf-8",
        )
        paths["releases"].mkdir(parents=True)
        expected_error = (
            "npm error code ENOLOCK\n"
            "The `npm ci` command can only install with an existing package-lock.json"
        )

        def prepare_release(release_dir, username):
            release_dir.mkdir()

        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo", return_value={"status": "updated"}
        ), patch.object(manager, "git_output", return_value="a" * 40), patch.object(
            manager, "_prepare_release_directory", side_effect=prepare_release
        ), patch.object(manager, "_account_ids", return_value=(1001, 1001)), patch.object(
            manager, "_environment_values_app", return_value={}
        ), patch.object(manager, "_service_path", return_value=Path(self.temp_dir.name) / "unit.service"), patch.object(
            manager, "_run_as_user",
            return_value=subprocess.CompletedProcess(
                args=["npm", "ci"],
                returncode=1,
                stdout="",
                stderr=expected_error,
            ),
        ), patch("backend.adapters._prepare_build_copy_permissions"), patch.object(
            manager, "_deployment_preflight", return_value={"passed": True}
        ):
            with self.assertRaisesRegex(RuntimeError, "ENOLOCK"):
                manager.deploy_app(app["id"])

        deployment = manager.get_registry()["apps"][0]["deployments"][-1]
        self.assertEqual(deployment["status"], "failed")
        self.assertFalse(Path(deployment["release"]).exists())
        logs = manager.deployment_logs_app(app["id"])["logs"]
        self.assertIn("Next.js npm preflight", logs)
        self.assertIn("lockfileVersion=3", logs)
        self.assertIn("app_group_readable:", logs)
        self.assertIn("Command: npm ci --include=dev --no-fund --no-audit", logs)
        self.assertIn(expected_error, logs)

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

    def test_local_health_redirects_cannot_leave_the_application(self):
        handler = manager._LocalHealthRedirectHandler()
        request = urllib.request.Request("http://127.0.0.1:8123/")
        for target, allowed in (("http://127.0.0.1:8123/login/", True),
                                ("http://127.0.0.1:8080/", False),
                                ("https://example.com/", False)):
            with self.subTest(target=target):
                redirected = handler.redirect_request(request, None, 302, "Found", {}, target)
                self.assertEqual(redirected is not None, allowed)

    def test_startup_failure_includes_redacted_runtime_diagnostics(self):
        app = self.add_app("Go diagnostics", framework="go")["app"]
        with patch.object(manager, "_environment_values_app", return_value={"TOKEN": "private-token"}), patch.object(
            manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, stdout="ExecMainStatus=1", stderr="")
        ), patch.object(manager, "_tail_log", return_value="permission denied private-token"), patch.object(
            manager, "run_command", return_value=subprocess.CompletedProcess([], 0, stdout="journal private-token", stderr="")
        ):
            message = manager._startup_failure(app, manager.get_config())
        self.assertIn(f"PORT={app['port']}", message)
        self.assertIn("APP_DATA_DIR", message)
        self.assertIn("ExecMainStatus=1", message)
        self.assertIn("permission denied [REDACTED]", message)
        self.assertIn("journal [REDACTED]", message)
        self.assertNotIn("private-token", message)

    def test_startup_failure_explains_systemd_exec_errors_and_includes_journal(self):
        app = self.add_app("Go exec failure", framework="go")["app"]
        with patch.object(manager, "_environment_values_app", return_value={}), patch.object(
            manager, "_systemctl", return_value=subprocess.CompletedProcess(
                [], 0, stdout="ActiveState=activating\nResult=exit-code\nExecMainStatus=203", stderr=""
            )
        ), patch.object(manager, "_tail_log", return_value=""), patch.object(
            manager, "run_command", return_value=subprocess.CompletedProcess(
                [], 0, stdout="Failed at step EXEC spawning server: Permission denied", stderr=""
            )
        ) as journal:
            message = manager._startup_failure(app, manager.get_config())
        self.assertIn("203/EXEC", message)
        self.assertIn("before HTTP health checks", message)
        self.assertIn("noexec", message)
        self.assertIn("Failed at step EXEC spawning server: Permission denied", message)
        self.assertNotIn("runtime must listen", message)
        self.assertEqual(journal.call_args.args[0][:3], ["journalctl", "--unit", manager._service_path(app).name])
        self.assertEqual(journal.call_args.kwargs["timeout"], 5)

    def test_exec_failure_captures_permissions_and_mount_before_release_cleanup(self):
        app = self.add_app("Go mount failure", framework="go")["app"]
        config = manager.get_config()
        executable = manager._app_paths(app["id"], config)["releases"] / "candidate" / "server"
        with patch.object(manager, "_environment_values_app", return_value={"TOKEN": "secret-token"}), patch.object(
            manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, stdout="ExecMainStatus=203", stderr="")
        ), patch.object(manager, "_tail_log", return_value=""), patch.object(manager, "run_command", side_effect=[
            subprocess.CompletedProcess([], 0, stdout="-r-xr-x--- root app-demo server secret-token", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="/opt ext4 rw,noexec", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="Failed at step EXEC: Permission denied", stderr=""),
        ]) as commands:
            message = manager._startup_failure(app, config, executable)
        self.assertIn("Executable path permissions:", message)
        self.assertIn("Executable filesystem:\n/opt ext4 rw,noexec", message)
        self.assertIn("server [REDACTED]", message)
        self.assertNotIn("secret-token", message)
        self.assertEqual(commands.call_args_list[0].args[0], ["namei", "-l", "--", str(executable)])
        self.assertEqual(commands.call_args_list[1].args[0][-2:], ["--target", str(executable)])
        self.assertTrue(all(call.kwargs["timeout"] == 5 for call in commands.call_args_list))

    def test_startup_failure_keeps_service_status_when_journal_is_unavailable(self):
        app = self.add_app("No journal", framework="go")["app"]
        with patch.object(manager, "_environment_values_app", return_value={}), patch.object(
            manager, "_systemctl", return_value=subprocess.CompletedProcess([], 0, stdout="ExecMainStatus=203", stderr="")
        ), patch.object(manager, "_tail_log", return_value=""), patch.object(
            manager, "run_command", side_effect=RuntimeError("journal unavailable")
        ):
            message = manager._startup_failure(app, manager.get_config())
        self.assertIn("203/EXEC", message)
        self.assertIn("ExecMainStatus=203", message)

    def test_runtime_listener_must_be_loopback_only(self):
        app = {"port": 8123}
        for address, valid in (("127.0.0.1:8123", True), ("[::1]:8123", True), ("0.0.0.0:8123", False), ("*:8123", False), ("[::]:8123", False)):
            with self.subTest(address=address), patch.object(manager, "run_command", return_value=subprocess.CompletedProcess(
                [], 0, stdout=f"LISTEN 0 128 {address} 0.0.0.0:*\n", stderr="",
            )):
                if valid:
                    manager._assert_loopback_binding(app, 10)
                else:
                    with self.assertRaisesRegex(RuntimeError, "exposed"):
                        manager._assert_loopback_binding(app, 10)
        with patch.object(manager, "run_command", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "not listening"):
                manager._assert_loopback_binding(app, 10)

    def test_static_deployment_uses_nginx_without_creating_a_runtime_service(self):
        app = self.add_app("Static deployment")["app"]
        paths = manager._app_paths(app["id"], manager.get_config())
        paths["source"].mkdir(parents=True)
        paths["releases"].mkdir(parents=True)
        (paths["source"] / "index.html").write_text("<h1>Ready</h1>", encoding="utf-8")
        activation = {"active": True, "route": app["route"], "message": "Static route active"}
        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo"
        ), patch.object(manager, "git_output", return_value="a" * 40), patch.object(
            manager, "_prepare_release_directory", side_effect=lambda path, user: path.mkdir()
        ), patch.object(manager, "_account_ids", return_value=(1001, 1001)), patch.object(
            manager, "_service_path", return_value=Path(self.temp_dir.name) / "static.service"
        ), patch.object(manager, "_environment_values_app", return_value={}), patch.object(
            manager, "_seal_release"
        ), patch.object(manager, "_write_service") as write_service, patch.object(
            manager, "_retire_legacy_root_service"
        ), patch.object(manager, "_atomic_symlink"), patch.object(
            manager, "_systemctl"
        ) as systemctl, patch.object(manager, "_wait_for_health") as runtime_health, patch.object(
            manager, "_activate_nginx_config", return_value=activation
        ), patch.object(manager, "_prune_releases"), patch("backend.adapters._prepare_build_copy_permissions"):
            result = manager.deploy_app(app["id"])
        write_service.assert_not_called()
        systemctl.assert_not_called()
        runtime_health.assert_not_called()
        self.assertTrue(result["app"]["hosting_active"])
        self.assertEqual(result["app"]["public_url"], manager.DEFAULT_PUBLIC_ORIGIN + app["route"])
        self.assertFalse(paths["source"].exists())
        self.assertTrue((Path(result["deployment"]["release"]) / "app" / "index.html").is_file())
        logs = manager.deployment_logs_app(app["id"])["logs"]
        self.assertIn(f"branch={app['branch']}", logs)
        self.assertIn(f"route={app['route']}", logs)
        self.assertIn("configured_framework=static", logs)
        self.assertIn(f"deployment_id={result['deployment']['id']}", logs)
        self.assertIn("Source revision: " + "a" * 40, logs)

    def test_nextjs_export_deploys_as_static_without_changing_framework_detection(self):
        app = self.add_app("Next export", framework="nextjs")["app"]
        paths = manager._app_paths(app["id"], manager.get_config())
        paths["source"].mkdir(parents=True)
        paths["releases"].mkdir(parents=True)
        activation = {"active": True, "route": app["route"], "message": "Static route active"}

        def build_export(app_snapshot, source, release, *args, **kwargs):
            output = release / "app" / "apps" / "web" / "out"
            output.mkdir(parents=True)
            (output / "index.html").write_text("<h1>Exported</h1>", encoding="utf-8")
            return {
                "start_command": [],
                "runtime_required": False,
                "static_root": "apps/web/out",
            }

        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo"
        ), patch.object(manager, "git_output", return_value="a" * 40), patch.object(
            manager, "_prepare_release_directory", side_effect=lambda path, user: path.mkdir()
        ), patch.object(manager, "_account_ids", return_value=(1001, 1001)), patch.object(
            manager, "_service_path", return_value=Path(self.temp_dir.name) / "next-export.service"
        ), patch.object(manager, "_environment_values_app", return_value={}), patch.object(
            manager, "_build_release", side_effect=build_export
        ), patch.object(manager, "_seal_release") as seal_release, patch.object(
            manager, "_write_service"
        ) as write_service, patch.object(manager, "_retire_legacy_root_service"), patch.object(
            manager, "_atomic_symlink"
        ), patch.object(manager, "_systemctl") as systemctl, patch.object(
            manager, "_activate_nginx_config", return_value=activation
        ) as activate_nginx, patch.object(manager, "_prune_releases"), patch(
            "backend.adapters._prepare_build_copy_permissions"
        ):
            result = manager.deploy_app(app["id"])

        write_service.assert_not_called()
        systemctl.assert_not_called()
        seal_release.assert_called_once()
        self.assertTrue(seal_release.call_args.kwargs["public"])
        self.assertEqual(activate_nginx.call_args.args[1], "static")
        self.assertEqual(result["app"]["framework"], "nextjs")
        self.assertTrue(result["app"]["static_export"])
        self.assertEqual(result["app"]["static_root"], "apps/web/out")
        self.assertTrue(result["deployment"]["static_export"])

    def test_staged_deployment_checks_candidate_before_switch_and_keeps_old_on_failure(self):
        for health_passes in (True, False):
            with self.subTest(health_passes=health_passes):
                app = self.add_app("Staged " + str(health_passes), framework="nextjs")["app"]
                config = manager.get_config()
                paths = manager._app_paths(app["id"], config)
                paths["source"].mkdir(parents=True)
                paths["releases"].mkdir(parents=True)
                paths["current"].mkdir()
                primary = Path(self.temp_dir.name) / (app["id"] + ".service")
                secondary = Path(self.temp_dir.name) / (app["id"] + "-secondary.service")
                primary.write_text(f"# Managed by Cockpit App Manager\nDescription=Cockpit App Manager - {app['id']}\nUser={app['service_user']}\n", encoding="utf-8")
                with manager._registry_transaction() as registry:
                    manager._find_app(registry, app["id"]).update(status="running", deployed_commit="a" * 40)
                events = []
                adapter = Mock()
                def build(candidate, source, release, **kwargs):
                    self.assertNotEqual(candidate["port"], app["port"])
                    (release / "app").mkdir()
                    return {"start_command": ["next", "start", "--port", str(candidate["port"])]}
                adapter.build.side_effect = build
                def write_service(candidate, release, command, settings, working_directory):
                    text = manager._render_service(candidate, command, settings, working_directory, release)
                    self.assertIn(str(release / "app"), text)
                    secondary.write_text(text, encoding="utf-8")
                def health(candidate, timeout):
                    events.append("health")
                    self.assertTrue(primary.exists())
                    self.assertNotEqual(candidate["port"], app["port"])
                    return health_passes
                def systemctl(*args, **kwargs):
                    events.append(tuple(args))
                with contextlib.ExitStack() as stack:
                    mocks = {
                        "_require_root": Mock(), "ensure_app_user": Mock(return_value=app["service_user"]),
                        "_setup_app_directories": Mock(return_value=paths), "clone_or_update_repo": Mock(),
                        "_assert_deployed_ancestor": Mock(),
                        "_deployment_preflight": Mock(return_value={"passed": True}),
                        "git_output": Mock(return_value="b" * 40), "_account_ids": Mock(return_value=(1001, 1001)),
                        "_environment_values_app": Mock(return_value={}), "get_adapter": Mock(return_value=adapter),
                        "_prepare_release_directory": Mock(side_effect=lambda path, user: path.mkdir()),
                        "_seal_release": Mock(), "_retire_legacy_root_service": Mock(),
                        "_service_path": Mock(side_effect=lambda candidate: secondary if candidate.get("service_slot") == "secondary" else primary),
                        "_write_service": Mock(side_effect=write_service), "_systemctl": Mock(side_effect=systemctl),
                        "_wait_for_health": Mock(side_effect=health), "_assert_loopback_binding": Mock(),
                        "_atomic_symlink": Mock(side_effect=lambda *args: events.append("symlink")),
                        "_activate_nginx_config": Mock(side_effect=lambda *args: events.append("nginx") or {"active": True, "message": "active"}),
                        "_prune_releases": Mock(), "_socket_port_available": Mock(return_value=True),
                        "get_listening_ports": Mock(return_value=set()),
                    }
                    for name, replacement in mocks.items():
                        stack.enter_context(patch.object(manager, name, replacement))
                    if health_passes:
                        manager.deploy_app(app["id"])
                        self.assertLess(events.index("health"), events.index("symlink"))
                        self.assertLess(events.index("nginx"), events.index(("disable", "--now", primary.name)))
                        stored = manager._find_app(manager.get_registry(), app["id"])
                        self.assertEqual(stored["service_slot"], "secondary")
                        self.assertEqual(stored["deployed_commit"], "b" * 40)
                        self.assertFalse(paths["source"].exists())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "health check"):
                            manager.deploy_app(app["id"])
                        self.assertNotIn("nginx", events)
                        self.assertNotIn(("restart", primary.name), events)
                        self.assertNotIn(("disable", "--now", primary.name), events)
                        self.assertTrue(primary.exists())
                        self.assertFalse(secondary.exists())
                        self.assertTrue(paths["source"].exists())
                    self.assertNotIn("candidate_port", manager._find_app(manager.get_registry(), app["id"]))

    def test_alternate_service_slot_cannot_collide_with_an_application_id(self):
        primary = manager._service_path({"id": "demo-secondary"})
        secondary = manager._service_path({"id": "demo", "service_slot": "secondary"})
        self.assertNotEqual(primary, secondary)
        self.assertEqual(secondary.name, "cockpit-app-demo@secondary.service")

    def test_source_cleanup_rejects_symlink_and_preserves_runtime(self):
        app = self.add_app()["app"]
        config = manager.get_config()
        paths = manager._app_paths(app["id"], config)
        paths["source"].mkdir(parents=True)
        (paths["source"] / "file.txt").write_text("source", encoding="utf-8")
        paths["releases"].mkdir(parents=True)
        manager._remove_deployed_source(app["id"], config)
        self.assertFalse(paths["source"].exists())
        self.assertTrue(paths["releases"].exists())
        with patch.object(manager, "_assert_no_symlink_components", side_effect=RuntimeError("symlink")):
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                manager._remove_deployed_source(app["id"], config)

    def test_remove_app_deletes_only_its_managed_resources(self):
        app = self.add_app("Remove Example")["app"]
        config = manager.get_config()
        paths = manager._app_paths(app["id"], config)
        other_app = self.add_app("Keep Example")["app"]
        other_paths = manager._app_paths(other_app["id"], config)
        for key in ("source", "production", "logs", "home"):
            paths[key].mkdir(parents=True)
            (paths[key] / "managed-file").write_text("managed", encoding="utf-8")
        paths["environment"].parent.mkdir(parents=True)
        paths["environment"].write_text("SECRET=value\n", encoding="utf-8")
        other_paths["production"].mkdir(parents=True)
        (other_paths["production"] / "keep").write_text("keep", encoding="utf-8")
        deployment_id = "20261111235959-abcdef12"
        deployment_log = manager._deployment_log_path(app["id"], deployment_id, create=True)
        deployment_log.write_text("deployment", encoding="utf-8")
        other_log = manager._deployment_log_path(other_app["id"], deployment_id, create=True)
        other_log.write_text("other deployment", encoding="utf-8")
        units = {
            "primary": Path(self.temp_dir.name) / "primary.service",
            "secondary": Path(self.temp_dir.name) / "secondary.service",
        }
        for unit in units.values():
            unit.write_text("managed unit", encoding="utf-8")

        with patch.object(manager, "_validate_app_account_for_removal", return_value=1234), patch.object(
            manager, "_remove_app_account"
        ) as remove_account, patch.object(
            manager, "_remove_nginx_config"
        ) as remove_nginx, patch.object(
            manager, "_retire_legacy_root_service"
        ), patch.object(
            manager, "_service_path", side_effect=lambda record: units[record.get("service_slot", "primary")]
        ), patch.object(
            manager, "_verified_service_bytes", return_value=b"managed"
        ), patch.object(manager, "_systemctl"):
            result = manager.remove_app(app["id"])

        self.assertEqual(result, {"ok": True, "removed": app["id"], "data_retained": False})
        remove_nginx.assert_called_once_with(app["id"], config)
        remove_account.assert_called_once()
        self.assertEqual(remove_account.call_args.args[0]["id"], app["id"])
        self.assertEqual(remove_account.call_args.args[1], 1234)
        for key in ("source", "production", "logs", "home", "environment"):
            self.assertFalse(paths[key].exists(), key)
        self.assertFalse(deployment_log.exists())
        self.assertTrue(other_paths["production"].exists())
        self.assertTrue(other_log.exists())
        self.assertTrue(all(not unit.exists() for unit in units.values()))
        self.assertEqual([item["id"] for item in manager.get_registry()["apps"]], [other_app["id"]])

    def test_remove_app_refuses_paths_or_accounts_before_modifying_resources(self):
        app = self.add_app()["app"]
        with patch.object(manager, "_assert_no_symlink_components", side_effect=RuntimeError("symlink")), patch.object(
            manager, "_remove_nginx_config"
        ) as remove_nginx:
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                manager.remove_app(app["id"])
        remove_nginx.assert_not_called()
        self.assertEqual(len(manager.get_registry()["apps"]), 1)

    def test_remove_app_refuses_active_deployment(self):
        app = self.add_app()["app"]
        with manager._registry_transaction() as registry:
            manager._find_app(registry, app["id"])["deployments"] = [{"status": "deploying"}]
        with patch.object(manager, "_remove_nginx_config") as remove_nginx:
            with self.assertRaisesRegex(RuntimeError, "deployment is in progress"):
                manager.remove_app(app["id"])
        remove_nginx.assert_not_called()
        self.assertEqual(len(manager.get_registry()["apps"]), 1)

    def test_application_account_removal_requires_its_private_group(self):
        app = {"id": "demo", "service_user": "app-demo"}
        account = Mock(
            pw_uid=1001,
            pw_gid=1001,
            pw_dir=str(manager._home_path(app["id"])),
            pw_shell="/usr/sbin/nologin",
            pw_name="app-demo",
        )
        private_group = Mock(gr_name="app-demo", gr_gid=1001, gr_mem=[])
        password_database = Mock()
        password_database.getpwnam.return_value = account
        password_database.getpwall.return_value = [account]
        group_database = Mock()
        group_database.getgrnam.return_value = private_group
        group_database.getgrall.return_value = [private_group]
        with patch.object(manager, "pwd", password_database), patch.object(
            manager, "grp", group_database
        ):
            self.assertEqual(manager._validate_app_account_for_removal(app), 1001)
            group_database.getgrall.return_value = [
                private_group,
                Mock(gr_name="sudo", gr_gid=27, gr_mem=["app-demo"]),
            ]
            with self.assertRaisesRegex(RuntimeError, "additional group"):
                manager._validate_app_account_for_removal(app)

    def test_runtime_start_checks_health_and_socket_isolation(self):
        app = self.add_app("Runtime checks")["app"]
        manager.edit_app(app["id"], {"framework": "node"})
        service_path = Path(self.temp_dir.name) / "runtime.service"
        service_path.touch()
        for healthy, isolated in ((True, True), (False, True), (True, False)):
            with self.subTest(healthy=healthy, isolated=isolated), patch.object(
                manager, "_require_root"
            ), patch.object(manager, "_service_path", return_value=service_path), patch.object(
                manager, "_systemctl"
            ) as systemctl, patch.object(manager, "_wait_for_health", return_value=healthy), patch.object(
                manager, "_assert_loopback_binding", side_effect=None if isolated else RuntimeError("Public listener")
            ) as binding:
                if healthy and isolated:
                    manager.start_app(app["id"])
                else:
                    with self.assertRaises(RuntimeError):
                        manager.start_app(app["id"])
                expected_calls = [("start", service_path.name)]
                if not (healthy and isolated):
                    expected_calls.append(("stop", service_path.name))
                self.assertEqual([item.args for item in systemctl.call_args_list], expected_calls)
                self.assertEqual(binding.call_count, int(healthy))
                stored = manager._find_app(manager.get_registry(), app["id"])
                self.assertEqual(stored["status"], "running" if healthy and isolated else "failed")

    def test_failed_static_conversion_restores_and_reenables_the_previous_runtime(self):
        app = self.add_app("Converted static")["app"]
        paths = manager._app_paths(app["id"], manager.get_config())
        paths["source"].mkdir(parents=True)
        paths["releases"].mkdir(parents=True)
        paths["current"].mkdir()
        (paths["source"] / "index.html").write_text("Ready", encoding="utf-8")
        service_path = Path(self.temp_dir.name) / "previous.service"
        previous_service = f"# Managed by Cockpit App Manager\nDescription=Cockpit App Manager - {app['id']}\nUser={app['service_user']}\n"
        service_path.write_bytes(previous_service.encode("utf-8"))
        with manager._registry_transaction() as registry:
            manager._find_app(registry, app["id"])["status"] = "running"
        with patch.object(manager, "_require_root"), patch.object(
            manager, "ensure_app_user", return_value=app["service_user"]
        ), patch.object(manager, "_setup_app_directories", return_value=paths), patch.object(
            manager, "clone_or_update_repo"
        ), patch.object(manager, "git_output", return_value="a" * 40), patch.object(
            manager, "_prepare_release_directory", side_effect=lambda path, user: path.mkdir()
        ), patch.object(manager, "_account_ids", return_value=(1001, 1001)), patch.object(
            manager, "_service_path", return_value=service_path
        ), patch.object(manager, "_environment_values_app", return_value={}), patch.object(
            manager, "_seal_release"
        ), patch.object(manager, "_retire_legacy_root_service"), patch.object(
            manager, "_atomic_symlink"
        ), patch.object(manager, "_restore_service_file") as restore, patch.object(
            manager, "_systemctl"
        ) as systemctl, patch.object(manager, "_wait_for_health", return_value=True), patch.object(
            manager, "_activate_nginx_config", side_effect=RuntimeError("Nginx refused activation")
        ), patch("backend.adapters._prepare_build_copy_permissions"):
            with self.assertRaisesRegex(RuntimeError, "Nginx refused activation"):
                manager.deploy_app(app["id"])
        self.assertEqual([item.args for item in restore.call_args_list], [
            (service_path, None), (service_path, previous_service.encode("utf-8")),
        ])
        actions = [item.args for item in systemctl.call_args_list]
        self.assertIn(("disable", "--now", service_path.name), actions)
        self.assertIn(("enable", service_path.name), actions)
        self.assertIn(("restart", service_path.name), actions)
        stored = manager._find_app(manager.get_registry(), app["id"])
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["deployments"][0]["status"], "failed")
        self.assertNotIn("rollback_error", stored["deployments"][0])

    def test_static_health_checks_the_active_release_without_a_runtime(self):
        app = self.add_app()["app"]
        index = manager._app_paths(app["id"], manager.get_config())["current"] / "app" / "index.html"
        index.parent.mkdir(parents=True)
        index.write_text("<h1>Live</h1>", encoding="utf-8")
        app.update(status="running", hosting_active=True, active_route=app["route"])
        with patch.object(manager.urllib.request, "build_opener") as opener:
            self.assertTrue(manager.check_app_health(app))
            opener.assert_not_called()
        self.assertEqual(manager.app_health_url(app), manager.DEFAULT_PUBLIC_ORIGIN + app["route"])
        app["status"] = "stopped"
        self.assertFalse(manager.check_app_health(app))

    def test_nextjs_health_path_matches_the_public_base_path(self):
        app = {"id": "demo", "framework": "nextjs", "port": 8123, "route": "/apps/demo/"}
        for health, expected in (
            ("/", "/apps/demo/"), ("/health", "/apps/demo/health"),
            ("/apps/demo/health", "/apps/demo/health"),
        ):
            with self.subTest(health=health):
                self.assertEqual(
                    manager.app_health_url(dict(app, health_check_url=health)),
                    "http://127.0.0.1:8123" + expected,
                )

    def test_static_start_and_stop_manage_only_the_application_route(self):
        app = self.add_app()["app"]
        index = manager._app_paths(app["id"], manager.get_config())["current"] / "app" / "index.html"
        index.parent.mkdir(parents=True)
        index.write_text("<h1>Live</h1>", encoding="utf-8")
        with patch.object(manager, "_require_root"), patch.object(
            manager, "_activate_nginx_config", return_value={"active": True}
        ) as activate, patch.object(manager, "_remove_nginx_config") as remove, patch.object(
            manager, "_systemctl"
        ) as systemctl:
            self.assertEqual(manager.start_app(app["id"])["status"], "running")
            self.assertTrue(manager.get_registry()["apps"][0]["hosting_active"])
            self.assertEqual(manager.stop_app(app["id"])["status"], "stopped")
            self.assertFalse(manager.get_registry()["apps"][0]["hosting_active"])
        activate.assert_called_once()
        remove.assert_called_once()
        systemctl.assert_not_called()

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
            (repo / "package.json").write_text(
                '{"dependencies":{"next":"^15"},"scripts":{"build":"next build"}}',
                encoding="utf-8",
            )
            failed = subprocess.CompletedProcess(args=["npm"], returncode=2, stdout="", stderr="compile failed")

            runner = Mock(return_value=failed)

            with self.assertRaisesRegex(RuntimeError, "compile failed"):
                NextJSAdapter().build({"port": 8100}, repo, release, runner=runner, timeout=1)
            self.assertEqual(runner.call_count, 1)


class NextJSDependencyInstallationTests(unittest.TestCase):
    def build_project(
        self,
        lockfiles,
        runner,
        package_subdirectory=None,
        root_package=None,
        app_build_config=None,
        lockfile_subdirectory=None,
        group_id=None,
        log=None,
        restrictive_permissions=False,
        app_route=None,
        next_config=None,
        next_config_name="next.config.mjs",
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        release = root / "release"
        repo.mkdir()
        release.mkdir()
        package_dir = repo / package_subdirectory if package_subdirectory else repo
        package_dir.mkdir(parents=True, exist_ok=True)
        package_data = {
            "dependencies": {"next": "^15.0.0", "react": "^19.0.0"},
            "devDependencies": {"typescript": "^5.0.0"},
            "scripts": {"build": "next build"},
        }
        (package_dir / "package.json").write_text(json.dumps(package_data), encoding="utf-8")
        if next_config is not None:
            (package_dir / next_config_name).write_text(next_config, encoding="utf-8")
        (repo / "app").mkdir(exist_ok=True)
        (repo / "app" / "page.tsx").write_text("export default function Page() { return null; }\n", encoding="utf-8")
        if root_package is not None:
            (repo / "package.json").write_text(json.dumps(root_package), encoding="utf-8")
        lockfile_dir = repo / lockfile_subdirectory if lockfile_subdirectory else package_dir
        lockfile_dir.mkdir(parents=True, exist_ok=True)
        for name, contents in lockfiles.items():
            path = lockfile_dir / name
            if isinstance(contents, bytes):
                path.write_bytes(contents)
            else:
                path.write_text(contents, encoding="utf-8")
        if restrictive_permissions:
            for current, directories, filenames in os.walk(repo):
                Path(current).chmod(0o700)
                for filename in filenames:
                    (Path(current) / filename).chmod(0o600)
        result = NextJSAdapter().build(
            {"port": 8123, "build_config": app_build_config or {}, "route": app_route},
            repo,
            release,
            runner=runner,
            timeout=17,
            group_id=group_id,
            log=log,
        )
        return repo, release, result, package_dir.relative_to(repo)

    def successful_runner(self):
        return Mock(side_effect=[
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            for _ in range(3)
        ])

    def test_npm_lockfile_uses_ci_build_and_production_prune(self):
        package_lock = json.dumps({"lockfileVersion": 3, "packages": {}})
        runner = self.successful_runner()
        repo, release, result, _ = self.build_project({"package-lock.json": package_lock}, runner)

        calls = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(calls, [
            ["npm", "ci", "--include=dev", "--no-fund", "--no-audit"],
            ["npm", "run", "build"],
            ["npm", "prune", "--omit=dev", "--no-fund", "--no-audit"],
        ])
        self.assertEqual(
            result["start_command"],
            ["npm", "exec", "--", "next", "start", "--hostname", "127.0.0.1", "--port", "8123"],
        )
        self.assertEqual((repo / "package-lock.json").read_text(encoding="utf-8"), package_lock)
        self.assertTrue((release / "app" / "package-lock.json").is_file())

    def test_nextjs_generates_route_defaults_only_in_the_release_copy(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        repo, release, _, _ = self.build_project({}, runner, app_route="/demo/")
        self.assertFalse((repo / "next.config.mjs").exists())
        config = (release / "app" / "next.config.mjs").read_text(encoding="utf-8")
        self.assertIn("process.env.APP_BASE_PATH", config)
        self.assertIn("trailingSlash: true", config)
        check = runner.call_args_list[1].args[0]
        self.assertEqual(check[:2], ["node", "--eval"])
        self.assertEqual(check[-1], "/demo")

    def test_nextjs_preserves_existing_config_and_validates_its_route(self):
        config = "export default { basePath: '/custom', trailingSlash: true };\n"
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        repo, release, _, _ = self.build_project({}, runner, app_route="/custom/", next_config=config)
        self.assertEqual((repo / "next.config.mjs").read_text(encoding="utf-8"), config)
        release_config = release / "app" / "next.config.mjs"
        self.assertIn("process.env.APP_BASE_PATH", release_config.read_text(encoding="utf-8"))
        original_config = release / "app" / "next.config.app-manager-original.mjs"
        self.assertEqual(original_config.read_text(encoding="utf-8"), config)
        self.assertEqual(runner.call_args_list[1].args[0][-1], "/custom")

    def test_nextjs_typescript_config_is_wrapped_and_preserved_for_route_adjustment(self):
        config = "import type { NextConfig } from 'next';\nconst config: NextConfig = {};\nexport default config;\n"
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        repo, release, _, _ = self.build_project(
            {}, runner, app_route="/demo/", next_config=config, next_config_name="next.config.ts"
        )

        self.assertEqual((repo / "next.config.ts").read_text(encoding="utf-8"), config)
        release_app = release / "app"
        wrapper = (release_app / "next.config.mjs").read_text(encoding="utf-8")
        self.assertIn(
            "nextRequire('next/dist/build/next-config-ts/transpile-config')",
            wrapper,
        )
        self.assertIn(
            "transpileConfig({ nextConfigPath: originalPath, dir: dirname(originalPath) })",
            wrapper,
        )
        self.assertNotIn("@swc/core", wrapper)
        self.assertIn("process.env.APP_BASE_PATH", wrapper)
        self.assertEqual(
            (release_app / "next.config.app-manager-original.ts").read_text(encoding="utf-8"),
            config,
        )

    def test_nextjs_cjs_and_extended_typescript_configs_are_wrapped(self):
        cases = (
            ("next.config.cjs", "module.exports = {};\n", "next.config.cjs", "module.exports"),
            (
                "next.config.mts",
                "export default {};\n",
                "next.config.mjs",
                "next/dist/build/next-config-ts/transpile-config",
            ),
            (
                "next.config.cts",
                "export = {};\n",
                "next.config.mjs",
                "next/dist/build/next-config-ts/transpile-config",
            ),
        )
        for config_name, contents, wrapper_name, expected in cases:
            with self.subTest(config=config_name):
                runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
                _, release, _, _ = self.build_project(
                    {},
                    runner,
                    app_route="/demo/",
                    next_config=contents,
                    next_config_name=config_name,
                )
                release_app = release / "app"
                wrapper = (release_app / wrapper_name).read_text(encoding="utf-8")
                backup = release_app / f"next.config.app-manager-original{Path(config_name).suffix}"
                self.assertTrue(backup.is_file())
                self.assertIn(expected, wrapper)

    def test_nextjs_static_export_uses_exported_files_without_a_runtime_service(self):
        commands = []

        def runner(command, cwd, timeout):
            commands.append(command)
            if command[:2] == ["node", "--eval"]:
                return subprocess.CompletedProcess(
                    command, 0, stdout="APP_MANAGER_NEXT_STATIC_EXPORT", stderr=""
                )
            if command == ["npm", "run", "build"]:
                output = cwd / "out"
                output.mkdir()
                (output / "index.html").write_text("<h1>Exported</h1>", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        repo, release, result, _ = self.build_project(
            {},
            runner,
            app_route="/demo/",
            next_config="module.exports = { output: 'export' };\n",
            next_config_name="next.config.js",
        )

        self.assertEqual(result["runtime_required"], False)
        self.assertEqual(result["static_root"], "out")
        self.assertEqual(result["start_command"], [])
        self.assertEqual(
            (release / "app" / "out" / "index.html").read_text(encoding="utf-8"),
            "<h1>Exported</h1>",
        )
        self.assertNotIn(["npm", "prune", "--omit=dev", "--no-fund", "--no-audit"], commands)

    def test_npm_shrinkwrap_is_used_as_npm_lockfile(self):
        shrinkwrap = json.dumps({"lockfileVersion": 3, "packages": {}})
        runner = self.successful_runner()
        self.build_project({"npm-shrinkwrap.json": shrinkwrap}, runner)

        self.assertEqual(
            runner.call_args_list[0].args[0],
            ["npm", "ci", "--include=dev", "--no-fund", "--no-audit"],
        )

    def test_npm_shrinkwrap_takes_precedence_over_package_lock(self):
        shrinkwrap = json.dumps({"lockfileVersion": 3, "packages": {"source": "shrinkwrap"}})
        package_lock = json.dumps({"lockfileVersion": 3, "packages": {"source": "package-lock"}})
        runner = self.successful_runner()
        repo, release, _, _ = self.build_project(
            {
                "npm-shrinkwrap.json": shrinkwrap,
                "package-lock.json": package_lock,
            },
            runner,
        )

        self.assertEqual(
            runner.call_args_list[0].args[0],
            ["npm", "ci", "--include=dev", "--no-fund", "--no-audit"],
        )
        self.assertEqual(
            (release / "app" / "npm-shrinkwrap.json").read_text(encoding="utf-8"),
            shrinkwrap,
        )
        self.assertEqual((repo / "package-lock.json").read_text(encoding="utf-8"), package_lock)

    def test_npm_without_lockfile_uses_install_only_in_release_copy(self):
        runner = self.successful_runner()
        repo, release, _, _ = self.build_project({}, runner)

        self.assertEqual(
            runner.call_args_list[0].args[0],
            ["npm", "install", "--include=dev", "--no-fund", "--no-audit"],
        )
        self.assertFalse((repo / "package-lock.json").exists())
        self.assertTrue((release / "app" / "package.json").is_file())

    def test_nextjs_subdirectory_runs_commands_from_its_package_directory(self):
        lockfile = json.dumps({"lockfileVersion": 3, "packages": {}})
        runner = self.successful_runner()
        repo, release, result, relative_dir = self.build_project(
            {"package-lock.json": lockfile},
            runner,
            package_subdirectory="frontend",
        )

        expected_cwd = release / "app" / relative_dir
        self.assertEqual(result["working_directory"], str(expected_cwd))
        self.assertTrue(all(call.args[1] == expected_cwd for call in runner.call_args_list))
        self.assertEqual(
            [call.args[0] for call in runner.call_args_list],
            [
                ["npm", "ci", "--include=dev", "--no-fund", "--no-audit"],
                ["npm", "run", "build"],
                ["npm", "prune", "--omit=dev", "--no-fund", "--no-audit"],
            ],
        )
        self.assertFalse((repo / "frontend" / "node_modules").exists())

    def test_root_package_files_and_next_app_directory_are_copied_and_used_from_package_root(self):
        package_lock = json.dumps({"lockfileVersion": 3, "packages": {}})
        runner = self.successful_runner()
        repo, release, result, _ = self.build_project({"package-lock.json": package_lock}, runner)
        release_app = release / "app"

        self.assertEqual(result["working_directory"], str(release_app))
        self.assertTrue(all(call.args[1] == release_app for call in runner.call_args_list))
        for relative_path in ("package.json", "package-lock.json", "app/page.tsx"):
            self.assertEqual(
                (repo / relative_path).read_bytes(),
                (release_app / relative_path).read_bytes(),
            )
        self.assertEqual(runner.call_args_list[0].args[0][1], "ci")
        self.assertFalse((repo / "node_modules").exists())

    def test_npm_preflight_logs_resolved_paths_sizes_and_lockfile_version_before_install(self):
        lockfile = json.dumps({"lockfileVersion": 3, "packages": {}})
        runner = self.successful_runner()
        messages = []
        _, release, _, _ = self.build_project(
            {"package-lock.json": lockfile},
            runner,
            log=messages.append,
        )

        preflight = next(message for message in messages if "Next.js npm preflight" in message)
        install = next(message for message in messages if "Installing Next.js dependencies" in message)
        self.assertLess(messages.index(preflight), messages.index(install))
        self.assertIn(str((release / "app").resolve()), preflight)
        self.assertIn("package-lock.json", preflight)
        self.assertIn("lockfileVersion=3", preflight)
        self.assertIn("size:", preflight)
        self.assertIn("is_file: True", preflight)
        self.assertIn("app_group_readable: True", preflight)

    @unittest.skipIf(os.name == "nt", "POSIX group ownership is required")
    def test_release_copy_is_owned_and_writable_by_the_application_group(self):
        runner = self.successful_runner()
        lockfile = json.dumps({"lockfileVersion": 3, "packages": {}})
        messages = []
        repo, release, _, _ = self.build_project(
            {"package-lock.json": lockfile},
            runner,
            group_id=os.getgid(),
            log=messages.append,
            restrictive_permissions=True,
        )

        package_path = release / "app" / "package.json"
        release_app = release / "app"
        copied_lockfile = release_app / "package-lock.json"
        self.assertEqual(package_path.stat().st_gid, os.getgid())
        self.assertEqual(package_path.stat().st_mode & 0o060, 0o060)
        self.assertEqual(copied_lockfile.stat().st_gid, os.getgid())
        self.assertEqual(copied_lockfile.stat().st_mode & 0o060, 0o060)
        self.assertEqual(release_app.stat().st_mode & 0o070, 0o070)
        self.assertEqual(repo.joinpath("package.json").stat().st_mode & 0o777, 0o600)
        preflight = next(message for message in messages if "Next.js npm preflight" in message)
        self.assertIn("app_group_readable: True", preflight)

    def test_nextjs_route_config_permissions_are_reapplied_after_dependency_install(self):
        events = []
        original_chmod = os.chmod

        def runner(args, cwd, timeout):
            config_path = cwd / "next.config.mjs"
            events.append(("command", args[0]))
            if args[0] == "npm":
                config_path.write_text("export default {};\n", encoding="utf-8")
                if os.name != "nt":
                    original_chmod(str(config_path), 0o200)
            elif args[0] == "node":
                npm_index = max(
                    index for index, event in enumerate(events)
                    if event == ("command", "npm")
                )
                restored = [
                    event[2] for event in events[npm_index + 1:]
                    if event[0] == "chmod" and event[1] == config_path
                ]
                self.assertTrue(restored)
                self.assertEqual(restored[-1] & 0o060, 0o060)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        def record_chmod(path, mode, **kwargs):
            events.append(("chmod", Path(path), mode))

        with patch("backend.adapters._prepare_build_copy_permissions"), patch(
            "backend.adapters.os.chown", create=True
        ), patch("backend.adapters.os.chmod", side_effect=record_chmod):
            self.build_project({}, runner, group_id=1234, app_route="/demo/")

    def test_explicit_application_directory_is_used_by_the_build_adapter(self):
        runner = self.successful_runner()
        _, release, result, relative_dir = self.build_project(
            {},
            runner,
            package_subdirectory="frontend",
            app_build_config={"app_directory": "frontend"},
        )

        expected_cwd = release / "app" / relative_dir
        self.assertEqual(result["working_directory"], str(expected_cwd))
        self.assertTrue(all(call.args[1] == expected_cwd for call in runner.call_args_list))

    def test_nextjs_npm_workspace_uses_root_lockfile_and_package_working_directory(self):
        package_lock = json.dumps({"lockfileVersion": 3, "packages": {}})
        runner = self.successful_runner()
        repo, release, result, relative_dir = self.build_project(
            {"package-lock.json": package_lock},
            runner,
            package_subdirectory="apps/web",
            root_package={
                "private": True,
                "workspaces": ["apps/*"],
                "dependencies": {"next": "^15.0.0"},
            },
            lockfile_subdirectory=".",
        )

        app_dir = release / "app" / relative_dir
        workspace_root = release / "app"
        self.assertEqual(result["working_directory"], str(app_dir))
        self.assertEqual(
            [call.args[0] for call in runner.call_args_list],
            [
                ["npm", "--prefix", str(workspace_root), "ci", "--include=dev", "--no-fund", "--no-audit"],
                ["npm", "run", "build"],
                ["npm", "--prefix", str(workspace_root), "prune", "--omit=dev", "--no-fund", "--no-audit"],
            ],
        )
        self.assertTrue(all(call.args[1] == app_dir for call in runner.call_args_list))
        self.assertTrue((repo / "package-lock.json").is_file())
        self.assertEqual((repo / "package-lock.json").read_text(encoding="utf-8"), package_lock)
        self.assertFalse((repo / "node_modules").exists())

    def test_explicit_application_directory_resolves_multiple_next_apps(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for name in ("apps/admin", "apps/site"):
                package_dir = repo / name
                package_dir.mkdir(parents=True)
                (package_dir / "package.json").write_text(
                    '{"dependencies":{"next":"^15"}}',
                    encoding="utf-8",
                )
            config = {"app_directory": "apps/site"}

            self.assertEqual(detect_framework(repo, config), "nextjs")
            with self.assertRaisesRegex(ValueError, "Multiple Next.js applications"):
                detect_framework(repo)

    def test_invalid_npm_lockfile_is_rejected_before_install(self):
        runner = self.successful_runner()
        with self.assertRaisesRegex(ValueError, "lockfileVersion.*one of 1, 2, or 3"):
            self.build_project({"package-lock.json": '{"lockfileVersion":0,"packages":{}}'}, runner)
        runner.assert_not_called()

    def test_unsupported_npm_lockfile_version_is_rejected_before_install(self):
        runner = self.successful_runner()
        with self.assertRaisesRegex(ValueError, "Unsupported npm lockfile.*lockfileVersion"):
            self.build_project({"package-lock.json": '{"lockfileVersion":4,"packages":{}}'}, runner)
        runner.assert_not_called()

    def test_npm_lockfile_with_missing_required_structure_is_rejected(self):
        runner = self.successful_runner()
        with self.assertRaisesRegex(ValueError, "requires a packages object"):
            self.build_project({"package-lock.json": '{"lockfileVersion":3}'}, runner)
        runner.assert_not_called()

    def test_malformed_npm_lockfile_is_not_treated_as_missing(self):
        runner = self.successful_runner()
        with self.assertRaisesRegex(ValueError, "Invalid npm lockfile"):
            self.build_project({"package-lock.json": '{"lockfileVersion":3,'}, runner)
        runner.assert_not_called()

    def test_incorrect_explicit_working_directory_is_reported_before_install(self):
        runner = self.successful_runner()
        with self.assertRaisesRegex(ValueError, "application directory does not exist"):
            self.build_project(
                {},
                runner,
                app_build_config={"app_directory": "apps/missing"},
            )
        runner.assert_not_called()

    def test_missing_lock_install_failure_reports_command_and_does_not_claim_inconsistency(self):
        stdout = "npm install output"
        stderr = "npm error code ENOENT"
        runner = Mock(return_value=subprocess.CompletedProcess(
            args=["npm", "install"],
            returncode=127,
            stdout=stdout,
            stderr=stderr,
        ))

        with self.assertRaises(RuntimeError) as raised:
            self.build_project({}, runner, package_subdirectory="web")

        message = str(raised.exception)
        self.assertIn("Application working directory:", message)
        self.assertIn("Package manager: npm", message)
        self.assertIn("Lockfile: none", message)
        self.assertIn("Command: npm install --include=dev --no-fund --no-audit", message)
        self.assertIn("Exit code: 127", message)
        self.assertIn(stdout, message)
        self.assertIn(stderr, message)
        self.assertNotIn("inconsistent", message.lower())

    def test_inconsistent_npm_lockfile_reports_complete_output_without_fallback(self):
        package_lock = json.dumps({"lockfileVersion": 3, "packages": {}})
        stdout = "npm ci started\n" + ("install output\n" * 600)
        stderr = "npm error code EUSAGE\npackage.json and package-lock.json must be in sync\n"
        runner = Mock(return_value=subprocess.CompletedProcess(
            args=["npm", "ci"],
            returncode=1,
            stdout=stdout,
            stderr=stderr,
        ))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name) / "repo"
        release = Path(temporary.name) / "release"
        repo.mkdir()
        release.mkdir()
        package_json = {
            "dependencies": {"next": "^15.0.0"},
            "scripts": {"build": "next build"},
        }
        (repo / "package.json").write_text(json.dumps(package_json), encoding="utf-8")
        (repo / "package-lock.json").write_text(package_lock, encoding="utf-8")

        with self.assertRaises(RuntimeError) as raised:
            NextJSAdapter().build({"port": 8123}, repo, release, runner=runner, timeout=17)

        message = str(raised.exception)
        self.assertIn("inconsistent with package.json", message)
        self.assertIn(stdout, message)
        self.assertIn(stderr.strip(), message)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual((repo / "package-lock.json").read_text(encoding="utf-8"), package_lock)

    def test_pnpm_yarn_and_bun_use_lockfile_specific_commands(self):
        cases = (
            (
                {"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
                ["pnpm", "install", "--frozen-lockfile", "--prod=false"],
                ["pnpm", "run", "build"],
                ["pnpm", "install", "--frozen-lockfile", "--prod=true"],
                ["pnpm", "exec", "next", "start", "--hostname", "127.0.0.1", "--port", "8123"],
            ),
            (
                {"yarn.lock": "# yarn lockfile"},
                ["yarn", "install", "--frozen-lockfile", "--production=false"],
                ["yarn", "run", "build"],
                ["yarn", "install", "--frozen-lockfile", "--production=true"],
                ["yarn", "run", "next", "start", "--hostname", "127.0.0.1", "--port", "8123"],
            ),
            (
                {"bun.lock": "{\"lockfileVersion\":1}"},
                ["bun", "install", "--frozen-lockfile"],
                ["bun", "run", "build"],
                ["bun", "install", "--frozen-lockfile", "--production"],
                ["bun", "run", "next", "start", "--hostname", "127.0.0.1", "--port", "8123"],
            ),
            (
                {"bun.lockb": b"\x00bun-lock"},
                ["bun", "install", "--frozen-lockfile"],
                ["bun", "run", "build"],
                ["bun", "install", "--frozen-lockfile", "--production"],
                ["bun", "run", "next", "start", "--hostname", "127.0.0.1", "--port", "8123"],
            ),
        )
        for lockfiles, install, build, production, start in cases:
            with self.subTest(lockfile=next(iter(lockfiles))):
                runner = self.successful_runner()
                repo, _, result, _ = self.build_project(lockfiles, runner)
                self.assertEqual(
                    [call.args[0] for call in runner.call_args_list],
                    [install, build, production],
                )
                self.assertEqual(result["start_command"], start)
                self.assertTrue(all(call.args[1] == repo.parent / "release" / "app" for call in runner.call_args_list))
                self.assertTrue(all(call.args[2] == 17 for call in runner.call_args_list))

    def test_failed_dependency_install_preserves_stdout_and_stderr(self):
        stdout = "resolver output\n" + ("package detail\n" * 600)
        stderr = "ERR_PNPM_FETCH_500: registry unavailable"
        runner = Mock(return_value=subprocess.CompletedProcess(
            args=["pnpm", "install"],
            returncode=1,
            stdout=stdout,
            stderr=stderr,
        ))

        with self.assertRaises(RuntimeError) as raised:
            self.build_project({"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"}, runner)

        self.assertIn("Installing Next.js dependencies with pnpm failed", str(raised.exception))
        self.assertIn(stdout, str(raised.exception))
        self.assertIn(stderr, str(raised.exception))
        self.assertEqual(runner.call_count, 1)


class FrameworkAdapterTests(unittest.TestCase):
    @staticmethod
    def _go_metadata(arguments, cwd):
        if arguments[:3] == ["go", "env", "-json"]:
            return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps({
                "GOHOSTOS": "linux", "GOHOSTARCH": "amd64", "GOOS": "windows", "GOARCH": "arm64",
            }), stderr="")
        if "list" in arguments:
            return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps({"Name": "main", "Dir": str(cwd)}), stderr="")
        return None

    def test_project_inspection_checks_startup_without_executing_repository_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text('{"scripts":{"build":"compile"}}', encoding="utf-8")
            report = inspect_project(root, "node", {})
            self.assertIn("start script", report["errors"][0])
            self.assertFalse(inspect_project(root, "node", {"start_command": ["node", "server.js"]})["errors"])
            (root / "requirements.txt").write_text("", encoding="utf-8")
            self.assertIn("app:app", inspect_project(root, "python", {})["errors"][0])
            (root / "app.py").write_text("app = object()\nraise RuntimeError('must never be imported')", encoding="utf-8")
            self.assertFalse(inspect_project(root, "python", {})["errors"])
            (root / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()", encoding="utf-8")
            self.assertTrue(any("ASGI" in error for error in inspect_project(root, "python", {})["errors"]))
            self.assertFalse(inspect_project(root, "python", {"start_command": ["uvicorn", "app:app"]})["errors"])
            self.assertTrue(inspect_project(root, "static", {})["errors"])

    def test_static_build_requires_no_runtime_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "index.html").write_text("<h1>Ready</h1>", encoding="utf-8")
            runner = Mock()
            result = get_adapter("static").build({"port": 8123}, source, root / "release", runner=runner)
            self.assertFalse(result["runtime_required"])
            self.assertEqual(result["start_command"], [])
            runner.assert_not_called()

    def test_static_html_assets_are_scoped_without_changing_source_or_other_apps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "assets").mkdir()
            (source / "assets" / "icon.svg").write_text("<svg/>", encoding="utf-8")
            page = '<!doctype html>\n<link rel="icon" href="/assets/icon.svg?v=1&amp;x=2"><img src="/assets/icon.svg"/><a href="/other-app/">Other</a><script>const url="/assets/icon.svg";</script><img src="//cdn.example/icon.svg"><img src="/docs/assets/icon.svg">'
            (source / "index.html").write_text(page, encoding="utf-8")
            result = get_adapter("static").build({"port": 8123, "route": "/docs/"}, source, root / "release")
            output = (Path(result["release_dir"]) / "app" / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="/docs/assets/icon.svg?v=1&amp;x=2"', output)
            self.assertIn('src="/docs/assets/icon.svg"', output)
            self.assertIn('href="/other-app/"', output)
            self.assertIn('const url="/assets/icon.svg";', output)
            self.assertIn('src="//cdn.example/icon.svg"', output)
            self.assertNotIn("/docs/docs/", output)
            self.assertEqual((source / "index.html").read_text(encoding="utf-8"), page)

    def test_go_builds_an_isolated_binary_and_preserves_static_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            (source / "icon.svg").write_text("<svg/>", encoding="utf-8")

            def build_binary(arguments, cwd, timeout, *, preserve_stdout=False):
                metadata = self._go_metadata(arguments, cwd)
                if metadata is not None:
                    return metadata
                binary = root / "release" / "server"
                binary.write_bytes(b"\x7fELF" + b"\0" * 60)
                binary.chmod(0o750)
                return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

            runner = Mock(side_effect=build_binary)
            result = get_adapter("go").build({"port": 8123}, source, root / "release", runner=runner)
            self.assertEqual(runner.call_count, 3)
            runner.assert_called_with(["env", "GOOS=linux", "GOARCH=amd64", "go", "build", "-o", str(root / "release" / "server"), "."], root / "release" / "app", 900)
            self.assertEqual(result["start_command"], [str(root / "release" / "server")])
            self.assertTrue((root / "release" / "app" / "icon.svg").is_file())

    def test_go_selects_main_packages_without_guessing_between_servers(self):
        cases = (
            ([("library", "."), ("main", "cmd/server")], None, "./cmd/server", None),
            ([("main", "."), ("main", "cmd/tool")], None, ".", None),
            ([("main", "cmd/api"), ("main", "cmd/worker")], None, None, "Multiple Go main packages"),
            ([("library", ".")], None, None, "No executable Go main package"),
            ([("main", "cmd/api")], "./cmd/api", "./cmd/api", None),
        )
        for packages, configured, expected, error in cases:
            with self.subTest(packages=packages, configured=configured), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                (source / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

                def runner(arguments, cwd, timeout, *, preserve_stdout=False):
                    if "list" in arguments:
                        self.assertEqual(arguments[-1], configured or "./...")
                        output = "\n".join(json.dumps({"Name": name, "Dir": str(cwd / directory)}) for name, directory in packages)
                        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")
                    metadata = self._go_metadata(arguments, cwd)
                    if metadata is not None:
                        return metadata
                    self.assertEqual(arguments[-1], expected)
                    binary = root / "release" / "server"
                    binary.write_bytes(b"\x7fELF" + b"\0" * 60)
                    binary.chmod(0o750)
                    return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

                build_config = {} if configured is None else {"go_package": configured}
                log = Mock()
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        get_adapter("go").build({"port": 8123, "build_config": build_config}, source, root / "release", runner=runner)
                else:
                    get_adapter("go").build({"port": 8123, "build_config": build_config}, source, root / "release", runner=runner, log=log)
                    self.assertIn("linux/amd64", log.call_args.args[0])
                    self.assertIn("package=" + expected, log.call_args.args[0])

    def test_go_metadata_survives_redaction_collisions_in_the_real_command_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "website-list"
            source = root / "source"
            source.mkdir(parents=True)
            (source / "go.mod").write_text("module example.com/stuserver\n", encoding="utf-8")
            log = root / "deployment.log"

            def runner(arguments, cwd, timeout, *, preserve_stdout=False):
                if "build" in arguments:
                    self.assertEqual(arguments[-1], "./cmd/server")
                    binary = root / "release" / "server"
                    binary.write_bytes(b"\x7fELF" + b"\0" * 60)
                    binary.chmod(0o750)
                    return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")
                if "list" in arguments:
                    output = "\n".join(json.dumps({"Name": name, "Dir": str(cwd / relative)}) for name, relative in (
                        ("stuserver", "."), ("main", "cmd/server"), ("config", "internal/config"),
                    ))
                else:
                    output = self._go_metadata(arguments, cwd).stdout
                return manager.run_command(
                    [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", output],
                    timeout=timeout, live_log_path=log, redact_values=["website-list", "amd64", "main"],
                    preserve_stdout=preserve_stdout,
                )

            result = get_adapter("go").build({"port": 8123}, source, root / "release", runner=runner)
            self.assertEqual(result["start_command"], [str(root / "release" / "server")])
            output = log.read_text(encoding="utf-8")
            self.assertNotIn("website-list", output)
            self.assertNotIn("amd64", output)
            self.assertNotIn('"main"', output)
            self.assertIn("[REDACTED]", output)

    def test_go_rejects_successful_builds_without_a_linux_executable(self):
        for contents in (None, b"!<arch>\nGo library archive", b"MZWindows executable", b"\xcf\xfa\xed\xfeMach-O"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                (source / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

                def runner(arguments, cwd, timeout, *, preserve_stdout=False):
                    metadata = self._go_metadata(arguments, cwd)
                    if metadata is not None:
                        return metadata
                    if contents is not None:
                        (root / "release" / "server").write_bytes(contents)
                    return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

                with self.assertRaisesRegex(RuntimeError, "regular server executable|Linux ELF executable"):
                    get_adapter("go").build({"port": 8123}, source, root / "release", runner=runner)

    @unittest.skipIf(os.name == "nt", "requires POSIX executable permission bits")
    def test_go_rejects_a_binary_without_execute_permission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

            def runner(arguments, cwd, timeout, *, preserve_stdout=False):
                metadata = self._go_metadata(arguments, cwd)
                if metadata is not None:
                    return metadata
                binary = root / "release" / "server"
                binary.write_bytes(b"\x7fELF" + b"\0" * 60)
                binary.chmod(0o640)
                return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

            with self.assertRaisesRegex(RuntimeError, "no execute permission"):
                get_adapter("go").build({"port": 8123}, source, root / "release", runner=runner)

    def test_python_uses_release_venv_and_loopback_gunicorn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "requirements.txt").write_text("flask\n", encoding="utf-8")
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
            result = get_adapter("python").build({"port": 8123}, source, root / "release", runner=runner)
            self.assertEqual(result["start_command"], [str(root / "release" / "app" / ".venv" / "bin" / "gunicorn"), "--bind", "127.0.0.1:8123", "app:app"])
            self.assertEqual(runner.call_args_list[0].args[0], ["python3", "-m", "venv", ".venv"])

    def test_python_custom_start_resolves_the_release_virtual_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "requirements.txt").write_text("uvicorn\nfastapi\n", encoding="utf-8")
            command = ["uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8123"]

            def runner(arguments, cwd, timeout):
                executable = cwd / ".venv" / "bin" / "uvicorn"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("entry point", encoding="utf-8")
                return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

            result = get_adapter("python").build(
                {"port": 8123, "build_config": {"start_command": command}}, source, root / "release", runner=runner,
            )
            self.assertEqual(result["start_command"][0], str(root / "release" / "app" / ".venv" / "bin" / "uvicorn"))
            self.assertEqual(result["start_command"][1:], command[1:])
            self.assertEqual(command[0], "uvicorn")

    def test_node_uses_lockfile_package_manager_without_injecting_start_flags(self):
        for package_manager, lockfile in (
            ("npm", None), ("pnpm", "pnpm-lock.yaml"),
            ("yarn", "yarn.lock"), ("bun", "bun.lock"),
        ):
            with self.subTest(package_manager=package_manager), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                (source / "package.json").write_text(json.dumps({
                    "scripts": {"build": "compile", "start": "node server.js"},
                }), encoding="utf-8")
                if lockfile:
                    (source / lockfile).write_text("lockfile", encoding="utf-8")
                runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
                result = get_adapter("node").build(
                    {"port": 8123}, source, root / "release", runner=runner,
                )
                self.assertEqual(result["start_command"], [package_manager, "run", "start"])
                self.assertEqual(runner.call_args_list[0].args[0][0], package_manager)
                self.assertIn(mock_call([package_manager, "run", "build"], root / "release" / "app", 900), runner.call_args_list)

    def test_node_requires_a_start_script_or_explicit_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text('{"scripts":{"build":"compile"}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "start script"):
                get_adapter("node").build({"port": 8123}, root, root / "release")

    def test_node_accepts_an_explicit_start_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "package.json").write_text("{}", encoding="utf-8")
            command = ["node", "dist/server.js"]
            result = get_adapter("node").build(
                {"port": 8123, "build_config": {"start_command": command}},
                source, root / "release",
                runner=Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")),
            )
            self.assertEqual(result["start_command"], command)

    def test_node_rejects_malformed_package_manifest(self):
        for contents in ("[]", '{"scripts":[]}', "not json"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "package.json").write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "package.json"):
                    get_adapter("node").build({"port": 8123}, root, root / "release")


class InfrastructureConfigurationTests(unittest.TestCase):
    def _nginx_site_fixture(self, root: Path, contents: str) -> tuple[Path, Path, str]:
        config_root = root / "etc" / "nginx"
        site_path = config_root / "sites-available" / "stuserver"
        site_path.parent.mkdir(parents=True)
        site_path.write_text(contents, encoding="utf-8")
        listing = f"# configuration file {site_path}:\n{contents}"
        return config_root, site_path, listing

    def test_cockpit_manifest_registers_menu_entry_and_installed_assets(self):
        project = Path(__file__).resolve().parent.parent
        cockpit_dir = project / "cockpit"
        manifest_path = cockpit_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        menu_entry = manifest["menu"]["app-manager"]
        html_path = cockpit_dir / menu_entry["path"]
        html = html_path.read_text(encoding="utf-8")
        installer = (project / "scripts" / "install.sh").read_text(encoding="utf-8")

        self.assertEqual(manifest["name"], "app-manager")
        self.assertEqual(menu_entry["label"], "App Manager")
        self.assertEqual(menu_entry["path"], "index.html")
        self.assertEqual(menu_entry["order"], 60)
        self.assertTrue(html_path.is_file())
        self.assertTrue((cockpit_dir / "app-manager.css").is_file())
        self.assertTrue((cockpit_dir / "app-manager.js").is_file())
        self.assertIn('<script src="app-manager.js" defer></script>', html)
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/manifest.json" "$PLUGIN_DEST/manifest.json"',
            installer,
        )
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/index.html" "$PLUGIN_DEST/index.html"',
            installer,
        )
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.css" "$PLUGIN_DEST/app-manager.css"',
            installer,
        )
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.js" "$PLUGIN_DEST/app-manager.js"',
            installer,
        )
        self.assertIn('<link rel="stylesheet" href="app-manager.css">', html)
        frontend = (cockpit_dir / "app-manager.js").read_text(encoding="utf-8")
        self.assertIn("cockpitApi.spawn([BACKEND, command, ...args]", frontend)
        self.assertIn("globalThis.cockpit", frontend)
        self.assertIn("Cockpit API is unavailable.", frontend)
        self.assertIn('const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";', frontend)
        for action in ("deploy", "pull", "edit", "health", "environment", "restart", "stop", "start", "logs", "remove"):
            self.assertIn(f'data-action="{action}"', frontend)
        for endpoint in (
            'callBackend("add", [JSON.stringify(setup.payload)])',
            'callBackend("edit", [app.id, JSON.stringify(payload)])',
            'callBackend("logs", [appId, "--lines", "200"])',
            'callBackend("health", [appId])',
            'callBackend("remove", [appId])',
            '() => callBackend(action, [appId])',
            'callBackend("deployment-logs", args)',
            'callBackend("deployment-start", [appId])',
            'callBackend("deployment-abort", [',
            'callBackend("plugin-update-check")',
            'callBackend("plugin-update")',
            'callBackend("env-get", [app.id])',
            'callBackendInput("env-save", [app.id], content)',
        ):
            self.assertIn(endpoint, frontend)
        self.assertIn('accept=".env,.env.*,.txt,text/plain"', frontend)
        self.assertIn('download = ".env"', frontend)
        self.assertIn("one KEY=value assignment per line", frontend)
        self.assertIn("function serializeEnvironmentRows(rows)", frontend)
        self.assertIn("data-env-remove", frontend)
        self.assertIn("am-log-line--${level}", frontend)
        self.assertIn('id="checkUpdateButton"', html)
        self.assertIn('function deployWithLiveLog(appId)', frontend)
        self.assertIn('class="am-log-output"', frontend)
        self.assertIn('if (result.available)', frontend)
        backend = (project / "backend" / "app_manager.py").read_text(encoding="utf-8")
        self.assertIn('EnvironmentFile=-{str(paths[\'environment\']).replace(\'%\', \'%%\')}', backend)
        self.assertIn("def get_environment_app(", backend)
        self.assertIn("def save_environment_app(", backend)
        self.assertIn('PLUGIN_REPOSITORY_URL = "https://github.com/stuartjeet122/cockpitdeploymentplugin.git"', backend)
        self.assertIn('PLUGIN_UPDATE_USER = "nobody"', backend)
        self.assertIn('["runuser", "--user", PLUGIN_UPDATE_USER, "--", *env_args]', backend)
        self.assertIn('["bash", str(installer)]', backend)
        self.assertIn('target="_blank" rel="noopener noreferrer"', frontend)
        self.assertIn('Another operation is still in progress.', frontend)
        self.assertIn('updateConnection("error", "Could not load data")', frontend)
        self.assertIn('id="addAppFormTemplate"', html)
        self.assertIn('data-open-add', html)
        self.assertNotIn('data-tab="add-app"', html)
        self.assertIn('function openAddApplicationDialog()', frontend)
        self.assertIn('byId("addAppFormTemplate").innerHTML', frontend)
        self.assertIn('function suggestSlug(value)', frontend)
        self.assertIn('role="tablist"', html)
        self.assertIn('type="text" inputmode="url"', html)
        self.assertIn('autocomplete="url"', html)

    def test_cockpit_api_reference_is_package_relative_and_initialization_waits(self):
        project = Path(__file__).resolve().parent.parent
        cockpit_dir = project / "cockpit"
        html = (cockpit_dir / "index.html").read_text(encoding="utf-8")
        frontend = (cockpit_dir / "app-manager.js").read_text(encoding="utf-8")
        installer = (project / "scripts" / "install.sh").read_text(encoding="utf-8")

        self.assertIn('<script src="../base1/cockpit.js"></script>', html)
        self.assertNotIn("/cockpit/login.js", html)
        self.assertNotIn("/cockpit/cockpit.js", html)
        self.assertIn("async function waitForCockpitApi(", frontend)
        self.assertIn("await waitForCockpitApi();", frontend)
        self.assertLess(
            frontend.index("await waitForCockpitApi();"),
            frontend.index("await runOperation(refreshData,"),
        )
        self.assertIn("Cockpit API failed to load from ../base1/cockpit.js.", frontend)
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/index.html" "$PLUGIN_DEST/index.html"',
            installer,
        )

        for source in cockpit_dir.glob("*.js"):
            contents = source.read_text(encoding="utf-8")
            self.assertNotIn("/cockpit/login.js", contents, source.name)
            self.assertNotIn("/cockpit/cockpit.js", contents, source.name)

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
        self.assertIn("MemoryMax=1024M", service)
        self.assertIn("CPUQuota=100%", service)
        self.assertIn("TasksMax=256", service)
        self.assertIn("CapabilityBoundingSet=\n", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", service)
        self.assertIn("Environment=\"APP_BASE_PATH=/demo\"", service)
        self.assertIn(
            f"EnvironmentFile=-{paths['environment']}",
            service,
        )
        self.assertNotIn('EnvironmentFile=-"', service)
        self.assertNotIn("CPUAccounting=", service)
        self.assertIn(f"ReadOnlyPaths={manager._systemd_quote(str(paths['releases']))}", service)
        template = (Path(__file__).resolve().parent.parent / "templates" / "app.service.tpl").read_text(
            encoding="utf-8"
        )
        self.assertIn("ReadOnlyPaths={{ app_root }}/releases", template)
        self.assertIn("EnvironmentFile=-{{ app_env_file }}", template)
        self.assertNotIn("CPUAccounting=", template)
        self.assertIn("CapabilityBoundingSet=", template)
        self.assertNotIn("CAP_NET_BIND_SERVICE", service)
        self.assertNotIn("AmbientCapabilities=", service)
        custom_route_service = manager._render_service(
            {**app, "route": "/public-site/"},
            ["/opt/webapps/demo/current/app/server"],
            config,
        )
        self.assertIn('Environment="APP_BASE_PATH=/public-site"', custom_route_service)
        expected_write_paths = "ReadWritePaths=" + " ".join(
            manager._systemd_quote(str(path))
            for path in (paths["home"], paths["config"], paths["logs"])
        )
        self.assertIn(expected_write_paths, service)
        read_write_paths = next(line for line in service.splitlines() if line.startswith("ReadWritePaths="))
        self.assertNotIn(manager._systemd_quote(str(paths["current"] / "app")), read_write_paths)
        self.assertNotIn(manager._systemd_quote(str(paths["releases"])), read_write_paths)

    def test_environment_file_directive_is_unquoted_and_escapes_systemd_specifiers(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            manager, "DEFAULT_STATE_ROOT", str(Path(temporary) / "state 100%")
        ):
            config = manager._default_config()
            app = {"id": "demo", "service_user": "app-demo", "port": 8100}
            path = manager._app_paths("demo", config)["environment"]
            service = manager._render_service(app, ["/usr/bin/true"], config)
        directive = next(line for line in service.splitlines() if line.startswith("EnvironmentFile="))
        self.assertEqual(directive, "EnvironmentFile=-" + str(path).replace("%", "%%"))
        self.assertNotIn('"', directive)
        self.assertIn("state 100%%", directive)

    def test_auto_deploy_service_can_write_nginx_log_directory(self):
        template = (Path(__file__).resolve().parent.parent / "templates" / "auto-deploy.service").read_text(
            encoding="utf-8"
        )
        read_write_paths = next(
            line for line in template.splitlines() if line.startswith("ReadWritePaths=")
        )
        self.assertIn("/var/lib/cockpit/app-manager", read_write_paths)
        self.assertIn("/etc/nginx", read_write_paths)
        self.assertIn("/var/log/nginx", read_write_paths)
        self.assertIn("PrivateUsers=false", template)

    def test_resource_limits_reject_unsafe_values_and_persist(self):
        for value in ({"memory_mb": True}, {"tasks_max": 0}, {"unknown": 1}, "unlimited"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manager._resource_limits(value)
        app = {"id": "demo", "service_user": "app-demo", "port": 8100,
               "resource_limits": manager._resource_limits({"memory_mb": 512})}
        self.assertEqual(app["resource_limits"]["memory_mb"], 512)
        self.assertIn("MemoryMax=512M", manager._render_service(app, ["node", "server.js"], manager._default_config()))

    def test_build_commands_have_cgroup_resource_limits(self):
        with patch.object(manager, "_require_root"), patch.object(manager.os, "name", "posix"), patch.object(
            manager, "_account_exists", return_value=True
        ), patch.object(manager, "_home_path", return_value="/managed/home"), patch.object(manager, "run_command") as run:
            manager._run_as_user("app-demo", ["npm", "ci"], resource_limits={"build_memory_mb": 768})
        command = run.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--property=MemoryMax=768M", command)
        self.assertIn("runuser", command)
        self.assertIn("app-demo", command)

    def test_nextjs_service_runs_with_production_node_environment(self):
        config = manager._default_config()
        app = {
            "id": "next-demo",
            "service_user": "app-next-demo",
            "port": 8123,
            "framework": "nextjs",
        }

        service = manager._render_service(app, ["/opt/webapps/next-demo/current/app/next"], config)

        self.assertIn('Environment="NODE_ENV=production"', service)

    def test_service_can_use_nested_nextjs_working_directory(self):
        config = manager._default_config()
        app = {"id": "demo", "service_user": "app-demo", "port": 8123, "framework": "nextjs"}
        nested_directory = manager._app_paths("demo", config)["current"] / "app" / "frontend"

        service = manager._render_service(
            app,
            ["npm", "exec", "--", "next", "start"],
            config,
            nested_directory,
        )

        self.assertIn(f"WorkingDirectory={nested_directory}", service)

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
        self.assertIn("return 308 /website1/$is_args$args;", nextjs)
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
            self.assertIn("proxy_redirect http://127.0.0.1:8123/ /website1/;", stripped_prefix)
            self.assertIn("proxy_cookie_path / /website1/;", stripped_prefix)
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
        exported = manager._render_nginx_config(
            app_id, 8123, template, "static", config=config, static_root="apps/web/out"
        )
        exported_root = static_root / "apps" / "web" / "out"
        self.assertIn(f"alias {manager._nginx_quote(str(exported_root) + os.sep)};", exported)
        with self.assertRaises(ValueError):
            manager._render_nginx_config(
                app_id, 8123, template, "static", config=config, static_root="../outside"
            )
        self.assertNotIn("proxy_pass http://127.0.0.1:8123/;", nextjs)
        with self.assertRaises(ValueError):
            manager._render_nginx_config("../outside", 8123, template, "nextjs")
        with self.assertRaises(ValueError):
            manager._render_nginx_config("demo", 8080, template, "nextjs")

    def test_nginx_config_uses_custom_application_route(self):
        template = Path(__file__).resolve().parent.parent / "templates" / "nginx.conf.tpl"
        config = manager._render_nginx_config(
            "website1",
            8123,
            template,
            "node",
            route="/customer-portal/",
        )
        self.assertIn("location = /customer-portal {", config)
        self.assertIn("return 308 /customer-portal/$is_args$args;", config)
        self.assertIn("location ^~ /customer-portal/ {", config)
        self.assertIn("proxy_set_header X-Forwarded-Prefix /customer-portal;", config)
        self.assertIn("proxy_redirect / /customer-portal/;", config)
        self.assertNotIn("/website1/", config)

    def test_nginx_deploy_adds_managed_include_to_matching_https_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snippet_root = root / "etc" / "nginx" / "snippets" / "cockpit-app-manager"
            config = manager._default_config()
            config["nginx_template"] = (
                Path(__file__).resolve().parent.parent / "templates" / "nginx.conf.tpl"
            )
            config_root, site_path, listing = self._nginx_site_fixture(
                root,
                "server {\n    listen 443 ssl;\n    server_name stuserver.online;\n    location / { proxy_pass http://127.0.0.1:8080; }\n}\n",
            )
            app = {"id": "demo", "port": 8123, "route": "/customer-portal/"}
            valid = subprocess.CompletedProcess(args=["nginx", "-t"], returncode=0, stdout="", stderr="")

            def run(args, **kwargs):
                if args == ["nginx", "-T"]:
                    site_contents = site_path.read_text(encoding="utf-8")
                    output = f"# configuration file {site_path}:\n{site_contents}"
                    snippet = snippet_root / "demo.conf"
                    if "include /etc/nginx/snippets/cockpit-app-manager/*.conf;" in site_contents:
                        output += f"\n# configuration file {snippet}:\n{snippet.read_text(encoding='utf-8')}"
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")
                return valid

            with patch.object(manager, "DEFAULT_NGINX_CONFIG_ROOT", str(config_root)), patch.object(
                manager, "DEFAULT_NGINX_SNIPPET_DIR", str(snippet_root)
            ), patch.object(manager, "run_command", side_effect=run) as run_command, patch.object(
                manager, "DEFAULT_STATE_ROOT", str(root / "state")
            ), patch.object(manager, "_systemctl") as systemctl:
                activation = manager._activate_nginx_config(app, "node", config)

            snippet = snippet_root / "demo.conf"
            self.assertTrue(snippet.is_file())
            self.assertIn("location ^~ /customer-portal/ {", snippet.read_text(encoding="utf-8"))
            site_contents = site_path.read_text(encoding="utf-8")
            self.assertIn("server_name stuserver.online;", site_contents)
            self.assertIn("location / { proxy_pass http://127.0.0.1:8080; }", site_contents)
            self.assertIn("include /etc/nginx/snippets/cockpit-app-manager/*.conf;", site_contents)
            self.assertTrue(activation["active"])
            self.assertIn("Added the managed include", activation["message"])
            self.assertEqual([call.args[0] for call in run_command.call_args_list], [
                ["nginx", "-T"], ["nginx", "-t"], ["nginx", "-T"],
            ])
            systemctl.assert_called_once_with(
                "reload",
                "nginx",
                timeout=config["command_timeout_seconds"],
            )
            backup = next((root / "state" / "nginx-backups").glob("*.before-app-manager"))
            self.assertIn("server_name stuserver.online;", backup.read_text(encoding="utf-8"))

    def test_nginx_site_include_targets_https_server_not_http_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root, site_path, listing = self._nginx_site_fixture(
                root,
                "server {\n    listen 80;\n    server_name stuserver.online;\n    return 301 https://$host$request_uri;\n}\n"
                "server {\n    listen 443 ssl;\n    server_name stuserver.online;\n    location / { proxy_pass http://127.0.0.1:8080; }\n}\n",
            )
            with patch.object(manager, "DEFAULT_NGINX_CONFIG_ROOT", str(config_root)), patch.object(
                manager, "DEFAULT_NGINX_SNIPPET_DIR", str(config_root / "snippets" / "cockpit-app-manager")
            ), patch.object(manager, "DEFAULT_STATE_ROOT", str(root / "state")):
                updated_path, old_source = manager._ensure_nginx_site_include(listing)

            self.assertEqual(updated_path, site_path.resolve())
            self.assertIsNotNone(old_source)
            contents = site_path.read_text(encoding="utf-8")
            http_block, https_block = contents.split("server {", 2)[1:]
            self.assertNotIn(manager.NGINX_MANAGED_INCLUDE, http_block.split("}", 1)[0])
            self.assertIn(manager.NGINX_MANAGED_INCLUDE, https_block.split("}", 1)[0])

    def test_nginx_site_include_refuses_ambiguous_matching_https_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root, site_path, _ = self._nginx_site_fixture(
                root,
                "server { listen 443 ssl; server_name stuserver.online; }\n"
                "server { listen 443 ssl; server_name stuserver.online; }\n",
            )
            listing = f"# configuration file {site_path}:\n{site_path.read_text(encoding='utf-8')}"
            with patch.object(manager, "DEFAULT_NGINX_CONFIG_ROOT", str(config_root)):
                with self.assertRaisesRegex(RuntimeError, "multiple Nginx server blocks"):
                    manager._ensure_nginx_site_include(listing)
            self.assertNotIn(manager.NGINX_MANAGED_INCLUDE, site_path.read_text(encoding="utf-8"))

    def test_nginx_snippet_activation_tests_and_reloads_only_managed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snippet_root = root / "etc" / "nginx" / "snippets" / "cockpit-app-manager"
            snippet_path = snippet_root / "demo.conf"
            config = manager._default_config()
            config["nginx_template"] = (
                Path(__file__).resolve().parent.parent / "templates" / "nginx.conf.tpl"
            )
            config_root, site_path, listing = self._nginx_site_fixture(
                root,
                "server {\n    listen 443 ssl;\n    server_name stuserver.online;\n}\n",
            )
            app = {"id": "demo", "port": 8123, "route": "/customer-portal/"}

            def run(args, **kwargs):
                if args == ["nginx", "-T"]:
                    site_contents = site_path.read_text(encoding="utf-8")
                    output = f"# configuration file {site_path}:\n{site_contents}"
                    if "include /etc/nginx/snippets/cockpit-app-manager/*.conf;" in site_contents:
                        output += f"\n# configuration file {snippet_path}:\n{snippet_path.read_text(encoding='utf-8')}"
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

            with patch.object(manager, "DEFAULT_NGINX_SNIPPET_DIR", str(snippet_root)), patch.object(
                manager, "DEFAULT_NGINX_CONFIG_ROOT", str(config_root)
            ), patch.object(manager, "DEFAULT_STATE_ROOT", str(root / "state")), patch.object(
                manager, "run_command", side_effect=run
            ) as run, patch.object(manager, "_systemctl") as systemctl:
                activation = manager._activate_nginx_config(app, "node", config)

            self.assertTrue(activation["active"])
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["nginx", "-T"], ["nginx", "-t"], ["nginx", "-T"],
            ])
            systemctl.assert_called_once_with(
                "reload",
                "nginx",
                timeout=config["command_timeout_seconds"],
            )

    def test_nginx_syntax_failure_restores_previous_app_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snippet_root = root / "etc" / "nginx" / "snippets" / "cockpit-app-manager"
            snippet_root.mkdir(parents=True)
            snippet_path = snippet_root / "demo.conf"
            previous = b"# Managed by Cockpit App Manager: demo\nlocation /old/ {}\n"
            snippet_path.write_bytes(previous)
            config = manager._default_config()
            config["nginx_template"] = (
                Path(__file__).resolve().parent.parent / "templates" / "nginx.conf.tpl"
            )
            config_root, site_path, listing = self._nginx_site_fixture(
                root,
                "server {\n    listen 443 ssl;\n    server_name stuserver.online;\n}\n",
            )
            previous_site = site_path.read_bytes()
            app = {"id": "demo", "port": 8123, "route": "/new/"}
            invalid = subprocess.CompletedProcess(
                args=["nginx", "-t"],
                returncode=1,
                stdout="",
                stderr="invalid location",
            )
            def run(args, **kwargs):
                if args == ["nginx", "-T"]:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout=f"# configuration file {site_path}:\n{site_path.read_text(encoding='utf-8')}",
                        stderr="",
                    )
                return invalid

            with patch.object(manager, "DEFAULT_NGINX_SNIPPET_DIR", str(snippet_root)), patch.object(
                manager, "DEFAULT_NGINX_CONFIG_ROOT", str(config_root)
            ), patch.object(manager, "DEFAULT_STATE_ROOT", str(root / "state")), patch.object(
                manager, "run_command", side_effect=run
            ), patch.object(manager, "_systemctl") as systemctl:
                with self.assertRaisesRegex(RuntimeError, "invalid location"):
                    manager._activate_nginx_config(app, "node", config)

            self.assertEqual(snippet_path.read_bytes(), previous)
            self.assertEqual(site_path.read_bytes(), previous_site)
            systemctl.assert_not_called()

    def test_nginx_reload_failure_restores_managed_include_and_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snippet_root = root / "etc" / "nginx" / "snippets" / "cockpit-app-manager"
            config_root, site_path, _ = self._nginx_site_fixture(
                root,
                "server { listen 443 ssl; server_name stuserver.online; }\n",
            )
            previous_site = site_path.read_bytes()
            snippet_path = snippet_root / "demo.conf"
            previous_snippet = b"# Managed by Cockpit App Manager: demo\nlocation /old/ {}\n"
            snippet_root.mkdir(parents=True)
            snippet_path.write_bytes(previous_snippet)
            config = manager._default_config()
            config["nginx_template"] = (
                Path(__file__).resolve().parent.parent / "templates" / "nginx.conf.tpl"
            )

            def run(args, **kwargs):
                if args == ["nginx", "-T"]:
                    site_contents = site_path.read_text(encoding="utf-8")
                    output = f"# configuration file {site_path}:\n{site_contents}"
                    if manager.NGINX_MANAGED_INCLUDE in site_contents:
                        output += f"\n# configuration file {snippet_path}:\n{snippet_path.read_text(encoding='utf-8')}"
                    return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

            with patch.object(manager, "DEFAULT_NGINX_CONFIG_ROOT", str(config_root)), patch.object(
                manager, "DEFAULT_NGINX_SNIPPET_DIR", str(snippet_root)
            ), patch.object(manager, "DEFAULT_STATE_ROOT", str(root / "state")), patch.object(
                manager, "run_command", side_effect=run
            ), patch.object(manager, "_systemctl", side_effect=RuntimeError("reload failed")):
                with self.assertRaisesRegex(RuntimeError, "reload failed"):
                    manager._activate_nginx_config(
                        {"id": "demo", "port": 8123, "route": "/demo/"}, "node", config
                    )

            self.assertEqual(site_path.read_bytes(), previous_site)
            self.assertEqual(snippet_path.read_bytes(), previous_snippet)

    def test_removing_app_deactivates_only_its_active_nginx_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            snippet_root = Path(tmp) / "nginx" / "snippets" / "cockpit-app-manager"
            snippet_root.mkdir(parents=True)
            snippet_path = snippet_root / "demo.conf"
            snippet_path.write_text(
                "# Managed by Cockpit App Manager: demo\nlocation /demo/ {}\n",
                encoding="utf-8",
            )
            config = manager._default_config()
            listing = subprocess.CompletedProcess(
                args=["nginx", "-T"],
                returncode=0,
                stdout=f"# configuration file {snippet_path.resolve(strict=False)}:\n",
                stderr="",
            )
            valid = subprocess.CompletedProcess(args=["nginx", "-t"], returncode=0, stdout="", stderr="")
            with patch.object(manager, "DEFAULT_NGINX_SNIPPET_DIR", str(snippet_root)), patch.object(
                manager, "run_command", side_effect=[listing, valid]
            ), patch.object(manager, "_systemctl") as systemctl:
                manager._remove_nginx_config("demo", config)

            self.assertFalse(snippet_path.exists())
            systemctl.assert_called_once_with(
                "reload",
                "nginx",
                timeout=config["command_timeout_seconds"],
            )

    def test_removing_unverified_nginx_snippet_fails_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            snippet_root = Path(tmp) / "nginx" / "snippets" / "cockpit-app-manager"
            snippet_root.mkdir(parents=True)
            snippet_path = snippet_root / "demo.conf"
            original = "location /unrelated/ {}\n"
            snippet_path.write_text(original, encoding="utf-8")
            with patch.object(manager, "DEFAULT_NGINX_SNIPPET_DIR", str(snippet_root)):
                with self.assertRaisesRegex(RuntimeError, "unverified Nginx snippet"):
                    manager._remove_nginx_config("demo", manager._default_config())
            self.assertEqual(snippet_path.read_text(encoding="utf-8"), original)

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
        self.assertIn('install -d -o root -g root -m 0755 "$BACKEND_DEST"', installer)
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
        frontend = (project / "cockpit" / "app-manager.js").read_text(encoding="utf-8")
        self.assertIn('const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";', frontend)
        self.assertNotIn("/usr/libexec/cockpit/app-manager\", command", frontend)
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
        for required in ("systemd-run", "ss", "nginx", "git", "runuser"):
            self.assertRegex(installer, rf"for required_command in [^;\n]*\b{required}\b[^;\n]*; do")
        self.assertIn('cockpit-app-manager-auto-deploy.timer', installer)
        self.assertIn("systemctl enable --now cockpit-app-manager-auto-deploy.timer", installer)
        self.assertIn("remove_auto_deploy_units", uninstaller)
        self.assertIn('python3 "$BACKEND_DEST/app-manager" --help', installer)
        self.assertIn('"$STATE_ROOT/apps.json" "$STATE_ROOT/config.json"', installer)
        self.assertIn('chmod 0600 "$STATE_ROOT/apps.json" "$STATE_ROOT/config.json"', installer)
        self.assertIn('if [[ ! -e "$STATE_ROOT/config.json" ]]', installer)
        self.assertIn('git -C "$APP_DIR" rev-parse --verify HEAD', installer)
        self.assertIn('"$STATE_ROOT/plugin-version.json"', installer)
        frontend = (project / "cockpit" / "app-manager.js").read_text(encoding="utf-8")
        self.assertIn('superuser: "require"', frontend)
        self.assertIn('config.command_timeout_seconds ?? 30', frontend)

    def test_standalone_frontend_assets_are_referenced_and_installed(self):
        project = Path(__file__).resolve().parent.parent
        cockpit_dir = project / "cockpit"
        cockpit_page = (cockpit_dir / "index.html").read_text(encoding="utf-8")
        stylesheet_path = cockpit_dir / "app-manager.css"
        installer = (project / "scripts" / "install.sh").read_text(encoding="utf-8")
        stylesheet = stylesheet_path.read_text(encoding="utf-8")

        self.assertTrue(stylesheet_path.is_file())
        self.assertIn('<link rel="stylesheet" href="app-manager.css">', cockpit_page)
        self.assertIn('<script src="app-manager.js" defer></script>', cockpit_page)
        self.assertNotIn("/cockpit/static/desktop.css", cockpit_page)
        self.assertNotIn("<style", cockpit_page)
        self.assertIn('name="route"', cockpit_page)
        for step in range(3):
            self.assertIn(f'data-setup-step="{step}"', cockpit_page)
        self.assertIn('id="setupReview"', cockpit_page)
        self.assertIn('<script src="icons.js" defer></script>', cockpit_page)
        self.assertTrue((cockpit_dir / "icons.js").is_file())
        self.assertTrue((cockpit_dir / "app-manager.js").is_file())
        frontend = (cockpit_dir / "app-manager.js").read_text(encoding="utf-8")
        self.assertIn("Deploy to activate", frontend)
        self.assertIn("const routeIsActive = Boolean(app.hosting_active && app.active_route);", frontend)
        self.assertIn("safeUrl && routeIsActive", frontend)
        self.assertIn("function renderAppList()", frontend)
        self.assertIn('data-action="deployment-log"', frontend)
        self.assertNotIn("process.close()", frontend)
        self.assertNotIn("@import", stylesheet)
        self.assertNotRegex(stylesheet, r"url\(\s*['\"]?https?://")
        self.assertIn(".am-app-manager", stylesheet)
        self.assertIn("@media (prefers-color-scheme: dark)", stylesheet)
        self.assertIn('name="viewport"', cockpit_page)
        self.assertIn(".am-app-grid", stylesheet)
        self.assertIn("@media (max-width: 520px)", stylesheet)
        self.assertIn('[[ -f "$APP_DIR/cockpit/app-manager.css" ]]', installer)
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.css" "$PLUGIN_DEST/app-manager.css"',
            installer,
        )
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/app-manager.js" "$PLUGIN_DEST/app-manager.js"',
            installer,
        )
        self.assertIn(
            'install -o root -g root -m 0644 "$APP_DIR/cockpit/icons.js" "$PLUGIN_DEST/icons.js"',
            installer,
        )


@unittest.skipIf(os.name == "nt", "Uninstall integration tests require POSIX Bash and file permissions")
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
