"""
Pull worker for bridge.py: copies finished transfers from their backend to the Jetson in the
background, so a long pull (a 34 GB 4K file) never freezes the bridge loop -- new adds, holds,
stall checks and other transfers keep being handled while it runs.

Ownership, to keep the two threads from racing:
  - the bridge thread submits jobs, reads their status, and alone writes state JSON, delivers
    (the atomic rename into the watch folder) and releases/holds;
  - this worker only writes inside its job's staging dir (downloads/.incoming/<cat>/<release>)
    and deletes that dir itself when a job is cancelled. The bridge's cleanup skips busy dirs.
PULL_WORKERS jobs run at once (default 2), so a small movie or a follow step never waits behind a
30 GB pull. They share the seedbox's uplink, so the total speed doesn't change -- only who waits.
A job whose size wouldn't fit the Jetson's disk (PULL_MIN_FREE_GB left over, counting what the other
running pulls still have to write) waits instead of starting.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from common import clog

PULL_MIN_FREE = float(os.environ.get("PULL_MIN_FREE_GB", "25")) * 1024**3
DATA_ROOT = os.environ.get("PULL_DATA_ROOT", "/data")
WORKERS = max(1, int(os.environ.get("PULL_WORKERS", "2")))


class Cancelled(Exception):
    pass


# Scene releases (common on private trackers) arrive as RAR volumes: name.rar + name.r00..rNN, or
# name.part01.rar..partNN.rar. Radarr/Sonarr never unpack archives, so a RAR'd release sits in the
# watch folder forever with only its sample importable. Extract in the staging dir before delivery.
_VOLUME = re.compile(r"\.(rar|r\d\d|sfv)$", re.I)


def _first_volume(name):
    n = name.lower()
    m = re.search(r"\.part(\d+)\.rar$", n)
    return int(m.group(1)) == 1 if m else n.endswith(".rar")


def extract_archives(stage, unrar="unrar"):
    """Extract every RAR set under stage (not inside Sample/ dirs) next to its volumes, then remove
    the volumes. Extraction goes to a temp dir first and is moved in only when unrar succeeds, so a
    half-extracted file never appears. Returns how many sets were extracted; raises on failure."""
    sets = [
        p for p in sorted(stage.rglob("*.rar"))
        if p.is_file() and _first_volume(p.name)
        and not any(part.lower() == "sample" for part in p.relative_to(stage).parts[:-1])
    ]
    done = 0
    for first in sets:
        tmp = first.parent / ".extracting"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        r = subprocess.run([unrar, "x", "-o+", "-y", "-idq", str(first), str(tmp) + os.sep],
                           capture_output=True, text=True, timeout=6 * 3600)
        if r.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"unrar failed on {first.name} ({r.returncode}): {(r.stderr or r.stdout).strip()[-200:]}")
        for f in tmp.iterdir():
            os.replace(f, first.parent / f.name)
        tmp.rmdir()
        stem = re.sub(r"(\.part\d+)?\.rar$", "", first.name, flags=re.I)
        for v in first.parent.iterdir():
            if v.is_file() and v.name.startswith(stem) and (_VOLUME.search(v.name) or re.search(r"\.part\d+\.rar$", v.name, re.I)):
                v.unlink()
        done += 1
    return done


class Job:
    def __init__(self, tid, be, rec, plan, stage, size, kind="pull"):
        self.tid, self.be, self.rec, self.plan, self.stage, self.size = tid, be, rec, plan, stage, size
        self.kind = kind  # pull | follow (one step of pulling finished pieces while it downloads)
        self.result = None  # what be.pull returned (a follow step's counts)
        self.cancel = threading.Event()
        self.status = "queued"  # queued | waiting_space | running | done | failed | cancelled
        self.error = None
        self.started = self.finished = None
        self.bytes = 0
        self.before = 0  # staged bytes when this run started (a resumed pull)


_jobs = {}
_lock = threading.Lock()
_q = queue.Queue()
_space = threading.Lock()  # one disk-guard decision at a time, so two jobs can't both claim the same room


def log(msg):
    clog("bridge", msg)


def submit(be, rec, plan, stage, size=None, kind="pull"):
    """Queue a pull (idempotent per transfer id). Returns the job."""
    with _lock:
        job = _jobs.get(rec["tid"])
        if job is None:
            job = Job(rec["tid"], be, dict(rec), plan, stage, size, kind)
            _jobs[rec["tid"]] = job
            _q.put(job)
        return job


def get(tid):
    with _lock:
        return _jobs.get(tid)


def forget(tid):
    with _lock:
        _jobs.pop(tid, None)


def cancel(tid):
    """Ask a queued/running pull to stop; the worker removes its staging dir itself."""
    with _lock:
        job = _jobs.get(tid)
    if job is not None:
        job.cancel.set()
    return job is not None


def busy_paths():
    """Staging dirs the worker owns right now (bridge cleanup must not touch them)."""
    with _lock:
        return {str(j.stage) for j in _jobs.values() if j.status in ("queued", "waiting_space", "running")}


def _free_bytes():
    st = os.statvfs(DATA_ROOT)
    return st.f_bavail * st.f_frsize


def _map_bytes(mapf):
    try:
        return sum(s[2] - s[0] for s in json.loads(mapf.read_text())["segs"])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _staged_bytes(path):
    """Bytes actually pulled into a staging dir. A segmented pull preallocates <file>.part at full
    size (sparse), so its st_size would read ~100% from the first second: count a .part from its
    .part.map when it has one, else only the blocks really written."""
    total = 0
    try:
        for f in path.rglob("*"):
            if not f.is_file() or f.name.endswith((".part.map", ".part.map.tmp")):
                continue
            st = f.stat()
            if f.name.endswith(".part"):
                got = _map_bytes(f.with_name(f.name + ".map"))
                total += got if got is not None else min(st.st_size, st.st_blocks * 512)
            else:
                total += st.st_size
    except OSError:
        return total
    return total


def _still_needed(job):
    return max(0, (job.size or 0) - _staged_bytes(job.stage))


def _others_need(job):
    """Bytes the other running pulls still have to write (the disk will lose them too)."""
    with _lock:
        running = [j for j in _jobs.values() if j is not job and j.status == "running"]
    return sum(_still_needed(j) for j in running)


def _run(job):
    # disk guard: the part still to be written -- plus what the other running pulls still have to
    # write -- must leave PULL_MIN_FREE on the Jetson
    while True:
        with _space:
            need = _still_needed(job)
            others = _others_need(job)
            if _free_bytes() - need - others >= PULL_MIN_FREE:
                job.before = _staged_bytes(job.stage)  # already there from an interrupted pull
                job.status, job.started = "running", time.time()
                break
        if job.status != "waiting_space":
            job.status = "waiting_space"
            busy = f" (+{others / 2**30:.1f} GiB for running pulls)" if others else ""
            log(f"    pull of '{job.rec.get('release_name')}' waits: {need / 2**30:.1f} GiB{busy} wouldn't leave "
                f"{PULL_MIN_FREE / 2**30:.0f} GiB free on the Jetson")
        if job.cancel.wait(60):
            raise Cancelled()
    job.result = job.be.pull(job.rec, job.plan, job.stage, job.cancel)
    if job.cancel.is_set():
        raise Cancelled()
    job.bytes = max(0, _staged_bytes(job.stage) - job.before)  # pulled by this run, before unrar
    if job.kind == "pull" and job.stage.exists():  # a follow step's files are still partial
        n = extract_archives(job.stage)
        if n:
            log(f"    extracted {n} RAR set(s) in '{job.rec.get('release_name')}'")


def _process(job):
    if job.cancel.is_set():
        job.status = "cancelled"
        shutil.rmtree(job.stage, ignore_errors=True)
        return
    try:
        _run(job)
        job.status = "done"
    except Cancelled:
        job.status = "cancelled"
        shutil.rmtree(job.stage, ignore_errors=True)
        log(f"    pull of '{job.rec.get('release_name')}' cancelled; staging removed")
    except Exception as e:
        job.status, job.error = "failed", str(e) or repr(e)
    finally:
        job.finished = time.time()


def run_worker():
    log(f"pull worker started ({threading.current_thread().name}, {WORKERS} at a time)")
    while True:
        _process(_q.get())


def run_pending():
    """Process every queued job synchronously (tests, one-off tools)."""
    while not _q.empty():
        _process(_q.get_nowait())
