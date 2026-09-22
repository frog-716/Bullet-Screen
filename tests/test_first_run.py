import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = ROOT / "macos" / "LauncherLifecycle.swift"


HARNESS = r'''
import Foundation

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        fputs("FAIL: \(message)\n", stderr)
        exit(1)
    }
}

require(LauncherOnboarding.normalizeRoomInput(provider: "bilibili", raw: "12345") == "12345", "Bilibili room id")
require(LauncherOnboarding.normalizeRoomInput(provider: "bilibili", raw: "https://live.bilibili.com/12345?foo=bar") == "12345", "Bilibili URL")
require(LauncherOnboarding.normalizeRoomInput(provider: "douyin", raw: "511304254586") == "511304254586", "Douyin room id")
require(LauncherOnboarding.normalizeRoomInput(provider: "douyin", raw: "https://live.douyin.com/511304254586") == "https://live.douyin.com/511304254586", "Douyin URL")
require(LauncherOnboarding.normalizeRoomInput(provider: "bilibili", raw: "https://example.com/12345") == nil, "wrong host rejected")
require(LauncherOnboarding.normalizeRoomInput(provider: "douyin", raw: "") == nil, "empty input rejected")

let demo = LauncherOnboarding.dashboardURL(port: 4173, provider: "douyin", roomInput: nil, demo: true)!
require(demo.absoluteString.contains("demo=1"), "demo marker")
require(demo.absoluteString.contains("autostart=1"), "demo auto start marker")
let room = LauncherOnboarding.dashboardURL(port: 4173, provider: "bilibili", roomInput: "https://live.bilibili.com/12345", demo: false)!
require(room.absoluteString.contains("room_id=12345"), "room passed to dashboard")

let temporary = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
try! FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
try! FileManager.default.createDirectory(at: temporary.appendingPathComponent("bilibili"), withIntermediateDirectories: true)
try! FileManager.default.createDirectory(at: temporary.appendingPathComponent("douyin"), withIntermediateDirectories: true)
FileManager.default.createFile(atPath: temporary.appendingPathComponent("bilibili/server.py").path, contents: Data())
FileManager.default.createFile(atPath: temporary.appendingPathComponent("douyin/server.py").path, contents: Data())
require(LauncherPreflight.isProjectRoot(root: temporary.path), "project root is recognized")
try? FileManager.default.removeItem(at: temporary)
print("first-run launcher contract: PASS")
'''


class FirstRunLauncherTests(unittest.TestCase):
    def test_launcher_input_demo_and_project_seams(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "main.swift"
            binary = Path(directory) / "first_run_launcher_harness"
            harness.write_text(HARNESS, encoding="utf-8")
            result = subprocess.run(
                ["swiftc", str(LIFECYCLE), str(harness), "-o", str(binary)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            run = subprocess.run([str(binary)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("first-run launcher contract: PASS", run.stdout)

    def test_launcher_exposes_plain_language_first_run_actions(self):
        source = (ROOT / "macos" / "BulletScreenLauncher.swift").read_text(encoding="utf-8")
        self.assertIn("先试 Demo", source)
        self.assertIn("登录 Douyin", source)
        self.assertIn("检查安装环境", source)
        self.assertIn("scripts/doctor.py", source)
        self.assertIn("login.py", source)

    def test_dashboard_does_not_ship_with_a_test_room(self):
        for provider in ("bilibili", "douyin"):
            for name in ("index.html", "app.js"):
                source = (ROOT / provider / name).read_text(encoding="utf-8")
                self.assertNotIn("7734200", source, f"{provider}/{name} still has a default test room")


if __name__ == "__main__":
    unittest.main()
