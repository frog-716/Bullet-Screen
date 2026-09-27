import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = ROOT / "macos" / "LauncherLifecycle.swift"
LAUNCHER = ROOT / "macos" / "BulletScreenLauncher.swift"


HARNESS = r'''
import AppKit

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        fputs("FAIL: \(message)\n", stderr)
        exit(1)
    }
}

let menu = LauncherEditingMenu.makeMainMenu()
let editMenu = menu.items.compactMap(\.submenu).first { $0.title == "编辑" }
require(editMenu != nil, "main menu exposes Edit commands")

for (title, key, action) in [
    ("全选", "a", Selector(("selectAll:"))),
    ("拷贝", "c", Selector(("copy:"))),
    ("粘贴", "v", Selector(("paste:")))
] {
    guard let item = editMenu?.items.first(where: { $0.title == title }) else {
        fatalError("missing menu item: \(title)")
    }
    require(item.keyEquivalent.lowercased() == key, "\(title) uses Command-\(key.uppercased())")
    require(item.keyEquivalentModifierMask == [.command], "\(title) is bound to Command")
    require(item.action == action, "\(title) uses the standard text responder action")
    require(item.target == nil, "\(title) follows the text-field responder chain")
}

print("launcher text editing shortcuts: PASS")
'''


class LauncherTextEditingTests(unittest.TestCase):
    def test_main_menu_exposes_standard_text_editing_shortcuts(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("application.mainMenu = LauncherEditingMenu.makeMainMenu()", source)

        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "main.swift"
            binary = Path(directory) / "launcher_text_editing_harness"
            harness.write_text(HARNESS, encoding="utf-8")
            subprocess.run(
                ["swiftc", str(LIFECYCLE), str(harness), "-o", str(binary)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("launcher text editing shortcuts: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
