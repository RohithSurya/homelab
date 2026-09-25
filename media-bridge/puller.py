"""
Pull worker for bridge.py: copies finished transfers from their backend to the Jetson in the
background, so a long pull (a 34 GB 4K file) never freezes the bridge loop -- new adds, holds,
stall checks and other transfers keep being handled while it runs.

Ownership, to keep the two threads from racing:
  - the bridge thread submits jobs, reads their status, and alone writes state JSON, delivers
    (the atomic rename into the watch folder) and releases/holds;
  - this worker only writes inside its job's staging dir (downloads/.incoming/<cat>/<release>)
    and deletes that dir itself when a job is cancelled. The bridge's cleanup skips busy dirs.
One job runs at a time (the home downlink is the shared resource); a job whose size wouldn't fit
the Jetson's disk (PULL_MIN_FREE_GB left over) waits instead of starting.
"""

import os
import queue
import shutil
import threading
import time

from common import clog

PULL_MIN_FREE = float(os.environ.get("PULL_MIN_FREE_GB", "25")) * 1024**3
DATA_ROOT = os.environ.get("PULL_DATA_ROOT", "/data")


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, tid, be, rec, plan, stage, size):
        self.tid, self.be, self.rec, self.plan, self.stage, self.size = tid, be, rec, plan, stage, size
        self.cancel = threading.Event()
        self.status = "queued"  # queued | waiting_space | running | done | failed | cancelled
        self.error = None
        self.started = self.finished = None
        self.bytes = 0


_jobs = {}
_lock = threading.Lock()
_q = queue.Queue()


def log(msg):
    clog("bridge", msg)


def submit(be, rec, plan, stage, size=None):
    """Queue a pull (idempotent per transfer id). Returns the job."""
    with _lock:
        job = _jobs.get(rec["tid"])
        if job is None:
            job = Job(rec["tid"], be, dict(rec), plan, stage, size)
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


def _staged_bytes(path):
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def _run(job):
    # disk guard: the part still to be written must leave PULL_MIN_FREE on the Jetson
    need = max(0, (job.size or 0) - _staged_bytes(job.stage))
    while _free_bytes() - need < PULL_MIN_FREE:
        if job.status != "waiting_space":
            job.status = "waiting_space"
            log(f"    pull of '{job.rec.get('release_name')}' waits: {need / 2**30:.1f} GiB wouldn't leave "
                f"{PULL_MIN_FREE / 2**30:.0f} GiB free on the Jetson")
        if job.cancel.wait(60):
            raise Cancelled()
    job.status, job.started = "running", time.time()
    before = _staged_bytes(job.stage)
    job.be.pull(job.rec, job.plan, job.stage, job.cancel)
    if job.cancel.is_set():
        raise Cancelled()
    job.bytes = max(0, _staged_bytes(job.stage) - before)


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
    log("pull worker started")
    while True:
        _process(_q.get())


def run_pending():
    """Process every queued job synchronously (tests, one-off tools)."""
    while not _q.empty():
        _process(_q.get_nowait())
