import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = ROOT / "macos" / "LauncherLifecycle.swift"


HARNESS = r'''
import Foundation

func require(_ actual: [String], _ expected: [String], _ message: String) {
    guard actual == expected else {
        fputs("FAIL: \(message)\nactual: \(actual)\nexpected: \(expected)\n", stderr)
        exit(1)
    }
}

let base = ["-u", "server.py", "--port", "4173"]

require(
    LauncherPreflight.serverArguments(provider: "douyin", demoMode: false, port: 4173, environment: [:]),
    base + ["--mode", "auto"],
    "an unspecified DB keeps Douyin's default DB behavior"
)
require(
    LauncherPreflight.serverArguments(provider: "douyin", demoMode: false, port: 4173, environment: ["BULLET_SCREEN_DB": "/tmp/test.sqlite3"]),
    base + ["--mode", "auto", "--db", "/tmp/test.sqlite3"],
    "an explicit DB override reaches Douyin argv"
)
require(
    LauncherPreflight.serverArguments(provider: "bilibili", demoMode: false, port: 4173, environment: ["BULLET_SCREEN_DB": "/tmp/bilibili.sqlite3"]),
    base + ["--db", "/tmp/bilibili.sqlite3"],
    "an explicit DB override remains supported for Bilibili"
)
require(
    LauncherPreflight.serverArguments(provider: "douyin", demoMode: false, port: 4173, environment: ["BULLET_SCREEN_DB": "/tmp/验收副本 with spaces.sqlite3"]),
    base + ["--mode", "auto", "--db", "/tmp/验收副本 with spaces.sqlite3"],
    "a path with spaces remains one argv value"
)
require(
    LauncherPreflight.serverArguments(provider: "douyin", demoMode: false, port: 4173, environment: ["BULLET_SCREEN_DB": ""]),
    base + ["--mode", "auto"],
    "an empty override does not create an invalid --db"
)
require(
    LauncherPreflight.serverArguments(provider: "douyin", demoMode: true, port: 4173, environment: ["BULLET_SCREEN_DB": "/tmp/ignored.sqlite3"]),
    base + ["--mode", "demo"],
    "demo remains isolated from a persistent DB override"
)
print("launcher DB propagation contract: PASS")
'''


class LauncherDatabasePropagationTests(unittest.TestCase):
    def test_server_argv_preserves_database_override_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "main.swift"
            binary = Path(directory) / "launcher_db_harness"
            harness.write_text(HARNESS, encoding="utf-8")
            subprocess.run(
                ["swiftc", str(LIFECYCLE), str(harness), "-o", str(binary)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("launcher DB propagation contract: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
