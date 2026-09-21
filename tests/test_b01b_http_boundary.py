import http.client
import importlib.util
import json
import socket
import sys
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOUYIN_ROOT = REPO_ROOT / "douyin"
sys.path.insert(0, str(DOUYIN_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BILIBILI = load_module("bilibili_server_b01b", REPO_ROOT / "bilibili" / "server.py")
DOUYIN = load_module("douyin_server_b01b", DOUYIN_ROOT / "server.py")


class FakeCollector:
    provider_name = "test"

    def status(self):
        return {
            "available": True,
            "connected": False,
            "status": "idle",
            "session_id": None,
            "last_error": "",
        }

    def metrics(self):
        return self.status()

    def recent_events(self, limit=100):
        return []

    def stop(self):
        return None


class HTTPBoundaryTests(unittest.TestCase):
    modules = (BILIBILI, DOUYIN)

    def setUp(self):
        self.servers = []
        self.threads = []
        self.endpoints = []
        for module in self.modules:
            handler = type(
                "B01bHandler",
                (module.AppHandler,),
                {"collector": FakeCollector(), "capability_token": "test-token"},
            )
            server = module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append(server)
            self.threads.append(thread)
            self.endpoints.append(("127.0.0.1", server.server_address[1]))

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def request(self, endpoint, method, path, headers=None, body=None):
        host, port = endpoint
        connection = http.client.HTTPConnection(host, port, timeout=3)
        request_headers = {"Host": f"{host}:{port}", **(headers or {})}
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def raw_status(self, endpoint, request):
        host, port = endpoint
        with socket.create_connection((host, port), timeout=3) as connection:
            connection.sendall(request.replace(b"{PORT}", str(port).encode("ascii")))
            response = b""
            while b"\r\n" not in response:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response += chunk
        return int(response.split(b" ", 2)[1])

    def test_each_service_only_serves_declared_static_files(self):
        for endpoint in self.endpoints:
            with self.subTest(endpoint=endpoint):
                status, _, body = self.request(endpoint, "GET", "/")
                self.assertEqual(status, 200)
                self.assertIn(b"<html", body.lower())

                for path in ("/server.py", "/data/danmaku.sqlite3", "/data/browser-profile/Default/Cookies"):
                    with self.subTest(path=path):
                        get_status, _, _ = self.request(endpoint, "GET", path)
                        head_status, _, head_body = self.request(endpoint, "HEAD", path)
                        self.assertEqual(get_status, 404)
                        self.assertEqual(head_status, 404)
                        self.assertEqual(head_body, b"")

    def test_each_service_requires_local_boundary_and_capability_token(self):
        for endpoint in self.endpoints:
            host, port = endpoint
            with self.subTest(endpoint=endpoint):
                status, _, _ = self.request(endpoint, "GET", "/api/health")
                self.assertEqual(status, 200)

                status, _, body = self.request(endpoint, "GET", "/api/bootstrap")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["token"], "test-token")

                status, _, _ = self.request(endpoint, "GET", "/api/status")
                self.assertEqual(status, 401)
                status, _, _ = self.request(endpoint, "GET", "/api/status?token=test-token")
                self.assertEqual(status, 401)

                status, _, _ = self.request(
                    endpoint,
                    "GET",
                    "/api/status",
                    headers={"X-Bullet-Screen-Token": "test-token"},
                )
                self.assertEqual(status, 200)

                status, _, _ = self.request(
                    endpoint,
                    "GET",
                    "/api/health",
                    headers={"Host": f"evil.example:{port}"},
                )
                self.assertEqual(status, 403)
                status, _, _ = self.request(
                    endpoint,
                    "GET",
                    "/api/health",
                    headers={"Origin": "http://evil.example", "X-Bullet-Screen-Token": "test-token"},
                )
                self.assertEqual(status, 403)
                status, _, _ = self.request(
                    endpoint,
                    "GET",
                    "/api/health",
                    headers={"Origin": "null"},
                )
                self.assertEqual(status, 403)

                status, _, _ = self.request(
                    endpoint,
                    "GET",
                    "/api/health",
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(status, 200)

    def test_each_service_rejects_invalid_or_oversized_json_body(self):
        for endpoint in self.endpoints:
            host, port = endpoint
            with self.subTest(endpoint=endpoint):
                common = (
                    b"POST /api/disconnect HTTP/1.1\r\n"
                    + f"Host: {host}:{port}\r\n".encode()
                    + b"X-Bullet-Screen-Token: test-token\r\n"
                    + b"Content-Type: application/json\r\n"
                    + b"Connection: close\r\n"
                )
                negative_length = common + b"Content-Length: -1\r\n\r\n"
                self.assertEqual(self.raw_status(endpoint, negative_length), 400)

                oversized = common + b"Content-Length: 65537\r\n\r\n"
                self.assertEqual(self.raw_status(endpoint, oversized), 413)

                wrong_type = common + b"Content-Length: 2\r\nContent-Type: text/plain\r\n\r\n{}"
                self.assertEqual(self.raw_status(endpoint, wrong_type), 415)


if __name__ == "__main__":
    unittest.main()
