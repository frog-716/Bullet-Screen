import importlib.util
import sys
import tempfile
import threading
import time
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


BILIBILI = load_module("bilibili_server_b02", REPO_ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_b02", DOUYIN_ROOT / "server.py")
DOUYIN_ADAPTER = load_module("douyin_adapter_b02", DOUYIN_ROOT / "douyin_adapter.py")


class BlockingBilibiliCollector:
    def __init__(self, base_cls, store, busy_error):
        self.release = threading.Event()
        self.started = threading.Event()
        self.contexts = []
        self.busy_error = busy_error
        self.collector = self._make(base_cls, store)

    def _make(self, base_cls, store):
        owner = self

        class FakeCollector(base_cls):
            def _run(self, context=None):
                owner.contexts.append(context)
                owner.started.set()
                owner.release.wait(5)

        return FakeCollector(store)


class BlockingDouyinCollector:
    def __init__(self, base_cls, db_path, busy_error):
        self.release = threading.Event()
        self.started = threading.Event()
        self.contexts = []
        self.busy_error = busy_error
        owner = self

        class FakeCollector(base_cls):
            def _run(self, context=None):
                owner.contexts.append(context)
                owner.started.set()
                owner.release.wait(5)

        self.collector = FakeCollector(db_path, mode="demo")


class LifecycleTests(unittest.TestCase):
    def _cases(self, temporary):
        return (
            BlockingBilibiliCollector(
                BILIBILI.Collector,
                BILIBILI.EventStore(Path(temporary) / "bilibili.sqlite3"),
                getattr(BILIBILI, "BusyError", RuntimeError),
            ),
            BlockingBilibiliCollector(
                DOUYIN_SERVER.Collector,
                DOUYIN_SERVER.EventStore(Path(temporary) / "provider-bilibili.sqlite3"),
                getattr(DOUYIN_SERVER, "BusyError", RuntimeError),
            ),
            BlockingDouyinCollector(
                DOUYIN_ADAPTER.DouyinCollector,
                Path(temporary) / "douyin.sqlite3",
                getattr(DOUYIN_ADAPTER, "BusyError", RuntimeError),
            ),
        )

    def tearDown(self):
        for case in getattr(self, "cases", ()):
            case.release.set()
            case.collector.stop(timeout=1)
            close = getattr(case.collector.store, "close", None)
            if close:
                close()

    def test_stop_timeout_keeps_owner_and_blocks_new_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.cases = self._cases(temporary)
            for case in self.cases:
                with self.subTest(collector=type(case.collector).__mro__[1].__name__):
                    collector = case.collector
                    first = collector.start("1001")
                    self.assertTrue(case.started.wait(1))

                    self.assertFalse(collector.stop(timeout=0.01))
                    current = collector.status()
                    self.assertEqual(current["status"], "stopping")
                    self.assertTrue(current["worker_alive"])
                    self.assertEqual(current["generation"], first.generation)
                    self.assertEqual(current["room_id"], "1001")
                    with self.assertRaises(case.busy_error):
                        collector.start("2002")

                    case.release.set()
                    self.assertTrue(collector.stop(timeout=1))
                    self.assertEqual(collector.status()["status"], "stopped")

    def test_each_run_has_its_own_context_and_old_context_loses_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.cases = self._cases(temporary)
            for case in self.cases:
                with self.subTest(collector=type(case.collector).__mro__[1].__name__):
                    collector = case.collector
                    first = collector.start("1001")
                    self.assertTrue(case.started.wait(1))
                    case.release.set()
                    self.assertTrue(collector.stop(timeout=1))

                    case.release = threading.Event()
                    case.started = threading.Event()
                    second = collector.start("2002")
                    self.assertTrue(case.started.wait(1))
                    self.assertGreater(second.generation, first.generation)
                    self.assertIsNot(first.cancel, second.cancel)
                    self.assertFalse(second.cancel.is_set())
                    self.assertFalse(collector.is_current_run(first))
                    self.assertTrue(collector.is_current_run(second))
                    self.assertEqual(collector.status()["room_id"], "2002")

    def test_repeated_stop_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.cases = self._cases(temporary)
            for case in self.cases:
                with self.subTest(collector=type(case.collector).__mro__[1].__name__):
                    self.assertTrue(case.collector.stop())
                    self.assertTrue(case.collector.stop())

    def test_late_old_callback_cannot_change_new_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.cases = self._cases(temporary)
            for case in self.cases:
                with self.subTest(collector=type(case.collector).__mro__[1].__name__):
                    collector = case.collector
                    first = collector.start("1001")
                    self.assertTrue(case.started.wait(1))
                    case.release.set()
                    self.assertTrue(collector.stop(timeout=1))

                    case.release = threading.Event()
                    case.started = threading.Event()
                    second = collector.start("2002")
                    self.assertTrue(case.started.wait(1))
                    callback_done = threading.Event()

                    def late_callback():
                        if collector.is_current_run(first):
                            with collector.lock:
                                collector.status_name = "error"
                                collector.last_error = "old run callback"
                        callback_done.set()

                    threading.Thread(target=late_callback, daemon=True).start()
                    self.assertTrue(callback_done.wait(1))
                    current = collector.status()
                    self.assertEqual(current["generation"], second.generation)
                    self.assertEqual(current["room_id"], "2002")
                    self.assertNotEqual(current["last_error"], "old run callback")

    def test_connected_state_expires_without_fresh_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.cases = self._cases(temporary)
            for case in self.cases:
                with self.subTest(collector=type(case.collector).__mro__[1].__name__):
                    collector = case.collector
                    collector.start("1001")
                    self.assertTrue(case.started.wait(1))
                    with collector.lock:
                        collector.status_name = "connected"
                        collector.session_id = 1
                        collector._last_valid_monotonic = time.monotonic() - 60
                    current = collector.status()
                    self.assertEqual(current["status"], "stale")
                    self.assertFalse(current["connected"])


if __name__ == "__main__":
    unittest.main()
