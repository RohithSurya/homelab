"""Offline tests for backends/qbit.py: a fake qBittorrent WebUI (HTTP only, no BitTorrent) on
127.0.0.1 plus a fake SFTP/remote. Nothing here ever speaks BitTorrent or touches the network.
"""

import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.update(SEEDBOX_SSH_HOST="box.invalid", SEEDBOX_SSH_USER="u")
os.environ.pop("NTFY_TOPIC", None)

import vault  # noqa: E402
import backends.qbit as q  # noqa: E402
from backends.base import DONE, DOWNLOADING, UNAVAILABLE, Exists, Full  # noqa: E402

GiB = 1024**3
PASSWORD = "qbit-secret"
vault.get = lambda name: {"SEEDBOX_QBIT_PASSWORD": PASSWORD}[name]
q.ntfy = lambda *a, **k: None


class FakeQbit:
    def __init__(self):
        self.torrents = {}
        self.files = {}
        self.add_mode = "ok4"
        self.adds, self.deleted, self.resumed = [], [], []
        self.logins = 0
        self.password = PASSWORD


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body="", ctype="text/plain"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _form(self):
        n = int(self.headers.get("Content-Length", 0))
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

    def _authed(self):
        return "SID=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        S = self.server.qb
        u = urllib.parse.urlsplit(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if not self._authed():
            return self._send(403, "Forbidden")
        if u.path == "/api/v2/app/version":
            return self._send(200, "v4.5.2")
        if u.path == "/api/v2/torrents/info":
            ts = list(S.torrents.values())
            if "hashes" in qs:
                want = set(qs["hashes"].split("|"))
                ts = [t for t in ts if t["hash"] in want]
            if "tag" in qs:
                ts = [t for t in ts if qs["tag"] in t.get("tags", "")]
            return self._send(200, json.dumps(ts), "application/json")
        if u.path == "/api/v2/torrents/files":
            return self._send(200, json.dumps(S.files.get(qs["hash"], [])), "application/json")
        self._send(404)

    def do_POST(self):
        S = self.server.qb
        u = urllib.parse.urlsplit(self.path)
        form = self._form()
        if u.path == "/api/v2/auth/login":
            S.logins += 1
            if form.get("password") == S.password:
                self.send_response(200)
                self.send_header("Set-Cookie", "SID=ok; path=/")
                self.send_header("Content-Length", "3")
                self.end_headers()
                return self.wfile.write(b"Ok.")
            return self._send(200, "Fails.")
        if not self._authed():
            return self._send(403, "Forbidden")
        if u.path == "/api/v2/torrents/add":
            S.adds.append(form)
            if S.add_mode == "409":
                return self._send(409, "")
            h = urllib.parse.parse_qs(urllib.parse.urlsplit(form["urls"]).query)["xt"][0].split(":")[-1]
            S.torrents[h] = dict(hash=h, name="x", progress=0.0, state="metaDL", amount_left=1, tags=form.get("tags", ""))
            if S.add_mode == "json5":
                return self._send(200, json.dumps({"success_count": 1, "failure_count": 0}), "application/json")
            return self._send(200, "Ok.")
        if u.path == "/api/v2/torrents/delete":
            S.deleted.append(form["hashes"])
            S.torrents.pop(form["hashes"], None)
            return self._send(200, "")
        if u.path in ("/api/v2/torrents/resume", "/api/v2/torrents/start"):
            S.resumed.append(form["hashes"])
            return self._send(200, "")
        self._send(404)


class FakeFile:
    def __init__(self, path):
        self.f = open(path, "rb")
        self.prefetched = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()

    def seek(self, n):
        self.f.seek(n)

    def prefetch(self, size, max_concurrent_requests=None):
        self.prefetched = (size, max_concurrent_requests)

    def read(self, n):
        return self.f.read(n)


class FakeSFTP:
    def __init__(self, root):
        self.root = root

    def close(self):
        pass

    def _p(self, remote):
        return self.root / remote.lstrip("/")

    def stat(self, remote):
        return os.stat(self._p(remote))

    def open(self, remote, mode="rb"):
        return FakeFile(self._p(remote))


class FakeRemote:
    def __init__(self, url, root):
        self.url, self.root = url, root
        self.small = {".qbittorrentPort": "37301", ".quota": ""}

    def base_url(self):
        return self.url

    def read_small(self, path):
        return self.small[path]

    def qbit_port(self, refresh=False):
        return 37301

    def sftp(self):
        return FakeSFTP(self.root)

    def open_sftp(self):
        return FakeTransport(), FakeSFTP(self.root)


class FakeTransport:
    def close(self):
        pass


def quota(used_gib, limit_gib=230):
    return (f"Disk quotas for user u (uid 1):\n  Filesystem space quota limit grace files quota limit grace\n"
            f"  /dev/md0 {int(used_gib * 1024 * 1024)}K {limit_gib}G {int(limit_gib * 1.25)}G  1 115k 144k\n")


def magnet(h, size=None):
    m = f"magnet:?xt=urn:btih:{h}&dn=Some.Release"
    return m + (f"&xl={size}" if size else "")


class QbitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.qb = self.srv.qb = FakeQbit()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.be = q.QbitBackend()
        self.be.remote = FakeRemote(f"http://127.0.0.1:{self.srv.server_address[1]}", self.tmp / "remote")
        self.be.remote.small[".quota"] = quota(10)
        self.bh = self.tmp / "blackhole" / "radarr"
        self.bh.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def drop(self, name, m):
        p = self.bh / f"{name}.magnet"
        p.write_text(m)
        return p, m

    # --- quota / add -----------------------------------------------------------------------------

    def test_parse_quota(self):
        self.assertEqual(q.parse_quota(quota(0.5)), (int(0.5 * GiB), 230 * GiB))

    def test_add_sets_safe_options(self):
        p, m = self.drop("R1", magnet("a" * 40, 5 * GiB))
        keys = self.be.add(p, m)
        self.assertEqual(keys["torrent_hash"], "a" * 40)
        a = self.qb.adds[0]
        self.assertEqual((a["category"], a["tags"], a["paused"], a["stopped"], a["ratioLimit"], a["seedingTimeLimit"]),
                         ("bridge-radarr", "media-bridge", "false", "false", "-1", "-1"))

    def test_add_full_from_quota_and_pending(self):
        self.be.remote.small[".quota"] = quota(150)
        self.qb.torrents["z" * 40] = dict(hash="z" * 40, amount_left=40 * GiB, tags="media-bridge", state="downloading", progress=0.5)
        p, m = self.drop("R2", magnet("b" * 40, 30 * GiB))  # 230-150-40-20 margin = 20 GiB < 30
        with self.assertRaises(Full) as cm:
            self.be.add(p, m)
        self.assertFalse(cm.exception.never_fits)
        p, m = self.drop("R3", magnet("c" * 40, 220 * GiB))
        with self.assertRaises(Full) as cm:
            self.be.add(p, m)
        self.assertTrue(cm.exception.never_fits)
        self.assertEqual(self.qb.adds, [])

    def test_full_counts_non_bridge_torrents_too(self):
        self.be.remote.small[".quota"] = quota(100)
        # a manual ratio-building torrent (no media-bridge tag) still has 100 GiB to write
        self.qb.torrents["r" * 40] = dict(hash="r" * 40, amount_left=100 * GiB, tags="ratio-building", state="downloading", progress=0.1)
        with self.assertRaises(Full):  # 230 - 100 - 100 - 20 = 10 GiB < 15
            self.be.add(*self.drop("R12", magnet("9" * 40, 15 * GiB)))

    def test_magnet_without_size_needs_headroom(self):
        self.be.remote.small[".quota"] = quota(185)  # 230-185-20 = 25 GiB < 40 GiB headroom
        p, m = self.drop("R4", magnet("d" * 40))
        with self.assertRaises(Full):
            self.be.add(p, m)

    def test_reserved_counts_adds_within_one_loop(self):
        # 230 - 170 used - 20 margin = 40 GiB: one 25 GiB add fits, a second one must not, even
        # though ~/.quota (refreshed each minute) doesn't show the first one yet
        self.be.remote.small[".quota"] = quota(170)
        self.be.add(*self.drop("R5", magnet("e" * 40, 25 * GiB)))
        self.qb.torrents["e" * 40]["amount_left"] = 0  # pretend qBit hasn't allocated yet
        with self.assertRaises(Full):
            self.be.add(*self.drop("R6", magnet("f" * 40, 25 * GiB)))

    def test_add_qbit5_json_and_409_and_existing(self):
        self.qb.add_mode = "json5"
        self.assertEqual(self.be.add(*self.drop("R7", magnet("1" * 40, GiB)))["torrent_hash"], "1" * 40)
        with self.assertRaises(Exists):
            self.be.add(*self.drop("R8", magnet("1" * 40, GiB)))  # already listed
        self.qb.add_mode = "409"
        with self.assertRaises(Exists):
            self.be.add(*self.drop("R9", magnet("2" * 40, GiB)))

    # --- status / holds --------------------------------------------------------------------------

    def rec(self, h, **kw):
        return dict({"title": "T", "category": "radarr", "torrent_hash": h}, **kw)

    def loop(self, *recs):
        self.be.begin_loop([(None, r) for r in recs])

    def test_status_states(self):
        self.qb.torrents["3" * 40] = dict(hash="3" * 40, progress=0.4, state="stoppedDL", name="n")
        self.qb.torrents["4" * 40] = dict(hash="4" * 40, progress=1.0, state="uploading", name="n", save_path="/d")
        r3, r4, r5 = self.rec("3" * 40), self.rec("4" * 40), self.rec("5" * 40)
        self.loop(r3, r4, r5)
        self.assertEqual(self.be.status(r3), (DOWNLOADING, 40.0))
        self.assertIn("3" * 40, self.qb.resumed, "a stopped download is resumed, not left to 'stall'")
        self.assertEqual(self.be.status(r4)[0], DONE)
        self.assertEqual(self.be.status(r5)[0], UNAVAILABLE)

    def test_hold_over_rules(self):
        rule = {"hours": 48, "ratio": None}
        H = {c: c * 40 for c in "6789a"}
        self.qb.torrents[H["6"]] = dict(hash=H["6"], state="stalledUP", seeding_time=(48 + 3) * 3600 + 1, ratio=0.1)
        self.qb.torrents[H["7"]] = dict(hash=H["7"], state="stalledUP", seeding_time=48 * 3600, ratio=0.1)  # margin not met
        self.qb.torrents[H["8"]] = dict(hash=H["8"], state="stoppedUP", seeding_time=3600, ratio=0.1)
        self.qb.torrents[H["9"]] = dict(hash=H["9"], state="uploading", seeding_time=10, ratio=1.2)
        self.qb.torrents[H["a"]] = dict(hash=H["a"], state="error", seeding_time=10, ratio=0)
        recs = {c: self.rec(h, seed_rule=dict(rule, ratio=1.0) if c == "9" else rule) for c, h in H.items()}
        missing = self.rec("b" * 40, seed_rule=rule)
        self.loop(*recs.values(), missing)
        self.assertTrue(self.be.hold_over(recs["6"]))
        self.assertFalse(self.be.hold_over(recs["7"]))
        self.assertFalse(self.be.hold_over(recs["8"]))
        self.assertIn(H["8"], self.qb.resumed)
        self.assertTrue(self.be.hold_over(recs["9"]), "ratio path")
        self.assertIsNone(self.be.hold_over(recs["a"]), "broken torrent: unknown, keep")
        self.assertIsNone(self.be.hold_over(missing), "missing is NEVER 'done'")

    def test_begin_loop_refuses_empty_listing_for_many(self):
        with self.assertRaises(RuntimeError):
            self.loop(self.rec("c" * 40), self.rec("d" * 40))

    def test_reap_skips_live_hash(self):
        h = "e" * 40
        self.qb.torrents[h] = dict(hash=h, state="downloading", progress=0.1)
        dead, live = self.rec(h, superseded=True), self.rec(h)
        self.loop(dead, live)
        self.be.reap(dead)
        self.assertEqual(self.qb.deleted, [], "a live re-add with the same hash survives the reaper")
        self.loop(dead)
        self.be.reap(dead)
        self.assertEqual(self.qb.deleted, [h])

    # --- fetch -----------------------------------------------------------------------------------

    def test_fetch_strips_root_and_resumes_part(self):
        root = self.tmp / "remote" / "home/u/data/Rel.Name"
        (root / "Subs").mkdir(parents=True)
        (root / "Rel.Name.mkv").write_bytes(b"A" * 5000)
        (root / "Subs" / "eng.srt").write_bytes(b"sub")
        h = "f" * 40
        self.qb.files[h] = [{"name": "Rel.Name/Rel.Name.mkv", "size": 5000, "priority": 1},
                            {"name": "Rel.Name/Subs/eng.srt", "size": 3, "priority": 1},
                            {"name": "Rel.Name/sample.mkv", "size": 9, "priority": 0}]
        dest = self.tmp / "stage"
        dest.mkdir()
        (dest / "Rel.Name.mkv.part").write_bytes(b"A" * 1200)  # earlier partial pull
        self.be.fetch({"title": "T"}, {"hash": h, "save_path": "/home/u/data", "name": "Rel.Name"}, dest)
        self.assertEqual((dest / "Rel.Name.mkv").read_bytes(), b"A" * 5000)
        self.assertEqual((dest / "Subs" / "eng.srt").read_bytes(), b"sub")
        self.assertFalse((dest / "sample.mkv").exists(), "deselected files are skipped")
        self.assertFalse(list(dest.rglob("*.part")))

    def test_dropped_keepalive_is_retried_once(self):
        # qBittorrent closes idle keep-alive connections during long SFTP pulls; the next request
        # on the pooled connection fails with RemoteDisconnected (seen live 2026-09-25)
        import requests
        self.be._api("GET", "/app/version")  # log in, pool a connection
        real, calls = self.be.s.request, []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("Remote end closed connection without response")
            return real(*a, **k)

        self.be.s.request = flaky
        self.assertEqual(self.be._api("GET", "/app/version").text, "v4.5.2")
        self.assertEqual(len(calls), 2)

    def test_segmented_pull_parallel_and_resume(self):
        q.SEGMENT_MIN, q.PULL_SEGMENTS, q.CHUNK = 1000, 4, 256
        try:
            root = self.tmp / "remote" / "home/u/data"
            root.mkdir(parents=True)
            data = bytes(range(256)) * 40  # 10240 B, distinct bytes per offset
            (root / "Big.mkv").write_bytes(data)
            plan = {"files": [{"remote": "/home/u/data/Big.mkv", "rel": ["Big.mkv"], "size": len(data)}]}
            dest = self.tmp / "stage"
            # an interrupted earlier pull: map says segment 0 got 1000 B, the rest nothing
            dest.mkdir()
            part = dest / "Big.mkv.part"
            with open(part, "wb") as f:
                f.truncate(len(data))
                f.seek(0)
                f.write(data[:1000])
            step = -(-len(data) // 4)
            segs = [[i * step, min(len(data), (i + 1) * step), i * step] for i in range(4)]
            segs[0][2] = 1000
            (dest / "Big.mkv.part.map").write_text(json.dumps({"size": len(data), "segs": segs}))
            self.be.pull({"title": "T"}, plan, dest)
            self.assertEqual((dest / "Big.mkv").read_bytes(), data, "every byte in place after a resumed parallel pull")
            self.assertFalse(list(dest.glob("*.part*")))
        finally:
            q.SEGMENT_MIN, q.PULL_SEGMENTS, q.CHUNK = 64 * 1024**2, 4, 1 << 20

    def test_segmented_pull_cancel_stops(self):
        import threading as th
        q.SEGMENT_MIN, q.CHUNK = 1000, 256
        try:
            root = self.tmp / "remote" / "home/u/data"
            root.mkdir(parents=True)
            (root / "Big.mkv").write_bytes(b"x" * 10240)
            plan = {"files": [{"remote": "/home/u/data/Big.mkv", "rel": ["Big.mkv"], "size": 10240}]}
            cancel = th.Event()
            cancel.set()
            self.be.pull({"title": "T"}, plan, self.tmp / "stage", cancel)
            self.assertFalse((self.tmp / "stage" / "Big.mkv").exists())
        finally:
            q.SEGMENT_MIN, q.CHUNK = 64 * 1024**2, 1 << 20

    # --- login -----------------------------------------------------------------------------------

    def test_refused_login_backs_off_without_retrying(self):
        self.qb.password = "changed-in-panel"
        with self.assertRaises(RuntimeError):
            self.be.add(*self.drop("R10", magnet("7" * 40, GiB)))
        with self.assertRaises(RuntimeError):
            self.be.add(*self.drop("R11", magnet("8" * 40, GiB)))
        self.assertEqual(self.qb.logins, 1, "exactly one login attempt, then back off")


class CountingFile(FakeFile):
    """FakeFile that records read sizes and tells its client when it is in use."""

    def __init__(self, path, client):
        super().__init__(path)
        self.client = client

    def __enter__(self):
        with self.client.lock:
            self.client.active += 1
            self.client.max_active = max(self.client.max_active, self.client.active)
        return self

    def __exit__(self, *a):
        with self.client.lock:
            self.client.active -= 1
        super().__exit__(*a)

    def read(self, n):
        c = self.client
        c.reads.append(n)
        if c.fail_after is not None and len(c.reads) > c.fail_after:
            raise OSError("connection dropped")
        if c.on_read:
            c.on_read()
        time.sleep(c.delay)
        return super().read(n)


class CountingSFTP(FakeSFTP):
    def __init__(self, root):
        super().__init__(root)
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self.reads, self.closed = [], False
        self.delay, self.fail_after, self.on_read = 0.0, None, None

    def open(self, remote, mode="rb"):
        return CountingFile(self._p(remote), self)

    def close(self):
        self.closed = True


class PullFixesTest(unittest.TestCase):
    """The pull path on its own (no qBittorrent needed): progress, cancel, durability, one client
    per stream, abort on the first error, path safety."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.root = self.tmp / "remote"
        (self.root / "home/u/data").mkdir(parents=True)
        self.be = q.QbitBackend()
        self.be.remote = FakeRemote(None, self.root)
        self.clients = []

        def open_sftp():
            c = CountingSFTP(self.root)
            self.clients.append(c)
            return FakeTransport(), c

        self.be.remote.open_sftp = open_sftp
        self.saved = (q.SEGMENT_MIN, q.PULL_SEGMENTS, q.CHUNK, q.READ_SIZE)
        q.SEGMENT_MIN, q.PULL_SEGMENTS, q.CHUNK = 1000, 4, 256
        self.dest = self.tmp / "stage"

    def tearDown(self):
        q.SEGMENT_MIN, q.PULL_SEGMENTS, q.CHUNK, q.READ_SIZE = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def remote_file(self, name, data):
        (self.root / "home/u/data" / name).write_bytes(data)
        return {"files": [{"remote": f"/home/u/data/{name}", "rel": [name], "size": len(data)}]}

    def test_small_file_pull_honours_cancel_and_resumes(self):
        q.READ_SIZE, q.SEGMENT_MIN = 1000, 10**9  # single-stream _get
        data = bytes(range(250)) * 20
        plan = self.remote_file("Small.mkv", data)
        cancel = threading.Event()
        orig = self.be.remote.open_sftp

        def open_sftp():
            t, c = orig()
            c.on_read = cancel.set  # cancelled right after the first read
            return t, c

        self.be.remote.open_sftp = open_sftp
        self.be.pull({"title": "T"}, plan, self.dest, cancel)
        self.assertFalse((self.dest / "Small.mkv").exists(), "a cancelled pull never renames")
        self.assertEqual((self.dest / "Small.mkv.part").stat().st_size, 1000, "partial kept for resume")
        self.assertTrue(self.clients[0].closed, "the abandoned connection is closed")
        self.be.remote.open_sftp = orig
        self.be.pull({"title": "T"}, plan, self.dest)
        self.assertEqual((self.dest / "Small.mkv").read_bytes(), data)

    def test_sparse_part_without_map_is_never_resumed(self):
        q.SEGMENT_MIN = 10**9
        data = b"z" * 100_000
        plan = self.remote_file("S.mkv", data)
        self.dest.mkdir()
        with open(self.dest / "S.mkv.part", "wb") as f:
            f.truncate(len(data))  # a segmented pull's preallocation whose map is gone
        self.be.pull({"title": "T"}, plan, self.dest)
        self.assertEqual((self.dest / "S.mkv").read_bytes(), data, "not 100 kB of zeros")

    def test_reads_ask_paramiko_for_one_request_at_a_time(self):
        data = os.urandom(20_000)
        plan = self.remote_file("R.mkv", data)
        CountingFile.MAX_REQUEST_SIZE = 512
        try:
            self.be.pull({"title": "T"}, plan, self.dest)
        finally:
            del CountingFile.MAX_REQUEST_SIZE
        self.assertEqual((self.dest / "R.mkv").read_bytes(), data)
        self.assertLessEqual(max(r for c in self.clients for r in c.reads), 512)

    def test_map_saved_only_after_fsync_and_atomically(self):
        data = os.urandom(200_000)
        plan = self.remote_file("M.mkv", data)
        q.PULL_SEGMENTS = 1
        events = []
        real_fsync, real_save = q.os.fsync, q._save_map
        with mock.patch.object(q.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1]), \
                mock.patch.object(q, "_save_map", lambda *a: (events.append("map"), real_save(*a))[1]):
            self.dest.mkdir()
            (self.dest / "M.mkv.part.map").write_text("{}")  # force the segmented path
            (self.dest / "M.mkv.part").write_bytes(b"")
            self.be.pull({"title": "T"}, plan, self.dest)
        self.assertEqual((self.dest / "M.mkv").read_bytes(), data)
        later = events[1:]  # the first map is written at preallocation, before any data
        self.assertGreater(later.count("map"), 1, "saved periodically, not only at the end")
        for i, e in enumerate(later):
            if e == "map":
                self.assertEqual(later[i - 1], "fsync", "data fsynced before the map claims it")
        self.assertFalse(list(self.dest.glob("*.tmp")))

    def test_resumed_map_with_more_ranges_than_clients_never_shares_a_client(self):
        data = bytes(range(256)) * 40
        plan = self.remote_file("Big.mkv", data)
        self.dest.mkdir()
        with open(self.dest / "Big.mkv.part", "wb") as f:
            f.truncate(len(data))
        step = -(-len(data) // 6)
        segs = [[i * step, min(len(data), (i + 1) * step), i * step] for i in range(6)]
        (self.dest / "Big.mkv.part.map").write_text(json.dumps({"size": len(data), "segs": segs}))
        q.PULL_SEGMENTS = 2  # the map was cut for 6 streams, only 2 now
        orig = self.be.remote.open_sftp

        def open_sftp():
            t, c = orig()
            c.delay = 0.001  # keeps streams overlapping
            return t, c

        self.be.remote.open_sftp = open_sftp
        self.be.pull({"title": "T"}, plan, self.dest)
        self.assertEqual((self.dest / "Big.mkv").read_bytes(), data)
        self.assertEqual(len(self.clients), 2)
        self.assertEqual([c.max_active for c in self.clients], [1, 1], "one open file per client at a time")

    def test_first_stream_error_stops_its_siblings(self):
        data = os.urandom(64 * 1024)
        plan = self.remote_file("E.mkv", data)
        n = 0

        def open_sftp():
            nonlocal n
            c = CountingSFTP(self.root)
            c.delay = 0.002
            if n == 0:
                c.fail_after = 2  # the first stream's connection drops early
            n += 1
            self.clients.append(c)
            return FakeTransport(), c

        self.be.remote.open_sftp = open_sftp
        q.READ_SIZE = 256
        with self.assertRaises(RuntimeError):
            self.be.pull({"title": "T"}, plan, self.dest)
        per_stream = len(data) // 4 // 256  # reads a sibling would need to finish its range
        self.assertTrue(all(len(c.reads) < per_stream for c in self.clients[1:]), "siblings stopped early")
        self.assertTrue(all(c.closed for c in self.clients))
        self.assertTrue((self.dest / "E.mkv.part.map").exists(), "progress kept for the retry")

    def test_paths_outside_the_stage_are_refused(self):
        (self.root / "home/u/data/x").write_bytes(b"x")
        for rel in (["..", "evil.mkv"], ["sub", "..", "..", "evil.mkv"], ["/tmp/evil.mkv"], [""]):
            plan = {"files": [{"remote": "/home/u/data/x", "rel": rel, "size": 1}]}
            with self.assertRaises(RuntimeError, msg=rel):
                self.be.pull({"title": "T"}, plan, self.dest)
        self.assertFalse((self.tmp / "evil.mkv").exists())
        self.assertEqual(self.clients, [], "refused before any connection is opened")


class RealParamikoTest(unittest.TestCase):
    """The pull against real paramiko: an in-process SFTP server over a socketpair (no network).
    Checks what the fakes can't: paramiko's own read/prefetch behaviour, and that an aborted pull
    leaves no prefetch thread spinning."""

    @classmethod
    def setUpClass(cls):
        import logging

        import paramiko

        logging.getLogger("paramiko").setLevel(logging.CRITICAL)  # "connection reset" at teardown
        cls.pk = paramiko
        cls.hostkey = paramiko.ECDSAKey.generate()

    def setUp(self):
        pk = self.pk
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.root = self.tmp / "remote"
        (self.root / "home/u/data").mkdir(parents=True)
        self.servers, self.delay = [], 0.0
        root, test = self.root, self

        class Handle(pk.SFTPHandle):
            def read(self, offset, length):
                time.sleep(test.delay)
                return super().read(offset, length)

            def stat(self):
                return pk.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))

        class Files(pk.SFTPServerInterface):
            def open(self, path, flags, attr):
                h = Handle(flags)
                h.readfile = open(root / path.lstrip("/"), "rb")
                h.filename = path
                return h

            def stat(self, path):
                return pk.SFTPAttributes.from_stat(os.stat(root / path.lstrip("/")))

            lstat = stat

        class Server(pk.ServerInterface):
            def get_allowed_auths(self, username):
                return "none"

            def check_auth_none(self, username):
                return pk.AUTH_SUCCESSFUL

            def check_channel_request(self, kind, chanid):
                return pk.OPEN_SUCCEEDED

        def open_sftp():
            a, b = socket.socketpair()
            st = pk.Transport(b)
            st.add_server_key(self.hostkey)
            st.set_subsystem_handler("sftp", pk.SFTPServer, Files)
            st.start_server(event=threading.Event(), server=Server())
            self.servers.append(st)
            ct = pk.Transport(a)
            ct.start_client(timeout=10)
            ct.auth_none("u")
            return ct, pk.SFTPClient.from_transport(ct)

        self.be = q.QbitBackend()
        self.be.remote = FakeRemote(None, self.root)
        self.be.remote.open_sftp = open_sftp
        self.saved = (q.SEGMENT_MIN, q.PULL_SEGMENTS, q.PREFETCH_REQUESTS)
        q.SEGMENT_MIN, q.PULL_SEGMENTS = 1 << 20, 4
        self.dest = self.tmp / "stage"

    def tearDown(self):
        q.SEGMENT_MIN, q.PULL_SEGMENTS, q.PREFETCH_REQUESTS = self.saved
        for st in self.servers:
            st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def prefetchers():
        return [t for t in threading.enumerate() if t.name.endswith("(_prefetch_thread)") and t.is_alive()]

    def test_segmented_pull_through_real_paramiko(self):
        data = os.urandom(3 << 20)
        (self.root / "home/u/data/Real.mkv").write_bytes(data)
        plan = {"files": [{"remote": "/home/u/data/Real.mkv", "rel": ["Real.mkv"], "size": len(data)}]}
        self.be.pull({"title": "T"}, plan, self.dest)
        self.assertEqual((self.dest / "Real.mkv").read_bytes(), data)

    def test_cancelled_pull_leaves_no_prefetch_thread_spinning(self):
        q.PREFETCH_REQUESTS = 4  # few slots: the prefetch thread sits in its wait loop
        self.delay = 0.02  # 32 requests per stream -> ~0.6 s, so the 0.3 s cancel lands mid-pull
        data = os.urandom(4 << 20)
        (self.root / "home/u/data/Slow.mkv").write_bytes(data)
        plan = {"files": [{"remote": "/home/u/data/Slow.mkv", "rel": ["Slow.mkv"], "size": len(data)}]}
        before = set(self.prefetchers())
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        self.be.pull({"title": "T"}, plan, self.dest, cancel)
        self.assertFalse((self.dest / "Slow.mkv").exists())
        deadline = time.time() + 5
        while set(self.prefetchers()) - before and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(set(self.prefetchers()) - before, set(), "no leaked prefetch threads")


if __name__ == "__main__":
    unittest.main()
