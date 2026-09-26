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


class ExtractTest(unittest.TestCase):
    """RAR extraction with a fake unrar (real archives need the non-free rar tool to create)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.fake = self.tmp / "fake-unrar"
        # fake unrar: "x -o+ -y -idq <first> <dest>/" -> writes <dest>/<stem>.mkv, or fails for "bad"
        self.fake.write_text("#!/bin/sh\ncase \"$5\" in *bad*) echo 'CRC failed' >&2; exit 3;; esac\n"
                             "n=$(basename \"$5\"); n=${n%.rar}; n=${n%.part01}\nprintf movie > \"$6$n.mkv\"\n")
        self.fake.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_old_style_volumes_and_skip_sample(self):
        st = self.tmp / "stage"
        (st / "Sample").mkdir(parents=True)
        for n in ("rel.rar", "rel.r00", "rel.r01", "rel.sfv", "rel.nfo"):
            (st / n).write_text("x")
        (st / "Sample" / "rel.sample.rar").write_text("x")
        self.assertEqual(puller.extract_archives(st, unrar=str(self.fake)), 1)
        self.assertEqual((st / "rel.mkv").read_text(), "movie")
        self.assertEqual(sorted(p.name for p in st.iterdir()), ["Sample", "rel.mkv", "rel.nfo"])
        self.assertTrue((st / "Sample" / "rel.sample.rar").exists(), "samples are left alone")

    def test_extract_part_style(self):
        st = self.tmp / "stage"
        st.mkdir()
        for n in ("rel.part01.rar", "rel.part02.rar", "rel.part03.rar"):
            (st / n).write_text("x")
        self.assertEqual(puller.extract_archives(st, unrar=str(self.fake)), 1)
        self.assertEqual(sorted(p.name for p in st.iterdir()), ["rel.mkv"])

    def test_failure_raises_and_leaves_volumes(self):
        st = self.tmp / "stage"
        st.mkdir()
        for n in ("bad.rar", "bad.r00"):
            (st / n).write_text("x")
        with self.assertRaises(RuntimeError):
            puller.extract_archives(st, unrar=str(self.fake))
        self.assertEqual(sorted(p.name for p in st.iterdir()), ["bad.r00", "bad.rar"], "no half-extracted output")


if __name__ == "__main__":
    unittest.main()
