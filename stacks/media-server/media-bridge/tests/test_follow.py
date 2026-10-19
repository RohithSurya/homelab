"""Offline tests for follow mode (backends/follow.py + the seedbox backend's follow steps): a fake
qBittorrent (HTTP) + a fake SFTP over local files. A real .torrent is built in memory, padded
multi-file included; the "box" is a directory whose files fill in as the fake download proceeds.
"""

import hashlib
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_qbit as tq  # noqa: E402  (fake qBittorrent + fake SFTP; sets the env/vault stubs)
import backends.qbit as q  # noqa: E402
from backends import follow  # noqa: E402

PL = 16384  # piece length


def benc(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(benc(v) for v in x) + b"e"
    return b"d" + b"".join(benc(k) + benc(x[k]) for k in sorted(x)) + b"e"


def make_torrent(name, files, pad=True):
    """files: [(path parts, bytes)] -> (.torrent bytes, [(parts, data, is_pad)] incl. BEP 47 pads)"""
    entries, stream = [], b""
    for i, (parts, data) in enumerate(files):
        entries.append((parts, data, False))
        stream += data
        if pad and i < len(files) - 1 and len(stream) % PL:
            n = PL - len(stream) % PL
            entries.append(([".pad", str(n)], bytes(n), True))
            stream += bytes(n)
    pieces = b"".join(hashlib.sha1(stream[i:i + PL]).digest() for i in range(0, len(stream), PL))
    info = {"name": name, "piece length": PL, "pieces": pieces,
            "files": [dict({"length": len(d), "path": p}, **({"attr": "p"} if isp else {})) for p, d, isp in entries]}
    return benc({"announce": "http://t.invalid/a", "info": info}), entries, stream


class FollowTest(unittest.TestCase):
    setUpClass = classmethod(tq.QbitTest.setUpClass.__func__)
    tearDownClass = classmethod(tq.QbitTest.tearDownClass.__func__)

    def setUp(self):
        tq.QbitTest.setUp(self)
        self.box = self.tmp / "remote"
        self.stage = self.tmp / "stage"
        self.h = "f" * 40
        self.saved = (q.PULL_SEGMENTS, q.FOLLOW_RETRY_WAIT, q.FOLLOW_STEP)
        q.PULL_SEGMENTS, q.FOLLOW_RETRY_WAIT = 2, 0

    def tearDown(self):
        q.PULL_SEGMENTS, q.FOLLOW_RETRY_WAIT, q.FOLLOW_STEP = self.saved
        tq.QbitTest.tearDown(self)

    def seed(self, name, files, pad=True, skip=()):
        tor, entries, stream = make_torrent(name, files, pad)
        self.qb.export[self.h] = tor
        self.qb.props[self.h] = {"save_path": "/home/u/data"}
        self.qb.files[self.h] = [{"name": "/".join([name] + p), "size": len(d), "priority": 0 if "/".join(p) in skip else 1}
                                 for p, d, isp in entries if not isp]
        self.entries, self.stream = entries, stream
        self.n = -(-len(stream) // PL)
        for p, d, isp in entries:  # qBittorrent preallocates every real file at full size (zeros)
            if not isp:
                f = self.box.joinpath("home/u/data", name, *p)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(bytes(len(d)))
        self.name = name

    def box_has(self, pieces, flushed=True):
        """qBittorrent finished these pieces; flushed=False: reported done, bytes not on disk yet."""
        states = self.qb.pieces.setdefault(self.h, [0] * self.n)
        for i in pieces:
            states[i] = 2
            if not flushed:
                continue
            a, b = i * PL, min(len(self.stream), (i + 1) * PL)
            off = 0
            for p, d, isp in self.entries:
                lo, hi = max(a, off), min(b, off + len(d))
                if lo < hi and not isp:
                    f = self.box.joinpath("home/u/data", self.name, *p)
                    with open(f, "r+b") as fh:
                        fh.seek(lo - off)
                        fh.write(self.stream[lo:hi])
                off += len(d)

    def rec(self):
        return {"title": "T", "torrent_hash": self.h}

    def step(self):
        plan = self.be.prepare_follow(self.rec(), self.stage)
        return plan, (self.be.pull(self.rec(), plan, self.stage) if plan else None)

    def final(self, files):
        plan = {"files": [{"remote": f"/home/u/data/{self.name}/" + "/".join(p), "rel": p, "size": len(d)}
                          for p, d in files]}
        self.be.pull(self.rec(), plan, self.stage)

    def test_steps_then_finish_padded_multi_file(self):
        files = [(["a.mkv"], os.urandom(PL * 5 + 777)), (["Sub", "b.srt"], os.urandom(3000)),
                 (["c.mkv"], os.urandom(PL * 3 + 5))]
        self.seed("Rel", files)
        self.box_has(range(0, self.n, 2))  # rarest-first: scattered pieces
        self.box_has([1], flushed=False)  # reported done before it reached the box's disk
        plan, r = self.step()
        self.assertEqual(r["bad"], 1, "the unflushed piece fails its hash and is not kept")
        st = follow.load(self.stage)
        self.assertEqual(st["verified"][1], 0)
        self.assertTrue(all(st["verified"][i] for i in range(0, self.n, 2) if i in follow.needed(st)))
        # nothing unverified was written: piece 1 of a.mkv is still a hole (zeros) locally
        with open(self.stage / "a.mkv.part", "rb") as fh:
            fh.seek(PL)
            self.assertEqual(fh.read(PL), bytes(PL))
        self.box_has(range(self.n))  # download finished and flushed
        self.final(files)
        for p, d in files:
            self.assertEqual(self.stage.joinpath(*p).read_bytes(), d)
        self.assertFalse((self.stage / follow.STATE).exists())
        self.assertEqual(sorted(x.name for x in self.stage.rglob("*.part")), [])

    def test_single_file_and_step_cap(self):
        data = os.urandom(PL * 20 + 1)
        tor, entries, stream = make_torrent("One.mkv", [])  # placeholder to reuse helpers
        info_files = [(["One.mkv"], data)]
        # single-file torrent: info has "length", no "files"
        pieces = b"".join(hashlib.sha1(data[i:i + PL]).digest() for i in range(0, len(data), PL))
        self.qb.export[self.h] = benc({"info": {"name": "One.mkv", "piece length": PL, "pieces": pieces, "length": len(data)}})
        self.qb.props[self.h] = {"save_path": "/home/u/data"}
        self.qb.files[self.h] = [{"name": "One.mkv", "size": len(data), "priority": 1}]
        self.entries, self.stream, self.n, self.name = [([], data, False)], data, len(pieces) // 20, ""
        f = self.box / "home/u/data/One.mkv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        self.qb.pieces[self.h] = [2] * self.n
        q.FOLLOW_STEP = PL * 8
        plan, r = self.step()
        self.assertEqual(r["verified"], 8, "one step pulls at most FOLLOW_STEP")
        plan, r = self.step()
        self.assertEqual(r["verified"], 8)
        plan = {"files": [{"remote": "/home/u/data/One.mkv", "rel": ["One.mkv"], "size": len(data)}]}
        self.be.pull(self.rec(), plan, self.stage)
        self.assertEqual((self.stage / "One.mkv").read_bytes(), data)

    def test_deselected_file_is_not_waited_for_or_written(self):
        files = [(["a.mkv"], os.urandom(PL * 4)), (["extras.zip"], os.urandom(PL * 4))]
        self.seed("Rel", files, skip={"extras.zip"})
        self.box_has(range(4))  # only a.mkv's pieces ever download
        self.step()
        st = follow.load(self.stage)
        self.assertEqual(follow.missing(st), [])
        self.final([files[0]])
        self.assertEqual((self.stage / "a.mkv").read_bytes(), files[0][1])
        self.assertFalse((self.stage / "extras.zip").exists())
        self.assertFalse((self.stage / "extras.zip.part").exists())

    def test_failed_finish_falls_back_to_a_normal_pull(self):
        files = [(["a.mkv"], os.urandom(PL * 3))]
        self.seed("Rel", files)
        self.box_has([0])
        self.step()
        self.qb.pieces[self.h] = [2] * self.n  # "done", but the box never flushed pieces 1-2
        self.be.remote.open_sftp = lambda: (tq.FakeTransport(), tq.FakeSFTP(self.box))
        with open(self.box / "home/u/data/Rel/a.mkv", "r+b") as fh:  # the normal pull reads the real file
            fh.write(files[0][1])
        saved, follow_fetch = q.FOLLOW_PASSES, self.be._follow_fetch
        q.FOLLOW_PASSES = 1
        self.be._follow_fetch = lambda *a: (_ for _ in ()).throw(RuntimeError("box went away"))
        try:
            self.final(files)
        finally:
            q.FOLLOW_PASSES = saved
            self.be._follow_fetch = follow_fetch
        self.assertEqual((self.stage / "a.mkv").read_bytes(), files[0][1])
        self.assertFalse((self.stage / follow.STATE).exists())

    def test_no_export_means_no_follow(self):
        self.qb.pieces[self.h] = [2, 2]
        self.assertIsNone(self.be.prepare_follow(self.rec(), self.stage))

    def test_unsafe_path_is_refused(self):
        self.seed("Rel", [(["..", "evil.mkv"], os.urandom(PL))], pad=False)
        self.box_has([0])
        with self.assertRaises(RuntimeError):
            self.step()
        self.assertFalse((self.tmp / "evil.mkv.part").exists())

    def test_other_torrents_state_is_discarded_by_a_normal_pull(self):
        files = [(["a.mkv"], os.urandom(PL * 3))]
        self.seed("Rel", files)
        self.box_has([0])
        self.step()
        self.assertTrue((self.stage / follow.STATE).exists())
        self.box_has(range(self.n))
        rec = {"title": "T", "torrent_hash": "e" * 40}  # a different grab of the same release
        plan = {"files": [{"remote": "/home/u/data/Rel/a.mkv", "rel": ["a.mkv"], "size": len(files[0][1])}]}
        self.be.remote.open_sftp = lambda: (tq.FakeTransport(), tq.FakeSFTP(self.box))
        self.be.pull(rec, plan, self.stage)
        self.assertEqual((self.stage / "a.mkv").read_bytes(), files[0][1])
        self.assertFalse((self.stage / follow.STATE).exists())


if __name__ == "__main__":
    unittest.main()
