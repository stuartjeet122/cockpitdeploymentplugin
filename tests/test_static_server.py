import http.server
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from backend.static_server import StaticRequestHandler


class StaticServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        (self.root / "index.html").write_text("<h1>Home</h1>", encoding="utf-8")
        (self.root / "asset.js").write_text("window.ready = true;", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=value", encoding="utf-8")
        (self.root / "empty").mkdir()
        handler = lambda *args, **kwargs: StaticRequestHandler(
            *args, directory=str(self.root), base_path="/portal/", spa_fallback=True, **kwargs
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_serves_prefixed_files_and_react_routes_from_content_root(self):
        with urllib.request.urlopen(self.base_url + "/portal/asset.js", timeout=2) as response:
            self.assertEqual(response.read().decode(), "window.ready = true;")
        with urllib.request.urlopen(self.base_url + "/portal/account/profile", timeout=2) as response:
            self.assertEqual(response.read().decode(), "<h1>Home</h1>")

    def test_missing_assets_hidden_files_and_traversal_are_not_served(self):
        for path in ("/portal/missing.js", "/portal/.env", "/portal/%2e%2e/.env", "/portal/empty/"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base_url + path, timeout=2)
            self.assertEqual(error.exception.code, 404)

