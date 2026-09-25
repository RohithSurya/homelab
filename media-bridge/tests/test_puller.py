"""Offline tests for puller.py (the background pull worker)."""

import pathlib
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import puller  # noqa: E402


class SlowBackend:
    def __init__(self):
        self.pulled = []

    def pull(self, rec, plan, dest, cancel):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "f.mkv").write_bytes(b"x" * 100)
        self.pulled.append(rec["tid"])


class PullerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        puller._jobs.clear()
        puller.DATA_ROOT = str(self.tmp)
        puller.PULL_MIN_FREE = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_submit_is_idempotent_and_runs(self):
        be = SlowBackend()
        j1 = puller.submit(be, {"tid": "t1", "release_name": "R"}, {}, self.tmp / "s1", 100)
        j2 = puller.submit(be, {"tid": "t1", "release_name": "R"}, {}, self.tmp / "s1", 100)
        self.assertIs(j1, j2)
        puller.run_pending()
        self.assertEqual((j1.status, j1.bytes, be.pulled), ("done", 100, ["t1"]))

    def test_cancelled_before_start_removes_stage(self):
        be = SlowBackend()
        stage = self.tmp / "s2"
        stage.mkdir()
        (stage / "old.part").write_bytes(b"x")
        job = puller.submit(be, {"tid": "t2"}, {}, stage, 1)
        self.assertIn(str(stage), puller.busy_paths())
        puller.cancel("t2")
        puller.run_pending()
        self.assertEqual(job.status, "cancelled")
        self.assertFalse(stage.exists())
        self.assertEqual(be.pulled, [])

    def test_disk_guard_waits_then_cancel_releases(self):
        puller.PULL_MIN_FREE = 10**18  # never enough space
        job = puller.submit(SlowBackend(), {"tid": "t3", "release_name": "R"}, {}, self.tmp / "s3", 10)
        t = threading.Thread(target=puller.run_pending, daemon=True)
        t.start()
        for _ in range(50):
            if job.status == "waiting_space":
                break
            time.sleep(0.02)
        self.assertEqual(job.status, "waiting_space")
        puller.cancel("t3")
        t.join(5)
        self.assertEqual(job.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
