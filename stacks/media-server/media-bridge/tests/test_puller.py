"""Offline tests for puller.py (the background pull worker)."""

import json
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


    # --- progress of sparse, segmented .part files -----------------------------------------------

    def sparse_part(self, stage, size, written, map_bytes=None):
        stage.mkdir(parents=True, exist_ok=True)
        part = stage / "Big.mkv.part"
        with open(part, "wb") as f:
            f.truncate(size)
            f.seek(0)
            f.write(b"x" * written)
        if map_bytes is not None:
            (stage / "Big.mkv.part.map").write_text(
                json.dumps({"size": size, "segs": [[0, size // 2, map_bytes], [size // 2, size, size // 2]]}))
        return part

    def test_staged_bytes_counts_the_map_not_the_preallocation(self):
        stage = self.tmp / "st"
        self.sparse_part(stage, 10 << 20, 1 << 20, map_bytes=1 << 20)
        (stage / "eng.srt").write_bytes(b"s" * 300)
        self.assertEqual(puller._staged_bytes(stage), (1 << 20) + 300)

    def test_staged_bytes_without_map_counts_allocated_blocks(self):
        stage = self.tmp / "st2"
        self.sparse_part(stage, 10 << 20, 1 << 20)
        got = puller._staged_bytes(stage)
        self.assertGreaterEqual(got, 1 << 20)
        self.assertLess(got, 2 << 20, "not the 10 MiB preallocated size")

    def test_disk_guard_sees_what_a_resumed_pull_still_needs(self):
        stage = self.tmp / "s4"
        self.sparse_part(stage, 10 << 20, 1 << 20, map_bytes=1 << 20)
        puller._free_bytes = lambda: 100 << 20
        puller.PULL_MIN_FREE = 95 << 20  # room for 5 MiB more; 9 MiB are still to come
        try:
            job = puller.submit(SlowBackend(), {"tid": "t4", "release_name": "R"}, {}, stage, 10 << 20)
            t = threading.Thread(target=puller.run_pending, daemon=True)
            t.start()
            for _ in range(50):
                if job.status == "waiting_space":
                    break
                time.sleep(0.02)
            self.assertEqual(job.status, "waiting_space")
            puller.cancel("t4")
            t.join(5)
        finally:
            puller._free_bytes = FREE_BYTES

    def test_job_bytes_count_only_this_run(self):
        stage = self.tmp / "s5"
        stage.mkdir()
        (stage / "f.mkv.part").write_bytes(b"x" * 60)  # an earlier, interrupted pull

        class Resuming(SlowBackend):
            def pull(self, rec, plan, dest, cancel):
                (dest / "f.mkv.part").unlink()
                super().pull(rec, plan, dest, cancel)

        job = puller.submit(Resuming(), {"tid": "t5", "release_name": "R"}, {}, stage, 100)
        puller.run_pending()
        self.assertEqual((job.status, job.before, job.bytes), ("done", 60, 40))


FREE_BYTES = puller._free_bytes


class GatedBackend:
    """pull() blocks until released, so a test can hold jobs 'running'."""

    def __init__(self):
        self.go = threading.Event()

    def pull(self, rec, plan, dest, cancel):
        dest.mkdir(parents=True, exist_ok=True)
        self.go.wait(5)
        (dest / "f.mkv").write_bytes(b"x" * 10)


class ParallelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        puller._jobs.clear()
        while not puller._q.empty():
            puller._q.get_nowait()
        puller.DATA_ROOT = str(self.tmp)
        puller.PULL_MIN_FREE = 0

    def tearDown(self):
        puller._free_bytes = FREE_BYTES
        shutil.rmtree(self.tmp, ignore_errors=True)

    def worker(self):
        t = threading.Thread(target=lambda: puller._process(puller._q.get(timeout=5)), daemon=True)
        t.start()
        return t

    def wait_for(self, job, status):
        for _ in range(250):
            if job.status == status:
                return
            time.sleep(0.02)
        self.fail(f"{job.tid} is {job.status}, not {status}")

    def test_two_jobs_run_side_by_side(self):
        be = GatedBackend()
        big = puller.submit(be, {"tid": "big", "release_name": "Big"}, {}, self.tmp / "big", 10)
        small = puller.submit(be, {"tid": "small", "release_name": "Small"}, {}, self.tmp / "small", 10)
        threads = [self.worker(), self.worker()]
        self.wait_for(big, "running")
        self.wait_for(small, "running")  # the second one didn't queue behind the first
        be.go.set()
        for t in threads:
            t.join(5)
        self.assertEqual((big.status, small.status), ("done", "done"))

    def test_disk_guard_counts_what_running_pulls_still_need(self):
        puller._free_bytes = lambda: 100 << 20
        puller.PULL_MIN_FREE = 60 << 20
        be = GatedBackend()
        a = puller.submit(be, {"tid": "a", "release_name": "A"}, {}, self.tmp / "a", 30 << 20)
        ta = self.worker()
        self.wait_for(a, "running")  # 100 - 30 >= 60: fits on its own
        b = puller.submit(be, {"tid": "b", "release_name": "B"}, {}, self.tmp / "b", 20 << 20)
        tb = self.worker()
        self.wait_for(b, "waiting_space")  # alone 100 - 20 would fit, but A still needs 30 of it
        puller.cancel("b")
        be.go.set()
        ta.join(5)
        tb.join(5)
        self.assertEqual((a.status, b.status), ("done", "cancelled"))


class ExtractTest(unittest.TestCase):
    """RAR extraction with a fake unrar (real archives need the non-free rar tool to create)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.fake = self.tmp / "fake-unrar"
        # fake unrar: "x -o+ -y -idq <first> <dest>/" -> writes <dest>/<stem>.mkv, or fails for "bad"
        # match on the archive's FILE name only: the full path holds a random temp-dir name,
        # which now and then contains "bad" and made this test fail at random (CI run 39393448852)
        self.fake.write_text("#!/bin/sh\ncase \"$(basename \"$5\")\" in *bad*) echo 'CRC failed' >&2; exit 3;; esac\n"
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
