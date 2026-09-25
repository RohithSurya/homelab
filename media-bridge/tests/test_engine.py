"""Offline tests for bridge.py (the engine) against a scripted fake backend.

No network, no BitTorrent client: run inside the image with
  docker run --rm -v "$PWD/media-bridge:/app" -w /app --entrypoint python media-server-media-bridge -m unittest discover -s tests -v
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ["BRIDGE_ROUTE"] = "*:fake"
os.environ["SEED_RULES"] = "hd-space=48h,iptorrents=1.0|336h,private-default=336h"
os.environ.pop("NTFY_TOPIC", None)

import backends  # noqa: E402
from backends.base import DONE, DOWNLOADING, NOT_VISIBLE, UNAVAILABLE, Backend, Exists, Full  # noqa: E402

backends.REGISTRY["fake"] = "unused:unused"


class Fake(Backend):
    name, label, tag = "fake", "Fake", "fake"
    max_seed_hours = None
    full_retries = 5

    def __init__(self):
        self.t = {}  # hash -> dict(progress, done, state, partial, hold)
        self.calls = []
        self.next_add = None
        self.fetch_error = None

    def add(self, drop, magnet):
        self.calls.append(("add", drop.stem))
        if self.next_add:
            e, self.next_add = self.next_add, None
            raise e
        h = "h-" + drop.stem
        self.t[h] = dict(progress=0.0, done=False, state=None, partial=0, hold=False)
        return {"title": drop.stem, "torrent_hash": h}

    def locate(self, btih, title):
        return {"torrent_hash": btih}, "in progress"

    def begin_loop(self, records):
        self.calls.append(("loop", len(records)))

    def status(self, rec):
        t = self.t.get(rec["torrent_hash"])
        if t is None or t["state"] == "gone":
            return UNAVAILABLE, "not listed"
        if t["state"] == "not_visible":
            return NOT_VISIBLE, None
        if not t["done"]:
            return DOWNLOADING, t["progress"]
        return DONE, {"name": "www.site - " + rec["title"], "h": rec["torrent_hash"]}

    def fetch(self, rec, result, dest):
        if self.fetch_error:
            raise RuntimeError(self.fetch_error)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "movie.mkv").write_text("x" * 10)
        self.calls.append(("fetch_to", str(dest)))

    def cancel(self, rec):
        self.calls.append(("cancel", rec["title"]))

    def discard(self, rec, result):
        self.calls.append(("discard", rec["title"]))

    def start_hold(self, rec, result):
        return None

    def hold_over(self, rec):
        t = self.t.get(rec["torrent_hash"])
        if t is None:
            return None
        return t["hold"]

    def end_hold(self, rec):
        self.calls.append(("end_hold", rec["title"]))

    def partial_bytes(self, rec):
        t = self.t.get(rec["torrent_hash"])
        return None if t is None else t["partial"]

    def reap(self, rec):
        self.calls.append(("reap", rec["title"]))


FAKE = Fake()
backends._instances["fake"] = FAKE
backends._instances["seedr"] = FAKE  # legacy recs without a backend key

import bridge  # noqa: E402

MAGNET = "magnet:?xt=urn:btih:{h}&dn=x"


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        bridge.DATA = self.tmp
        bridge.STATE_DIR = self.tmp / "blackhole" / ".bridge-state"
        bridge.INCOMING = self.tmp / "downloads" / ".incoming"
        bridge.ARR = {}
        for d in (bridge.FAIL_COUNTS, bridge.WARNED, bridge.INDEXERS, bridge.INDEXER_TRIES,
                  bridge.FAILURES, bridge.BREAKER_UNTIL):
            d.clear()
        self.marked = []
        bridge.arr_mark_failed = lambda cat, rel: self.marked.append(rel) or True
        bridge.notify = lambda *a, **k: None
        FAKE.t.clear()
        FAKE.calls.clear()
        FAKE.next_add = None
        FAKE.fetch_error = None
        self.bh = self.tmp / "blackhole" / "radarr"
        self.bh.mkdir(parents=True)
        (self.tmp / "blackhole" / "sonarr").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def drop(self, name, h="a" * 40):
        (self.bh / f"{name}.magnet").write_text(MAGNET.format(h=h))

    def recs(self):
        return {r["title"]: (sp, r) for sp, r in bridge.load_records()} if bridge.STATE_DIR.exists() else {}

    def rec(self, title):
        return self.recs()[title][1]

    def save(self, title, **kw):
        sp, r = self.recs()[title]
        r.update(kw)
        sp.write_text(json.dumps(r))

    def loop(self):
        bridge.process_new_magnets()
        bridge.check_completions()

    # --- staged delivery -------------------------------------------------------------------------

    def test_public_delivered_via_staging_with_one_rename(self):
        self.drop("Real Steel 2011 1080p BluRay x264")
        self.loop()
        FAKE.t["h-Real Steel 2011 1080p BluRay x264"]["done"] = True
        self.loop()
        dest = self.tmp / "downloads/radarr/Real Steel 2011 1080p BluRay x264"
        self.assertTrue((dest / "movie.mkv").exists())
        fetched_to = [c[1] for c in FAKE.calls if c[0] == "fetch_to"][0]
        self.assertIn("/.incoming/radarr/", fetched_to, "fetch must land in staging, not the watch folder")
        self.assertFalse((self.tmp / "downloads/.incoming/radarr/Real Steel 2011 1080p BluRay x264").exists())
        self.assertNotIn("Real Steel 2011 1080p BluRay x264", self.recs())
        self.assertIn(("discard", "Real Steel 2011 1080p BluRay x264"), FAKE.calls)

    def test_failed_fetch_never_touches_watch_folder(self):
        self.drop("Broken 2019 1080p WEB")
        self.loop()
        FAKE.t["h-Broken 2019 1080p WEB"]["done"] = True
        FAKE.fetch_error = "boom"
        self.loop()
        self.assertFalse((self.tmp / "downloads/radarr/Broken 2019 1080p WEB").exists())
        self.assertTrue(self.rec("Broken 2019 1080p WEB").get("fetch_failing_since"))

    def test_deliver_identical_existing_keeps_it_and_different_is_swapped(self):
        stage = self.tmp / "downloads/.incoming/radarr/R"
        dest = self.tmp / "downloads/radarr/R"
        for d in (stage, dest):
            d.mkdir(parents=True)
            (d / "a.mkv").write_text("same")
        self.assertEqual(bridge.deliver(stage, dest), "already delivered")
        self.assertFalse(stage.exists())
        stage.mkdir(parents=True)
        (stage / "a.mkv").write_text("newer!")
        self.assertEqual(bridge.deliver(stage, dest), "replaced an older copy")
        self.assertEqual((dest / "a.mkv").read_text(), "newer!")

    # --- unavailable / holds -----------------------------------------------------------------------

    def test_unavailable_never_fails_or_tombstones(self):
        self.drop("Ghost 2020 1080p WEB")
        self.loop()
        FAKE.t["h-Ghost 2020 1080p WEB"]["state"] = "gone"
        bridge.STALL_TIMEOUT = 0
        try:
            for _ in range(3):
                self.loop()
        finally:
            bridge.STALL_TIMEOUT = 2700
        r = self.rec("Ghost 2020 1080p WEB")
        self.assertFalse(r.get("superseded") or r.get("stalled"))
        self.assertTrue(r.get("unavailable_since"))
        self.assertEqual(self.marked, [])

    def test_private_hold_unknown_is_kept_and_true_releases(self):
        self.drop("Priv 2021 1080p WEB")
        self.loop()
        self.save("Priv 2021 1080p WEB", private=True, seed_rule={"hours": 48, "ratio": None, "key": "hd-space"})
        FAKE.t["h-Priv 2021 1080p WEB"]["done"] = True
        self.loop()
        self.assertTrue(self.rec("Priv 2021 1080p WEB").get("held"))
        del FAKE.t["h-Priv 2021 1080p WEB"]  # vanished from the backend listing
        self.loop()
        self.assertIn("Priv 2021 1080p WEB", self.recs(), "a hold must never end because the torrent is missing")
        self.assertNotIn(("end_hold", "Priv 2021 1080p WEB"), FAKE.calls)
        FAKE.t["h-Priv 2021 1080p WEB"] = dict(progress=100, done=True, state=None, partial=0, hold=True)
        self.loop()
        self.assertNotIn("Priv 2021 1080p WEB", self.recs())
        self.assertIn(("end_hold", "Priv 2021 1080p WEB"), FAKE.calls)

    # --- clocks + breaker -------------------------------------------------------------------------

    def test_clock_freeze_shifts_only_clocks_older_than_the_gap(self):
        self.drop("Old 2018 1080p WEB")
        self.loop()
        # last progress 65 min ago, but 60 of those nobody was looking: only ~5 min count, so
        # it must NOT stall (STALL_TIMEOUT 45 min) -- without the freeze it would
        t0 = time.time() - 3900
        self.save("Old 2018 1080p WEB", last_progress_at=t0, last_progress=5.0)
        FAKE.t["h-Old 2018 1080p WEB"]["progress"] = 5.0
        marker = bridge.STATE_DIR / ".seen-fake"
        marker.write_text(str(time.time() - 3600))  # nobody looked for an hour
        self.drop("New 2022 1080p WEB", h="b" * 40)
        bridge.process_new_magnets()
        new_added = self.rec("New 2022 1080p WEB")["added"]
        bridge.check_completions()
        old = self.rec("Old 2018 1080p WEB")
        self.assertAlmostEqual(old["last_progress_at"], t0 + 3600 - bridge.POLL, delta=5, msg="pushed back by the gap")
        self.assertFalse(old.get("stalled"))
        self.assertAlmostEqual(self.rec("New 2022 1080p WEB")["added"], new_added, delta=1)

    def test_circuit_breaker_stops_the_fourth_failure(self):
        bridge.STALL_TIMEOUT = 0
        try:
            names = [f"Movie{i} 20{10 + i} 1080p WEB" for i in range(4)]
            for i, n in enumerate(names):
                self.drop(n, h=str(i) * 40)
            self.loop()
            self.loop()  # progress recorded, then no advance -> stall
            self.loop()
        finally:
            bridge.STALL_TIMEOUT = 2700
        stalled = [n for n in names if self.rec(n).get("stalled")]
        self.assertEqual(len(stalled), 3, "3 failures go through, the 4th is held back")
        self.assertGreater(bridge.BREAKER_UNTIL.get("fake", 0), time.time())

    # --- private partials ------------------------------------------------------------------------

    def test_private_partial_stall_is_kept_not_cancelled(self):
        self.drop("Slow 2017 1080p WEB")
        self.loop()
        self.save("Slow 2017 1080p WEB", private=True, seed_rule={"hours": 72, "ratio": None, "key": "x"})
        FAKE.t["h-Slow 2017 1080p WEB"]["partial"] = 500 * 2**20
        bridge.STALL_TIMEOUT = 0
        try:
            self.loop()
            self.loop()
        finally:
            bridge.STALL_TIMEOUT = 2700
        r = self.rec("Slow 2017 1080p WEB")
        self.assertTrue(r.get("held") and r.get("no_deliver") and r.get("hold_max_until"))
        self.assertNotIn(("cancel", "Slow 2017 1080p WEB"), FAKE.calls)
        self.assertEqual(self.marked, ["Slow 2017 1080p WEB"])

    def test_private_partial_supersede_is_kept(self):
        self.drop("Dune 2021 720p WEB", h="c" * 40)
        self.loop()
        self.save("Dune 2021 720p WEB", private=True)
        FAKE.t["h-Dune 2021 720p WEB"]["partial"] = 100 * 2**20
        self.drop("Dune 2021 2160p REMUX", h="d" * 40)
        bridge.process_new_magnets()
        r = self.rec("Dune 2021 720p WEB")
        self.assertTrue(r.get("held") and r.get("no_deliver"))
        self.assertFalse(r.get("superseded"))
        self.assertNotIn(("cancel", "Dune 2021 720p WEB"), FAKE.calls)

    def test_public_supersede_still_cancels(self):
        self.drop("Dune 2021 720p WEB", h="c" * 40)
        self.loop()
        self.drop("Dune 2021 2160p REMUX", h="d" * 40)
        bridge.process_new_magnets()
        self.assertTrue(self.rec("Dune 2021 720p WEB").get("superseded"))
        self.assertIn(("cancel", "Dune 2021 720p WEB"), FAKE.calls)

    # --- routing / indexer ------------------------------------------------------------------------

    def test_private_magnet_detected_by_indexer(self):
        orig = bridge.grab_indexer
        bridge.grab_indexer = lambda cat, rel: "HD-Space (Prowlarr)"
        try:
            self.drop("Freeleech 2015 1080p BluRay")
            bridge.process_new_magnets()
        finally:
            bridge.grab_indexer = orig
        r = self.rec("Freeleech 2015 1080p BluRay")
        self.assertTrue(r.get("private"))
        self.assertEqual(r["seed_rule"]["key"], "hd-space")

    def test_legacy_state_without_backend_key(self):
        legacy = {"category": "sonarr", "title": "Legacy S01E01", "release_name": "Legacy S01E01",
                  "torrent_hash": "legacy", "added": time.time()}
        FAKE.t["legacy"] = dict(progress=50.0, done=False, state=None, partial=0, hold=False)
        bridge.state_file("sonarr", legacy["title"]).write_text(json.dumps(legacy))
        bridge.check_completions()
        self.assertEqual(self.rec("Legacy S01E01").get("last_progress"), 50.0)


if __name__ == "__main__":
    unittest.main()
