import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")


class LocalService:
    def __init__(self, command, cwd, env=None):
        merged_env = os.environ.copy()
        merged_env.update(env or {})
        merged_env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines = []
        self.port = self._wait_for_port()

    def _wait_for_port(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._read_available()
                raise AssertionError("service exited before ready: " + "".join(self.lines))
            line = self.process.stdout.readline()
            if line:
                self.lines.append(line)
                match = URL_RE.search(line)
                if match:
                    return int(match.group(1))
            else:
                time.sleep(0.02)
        self._read_available()
        raise AssertionError("service did not announce a port: " + "".join(self.lines))

    def _read_available(self):
        if self.process.stdout is None:
            return
        while True:
            line = self.process.stdout.readline()
            if not line:
                return
            self.lines.append(line)

    def json(self, path, headers=None):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            headers={"Host": "127.0.0.1:%d" % self.port, **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=8)
        self.assertFalseRunning()
        if self.process.stdout is not None:
            self.process.stdout.close()

    def assertFalseRunning(self):
        if self.process.poll() is None:
            raise AssertionError("owned service process is still running")


class LauncherRuntimeTests(unittest.TestCase):
    def test_bilibili_temp_database_ready_snapshot_stop_start(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bilibili.sqlite3"
            command = [
                sys.executable,
                str(ROOT / "bilibili" / "server.py"),
                "--port",
                "0",
                "--db",
                str(database),
            ]
            first = LocalService(command, ROOT / "bilibili")
            status, health = first.json("/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])
            status, bootstrap = first.json("/api/bootstrap")
            self.assertEqual(status, 200)
            token = bootstrap["token"]
            status, snapshot = first.json("/api/snapshot", {"X-Bullet-Screen-Token": token})
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["provider"], "bilibili")
            first.stop()

            second = LocalService(command, ROOT / "bilibili")
            self.assertEqual(second.json("/api/health")[0], 200)
            second.stop()

    def test_douyin_demo_ready_snapshot_stop_start_without_profile(self):
        command = [sys.executable, str(ROOT / "douyin" / "server.py"), "--port", "0", "--mode", "demo"]
        first = LocalService(command, ROOT / "douyin")
        status, health = first.json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        status, bootstrap = first.json("/api/bootstrap")
        self.assertEqual(status, 200)
        status, snapshot = first.json("/api/snapshot", {"X-Bullet-Screen-Token": bootstrap["token"]})
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["provider"], "douyin")
        first.stop()

        second = LocalService(command, ROOT / "douyin")
        self.assertEqual(second.json("/api/health")[0], 200)
        second.stop()


if __name__ == "__main__":
    unittest.main()
