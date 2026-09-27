import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "douyin"))

import douyin_adapter as adapter  # noqa: E402


class DouyinRoomAliasTests(unittest.TestCase):
    def test_public_short_link_preserves_the_navigable_room_alias(self):
        self.assertEqual(
            adapter.parse_room_input("https://live.douyin.com/keyis153"),
            ("keyis153", "https://live.douyin.com/keyis153"),
        )

    def test_tracking_query_is_not_persisted_with_short_link(self):
        self.assertEqual(
            adapter.parse_room_input(
                "https://live.douyin.com/keyis153?enter_from_merge=share&enter_method=copy"
            ),
            ("keyis153", "https://live.douyin.com/keyis153"),
        )

    def test_numeric_room_id_remains_supported(self):
        self.assertEqual(
            adapter.parse_room_input("7689851218365139754"),
            (
                "7689851218365139754",
                "https://live.douyin.com/7689851218365139754",
            ),
        )

    def test_short_alias_is_bound_to_the_public_live_host(self):
        with self.assertRaises(adapter.AdapterError):
            adapter.parse_room_input("https://example.com/keyis153")


if __name__ == "__main__":
    unittest.main()
