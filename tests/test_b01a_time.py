import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "douyin"))

from douyin_adapter import _platform_timestamp


class PlatformTimestampTests(unittest.TestCase):
    def test_seconds_are_rendered_as_utc_iso8601(self):
        self.assertEqual(
            _platform_timestamp(1700000000),
            "2023-11-14T22:13:20.000+00:00",
        )

    def test_milliseconds_are_rendered_as_utc_iso8601(self):
        self.assertEqual(
            _platform_timestamp(1700000000000),
            "2023-11-14T22:13:20.000+00:00",
        )

    def test_invalid_timestamps_are_unknown(self):
        for value in (None, "", 0, -1, 99999999999, "not-a-timestamp"):
            with self.subTest(value=value):
                self.assertEqual(_platform_timestamp(value), "")


if __name__ == "__main__":
    unittest.main()
