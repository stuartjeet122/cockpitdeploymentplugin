import json
import tempfile
import unittest
from pathlib import Path

from backend.adapters import detect_framework
from backend.app_manager import add_app, get_registry, load_json, sanitize_slug


class AppManagerTests(unittest.TestCase):
    def test_detect_framework_nextjs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text('{"dependencies": {"next": "^15.0.0"}}', encoding="utf-8")
            self.assertEqual(detect_framework(repo), "nextjs")

    def test_detect_framework_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
            self.assertEqual(detect_framework(repo), "go")

    def test_detect_framework_static(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "index.html").write_text("<html></html>", encoding="utf-8")
            self.assertEqual(detect_framework(repo), "static")

    def test_add_app_registers_and_sanitizes_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "apps.json"
            import backend.app_manager as app_manager
            app_manager.DEFAULT_CONFIG_PATH = str(Path(tmp) / "config.json")
            app_manager.DEFAULT_REGISTRY_PATH = str(registry_path)
            app_manager.DEFAULT_SOURCE_ROOT = str(Path(tmp) / "sources")
            app_manager.DEFAULT_PRODUCTION_ROOT = str(Path(tmp) / "webapps")

            payload = {
                "name": "Example Site!",
                "repo_url": "https://github.com/example/demo.git",
                "branch": "main",
                "framework": "static",
            }
            record = add_app(payload)
            self.assertEqual(record["app"]["slug"], "example-site")
            registry = get_registry()
            self.assertEqual(len(registry["apps"]), 1)
            self.assertEqual(registry["apps"][0]["id"], "example-site")

    def test_slug_sanitizes_spaces(self):
        self.assertEqual(sanitize_slug("My App Name"), "my-app-name")


if __name__ == "__main__":
    unittest.main()
