import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = ROOT / "macos" / "LauncherLifecycle.swift"


HARNESS = r'''
import Foundation
import Darwin

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        fputs("FAIL: \(message)\n", stderr)
        exit(1)
    }
}

let model = LauncherLifecycleModel()
require(model.state == .stopped, "new model is stopped")
let first = model.beginStart()!
require(model.state == .starting, "start enters starting")
require(model.beginStart() == nil, "second start is rejected while starting")
require(model.markWaitingForReady(first), "run enters ready wait")
require(model.beginStop(for: first), "stop enters stopping")
require(model.state == .stopping, "stop does not pretend to be complete")
require(model.beginStart() == nil, "start is rejected while stopping")
require(model.markStopTimedOut(for: first), "stop timeout remains owned by first run")
require(model.state == .stopping, "timeout remains stopping")
require(!model.accepts(first), "timed-out run callbacks are no longer accepted")
require(model.finishStop(for: first), "first run can finish stopping")
let second = model.beginStart()!
require(second.generation > first.generation, "new run has a new generation")
require(!model.accepts(first), "old run cannot modify new run")
require(model.accepts(second), "new run is accepted")
require(!model.markReady(first), "old ready callback is rejected")
require(model.markWaitingForReady(second), "second run enters ready wait")
require(model.markReady(second), "second run becomes running")
require(model.state == .running, "second run is running")
require(model.beginStop(for: second), "second run can stop")
require(model.finishStop(for: second), "second run can finish stopping")
let failed = model.beginStart()!
require(model.markWaitingForReady(failed), "failed run enters ready wait")
require(model.fail(failed), "process exit before ready fails the run")
require(model.state == .failed, "failed run is visible as failed")
require(!model.markReady(failed), "failed run callback is rejected")

require(LauncherPreflight.parsePort(nil) == 0, "default port is automatic")
require(LauncherPreflight.parsePort("4173") == 4173, "configured port is accepted")
require(LauncherPreflight.parsePort("65536") == nil, "out of range port is rejected")
require(!LauncherPreflight.isExecutable("/definitely/missing/python"), "missing Python is rejected")
let missingDependency = LauncherPreflight.runPythonCheck(at: "/usr/bin/python3", script: "import definitely_missing_bullet_screen_dependency")
require(!missingDependency.ok, "missing Python dependency is rejected")
let root = FileManager.default.currentDirectoryPath
require(LauncherPreflight.hasServer(root: root, provider: "bilibili"), "Bilibili entry is found")
require(!LauncherPreflight.hasServer(root: root, provider: "missing"), "missing entry is rejected")
require(LauncherPreflight.hasDatabaseParent("/tmp/launcher-lifecycle.sqlite3"), "existing database parent is accepted")

let descriptor = socket(AF_INET, SOCK_STREAM, 0)
require(descriptor >= 0, "test socket opens")
var address = sockaddr_in()
address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
address.sin_family = sa_family_t(AF_INET)
address.sin_port = 0
address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
withUnsafePointer(to: &address) {
    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        require(bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0, "test socket binds")
    }
}
var bound = sockaddr_in()
var boundLength = socklen_t(MemoryLayout<sockaddr_in>.size)
withUnsafeMutablePointer(to: &bound) {
    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        require(getsockname(descriptor, $0, &boundLength) == 0, "test socket reports port")
    }
}
let occupiedPort = Int(UInt16(bigEndian: bound.sin_port))
require(!LauncherPreflight.isPortAvailable(occupiedPort), "occupied port is rejected")
close(descriptor)
print("launcher lifecycle contract: PASS")
'''


class LauncherLifecycleTests(unittest.TestCase):
    def test_generation_and_stop_timeout_contract(self):
        self.assertTrue(LIFECYCLE.exists(), "LauncherLifecycle.swift must define the testable lifecycle seam")
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "main.swift"
            binary = Path(directory) / "launcher_lifecycle_harness"
            harness.write_text(HARNESS, encoding="utf-8")
            subprocess.run(
                ["swiftc", str(LIFECYCLE), str(harness), "-o", str(binary)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("launcher lifecycle contract: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
