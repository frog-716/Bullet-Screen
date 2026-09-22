import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import doctor  # noqa: E402
import setup as setup_module  # noqa: E402


class CleanInstallTests(unittest.TestCase):
    def test_supported_python_version_is_explicit(self):
        self.assertEqual(doctor.MIN_PYTHON, (3, 9))
        self.assertTrue(doctor.python_version_supported((3, 9, 0)))
        self.assertFalse(doctor.python_version_supported((3, 8, 18)))

    def test_python_resolution_reports_missing_python_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(doctor.resolve_python(root, env={}, path=""))

    def test_venv_creation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(setup_module.create_venv(root, sys.executable))
            self.assertTrue((root / ".venv" / "bin" / "python").exists())
            self.assertFalse(setup_module.create_venv(root, sys.executable))

    def test_requirements_install_entry_is_pinned_and_explicit(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "requirements.txt"
            douyin = root / "requirements-douyin.txt"
            core.write_text("# core\n", encoding="utf-8")
            douyin.write_text("playwright==1.60.0\n", encoding="utf-8")
            setup_module.install_requirements(sys.executable, (core, douyin), runner=runner)

        self.assertEqual(calls[0][-2:], ["--requirement", str(core)])
        self.assertEqual(calls[1][-2:], ["--requirement", str(douyin)])

    def test_doctor_reports_missing_playwright_and_chromium(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_python = root / "python"
            fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_python.chmod(0o755)
            results = doctor.check_environment(
                root,
                python_executable=fake_python,
                platform_name="Darwin",
                executable_lookup=lambda _: "/usr/bin/available",
                port=0,
            )
        self.assertFalse(results["python"]["ok"])
        self.assertFalse(results["playwright"]["ok"])
        self.assertFalse(results["chromium"]["ok"])

    def test_doctor_reports_missing_swift_and_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                results = doctor.check_environment(
                    Path(directory),
                    python_executable=Path(sys.executable),
                    platform_name="Darwin",
                    executable_lookup=lambda name: None if name == "swiftc" else "/usr/bin/available",
                    port=port,
                )
        self.assertFalse(results["swiftc"]["ok"])
        self.assertFalse(results["port"]["ok"])

    def test_doctor_output_contains_no_sensitive_fields(self):
        output = doctor.format_report({
            "python": {"ok": True, "detail": "3.9"},
            "playwright": {"ok": False, "detail": "未安装"},
        })
        lowered = output.lower()
        self.assertNotIn("cookie", lowered)
        self.assertNotIn("token", lowered)
        self.assertNotIn("localstorage", lowered)

    def test_smoke_entrypoint_exists_and_is_temp_database_only(self):
        smoke = SCRIPTS / "smoke_test.py"
        self.assertTrue(smoke.exists())
        source = smoke.read_text(encoding="utf-8")
        self.assertIn("TemporaryDirectory", source)
        self.assertIn("--mode", source)
        self.assertIn("demo", source)
        self.assertNotIn("browser-profile", source)

    def test_build_script_has_clear_preflight_and_ad_hoc_signing_notice(self):
        build = (ROOT / "macos" / "build-app.sh").read_text(encoding="utf-8")
        self.assertIn("swiftc", build)
        self.assertIn("codesign", build)
        self.assertIn("BULLET_SCREEN_PYTHON", build)
        self.assertIn("ad-hoc", build)


if __name__ == "__main__":
    unittest.main()
