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
        self.added_magnets.append(magnet)
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

    def extra_trackers(self, rec, trackers_):
        self.extra.append(rec["title"])
        return len(trackers_)

    def swarm(self, rec):
        t = self.t.get(rec["torrent_hash"])
        return None if t is None or "swarm" not in t else t["swarm"]


FAKE = Fake()
backends._instances["fake"] = FAKE
backends._instances["seedr"] = FAKE  # legacy recs without a backend key

import bridge  # noqa: E402
import bridgelog  # noqa: E402
import puller  # noqa: E402
import trackers  # noqa: E402

PUBLIC_LIST = ["udp://tracker.one:1337/announce", "udp://tracker.two:6969/announce"]

MAGNET = "magnet:?xt=urn:btih:{h}&dn=x"


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        bridge.DATA = self.tmp
        bridge.STATE_DIR = self.tmp / "blackhole" / ".bridge-state"
        bridge.INCOMING = self.tmp / "downloads" / ".incoming"
        bridge.ARR = {}
        bridgelog._conn = None
        bridgelog.DB_PATH = str(self.tmp / "bridge.db")
        puller.DATA_ROOT = str(self.tmp)
        puller.PULL_MIN_FREE = 0
        puller._jobs.clear()
        for d in (bridge.FAIL_COUNTS, bridge.WARNED, bridge.INDEXERS, bridge.INDEXER_TRIES,
                  bridge.FAILURES, bridge.BREAKER_UNTIL):
            d.clear()
        self.marked = []
        self.actions = []
        bridge.arr_mark_failed = lambda cat, rel, hids=None: (self.marked.append(rel), self.actions.append(("fail", rel)))[0] or True
        bridge.unmonitor = lambda cat, keys: self.actions.append(("unmonitor", tuple(keys))) or True
        bridge.SLOW_DRY_RUN, bridge.GIVE_UP_DRY_RUN, bridge.GIVE_UP_AFTER = True, True, 10
        bridge.FAILFAST_DRY_RUN = True
        trackers.get = lambda: list(PUBLIC_LIST)  # never fetch from GitHub in tests
        FAKE.added_magnets, FAKE.extra = [], []
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
        puller.run_pending()  # the pull worker, synchronously

    # --- staged delivery -------------------------------------------------------------------------

    def test_public_delivered_via_staging_with_one_rename(self):
        self.drop("Real Steel 2011 1080p BluRay x264")
        self.loop()
        FAKE.t["h-Real Steel 2011 1080p BluRay x264"]["done"] = True
        self.loop()  # pull queued + run by the worker
        self.assertTrue(self.rec("Real Steel 2011 1080p BluRay x264").get("pulling"))
        self.assertFalse((self.tmp / "downloads/radarr/Real Steel 2011 1080p BluRay x264").exists(),
                         "nothing in the watch folder until the pull is done and delivered")
        self.loop()  # bridge sees the finished job -> deliver + discard
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
        self.loop()  # worker fails
        self.loop()  # bridge sees the failure
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
        orig = bridge.grab_info
        bridge.ARR = {"radarr": ("http://radarr.invalid", "k")}
        bridge.grab_info = lambda cat, rel: {"indexer": "HD-Space (Prowlarr)", "history_ids": [7], "keys": ["radarr:movie:5"]}
        try:
            self.drop("Freeleech 2015 1080p BluRay")
            bridge.process_new_magnets()
        finally:
            bridge.grab_info = orig
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


class EventLogTest(unittest.TestCase):
    # same fixture and helpers as EngineTest, without re-running its tests
    setUp, tearDown = EngineTest.setUp, EngineTest.tearDown
    drop, recs, rec, save, loop = EngineTest.drop, EngineTest.recs, EngineTest.rec, EngineTest.save, EngineTest.loop

    def test_timeline_of_a_public_transfer(self):
        self.drop("Karuppu 2026 1080p WEB")
        self.loop()
        FAKE.t["h-Karuppu 2026 1080p WEB"]["progress"] = 42.0
        self.loop()
        FAKE.t["h-Karuppu 2026 1080p WEB"]["done"] = True
        self.loop()
        self.loop()
        rows = bridgelog._db().execute(
            "SELECT type, phase FROM events WHERE release=? ORDER BY id", ("Karuppu 2026 1080p WEB",)).fetchall()
        self.assertEqual([r[0] for r in rows], ["added", "phase", "phase", "delivered", "done"])
        self.assertEqual([r[1] for r in rows], ["queued", "downloading", "pulling", "delivered", "done"])

    def test_waiting_reason_logged_once(self):
        FAKE.full_retries = None
        try:
            for _ in range(4):
                FAKE.next_add = Full(1e9)
                if not (self.bh / "Big 2018 2160p.magnet").exists():
                    self.drop("Big 2018 2160p", h="9" * 40)
                bridge.process_new_magnets()
        finally:
            FAKE.full_retries = 5
        n = bridgelog._db().execute("SELECT COUNT(*) FROM events WHERE type='waiting'").fetchone()[0]
        self.assertEqual(n, 1, "a waiting drop is logged when its reason changes, not every poll")

    def test_event_log_failure_never_breaks_a_transfer(self):
        bridgelog._conn = None
        bridgelog.DB_PATH = "/nonexistent/dir/bridge.db"
        self.drop("Resilient 2020 1080p WEB")
        self.loop()
        self.assertIn("Resilient 2020 1080p WEB", self.recs())


class RetryRulesTest(unittest.TestCase):
    setUp, tearDown = EngineTest.setUp, EngineTest.tearDown
    drop, recs, rec, save, loop = EngineTest.drop, EngineTest.recs, EngineTest.rec, EngineTest.save, EngineTest.loop

    def age(self, title, hours, progress):
        """Make a transfer look `hours` old, progressing at progress%/h over the last hour."""
        now = time.time()
        self.save(title, added=now - hours * 3600, last_progress=progress, last_progress_at=now,
                  rate=[[now - 3600, max(progress - 0.5, 0)], [now - 60, progress]])
        FAKE.t["h-" + title]["progress"] = progress

    def test_slow_under_5_percent_dry_run_only_flags(self):
        self.drop("Crawl 2019 1080p WEB")
        self.loop()
        self.age("Crawl 2019 1080p WEB", 2.5, 3.0)
        self.loop()
        r = self.rec("Crawl 2019 1080p WEB")
        self.assertIn("under 5%", r.get("slow_flagged", ""))
        self.assertFalse(r.get("superseded"))
        self.assertEqual(self.marked, [])

    def test_slow_live_fails_public_and_regrabs(self):
        bridge.SLOW_DRY_RUN = False
        self.drop("Crawl 2019 1080p WEB")
        self.loop()
        self.age("Crawl 2019 1080p WEB", 2.5, 3.0)
        self.loop()
        r = self.rec("Crawl 2019 1080p WEB")
        self.assertTrue(r.get("slow") and r.get("superseded"))
        self.assertIn(("cancel", "Crawl 2019 1080p WEB"), FAKE.calls)
        self.assertEqual(self.marked, ["Crawl 2019 1080p WEB"])

    def test_slow_eta_needs_an_hour_of_confirmation(self):
        bridge.SLOW_DRY_RUN = False
        self.drop("Drip 2020 1080p WEB")
        self.loop()
        self.age("Drip 2020 1080p WEB", 3, 40.0)  # 0.5%/h -> 120h left
        self.loop()
        r = self.rec("Drip 2020 1080p WEB")
        self.assertTrue(r.get("slow_since") and not r.get("superseded"), "first sighting only starts the clock")
        self.save("Drip 2020 1080p WEB", slow_since=time.time() - 3700)
        self.loop()
        self.assertTrue(self.rec("Drip 2020 1080p WEB").get("slow"))

    def test_slow_rule_skips_private_by_default(self):
        bridge.SLOW_DRY_RUN = False
        self.drop("Rare 2010 1080p BluRay")
        self.loop()
        self.save("Rare 2010 1080p BluRay", private=True)
        self.age("Rare 2010 1080p BluRay", 5, 1.0)
        self.loop()
        self.assertFalse(self.rec("Rare 2010 1080p BluRay").get("slow_flagged") or
                         self.rec("Rare 2010 1080p BluRay").get("slow"))

    def test_give_up_unmonitors_before_failing_then_drops_later_grabs(self):
        bridge.GIVE_UP_DRY_RUN, bridge.GIVE_UP_AFTER = False, 2
        bridge.ARR = {"radarr": ("http://radarr.invalid", "k")}
        orig = bridge.grab_info
        bridge.grab_info = lambda cat, rel: {"indexer": "1337x", "history_ids": [1], "keys": ["radarr:movie:42"]}
        bridge.STALL_TIMEOUT = 0
        try:
            for i, title in enumerate(["Cursed 2001 720p WEB", "Cursed 2001 1080p WEB"]):
                self.drop(title, h=str(i) * 40)
                self.loop()
                self.loop()  # no progress -> stalled -> attempt counted
            self.assertEqual(self.actions[-2:], [("unmonitor", ("radarr:movie:42",)), ("fail", "Cursed 2001 1080p WEB")],
                             "unmonitor must come BEFORE marking failed (which triggers a re-search)")
            self.drop("Cursed 2001 2160p WEB", h="7" * 40)
            bridge.process_new_magnets()
            self.assertFalse((self.bh / "Cursed 2001 2160p WEB.magnet").exists())
            self.assertNotIn("Cursed 2001 2160p WEB", self.recs())
        finally:
            bridge.grab_info, bridge.STALL_TIMEOUT = orig, 2700

    def test_replacement_grab_is_recorded_with_attempt_number(self):
        bridge.ARR = {"radarr": ("http://radarr.invalid", "k")}
        orig = bridge.grab_info
        bridge.grab_info = lambda cat, rel: {"indexer": "1337x", "history_ids": [1], "keys": ["radarr:movie:9"]}
        bridge.STALL_TIMEOUT = 0
        try:
            self.drop("Again 2005 720p WEB", h="1" * 40)
            self.loop()
            self.loop()
            self.drop("Again 2005 1080p WEB", h="2" * 40)
            bridge.process_new_magnets()
        finally:
            bridge.grab_info, bridge.STALL_TIMEOUT = orig, 2700
        row = bridgelog._db().execute("SELECT detail FROM events WHERE type='replacement_grabbed'").fetchone()
        self.assertEqual(json.loads(row[0])["attempt"], 2)


class PublicTrackersTest(unittest.TestCase):
    setUp, tearDown = EngineTest.setUp, EngineTest.tearDown
    drop, recs, rec, save, loop = EngineTest.drop, EngineTest.recs, EngineTest.rec, EngineTest.save, EngineTest.loop

    def test_magnet_dedupe(self):
        m = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=udp%3A%2F%2Ftracker.one%3A1337%2Fannounce"
        out, n = trackers.add_to_magnet(m, PUBLIC_LIST)
        self.assertEqual(n, 1)
        self.assertEqual(out.count("tracker.one"), 1)
        self.assertIn("tracker.two", out)

    def test_public_magnet_gets_trackers(self):
        self.drop("Public 2020 1080p WEB")
        bridge.process_new_magnets()
        self.assertIn("tracker.two", FAKE.added_magnets[0])

    def test_private_indexer_magnet_untouched(self):
        orig = bridge.grab_info
        bridge.ARR = {"radarr": ("http://radarr.invalid", "k")}
        bridge.grab_info = lambda cat, rel: {"indexer": "HD-Space (Prowlarr)", "history_ids": [1], "keys": []}
        try:
            self.drop("PrivMag 2021 1080p WEB")
            bridge.process_new_magnets()
        finally:
            bridge.grab_info = orig
        self.assertNotIn("tracker.one", FAKE.added_magnets[0])
        self.assertTrue(self.rec("PrivMag 2021 1080p WEB").get("private"))

    def _torrent(self, name, private):
        import hashlib
        def be(x):
            if isinstance(x, int): return b"i%de" % x
            if isinstance(x, str): x = x.encode()
            if isinstance(x, bytes): return b"%d:%s" % (len(x), x)
            if isinstance(x, dict): return b"d" + b"".join(be(k) + be(x[k]) for k in sorted(x)) + b"e"
        info = {"name": name, "piece length": 16384, "pieces": hashlib.sha1(b"x").digest(), "length": 1}
        if private:
            info["private"] = 1
        (self.bh / f"{name}.torrent").write_bytes(be({"announce": "http://t.invalid/a", "info": info}))

    def test_private_torrent_never_gets_trackers(self):
        self._torrent("Priv 2019 1080p BluRay", private=True)
        bridge.process_new_magnets()
        self.assertEqual(FAKE.extra, [])
        self.assertTrue(self.rec("Priv 2019 1080p BluRay").get("private"))

    def test_public_torrent_file_gets_trackers(self):
        self._torrent("Pub 2019 1080p BluRay", private=False)
        bridge.process_new_magnets()
        self.assertEqual(FAKE.extra, ["Pub 2019 1080p BluRay"])

    def test_list_fetch_failure_uses_cache(self):
        import importlib
        real = importlib.reload(trackers)
        try:
            real.CACHE = self.tmp / "trackers.txt"
            real.CACHE.write_text("udp://cached:1/announce\n")
            import os as _os
            _os.utime(real.CACHE, (0, 0))  # stale -> a refresh is attempted
            real.URL = "http://127.0.0.1:9/nothing"  # refused
            self.assertEqual(real.get(), ["udp://cached:1/announce"])
        finally:
            real.get = lambda: list(PUBLIC_LIST)


class FailFastTest(unittest.TestCase):
    setUp, tearDown = EngineTest.setUp, EngineTest.tearDown
    drop, recs, rec, save, loop = EngineTest.drop, EngineTest.recs, EngineTest.rec, EngineTest.save, EngineTest.loop

    def dead(self, title, swarm=None):
        self.drop(title)
        self.loop()
        self.save(title, added=time.time() - 900)
        if swarm is not None:
            FAKE.t["h-" + title]["swarm"] = swarm

    def test_dry_run_only_flags(self):
        self.dead("Dead 2011 1080p WEB", {"metadata": False, "seeds": None})
        self.loop()
        r = self.rec("Dead 2011 1080p WEB")
        self.assertIn("no metadata", r.get("failfast_flagged", ""))
        self.assertFalse(r.get("superseded"))

    def test_live_fails_zero_seeds(self):
        bridge.FAILFAST_DRY_RUN = False
        self.dead("Dead 2012 1080p WEB", {"metadata": True, "seeds": 0})
        self.loop()
        r = self.rec("Dead 2012 1080p WEB")
        self.assertTrue(r.get("dead") and r.get("superseded"))
        self.assertEqual(self.marked, ["Dead 2012 1080p WEB"])

    def test_started_download_is_alive(self):
        bridge.FAILFAST_DRY_RUN = False
        self.dead("Alive 2013 1080p WEB", {"metadata": True, "seeds": 0})
        self.save("Alive 2013 1080p WEB", last_progress=0.4)
        FAKE.t["h-Alive 2013 1080p WEB"]["progress"] = 0.4
        self.loop()
        self.assertFalse(self.rec("Alive 2013 1080p WEB").get("dead"))

    def test_private_is_skipped(self):
        bridge.FAILFAST_DRY_RUN = False
        self.dead("PrivDead 2014 1080p WEB", {"metadata": False, "seeds": 0})
        self.save("PrivDead 2014 1080p WEB", private=True)
        self.loop()
        self.assertFalse(self.rec("PrivDead 2014 1080p WEB").get("dead"))


class SeedrSwarmTest(unittest.TestCase):
    def test_seedr_swarm_from_transfer_entry(self):
        os.environ.setdefault("SEEDR_EMAIL", "x")
        os.environ.setdefault("SEEDR_PASSWORD", "y")
        import backends.seedr as sd
        b = sd.SeedrBackend()
        sd.list_folder = lambda folder_id="": {"folders": [], "files": [], "torrents": [
            {"id": 1, "name": "A", "progress": 0, "size": 0, "seeders": 0},
            {"id": 2, "name": "B", "progress": 0, "size": 700 * 2**20, "seeders": 0},
            {"id": 3, "name": "C", "progress": 3, "size": 700 * 2**20, "seeders": "12"}]}
        b.begin_loop([])
        self.assertEqual(b.swarm({"title": "A", "user_torrent_id": 1}), {"metadata": False, "seeds": 0})
        self.assertEqual(b.swarm({"title": "B", "user_torrent_id": 2}), {"metadata": True, "seeds": 0})
        self.assertEqual(b.swarm({"title": "C", "user_torrent_id": 3}), {"metadata": True, "seeds": 12})
        self.assertIsNone(b.swarm({"title": "gone", "user_torrent_id": 9}))


class StatusPageTest(unittest.TestCase):
    setUp, tearDown = EngineTest.setUp, EngineTest.tearDown
    drop, recs, rec, save, loop = EngineTest.drop, EngineTest.recs, EngineTest.rec, EngineTest.save, EngineTest.loop

    def test_status_and_escaping(self):
        import bridgeweb
        evil = "Evil <img src=x onerror=alert(1)> 2020 1080p WEB"
        self.drop(evil)
        self.loop()
        FAKE.t["h-" + evil]["progress"] = 12.5
        self.loop()
        self.drop("Waiting 2021 1080p WEB", h="w" * 40)  # not processed yet -> shows as waiting
        s = bridgeweb.status()
        self.assertEqual(s["active"][0]["phase"], "downloading")
        self.assertEqual(s["active"][0]["progress"], 12.5)
        self.assertEqual(len(s["waiting"]), 1)
        page = bridgeweb.page()
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        code, ctype, body = bridgeweb.handle("/bridge/status.json")
        self.assertEqual((code, ctype), (200, "application/json"))
        json.loads(body)
        tid = self.rec(evil)["tid"]
        code, _, body = bridgeweb.handle(f"/bridge/transfer/{tid}")
        self.assertEqual(code, 200)
        self.assertIn("added", body)
        self.assertNotIn("<img", body)
