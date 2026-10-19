"""
Follow mode for the seedbox backend: pull a torrent's finished pieces while qBittorrent is still
downloading it, so the copy home overlaps the download instead of starting after it.

Measured 2026-09-26 (plan file): on public ISOs + a 35-file set the local copy was complete 8-29 s
after the box finished (vs ~2 min for a 4 GB pull started at completion), byte-exact every time.

How it knows what to copy: qBittorrent reports which pieces are finished (/torrents/pieceStates),
and the torrent itself (/torrents/export) gives the piece size, every piece's SHA-1 and the exact
byte layout of its files, BEP 47 pad files included -- qBittorrent's own file list hides pad files,
so offsets built from it would be wrong for padded torrents.

Each step reads finished-but-unverified pieces as byte ranges over the read-only SFTP key (runs of
contiguous pieces, so 256 KiB pieces don't mean 256 KiB requests), checks every piece's SHA-1 in
memory, and writes only verified pieces into sparse <file>.part files in the staging dir.
libtorrent can report a piece finished before its bytes are on the box's disk (seen in testing):
such a piece fails its hash and is simply read again next step. Nothing unverified is ever written.

State, in the staging dir: .follow.json (layout + verified-piece bitmap; rewritten atomically, and
only after the data it claims is fsynced) and .follow.hashes (the piece hashes, written once).

Never lftp `get1 --target-position` for this: in testing it corrupted ranges it had written earlier.
"""

import base64
import hashlib
import json
import os
import threading
import zlib

STATE = ".follow.json"
HASHES = ".follow.hashes"
RUN_MAX = 64 * 1024**2  # contiguous pieces read as one range, per stream (bounds memory)


def layout(info, save_path, skip_names=()):
    """Decoded info dict -> {"piece_len", "total", "files": [...]} in torrent order, pad files
    included (never written; they only make the piece math line up). rel = the local path under
    the staging dir, with the torrent's root folder stripped the way prepare_fetch strips it;
    skip = deselected in qBittorrent (read for hashing, never written)."""
    name = info[b"name"].decode("utf-8", "replace")
    if b"length" in info:
        raw = [([name], info[b"length"], False)]
    else:
        raw = [([name] + [p.decode("utf-8", "replace") for p in f[b"path"]], f[b"length"],
                b"p" in f.get(b"attr", b"")) for f in info[b"files"]]
    real = [parts for parts, _, pad in raw if not pad]
    strip = 1 if real and all(len(p) > 1 for p in real) and len({p[0] for p in real}) == 1 else 0
    files, off = [], 0
    for parts, ln, pad in raw:
        joined = "/".join(parts)
        files.append({"rel": parts[strip:], "remote": save_path.rstrip("/") + "/" + joined, "off": off,
                      "len": ln, "pad": pad, "skip": joined in skip_names})
        off += ln
    return {"piece_len": info[b"piece length"], "total": off, "files": files}


def writable(f):
    return not f["pad"] and not f["skip"] and f["len"] > 0


def part_path(stage, f):
    p = stage.joinpath(*f["rel"])
    return p.with_name(p.name + ".part")


def _bits(b):
    return base64.b64encode(zlib.compress(bytes(b))).decode()


def _unbits(s, n):
    b = bytearray(zlib.decompress(base64.b64decode(s)))
    if len(b) != n:
        raise ValueError("bitmap size")
    return b


def load(stage):
    """The follow state of a staging dir, or None (absent, torn, or inconsistent)."""
    try:
        st = json.loads((stage / STATE).read_text())
        hashes = (stage / HASHES).read_bytes()
        n = len(hashes) // 20
        if n * 20 != len(hashes) or n != -(-st["total"] // st["piece_len"]):
            return None
        st["verified"] = _unbits(st["verified"], n)
        st["hashes"] = hashes
        return st
    except (OSError, ValueError, KeyError, TypeError, zlib.error):
        return None


def save(stage, st):
    tmp = stage / (STATE + ".tmp")
    tmp.write_text(json.dumps({k: (_bits(v) if k == "verified" else v) for k, v in st.items() if k != "hashes"}))
    os.replace(tmp, stage / STATE)


def init(stage, torrent_hash, lay, hashes):
    """Fresh follow state: sparse .part files for everything that will be delivered."""
    stage.mkdir(parents=True, exist_ok=True)
    for f in lay["files"]:
        if writable(f):
            p = part_path(stage, f)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as fh:
                fh.truncate(f["len"])
    (stage / HASHES).write_bytes(hashes)
    st = dict(lay, hash=torrent_hash, verified=bytearray(len(hashes) // 20), hashes=hashes)
    save(stage, st)
    return st


def discard(stage, st=None):
    """Remove follow state and the .part files it created (another torrent's leftovers)."""
    for f in (st or {}).get("files", []):
        if writable(f):
            part_path(stage, f).unlink(missing_ok=True)
    for n in (STATE, STATE + ".tmp", HASHES):
        (stage / n).unlink(missing_ok=True)


def needed(st):
    """Pieces that overlap a file we deliver. A piece wholly inside a deselected or pad file is
    never downloaded by qBittorrent, so it must not hold up completion."""
    pl, want = st["piece_len"], set()
    for f in st["files"]:
        if writable(f):
            want.update(range(f["off"] // pl, (f["off"] + f["len"] - 1) // pl + 1))
    return want


def missing(st):
    v = st["verified"]
    return sorted(i for i in needed(st) if not v[i])


def done_bytes(st):
    pl, total = st["piece_len"], st["total"]
    n = sum(st["verified"])
    if n and st["verified"][-1]:  # the last piece is usually short
        n -= 1
        return n * pl + (total - (len(st["verified"]) - 1) * pl)
    return n * pl


def runs(pieces, piece_len, cap=RUN_MAX):
    """Sorted piece indexes -> [(first, last)] of contiguous pieces, each at most `cap` bytes."""
    per = max(1, cap // piece_len)
    out = []
    for i in sorted(pieces):
        if out and i == out[-1][1] + 1 and i - out[-1][0] < per:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [tuple(r) for r in out]


def segments(files, a, b):
    """Torrent byte range [a, b) -> [(file, offset in file, length)] in order."""
    out = []
    for f in files:
        lo, hi = max(a, f["off"]), min(b, f["off"] + f["len"])
        if lo < hi:
            out.append((f, lo - f["off"], hi - lo))
    return out


def fetch(st, stage, pieces, sftps, open_remote, prefetch, halt):
    """Pull + verify + write `pieces`, one run per stream at a time. Marks st["verified"]; the
    caller saves the state (data is fsynced here first). Returns (verified, bad, bytes read).
    The first failing stream stops the others and its error is raised."""
    pl, total, files, hashes = st["piece_len"], st["total"], st["files"], st["hashes"]
    pending = runs(pieces, pl)
    lock = threading.Lock()
    errors, stop = [], threading.Event()
    fds = {}
    stats = {"ok": 0, "bad": 0, "bytes": 0}

    def aborted():
        return stop.is_set() or (halt is not None and halt.is_set())

    def fd(f):
        k = tuple(f["rel"])
        with lock:
            if k not in fds:
                fds[k] = os.open(part_path(stage, f), os.O_RDWR)
            return fds[k]

    def read_range(sftp, f, lo, hi):
        buf = bytearray()
        with open_remote(sftp, f["remote"]) as rf:
            size = getattr(rf, "MAX_REQUEST_SIZE", 32768)
            rf.seek(lo)
            rf.prefetch(hi, max_concurrent_requests=prefetch)  # arg = END offset
            while len(buf) < hi - lo:
                if aborted():
                    raise InterruptedError("aborted")
                d = rf.read(min(size, hi - lo - len(buf)))
                if not d:
                    raise RuntimeError(f"short read in {f['remote']} at {lo + len(buf):,}")
                buf += d
        return buf

    def do_run(sftp, first, last):
        a, b = first * pl, min(total, (last + 1) * pl)
        buf = bytearray()
        for f, fo, ln in segments(files, a, b):
            buf += bytes(ln) if f["pad"] else read_range(sftp, f, fo, fo + ln)
        ok = bad = 0
        for i in range(first, last + 1):
            pa, pb = i * pl, min(total, (i + 1) * pl)
            piece = memoryview(buf)[pa - a:pb - a]
            if hashlib.sha1(piece).digest() != hashes[i * 20:(i + 1) * 20]:
                bad += 1  # not on the box's disk yet (or a bad read): next step reads it again
                continue
            pos = pa - a
            for f, fo, ln in segments(files, pa, pb):
                if writable(f):
                    os.pwrite(fd(f), memoryview(buf)[pos:pos + ln], fo)
                pos += ln
            ok += 1
            with lock:
                st["verified"][i] = 1
        with lock:
            stats["ok"] += ok
            stats["bad"] += bad
            stats["bytes"] += len(buf)

    def worker(sftp):
        try:
            while not aborted():
                with lock:
                    if not pending:
                        return
                    r = pending.pop(0)
                do_run(sftp, *r)
        except InterruptedError:
            pass
        except Exception as e:
            errors.append(e)
            stop.set()

    threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in sftps[:max(1, len(pending))]]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for f in list(fds.values()):
            try:
                os.fsync(f)  # before the caller saves a bitmap that claims these pieces
            finally:
                os.close(f)
    if errors:
        # pieces written before the error are fsynced and stay verified; the error still stops the step
        raise errors[0]
    return stats["ok"], stats["bad"], stats["bytes"]


def finalize(stage, st):
    """Every piece verified: .part -> final names, state removed."""
    if missing(st):
        raise RuntimeError(f"{len(missing(st))} pieces still unverified")
    for f in st["files"]:
        if writable(f):
            p = part_path(stage, f)
            os.replace(p, p.with_name(p.name[:-len(".part")]))
        elif not f["pad"] and not f["skip"]:  # empty file: nothing to pull, still delivered
            p = stage.joinpath(*f["rel"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
    discard(stage)
