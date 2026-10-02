"""
Transfer event log for bridge.py: where each download is, and what happened to it.

One SQLite file, config/bridge.db (WAL). The JSON state files stay the engine's source of truth;
this is an append-only history plus the per-title attempt counter. Every write is wrapped so a
database problem can never break a transfer: errors are logged (rate-limited) and swallowed.

  events    id, ts, tid, type, phase, release, category, backend, detail(JSON)
  attempts  key ('radarr:movie:<id>' / 'sonarr:episode:<id>'), count, last_release,
            last_reason, updated, gave_up
  meta      schema bookkeeping

CLI (inside the container):
  python -m bridgelog               active transfers + drops waiting, with where each one is now
  python -m bridgelog <text>        timeline of every transfer whose release name contains <text>
  python -m bridgelog --attempts    attempt counters (and give-ups)
"""

import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
import uuid

from common import clog

DB_PATH = os.environ.get("BRIDGE_DB", "/config/bridge.db")
SCHEMA_VERSION = 1
_lock = threading.Lock()
_conn = None
_last_error_log = 0.0


def log(msg):
    clog("bridge", msg)


def _db():
    global _conn
    if _conn is None:
        c = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=3000")
        if c.execute("PRAGMA user_version").fetchone()[0] < 1:
            # additive migrations only: this history can't be rebuilt from anything else
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL, tid TEXT, type TEXT NOT NULL,
                    phase TEXT, release TEXT, category TEXT, backend TEXT, detail TEXT);
                CREATE INDEX IF NOT EXISTS events_tid ON events(tid, ts);
                CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS events_release ON events(release);
                CREATE TABLE IF NOT EXISTS attempts(
                    key TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0, last_release TEXT,
                    last_reason TEXT, updated REAL, gave_up REAL);
                CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
                PRAGMA user_version=1;
                """
            )
        _conn = c
    return _conn


def _swallow(what, e):
    global _last_error_log
    if time.time() - _last_error_log > 60:
        _last_error_log = time.time()
        log(f"    (event log) {what} failed, continuing without it: {e!r}")


def ensure_tid(rec):
    """Every transfer gets a random id on first sight (legacy state files included). Returns
    True if it was just added (the caller persists the rec)."""
    if rec.get("tid"):
        return False
    rec["tid"] = uuid.uuid4().hex[:16]
    return True


def emit(rec, type_, phase=None, **detail):
    """Append one event for a transfer (rec = its state dict) or a waiting drop
    (rec = {'tid': 'drop:<file>', 'release_name': ..., 'category': ...}). Never raises."""
    try:
        with _lock:
            _db().execute(
                "INSERT INTO events(ts, tid, type, phase, release, category, backend, detail) VALUES (?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    rec.get("tid"),
                    type_,
                    phase,
                    rec.get("release_name") or rec.get("title"),
                    rec.get("category"),
                    rec.get("backend"),
                    json.dumps(detail, default=str) if detail else None,
                ),
            )
    except Exception as e:
        _swallow(f"event {type_}", e)


def drop_ref(cat, mfile):
    return {"tid": f"drop:{cat}:{mfile.name}", "release_name": mfile.stem, "category": cat}


def attempt_failed(keys, release, reason):
    """Count one failed attempt against every key (a season pack touches several episodes).
    Returns the highest count after this failure; 0 if the log is unavailable."""
    if not keys:
        return 0
    try:
        with _lock:
            db = _db()
            now = time.time()
            top = 0
            for k in keys:
                db.execute(
                    "INSERT INTO attempts(key, count, last_release, last_reason, updated) VALUES (?,1,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET count=count+1, last_release=excluded.last_release, "
                    "last_reason=excluded.last_reason, updated=excluded.updated",
                    (k, release, reason, now),
                )
                top = max(top, db.execute("SELECT count FROM attempts WHERE key=?", (k,)).fetchone()[0])
            return top
    except Exception as e:
        _swallow("attempt count", e)
        return 0


def attempt_count(keys):
    """Failed attempts so far for these keys (max over them); 0 if none or unavailable."""
    if not keys:
        return 0
    try:
        with _lock:
            q = "SELECT MAX(count) FROM attempts WHERE key IN (%s)" % ",".join("?" * len(keys))
            return _db().execute(q, list(keys)).fetchone()[0] or 0
    except Exception as e:
        _swallow("attempt lookup", e)
        return 0


def mark_gave_up(keys):
    try:
        with _lock:
            for k in keys:
                _db().execute("UPDATE attempts SET gave_up=? WHERE key=?", (time.time(), k))
    except Exception as e:
        _swallow("give-up mark", e)


def given_up(keys):
    """True if any key was given up on (later grabs for it are dropped silently)."""
    if not keys:
        return False
    try:
        with _lock:
            q = "SELECT 1 FROM attempts WHERE gave_up IS NOT NULL AND key IN (%s) LIMIT 1" % ",".join("?" * len(keys))
            return _db().execute(q, list(keys)).fetchone() is not None
    except Exception as e:
        _swallow("give-up check", e)
        return False


# --- read side (CLI) -----------------------------------------------------------------------------


def _fmt_ts(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _ago(ts):
    d = time.time() - ts
    if d < 0:
        return f"in {_dur(-d)}"
    return f"{_dur(d)} ago"


def _dur(s):
    s = int(s)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def where_now(rec, stage_dir=None):
    """Human description of a state record's current phase."""
    if rec.get("held"):
        rule = rec.get("seed_rule") or {}
        if rec.get("no_deliver"):
            return f"kept seeding undelivered ({rec.get('partial_reason', '')}), cap {_ago(rec.get('hold_max_until', 0))}"
        if rec.get("hold_until"):
            return f"seeding hold, ends {_fmt_ts(rec['hold_until'])} ({_ago(rec['hold_until'])})"
        return f"seeding hold until {rule.get('key', 'rule')}: {rule.get('hours', '?')}h" + (
            f" or ratio {rule['ratio']}" if rule.get("ratio") else "")
    if rec.get("superseded"):
        why = "stalled" if rec.get("stalled") else "missing result" if rec.get("missing_result") else \
            "unfetchable" if rec.get("unfetchable") else f"superseded by {rec.get('new_title')}"
        return f"cleaning up ({why})"
    if rec.get("unavailable_since"):
        return f"not visible on its backend for {_dur(time.time() - rec['unavailable_since'])} (waiting)"
    if rec.get("pulling"):
        got = ""
        if stage_dir is not None and stage_dir.exists():
            n = sum(f.stat().st_size for f in stage_dir.rglob("*") if f.is_file())
            got = f" {n / 2**30:.2f} GiB" + (f" of {rec['size'] / 2**30:.2f} GiB" if rec.get("size") else "")
        return f"pulling to the Jetson{got}"
    if rec.get("fetch_failing_since"):
        return f"fetch retrying since {_fmt_ts(rec['fetch_failing_since'])}"
    if rec.get("result_missing_since"):
        return f"finished, result not visible since {_fmt_ts(rec['result_missing_since'])}"
    if rec.get("last_progress") is not None:
        stale = time.time() - rec.get("last_progress_at", time.time())
        return f"downloading {rec['last_progress']}% on {rec.get('backend', 'seedr')}" + (
            f" (no progress for {_dur(stale)})" if stale > 600 else "")
    return f"queued on {rec.get('backend', 'seedr')}"


def _active(data="/data"):
    state = pathlib.Path(data) / "blackhole" / ".bridge-state"
    incoming = pathlib.Path(data) / "downloads" / ".incoming"
    rows = []
    for sp in sorted(state.glob("*.json")):
        if sp.name.startswith("."):  # .import-watch.json is not a transfer
            continue
        try:
            r = json.loads(sp.read_text())
        except (OSError, ValueError):
            continue
        rel = r.get("release_name") or r["title"]
        rows.append((r.get("added", 0), rel, r.get("category"), where_now(r, incoming / r["category"] / rel)))
    print(f"{len(rows)} transfer(s):")
    for added, rel, cat, where in sorted(rows):
        print(f"  [{cat}] {rel}\n      {where}   (added {_ago(added)})")
    drops = []
    for cat in ("radarr", "sonarr"):
        for f in sorted((pathlib.Path(data) / "blackhole" / cat).glob("*")):
            if f.suffix in (".magnet", ".torrent", ".txt"):
                drops.append((cat, f))
    if drops:
        print(f"{len(drops)} drop(s) waiting in the blackhole:")
        for cat, f in drops:
            last = None
            try:
                last = _db().execute(
                    "SELECT type, detail, ts FROM events WHERE tid=? ORDER BY ts DESC LIMIT 1", (f"drop:{cat}:{f.name}",)
                ).fetchone()
            except Exception:
                pass
            why = f"{last[0]} {last[1] or ''} ({_ago(last[2])})" if last else "not picked up yet"
            print(f"  [{cat}] {f.stem}\n      {why}")


def _timeline(text):
    rows = _db().execute(
        "SELECT ts, tid, type, phase, release, backend, detail FROM events WHERE release LIKE ? ORDER BY ts",
        (f"%{text}%",),
    ).fetchall()
    if not rows:
        print(f"no events for releases containing {text!r}")
        return
    cur = None
    for ts, tid, type_, phase, release, backend, detail in rows:
        if tid != cur:
            cur = tid
            print(f"\n{release}  [{backend or '-'}]  ({tid})")
        extra = ""
        if detail:
            d = json.loads(detail)
            extra = "  " + ", ".join(f"{k}={v}" for k, v in d.items())
        print(f"  {_fmt_ts(ts)}  {type_}{' -> ' + phase if phase else ''}{extra}")


def _attempts():
    rows = _db().execute(
        "SELECT key, count, last_release, last_reason, updated, gave_up FROM attempts ORDER BY updated DESC LIMIT 50"
    ).fetchall()
    for key, count, rel, reason, upd, gave in rows:
        print(f"  {key:28} {count:2d} attempt(s), last: {reason} ({_ago(upd)}) {rel}" + ("  GAVE UP" if gave else ""))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _active()
    elif args[0] == "--attempts":
        _attempts()
    else:
        _timeline(" ".join(args))
