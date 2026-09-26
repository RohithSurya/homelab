#!/usr/bin/env python3
"""
Blackhole bridge: Sonarr/Radarr's download client, with pluggable download backends.

Flow (Torrent Blackhole pattern -- the *arr apps see only these two folders):
  1. Sonarr/Radarr write <release>.magnet / .torrent into /data/blackhole/{sonarr,radarr}/
  2. We route the drop to a backend (BRIDGE_ROUTE) and start the transfer there
  3. We poll the backend until the transfer finishes
  4. We fetch the finished files into /data/downloads/{sonarr,radarr}/<release name>/
     (the *arr "watch" folder), named after the release the *arr GRABBED
  5. We drop the result from the backend -- or, for a private-tracker torrent, keep it
     seeding until its hold is over (hit-and-run protection), then drop it

Everything backend-independent lives here: the blackhole scan, per-transfer state files,
routing, supersede, stall / missing-result / unfetchable deadlines, failing a grab back to
Radarr/Sonarr, and the private hold. A backend (backends/*.py, contract in backends/base.py)
only runs torrents and hands their bytes back.

BRIDGE_ROUTE picks the backend per drop, first match wins: comma-separated `<match>:<backend>`
with match = private | public | * (e.g. `private:seedbox,public:seedr`). Default `*:seedr`.

SEED_RULES says how long each private tracker needs a torrent seeded (hit-and-run rules):
comma-separated `<key>=<rule>`, rule = `<hours>h` (seed time required) or `<ratio>|<hours>h`
(whichever comes first). A key matches the grab's indexer name as Radarr/Sonarr recorded it
(normalized, substring), else a tracker host; `private-default` covers any private torrent that
matches nothing and should be the strictest. A drop whose rule is longer than its backend can
seed (Seedr: 48h) is never sent there -- it waits in the blackhole and ntfys once.
"""

import base64
import json
import os
import pathlib
import re
import shutil
import time

import requests

import backends
import bridgelog as ev
import puller
import trackers
from backends.base import DONE, DOWNLOADING, UNAVAILABLE, Exists, Full, Rejected
from common import clog, name_matches, norm, ntfy
from torrentfile import (
    magnet_btih,
    magnet_from_torrent,
    magnet_meta,
    torrent_is_private,
    torrent_meta,
)

FAIL_COUNTS = {}  # blackhole filename -> consecutive "backend full" attempts (in-memory)
INDEXER_TRIES = {}  # blackhole filename -> history lookups that found no grab yet (in-memory)
INDEXER_MAX_TRIES = 5  # the *arr writes the drop before its history row; retry a few polls
DROP_STATE = {}  # blackhole filename -> last waiting reason logged as an event (in-memory)
BACKEND_UP = {}  # backend name -> last known reachability (events only on change)
WARNED = set()  # blackhole filenames already ntfy'd as un-routable (in-memory)
INDEXERS = {}  # blackhole filename -> indexer it was grabbed from (in-memory)
TOMBSTONE_GRACE = (
    600  # secs to keep reaping a superseded release's late-appearing backend result
)
POLL = int(os.environ.get("POLL_INTERVAL", "30"))
DATA = pathlib.Path("/data")
STATE_DIR = DATA / "blackhole" / ".bridge-state"
# Fetches land here first and are renamed into downloads/<cat>/ only once complete: the *arr's
# blackhole scan imports any watch-folder item that stops changing for ~30s, so fetching into
# the watch folder itself let a slow or retrying fetch be imported half-done. It sits beside
# the category folders (never inside one), on the same filesystem, so the final rename is atomic.
INCOMING = DATA / "downloads" / ".incoming"
CATEGORIES = ["sonarr", "radarr"]
ROUTE_SPEC = os.environ.get("BRIDGE_ROUTE", "*:seedr")
DEFAULT_BACKEND = "seedr"  # state files written before backends existed carry no "backend" key

# Stalled-download handling: if a transfer's progress hasn't advanced for this long,
# fail it in Radarr/Sonarr (-> blocklist + auto re-grab a different release) and notify.
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "2700"))  # 45 min
# Fetch-failure handling: a transfer the backend reports complete but whose bytes it won't
# serve (Seedr: a persistent 404 from a storage node) would otherwise retry-fetch forever,
# hogging backend space and never failing in the *arr. After this long failing to fetch, give
# up: purge it, fail it in Radarr/Sonarr (-> blocklist + re-grab a different release) and
# notify. The post-completion analogue of STALL_TIMEOUT.
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "5400"))  # 90 min
# Missing-result handling: a transfer that finished but whose result never shows up (Seedr
# deduped or dropped it -- seen when a single-episode torrent finished alongside a pack
# holding the same file) would otherwise log "result not visible yet" every poll forever.
# After this long, tombstone it and fail it in the *arr.
RESULT_TIMEOUT = int(os.environ.get("RESULT_TIMEOUT", "1800"))  # 30 min
# A backend unobserved for longer than this (bridge down, backend unreachable) gets every
# deadline pushed back by the gap, so an outage can't make every transfer look stalled at once.
CLOCK_GAP = int(os.environ.get("CLOCK_GAP", str(3 * POLL)))
# Circuit breaker: this many automatic failures (stall/missing/unfetchable) on one backend within
# the window means the backend -- not the releases -- is broken: stop failing for BREAKER_PAUSE.
BREAKER_FAILS = int(os.environ.get("BREAKER_FAILS", "3"))
BREAKER_WINDOW = int(os.environ.get("BREAKER_WINDOW", "900"))
BREAKER_PAUSE = int(os.environ.get("BREAKER_PAUSE", "3600"))
FAILURES = {}  # backend name -> [epoch of recent automatic failures] (in-memory)
BREAKER_UNTIL = {}  # backend name -> epoch until which automatic failing is paused
# A private torrent that has downloaded more than this is never cancelled (stall, supersede):
# it is failed in the *arr but kept as an undelivered seeding hold, for at most PARTIAL_HOLD.
PARTIAL_MIN_BYTES = int(os.environ.get("PARTIAL_MIN_BYTES", str(50 * 1024 * 1024)))
PARTIAL_HOLD = int(os.environ.get("PARTIAL_HOLD", str(7 * 86400)))
# Slow rule (user decision 2026-09-25): fail + re-grab a download still under SLOW_MIN_PROGRESS% after
# SLOW_MIN_AGE, or whose projected time left (the bridge's own rate over ~1h, not qBittorrent's
# jittery eta) stays above SLOW_MAX_ETA_H for SLOW_CONFIRM. Scope public|all|off: private swarms are
# thin by nature, and a private partial can't be cancelled anyway. Dry-run by default: log only.
SLOW_RULE = os.environ.get("SLOW_RULE", "public").lower()
SLOW_DRY_RUN = os.environ.get("SLOW_RULE_DRY_RUN", "true").lower() != "false"
SLOW_MIN_AGE = int(os.environ.get("SLOW_MIN_AGE", "7200"))
SLOW_MIN_PROGRESS = float(os.environ.get("SLOW_MIN_PROGRESS", "5"))
SLOW_MAX_ETA_H = float(os.environ.get("SLOW_MAX_ETA_H", "24"))
SLOW_CONFIRM = int(os.environ.get("SLOW_CONFIRM", "3600"))
RATE_WINDOW = 3600
# Fail-fast (public only): a download that after FAILFAST_AFTER still has no metadata, or whose
# swarm reports 0 seeds, with nothing downloaded -> fail + re-grab now instead of waiting for the
# 45-min stall rule (movierulz/tamilmv report fake seeder counts, so dead picks are common).
# Seedr exposes no swarm data: there it's "still 0%". Dry-run by default: log only.
FAILFAST = os.environ.get("FAILFAST", "public").lower()  # public|off
FAILFAST_DRY_RUN = os.environ.get("FAILFAST_DRY_RUN", "true").lower() != "false"
FAILFAST_AFTER = int(os.environ.get("FAILFAST_AFTER", "600"))
# Give up on a movie/episode after this many failed attempts (user decision: 10): unmonitor it in the
# *arr FIRST (marking failed always triggers a re-search), then fail the grab, and silently drop any
# later grab of it. Dry-run by default: log only.
GIVE_UP_AFTER = int(os.environ.get("GIVE_UP_AFTER", "10"))
GIVE_UP_DRY_RUN = os.environ.get("GIVE_UP_DRY_RUN", "true").lower() != "false"
# Import fix: the bridge's own deliveries that the *arr blocks only because the release name doesn't
# match the series/movie (e.g. "Rewired with Joe Dispenza" vs Sonarr's "Rewired") are manual-imported
# with the movie/episode ids the *arr itself recorded at grab time -- never guessed from names.
IMPORT_FIX = os.environ.get("IMPORT_FIX", "on").lower() not in ("off", "false", "0")
IMPORT_CHECK_INTERVAL = int(os.environ.get("IMPORT_CHECK_INTERVAL", "120"))
IMPORT_WATCH_TTL = int(os.environ.get("IMPORT_WATCH_TTL", str(24 * 3600)))
IMPORT_BLOCK_RE = re.compile(r"title mismatch|unknown (series|movie)", re.I)
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv")
_import_checked_at = 0.0
# A transfer its backend can't see (UNAVAILABLE) for this long is ntfy'd once.
UNAVAILABLE_ALERT = int(os.environ.get("UNAVAILABLE_ALERT", "7200"))
ARR = {
    "radarr": (os.environ.get("RADARR_URL"), os.environ.get("RADARR_API_KEY")),
    "sonarr": (os.environ.get("SONARR_URL"), os.environ.get("SONARR_API_KEY")),
}


def log(msg):
    clog("bridge", msg)


def movie_key(release_name):
    """Normalized 'title + year' identity for a Radarr release, or None if undecidable.
    'Real Steel 2011 1080p PROPER BluRay...'  -> 'realsteel2011'
    'Real Steel (2011) 1080p BRRip x264 -YTS' -> 'realsteel2011'
    Used to recognise that a new release supersedes an old one of the SAME movie."""
    m = re.search(r"\b(19|20)\d{2}\b", release_name)
    if m:
        return norm(release_name[: m.end()])
    q = re.search(
        r"\b(2160p|1080p|720p|480p|bluray|brrip|bdrip|webrip|web|hdtv|x264|x265|hevc)\b",
        release_name,
        re.I,
    )
    return (
        norm(release_name[: q.start()]) if q else None
    )  # None -> skip supersede (too risky)


def parse_routes(spec):
    routes = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        match, _, name = part.partition(":")
        match, name = match.strip().lower(), name.strip().lower()
        if match not in ("private", "public", "*") or name not in backends.REGISTRY:
            log(f"ignoring bad BRIDGE_ROUTE entry '{part}' (backends: {', '.join(backends.REGISTRY)})")
            continue
        routes.append((match, name))
    return routes


ROUTES = parse_routes(ROUTE_SPEC)


def parse_seed_rules(spec):
    """'iptorrents=1.0|336h, retrotoon=72h' -> {'iptorrents': {'hours': 336.0, 'ratio': 1.0}, ...}"""
    rules = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, rule = part.partition("=")
        ratio, _, hours = rule.strip().rpartition("|")
        try:
            if not hours.lower().endswith("h"):
                raise ValueError("hours must end in h")
            r = {"hours": float(hours[:-1]), "ratio": float(ratio) if ratio else None}
        except ValueError as e:
            log(f"ignoring bad SEED_RULES entry '{part}' ({e})")
            continue
        rules[key.strip().lower()] = r
    return rules


SEED_RULES = parse_seed_rules(os.environ.get("SEED_RULES", ""))
PRIVATE_DEFAULT = {"hours": 336.0, "ratio": None}  # when SEED_RULES has no private-default


def seed_rule(indexer, hosts):
    """The seeding rule for a private torrent: by indexer name, then tracker host, then
    private-default (strictest). Returns (rule dict incl. 'key', how it matched)."""
    ni = norm(indexer or "")
    for key, rule in SEED_RULES.items():
        if key != "private-default" and ni and norm(key) in ni:
            return dict(rule, key=key), f"indexer '{indexer}'"
    for key, rule in SEED_RULES.items():
        if key != "private-default" and any(norm(key) in norm(h) for h in hosts):
            return dict(rule, key=key), "tracker host"
    return dict(SEED_RULES.get("private-default", PRIVATE_DEFAULT), key="private-default"), "default"


def tracker_rule_key(indexer):
    """The SEED_RULES key an indexer name matches (a known private tracker), or None."""
    ni = norm(indexer or "")
    return next((k for k in SEED_RULES if k != "private-default" and ni and norm(k) in ni), None)


def grab_info(cat, release_name):
    """What Radarr/Sonarr recorded when it grabbed release_name: {'indexer', 'history_ids',
    'keys'}, or None if no 'grabbed' history row matches (yet). A season pack writes one row per
    episode at the same moment -> every episode is a key. Keys ('radarr:movie:<id>',
    'sonarr:episode:<id>') link retries of the same movie/episode across different releases."""
    url, key = ARR.get(cat, (None, None))
    if not url or not key:
        return None
    try:
        h = requests.get(
            f"{url}/api/v3/history",
            params={"eventType": 1, "pageSize": 200, "sortKey": "date", "sortDirection": "descending"},
            headers={"X-Api-Key": key},
            timeout=30,
        )
        h.raise_for_status()
        nrel = norm(release_name)
        rows = [r for r in h.json().get("records", []) if norm(r.get("sourceTitle", "")) == nrel]
        if not rows:
            return None
        newest = rows[0]["date"][:16]  # same grab = same minute
        rows = [r for r in rows if r["date"][:16] == newest]
        if cat == "radarr":
            keys = sorted({f"radarr:movie:{r['movieId']}" for r in rows if r.get("movieId")})
        else:
            keys = sorted({f"sonarr:episode:{r['episodeId']}" for r in rows if r.get("episodeId")})
        return {
            "indexer": (rows[0].get("data") or {}).get("indexer"),
            "history_ids": [r["id"] for r in rows],
            "keys": keys,
        }
    except Exception as e:
        log(f"    (warn) {cat} history lookup failed: {e}")
    return None


def grab_indexer(cat, release_name):
    info = grab_info(cat, release_name)
    return info and info.get("indexer")


def route(private):
    for match, name in ROUTES:
        if match == "*" or match == ("private" if private else "public"):
            return name
    return None


def backend_for(rec):
    """The backend that owns a state record, or None if it can't be loaded (logged)."""
    name = rec.get("backend", DEFAULT_BACKEND)
    try:
        return backends.get(name)
    except Exception as e:
        log(f"backend '{name}' unavailable ({e!r}); skipping its transfer '{rec.get('title')}'")
        return None


def drop_wait(cat, mfile, reason, **detail):
    """A drop is waiting in the blackhole: log it as an event only when the reason changes."""
    if DROP_STATE.get(mfile.name) != reason:
        DROP_STATE[mfile.name] = reason
        ev.emit(ev.drop_ref(cat, mfile), "waiting", reason, **detail)


def save(sp, rec):
    sp.write_text(json.dumps(rec))


def set_phase(sp, rec, phase, **detail):
    """Record a phase change (and only a change) as an event."""
    if rec.get("phase") != phase:
        rec["phase"] = phase
        save(sp, rec)
        ev.emit(rec, "phase", phase, **detail)


def held(rec):
    return bool(rec.get("hold_until") or rec.get("held"))


def state_file(category, title):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = base64.urlsafe_b64encode(f"{category}|{title}".encode()).decode()
    return STATE_DIR / f"{safe}.json"


def load_records():
    records = []
    for sp in sorted(STATE_DIR.glob("*.json")):
        try:
            records.append((sp, json.loads(sp.read_text())))
        except (OSError, ValueError) as e:
            log(f"unreadable state file {sp.name}: {e}")
    return records


def cleanup_local_partial(cat, old_title, new_title=None, old_release=None):
    # remove a superseded release's partially-fetched download dir. The dir is named after
    # the *arr release (see check_completions) -- or, for dirs written before that, after the
    # backend's result name (often sanitized) -- so match either name fuzzily.
    busy = puller.busy_paths()
    for base in (DATA / "downloads" / cat, INCOMING / cat):
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir() or str(d) in busy:  # a running pull owns (and cleans) its own dir
                continue
            if new_title and name_matches(d.name, new_title):  # never the replacement's dir
                continue
            if name_matches(d.name, old_title) or (
                old_release and name_matches(d.name, old_release)
            ):
                shutil.rmtree(d, ignore_errors=True)
                log(f"    [override] removed local partial '{d.name}'")


def _tree(root):
    """{relative path: size} of every file under root."""
    return {
        str(f.relative_to(root)): f.stat().st_size for f in root.rglob("*") if f.is_file()
    }


def deliver(stage, dest):
    """Move a complete staged fetch into the *arr's watch folder in ONE rename, so the *arr
    never sees a partial. If dest already exists (a re-grab of the same release), keep it when
    it holds the same files, else swap it out whole -- never mix files into it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if _tree(dest) == _tree(stage):
            shutil.rmtree(stage)
            return "already delivered"
        old = INCOMING / ".replaced" / f"{dest.name}.{int(time.time())}"
        old.parent.mkdir(parents=True, exist_ok=True)
        os.rename(dest, old)
        os.rename(stage, dest)
        shutil.rmtree(old, ignore_errors=True)
        return "replaced an older copy"
    os.rename(stage, dest)
    return "delivered"


def may_fail(be, what):
    """Circuit breaker: False while automatic failing is paused on this backend."""
    now = time.time()
    if now < BREAKER_UNTIL.get(be.name, 0):
        return False
    recent = [t for t in FAILURES.get(be.name, []) if now - t < BREAKER_WINDOW]
    if len(recent) >= BREAKER_FAILS:
        BREAKER_UNTIL[be.name] = now + BREAKER_PAUSE
        FAILURES[be.name] = []
        ev.emit({"tid": f"backend:{be.name}", "backend": be.name}, "circuit_breaker", None,
                failures=len(recent), held_back=what)
        be.log(
            f"CIRCUIT BREAKER: {len(recent)} automatic failures on {be.label} within "
            f"{BREAKER_WINDOW // 60}min -> not failing anything there for {BREAKER_PAUSE // 60}min ({what} held back)"
        )
        notify(
            f"Bridge paused auto-failing on {be.label}",
            f"{len(recent)} transfers failed within {BREAKER_WINDOW // 60} minutes, which looks like "
            f"{be.label} itself is broken, not the releases. Nothing else is failed or blocklisted there "
            f"for {BREAKER_PAUSE // 60} minutes.",
        )
        return False
    FAILURES[be.name] = recent + [now]
    return True


def freeze_clocks(be, recs):
    """If this backend went unobserved for a while (bridge down, backend unreachable), push every
    running deadline back by the gap: time nobody was looking must not count as 'stalled'."""
    marker = STATE_DIR / f".seen-{be.name}"
    now = time.time()
    try:
        prev = float(marker.read_text())
    except (OSError, ValueError):
        prev = None
    try:
        marker.write_text(str(now))
    except OSError:
        pass
    if prev is None or now - prev <= CLOCK_GAP:
        return
    gap = now - prev - POLL
    shifted = 0
    for sp, rec in recs:
        changed = False
        # only clocks that were already running when the gap began: a transfer added during
        # the gap (e.g. the first one after days idle) has nothing to catch up on
        for k in ("last_progress_at", "result_missing_since", "fetch_failing_since", "unavailable_since"):
            if rec.get(k) and rec[k] <= prev:
                rec[k] += gap
                changed = True
        if rec.get("last_progress_at") is None and rec.get("added", now) <= prev and not held(rec):
            rec["added"] += gap
            changed = True
        if changed:
            sp.write_text(json.dumps(rec))
            shifted += 1
    be.log(f"{be.label} was unobserved for {gap / 60:.0f}min -> deadlines of {shifted} transfer(s) pushed back")


def arr_api(cat, method, path, params=None, body=None, timeout=60):
    """Plain-requests call to Radarr/Sonarr (never a backend session). Returns parsed JSON."""
    url, key = ARR.get(cat, (None, None))
    if not url or not key:
        raise RuntimeError(f"{cat} not configured")
    r = requests.request(method, f"{url}/api/v3{path}", params=params, json=body,
                         headers={"X-Api-Key": key}, timeout=timeout)
    r.raise_for_status()
    return r.json() if r.content else None


def _watch_file():
    return STATE_DIR / ".import-watch.json"


def watch_import(rec, dest):
    """Remember a delivery so check_imports can fix a title-mismatch block on it."""
    if not IMPORT_FIX or not rec.get("arr_keys"):
        return
    try:
        f = _watch_file()
        w = json.loads(f.read_text()) if f.exists() else {}
        w[rec["tid"]] = {"category": rec["category"], "release": rec.get("release_name") or rec["title"],
                         "dest": str(dest), "keys": rec["arr_keys"], "tid": rec["tid"], "at": time.time(), "tries": 0}
        f.write_text(json.dumps(w))
    except Exception as e:
        log(f"    (warn) import watch: {e}")


def _episode_map(keys):
    """{(season, episode): episode id} and the series id, for the *arr's recorded episode keys."""
    ids = [int(k.rsplit(":", 1)[1]) for k in keys if k.startswith("sonarr:episode:")]
    eps = arr_api("sonarr", "GET", "/episode", params={"episodeIds": ids}) if ids else []
    if isinstance(eps, dict):
        eps = [eps]
    return {(e["seasonNumber"], e["episodeNumber"]): e["id"] for e in eps}, (eps[0]["seriesId"] if eps else None)


def manual_import(cat, entry, download_id):
    """ManualImport the delivered folder, mapped ONLY to the movie/episodes recorded at grab time.
    Returns the number of files handed to the *arr."""
    items = arr_api(cat, "GET", "/manualimport", params={"folder": entry["dest"], "filterExistingFiles": "false"})
    vids = [i for i in items if i["path"].lower().endswith(VIDEO_EXT) and "sample" not in i["path"].lower()]
    if not vids:
        return 0
    files = []
    base = lambda i: {"path": i["path"], "folderName": pathlib.Path(entry["dest"]).name, "quality": i["quality"],
                      "languages": i.get("languages") or [], "releaseGroup": i.get("releaseGroup") or "",
                      "downloadId": download_id or i.get("downloadId"), "indexerFlags": i.get("indexerFlags") or 0}
    if cat == "radarr":
        movie = next((int(k.rsplit(":", 1)[1]) for k in entry["keys"] if k.startswith("radarr:movie:")), None)
        if movie is None:
            return 0
        best = max(vids, key=lambda i: i.get("size") or 0)  # one movie: the main file
        files.append(dict(base(best), movieId=movie))
    else:
        emap, series = _episode_map(entry["keys"])
        if not emap or series is None:
            return 0
        for i in vids:
            name = pathlib.Path(i["path"]).name
            m = re.search(r"s(\d{1,2})e(\d{1,3})(?:-?e(\d{1,3}))?", name, re.I)
            if m:
                s_, e1 = int(m.group(1)), int(m.group(2))
                e2 = int(m.group(3)) if m.group(3) else e1
                ids = [emap[(s_, e)] for e in range(e1, e2 + 1) if (s_, e) in emap]
            elif len(vids) == 1 and len(emap) == 1:
                ids = list(emap.values())
            else:
                ids = []
            if ids:
                files.append(dict(base(i), seriesId=series, episodeIds=ids, releaseType="singleEpisode" if len(ids) == 1 else "multiEpisode"))
            else:
                log(f"    import fix: can't map {name} to one of the grabbed episodes; left for manual import")
    if files:
        arr_api(cat, "POST", "/command", body={"name": "ManualImport", "files": files, "importMode": "auto"})
    return len(files)


def check_imports():
    """Every IMPORT_CHECK_INTERVAL: fix the bridge's own deliveries the *arr blocks only for a title
    mismatch; drop watched entries that left the queue (imported) or expired."""
    global _import_checked_at
    if not IMPORT_FIX or time.time() - _import_checked_at < IMPORT_CHECK_INTERVAL:
        return
    _import_checked_at = time.time()
    f = _watch_file()
    if not f.exists():
        return
    try:
        watch = json.loads(f.read_text())
    except (OSError, ValueError):
        return
    if not watch:
        return
    queues = {}
    for cat in {e["category"] for e in watch.values()}:
        try:
            extra = {"includeUnknownSeriesItems": "true"} if cat == "sonarr" else {"includeUnknownMovieItems": "true"}
            queues[cat] = arr_api(cat, "GET", "/queue", params=dict(pageSize=500, **extra)).get("records", [])
        except Exception as e:
            log(f"    (warn) import fix: {cat} queue unreadable: {e}")
    now = time.time()
    for tid, entry in list(watch.items()):
        cat = entry["category"]
        if cat not in queues:
            continue
        rel = norm(entry["release"])
        hits = [q for q in queues[cat] if norm(q.get("title", "")) == rel]
        ref = {"tid": tid, "release_name": entry["release"], "category": cat}
        if not hits:
            if now - entry["at"] > 600:  # give the *arr a scan or two before calling it gone
                watch.pop(tid)
            continue
        q = hits[0]
        msgs = " ".join(m for s_ in q.get("statusMessages", []) for m in s_.get("messages", []))
        if q.get("trackedDownloadState") != "importBlocked":
            pass
        elif IMPORT_BLOCK_RE.search(msgs) and entry["tries"] < 3 and now - entry.get("last_try", 0) > 600:
            entry["tries"] += 1
            entry["last_try"] = now
            try:
                n = manual_import(cat, entry, q.get("downloadId"))
                ev.emit(ref, "import_forced", None, files=n, reason=msgs[:120])
                log(f"[{cat}] import fix: '{entry['release']}' blocked ({msgs[:60]}) -> manual-imported {n} file(s)")
            except Exception as e:
                ev.emit(ref, "import_fix_failed", None, error=str(e)[:200])
                log(f"    (warn) import fix failed for '{entry['release']}': {e}")
        elif not IMPORT_BLOCK_RE.search(msgs) and not entry.get("noted"):
            entry["noted"] = True  # e.g. Sonarr's "TBA title, recently aired": it clears by itself
            ev.emit(ref, "import_blocked", None, reason=msgs[:200])
        if now - entry["at"] > IMPORT_WATCH_TTL:
            watch.pop(tid, None)
    try:
        f.write_text(json.dumps(watch))
    except OSError as e:
        log(f"    (warn) import watch: {e}")


def unmonitor(cat, keys):
    """Stop the *arr searching for these movies/episodes (give-up). Plain requests."""
    url, key = ARR.get(cat, (None, None))
    if not url or not key:
        return False
    ids = [int(k.rsplit(":", 1)[1]) for k in keys]
    try:
        if cat == "radarr":
            r = requests.put(f"{url}/api/v3/movie/editor", json={"movieIds": ids, "monitored": False},
                             headers={"X-Api-Key": key}, timeout=30)
        else:
            r = requests.put(f"{url}/api/v3/episode/monitor", json={"episodeIds": ids, "monitored": False},
                             headers={"X-Api-Key": key}, timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"    (warn) {cat} unmonitor {keys} failed: {e}")
        return False


def record_failure(be, cat, rec, reason):
    """Count a failed attempt for the transfer's movie/episodes; on the GIVE_UP_AFTER-th, give up
    on them. Called BEFORE arr_mark_failed: unmonitoring must come first, because marking a grab
    failed always makes the *arr search again."""
    keys = rec.get("arr_keys") or []
    n = ev.attempt_failed(keys, rec.get("release_name"), reason)
    if not keys or n < GIVE_UP_AFTER:
        return
    if GIVE_UP_DRY_RUN:
        ev.emit(rec, "would_give_up", None, attempts=n, keys=keys)
        be.log(f"[{cat}] '{rec['title']}': {n} failed attempts for {keys} -> WOULD give up (dry-run)")
        return
    done = unmonitor(cat, keys)
    ev.mark_gave_up(keys)
    ev.emit(rec, "gave_up", None, attempts=n, keys=keys, unmonitored=done)
    be.log(f"[{cat}] '{rec['title']}': {n} failed attempts -> gave up on {keys} (unmonitored={done})")


def keep_private_partial(be, cat, rec, sp, why):
    """A private torrent that already downloaded something must not be deleted (trackers count a
    partial snatch as a hit-and-run). Fail it in the *arr so it re-grabs, but keep the torrent
    as an undelivered seeding hold, capped at PARTIAL_HOLD. Returns True if it was kept."""
    if not rec.get("private"):
        return False
    got = be.partial_bytes(rec)
    if not got or got < PARTIAL_MIN_BYTES:
        return False
    release = rec.get("release_name", rec["title"])
    record_failure(be, cat, rec, why)
    arr_mark_failed(cat, release, rec.get("history_ids"))
    rec.update(held=True, no_deliver=True, hold_max_until=time.time() + PARTIAL_HOLD, partial_reason=why)
    sp.write_text(json.dumps(rec))
    ev.emit(rec, "partial_kept", "seeding_undelivered", reason=why, downloaded=got)
    be.log(
        f"[{cat}] '{rec['title']}' {why}, but it is private with {got / 2**20:.0f} MiB downloaded -> "
        f"failed in the *arr, torrent KEPT seeding (max {PARTIAL_HOLD // 86400}d) to avoid a hit-and-run"
    )
    return True


def purge_superseded(cat, new_key, new_name):
    """Mark every in-progress Radarr release of the SAME movie as `new_key` (but a
    DIFFERENT release than `new_name`) for deferred cleanup: cancel its transfer now
    (frees in-progress space), drop any local partial, and rewrite its state file as a
    tombstone so check_completions reaps the (often late-appearing) result.
    Returns the number of releases superseded."""
    if not new_key or not STATE_DIR.exists():
        return 0
    n = 0
    for sp, rec in load_records():
        if rec.get("category") != cat or rec.get("superseded"):
            continue
        if held(rec):  # delivered + seeding: deleting it now is a hit-and-run
            continue
        if norm(rec.get("title", "")) == norm(new_name):  # same release -> leave it
            continue
        if movie_key(rec.get("title", "")) != new_key:  # different movie -> leave it
            continue
        be = backend_for(rec)
        if be is not None and keep_private_partial(be, cat, rec, sp, "superseded by a newer grab"):
            continue
        if be is not None:
            be.cancel(rec)
        if rec.get("tid"):
            puller.cancel(rec["tid"])
        cleanup_local_partial(cat, rec["title"], new_name, rec.get("release_name"))
        rec.update(superseded=True, new_title=new_name, superseded_at=time.time())
        sp.write_text(json.dumps(rec))  # tombstone -> reaped by check_completions
        ev.emit(rec, "superseded", "cleaning_up", by=new_name)
        n += 1
        tid = rec.get("user_torrent_id") or rec.get("torrent_hash")
        log(f"    [override] superseded '{rec['title']}' (transfer {tid}); deferring result cleanup")
    return n


def notify(title, body):
    ntfy(title, body, priority="high", tags="warning")


def arr_mark_failed(cat, release_name, history_ids=None):
    """Mark the grab failed -> Radarr/Sonarr blocklists it and (autoRedownloadFailed) grabs a
    different release. By the history ids recorded at add time when known (every episode of a
    pack; the name search below only sees the newest 100 rows and marks one episode), else by
    finding the newest 'grabbed' row for release_name.
    Uses plain requests, never a backend session (Seedr's carries Basic-auth we must not leak)."""
    url, key = ARR.get(cat, (None, None))
    if not url or not key:
        return False
    if history_ids:
        ok = False
        for hid in history_ids:
            try:
                requests.post(
                    f"{url}/api/v3/history/failed/{hid}", headers={"X-Api-Key": key}, timeout=30
                ).raise_for_status()
                ok = True
            except Exception as e:
                log(f"    (warn) {cat} mark-failed of history {hid} failed: {e}")
        if ok:
            log(f"    marked {cat} history {history_ids} failed -> blocklist + auto re-search")
            return True
    try:
        h = requests.get(
            f"{url}/api/v3/history",
            params={
                "eventType": 1,
                "pageSize": 100,
                "sortKey": "date",
                "sortDirection": "descending",
            },
            headers={"X-Api-Key": key},
            timeout=30,
        )
        h.raise_for_status()
        nrel = norm(release_name)
        hid = next(
            (
                r["id"]
                for r in h.json().get("records", [])
                if norm(r.get("sourceTitle", "")) == nrel
            ),
            None,
        )
        if hid is None:
            log(f"    (warn) no grabbed history match for '{release_name}' in {cat}")
            return False
        requests.post(
            f"{url}/api/v3/history/failed/{hid}", headers={"X-Api-Key": key}, timeout=30
        ).raise_for_status()
        log(f"    marked {cat} history {hid} failed -> blocklist + auto re-search")
        return True
    except Exception as e:
        log(f"    (warn) {cat} mark-failed failed: {e}")
        return False


def release_or_hold(be, cat, rec, sp, result):
    """Done with a finished result. Public: drop it from the backend now and drop the state.
    Private: trackers punish hit-and-runs, so keep the result (and the state file) seeding
    until the backend says the hold is over; check_completions drops it then."""
    if rec.get("private"):
        until = be.start_hold(rec, result)
        rec["held"] = True
        sp.write_text(json.dumps(rec))
        ev.emit(rec, "hold_started", "seeding_hold", until=until, rule=rec.get("seed_rule"))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(until)) if until else "its seeding rule is met"
        be.log(f"[{cat}] '{rec['title']}' is private -> holding on {be.label} to seed until {when}")
        return
    be.discard(rec, result)
    sp.unlink()
    ev.emit(rec, "done", "done")


def handle_stalled(be, cat, rec, sp, prog):
    title = rec["title"]
    release = rec.get("release_name", title)
    if not may_fail(be, f"stall of '{title}'"):
        return
    if keep_private_partial(be, cat, rec, sp, f"stalled at {prog}%"):
        return
    be.log(
        f"[{cat}] '{title}' STALLED on {be.label} ({prog}% for >{STALL_TIMEOUT // 60}min) -> failing & retrying"
    )
    be.cancel(rec)  # free the dead transfer
    record_failure(be, cat, rec, "stalled")
    retried = arr_mark_failed(cat, release, rec.get("history_ids"))
    notify(
        f"Stalled: {release}",
        f"Stuck at {prog}% for {STALL_TIMEOUT // 60} min on {be.label}. "
        + (
            "Asked Radarr/Sonarr to grab a different release."
            if retried
            else "Could not reach Radarr/Sonarr -- override it manually."
        ),
    )
    # tombstone -> the reaper cleans any partial result/local dir and we stop re-failing
    rec.update(superseded=True, new_title=None, superseded_at=time.time(), stalled=True)
    sp.write_text(json.dumps(rec))
    ev.emit(rec, "failed", "cleaning_up", reason="stalled", progress=prog, rearr=retried)
    cleanup_local_partial(cat, title, old_release=release)


def handle_missing_result(be, cat, rec, sp):
    """A finished transfer whose result never appeared. Start a deadline on the first miss;
    once RESULT_TIMEOUT elapses, fail it in Radarr/Sonarr and tombstone it, so a
    late-appearing result is reaped instead of looping forever. A successful match unlinks
    the state file, so the deadline never outlives a delivery."""
    now = time.time()
    since = rec.get("result_missing_since")
    if since is None:
        rec["result_missing_since"] = now
        sp.write_text(json.dumps(rec))
        ev.emit(rec, "result_missing", "waiting_for_result")
        return
    if now - since < RESULT_TIMEOUT:
        return
    title = rec["title"]
    release = rec.get("release_name", title)
    if not may_fail(be, f"missing result of '{title}'"):
        return
    be.log(
        f"[{cat}] '{title}' finished but result MISSING from {be.label} for >{RESULT_TIMEOUT // 60}min -> failing"
    )
    record_failure(be, cat, rec, "missing_result")
    retried = arr_mark_failed(cat, release, rec.get("history_ids"))
    notify(
        f"Missing result: {release}",
        f"{be.label} finished it but never produced a file for {RESULT_TIMEOUT // 60} min. "
        + (
            "Marked failed in Radarr/Sonarr (it re-grabs if still wanted)."
            if retried
            else "Could not reach Radarr/Sonarr -- check it manually."
        ),
    )
    rec.update(superseded=True, new_title=None, superseded_at=now, missing_result=True)
    sp.write_text(json.dumps(rec))
    ev.emit(rec, "failed", "cleaning_up", reason="missing_result", rearr=retried)
    cleanup_local_partial(cat, title, old_release=release)


def handle_fetch_failure(be, cat, rec, sp, err, result):
    """A transfer the backend marked complete but whose bytes keep failing to fetch.
    Start a deadline on first failure; once FETCH_TIMEOUT elapses, stop retrying forever:
    purge the dead result, fail it in Radarr/Sonarr (-> blocklist + auto re-grab a
    different release) and notify. Mirrors handle_stalled for the post-completion path. A
    successful fetch unlinks the state file, so the deadline naturally resets on recovery."""
    now = time.time()
    since = rec.get("fetch_failing_since")
    if since is None:
        rec["fetch_failing_since"] = now
        sp.write_text(json.dumps(rec))
        ev.emit(rec, "fetch_failing", "fetch_retrying", error=err)
        return
    if now - since < FETCH_TIMEOUT:
        return
    title = rec["title"]
    release = rec.get("release_name", title)
    if not may_fail(be, f"unfetchable '{title}'"):
        return
    be.log(
        f"[{cat}] '{title}' UNFETCHABLE ({be.fetch_fail_log} >{FETCH_TIMEOUT // 60}min: {err}) -> failing & retrying"
    )
    if not rec.get("private"):  # private: it is seeding -> held below instead
        be.discard(rec, result)
    record_failure(be, cat, rec, "unfetchable")
    retried = arr_mark_failed(cat, release, rec.get("history_ids"))
    notify(
        f"Unfetchable: {release}",
        f"{be.fetch_fail_note} for {FETCH_TIMEOUT // 60} min. "
        + (
            "Asked Radarr/Sonarr to grab a different release."
            if retried
            else "Could not reach Radarr/Sonarr -- override it manually."
        ),
    )
    cleanup_local_partial(cat, title, old_release=release)
    if rec.get("private"):
        # NOT a tombstone: the tombstone reaper deletes matching results every loop.
        rec["unfetchable"] = True
        release_or_hold(be, cat, rec, sp, result)
        return
    # tombstone -> reaper cleans any leftover result / local partial and we stop retrying
    rec.update(superseded=True, new_title=None, superseded_at=now, unfetchable=True)
    sp.write_text(json.dumps(rec))
    ev.emit(rec, "failed", "cleaning_up", reason="unfetchable", error=err, rearr=retried)


def adopt_existing(be, cat, mfile, magnet, nkey, private=False, rule=None, indexer=None,
                   arr_keys=None, history_ids=None):
    """The backend says it ALREADY holds this torrent -- an out-of-band add (adding the
    magnet by hand on seedr.cc is a normal thing to do) or an earlier loop whose response we
    lost. That leaves no state file behind, and check_completions() only ever looks at state
    files, so the result would never be fetched while the drop is refused again on every
    single poll -- forever. Adopt the transfer instead: write exactly the record a successful
    add would have. Returns True if the drop was adopted (and consumed)."""
    btih = magnet_btih(magnet)
    # The *arr grabbed the same release twice (2026-09-24: an add's automatic search and an
    # interactive grab of the same guid seconds apart), so a second drop lands while the first
    # is already tracked. Adopting it would write a SECOND state file for one torrent -- under
    # the release name, not the backend's title -- which, once the first delivers and the
    # backend drops the transfer, loops "finished but result not visible yet" and then
    # handle_missing_result marks the already-imported grab failed. Already tracked -> just
    # consume the duplicate.
    if btih:
        for sp, rec in load_records():
            if (rec.get("torrent_hash") or "").lower() == btih and not rec.get("superseded"):
                mfile.unlink()
                ev.emit(rec, "duplicate_dropped", None, drop=mfile.name)
                be.log(f"    409: already tracked as '{rec.get('title')}' -> dropped duplicate {mfile.name}")
                return True
    found = be.locate(btih, mfile.stem)
    if found is None:
        return False
    keys, where = found
    if nkey:
        purge_superseded(cat, nkey, mfile.stem)
    title = mfile.stem
    rec = {"category": cat, "title": title, "release_name": mfile.stem, "backend": be.name}
    rec.update(keys)
    rec["added"] = time.time()
    ev.ensure_tid(rec)
    if private:
        rec.update(private=True, seed_rule=rule, indexer=indexer)
    if arr_keys:
        rec.update(arr_keys=arr_keys, history_ids=history_ids)
    state_file(cat, title).write_text(json.dumps(rec))
    mfile.unlink()
    DROP_STATE.pop(mfile.name, None)
    ev.emit(rec, "adopted", "queued", where=where, indexer=indexer)
    be.log(f"    already on {be.label} ({where}) -> adopted as '{title}'")
    return True


def process_new_magnets():
    for cat in CATEGORIES:
        bh = DATA / "blackhole" / cat
        bh.mkdir(parents=True, exist_ok=True)
        drops = (
            sorted(bh.glob("*.magnet"))
            + sorted(bh.glob("*.txt"))
            + sorted(bh.glob("*.torrent"))
        )
        for mfile in drops:
            private = False
            if mfile.suffix == ".torrent":
                # Indexers that hand the *arr a real .torrent (private trackers, YTS, ...)
                # land here. The file itself is uploaded; the magnet only supplies the btih.
                try:
                    magnet = magnet_from_torrent(mfile)
                    private = torrent_is_private(mfile)
                    hosts = torrent_meta(mfile)[3]
                except Exception as e:
                    log(f"[{cat}] cannot parse {mfile.name} ({e}); skipping")
                    continue
            else:
                magnet = mfile.read_text().strip()
                if not magnet.startswith("magnet:"):
                    continue
                hosts = magnet_meta(magnet)[3]
            # What the *arr recorded for this grab: its indexer (private-tracker rules; a magnet
            # from a known private tracker is private too) and the movie/episode ids (attempts,
            # give-up, mark-failed by id). The *arr writes the drop just before its history row,
            # so a miss is retried for a few polls (private: longer -- the rule depends on it).
            if ARR.get(cat, (None, None))[0] and mfile.name not in INDEXERS:
                found = grab_info(cat, mfile.stem)
                tries = INDEXER_TRIES.get(mfile.name, 0) + 1
                limit = INDEXER_MAX_TRIES if private else 2
                if found is None and tries < limit:
                    INDEXER_TRIES[mfile.name] = tries
                    drop_wait(cat, mfile, "looking_up_grab")
                    continue
                INDEXERS[mfile.name] = found or {}
            info = INDEXERS.get(mfile.name) or {}
            if mfile.suffix != ".torrent" and tracker_rule_key(info.get("indexer")):
                private = True
            arr_keys = info.get("keys") or []
            if arr_keys and ev.given_up(arr_keys):
                if GIVE_UP_DRY_RUN:
                    drop_wait(cat, mfile, "would_drop_given_up", keys=arr_keys)
                else:
                    ev.emit(ev.drop_ref(cat, mfile), "dropped", None, reason="given_up", keys=arr_keys)
                    DROP_STATE.pop(mfile.name, None)
                    INDEXERS.pop(mfile.name, None)
                    INDEXER_TRIES.pop(mfile.name, None)
                    log(f"[{cat}] {mfile.name}: gave up on {arr_keys} earlier -> dropping this grab")
                    mfile.unlink()
                    continue
            name = route(private)
            if name is None:
                log(f"[{cat}] no BRIDGE_ROUTE matches {mfile.name} ({'private' if private else 'public'}); leaving it")
                drop_wait(cat, mfile, "no_route", private=private)
                continue
            try:
                be = backends.get(name)
            except Exception as e:
                log(f"[{cat}] backend '{name}' unavailable ({e!r}); leaving {mfile.name}")
                drop_wait(cat, mfile, "backend_unavailable", backend=name, error=repr(e))
                continue
            rule = None
            indexer = info.get("indexer")
            if private:
                rule, how = seed_rule(indexer, hosts)
                cap = be.max_seed_hours
                if cap is not None and rule["hours"] > cap:
                    # Sending it would be a hit-and-run: the backend stops seeding first.
                    drop_wait(cat, mfile, "seed_rule_exceeds_backend", rule=rule["key"],
                              needs_hours=rule["hours"], backend=be.name, cap_hours=cap)
                    if mfile.name not in WARNED:
                        WARNED.add(mfile.name)
                        be.log(
                            f"[{cat}] {mfile.name}: '{rule['key']}' needs {rule['hours']:g}h seeding, "
                            f"{be.label} does {cap:g}h -> leaving it in the blackhole"
                        )
                        notify(
                            f"Not sent: {mfile.stem}",
                            f"{rule['key']} needs {rule['hours']:g}h of seeding but {be.label} only seeds "
                            f"{cap:g}h. Route private torrents to a backend that can (BRIDGE_ROUTE), or "
                            "delete the file from the blackhole.",
                        )
                    continue
            extra = 0
            if not private and mfile.suffix != ".torrent":
                # public magnet: more trackers -> the backend finds the swarm sooner (never private)
                magnet, extra = trackers.add_to_magnet(magnet, trackers.get())
            # movie identity (Radarr only) -> lets a new release supersede an old
            # in-progress 'zombie' transfer of the SAME movie.
            nkey = movie_key(mfile.stem) if cat == "radarr" else None
            be.log(
                f"[{cat}] adding {mfile.name}"
                + (f" (private: {rule['key']} via {how}, seed {rule['hours']:g}h)" if private else "")
            )
            try:
                keys = be.add(mfile, magnet)
            except Full as e:
                free = e.free or 0
                # override: a same-movie zombie is hogging space -> free it and retry the
                # add next loop instead of dropping this one as too large.
                if nkey and purge_superseded(cat, nkey, mfile.stem) > 0:
                    FAIL_COUNTS.pop(mfile.name, None)
                    be.log(
                        f"    [override] freed {be.label} space from old release; will retry add for {mfile.name}"
                    )
                    continue
                n = FAIL_COUNTS.get(mfile.name, 0) + 1
                FAIL_COUNTS[mfile.name] = n
                limit = be.full_retries
                if e.never_fits or (limit is not None and n >= limit):
                    ev.emit(ev.drop_ref(cat, mfile), "dropped", None, reason="too_large", free=free)
                    DROP_STATE.pop(mfile.name, None)
                    be.log(
                        f"    '{mfile.name}' does not fit {be.label} ({free / 1e9:.0f}GB free) -> dropping (too large)"
                    )
                    mfile.unlink()
                    FAIL_COUNTS.pop(mfile.name, None)
                elif limit is not None:
                    be.log(
                        f"    add refused ({be.label} full, {free / 1e9:.0f}GB free); attempt {n}/{limit}, will retry"
                    )
                if mfile.exists():  # not just dropped as too large
                    drop_wait(cat, mfile, "backend_full", backend=be.name, free_gib=round(free / 2**30, 1))
                if limit is None and (n == 1 or n % 120 == 0):  # waits indefinitely -> log ~hourly
                    be.log(
                        f"    {be.label} full ({free / 1e9:.0f}GB free) -> {mfile.name} waits in the blackhole for space"
                    )
                continue
            except Exists:
                if not adopt_existing(be, cat, mfile, magnet, nkey, private, rule, indexer,
                                      arr_keys, info.get("history_ids")):
                    be.log("    add failed (already exists, not adoptable yet); will retry next loop")
                continue
            except Rejected as e:
                be.log(f"    {be.label} rejected it: {e}; dropping {mfile.name}")
                ev.emit(ev.drop_ref(cat, mfile), "dropped", None, reason="rejected", error=str(e))
                DROP_STATE.pop(mfile.name, None)
                mfile.unlink()
                continue
            except Exception as e:
                be.log(f"    add failed ({e}); will retry next loop")
                drop_wait(cat, mfile, "add_error", backend=be.name, error=str(e)[:200])
                continue
            FAIL_COUNTS.pop(mfile.name, None)
            WARNED.discard(mfile.name)
            INDEXERS.pop(mfile.name, None)
            INDEXER_TRIES.pop(mfile.name, None)
            title = keys.get("title") or mfile.stem
            # override: drop any old in-progress release of the same movie.
            if nkey:
                purge_superseded(cat, nkey, mfile.stem)
            rec = {"category": cat, "title": title, "release_name": mfile.stem, "backend": be.name}
            rec.update({k: v for k, v in keys.items() if k != "title"})
            rec["added"] = time.time()
            ev.ensure_tid(rec)
            if private:
                rec.update(private=True, seed_rule=rule, indexer=indexer)
            if arr_keys:
                rec.update(arr_keys=arr_keys, history_ids=info.get("history_ids"))
            if not private and mfile.suffix == ".torrent":
                extra = be.extra_trackers(rec, trackers.get())  # public .torrent (e.g. YTS) on the seedbox
            state_file(cat, title).write_text(json.dumps(rec))
            mfile.unlink()
            DROP_STATE.pop(mfile.name, None)
            ev.emit(rec, "added", "queued", private=bool(private), indexer=indexer,
                    rule=(rule or {}).get("key"), size=keys.get("size"), keys=arr_keys, trackers_added=extra)
            prior = ev.attempt_count(arr_keys)
            if prior:
                ev.emit(rec, "replacement_grabbed", None, attempt=prior + 1)
            be.log(f"    queued on {be.label} as '{title}'")


def check_completions():
    if not STATE_DIR.exists() or not any(STATE_DIR.glob("*.json")):
        return
    records = load_records()
    for sp, rec in records:
        if ev.ensure_tid(rec):  # state written before the event log existed
            save(sp, rec)
    by_backend = {}
    for sp, rec in records:
        be = backend_for(rec)
        if be is not None:
            by_backend.setdefault(be.name, (be, []))[1].append((sp, rec))
    for name, (be, recs) in by_backend.items():
        try:
            be.begin_loop(recs)
        except Exception as e:
            # one backend being unreachable must not stall the others
            be.log(f"loop error: {e}")
            if BACKEND_UP.get(be.name, True):
                BACKEND_UP[be.name] = False
                ev.emit({"tid": f"backend:{be.name}", "backend": be.name}, "backend_down", None, error=str(e)[:300])
            continue
        if not BACKEND_UP.get(be.name, True):
            ev.emit({"tid": f"backend:{be.name}", "backend": be.name}, "backend_up", None)
        BACKEND_UP[be.name] = True
        freeze_clocks(be, recs)
        for sp, rec in recs:
            check_one(be, sp, rec)


def check_one(be, sp, rec):
    title, cat = rec["title"], rec["category"]
    # delivered private result -> keep seeding until the hold is over, then free the space.
    if held(rec):
        over = be.hold_over(rec)
        if over is None:  # backend can't see it right now: keep holding, never guess "done"
            note_unavailable(be, cat, rec, sp, "held torrent not visible")
            return
        clear_unavailable(rec, sp)
        if not over and rec.get("hold_max_until") and time.time() >= rec["hold_max_until"]:
            over = True
            be.log(f"[{cat}] '{title}' undelivered private hold reached its {PARTIAL_HOLD // 86400}d cap")
        if over:
            be.end_hold(rec)
            sp.unlink()
            ev.emit(rec, "hold_ended", "done", rule=(rec.get("seed_rule") or {}).get("key"))
            be.log(f"[{cat}] '{title}' seeding hold over -> removed from {be.label}")
        return
    # superseded release -> reap its (often late-appearing) footprint, never the
    # replacement's, until the grace window closes.
    if rec.get("superseded"):
        be.reap(rec)
        cleanup_local_partial(cat, title, rec.get("new_title"), rec.get("release_name"))
        if time.time() - rec.get("superseded_at", 0) > TOMBSTONE_GRACE:
            sp.unlink()
            ev.emit(rec, "cleanup_closed", "ended")
            be.log(f"    [override] cleanup window closed for '{title}'")
        return
    state, info = be.status(rec)
    if state == UNAVAILABLE:
        note_unavailable(be, cat, rec, sp, info)
        return  # never fail, tombstone or reap what the backend merely can't see right now
    clear_unavailable(rec, sp)
    if state == DOWNLOADING:
        prog, now = info, time.time()
        set_phase(sp, rec, "downloading")
        be.log(f"[{cat}] '{title}' downloading on {be.label} ({prog}%)")
        if rec.get("last_progress") is None or prog > rec["last_progress"] + 0.05:
            rec.update(last_progress=prog, last_progress_at=now)  # advanced -> reset stall clock
            sp.write_text(json.dumps(rec))
        elif now - rec.get("last_progress_at", rec.get("added", now)) > STALL_TIMEOUT:
            handle_stalled(be, cat, rec, sp, prog)
            return
        if check_failfast(be, cat, rec, sp, prog, now):
            return
        check_slow(be, cat, rec, sp, prog, now)
        return  # still in progress -> do NOT fetch yet
    if state == DONE:
        result = info
        # Deliver under the release name the *arr GRABBED, not the backend's name for the
        # result: the *arr maps a watch-folder item to its series/movie by parsing the folder
        # name, and the backend's name is the torrent's raw internal one ('www.1TamilMV.promo -
        # Heart Beat (2026) S03 EP (01-04) ...'), which can map to nothing -- or the wrong show.
        release = rec.get("release_name") or title
        dest = DATA / "downloads" / cat / release
        stage = INCOMING / cat / release
        job = puller.get(rec["tid"])
        if job is None:
            # hand the pull to the worker; this loop moves on (a 30 GB pull no longer blocks it)
            try:
                plan = be.prepare_fetch(rec, result)
            except Exception as e:
                be.log(f"    can't plan the fetch of '{title}': {e}; will retry")
                handle_fetch_failure(be, cat, rec, sp, str(e), result)
                return
            size = (plan.get("size") if isinstance(plan, dict) else None) or rec.get("size")
            puller.submit(be, rec, plan, stage, size)
            if not rec.get("pulling"):
                rec["pulling"] = True
                if size:
                    rec["size"] = size
                save(sp, rec)
            set_phase(sp, rec, "pulling", size=size)
            be.log(f"[{cat}] '{result['name']}' complete -> pulling to the Jetson")
            return
        if job.status in ("queued", "waiting_space", "running"):
            return  # still pulling
        puller.forget(rec["tid"])
        if job.status == "cancelled":
            return
        if job.status == "failed":
            rec.pop("pulling", None)
            save(sp, rec)
            be.log(f"    fetch failed for '{title}': {job.error}; will retry")
            handle_fetch_failure(be, cat, rec, sp, job.error, result)
            return
        try:
            if not stage.exists():
                raise RuntimeError("fetch produced no files")
            how = deliver(stage, dest)
        except Exception as e:
            rec.pop("pulling", None)
            save(sp, rec)
            be.log(f"    delivery of '{title}' failed: {e}; will retry")
            handle_fetch_failure(be, cat, rec, sp, str(e), result)
            return
        secs = max((job.finished or time.time()) - (job.started or time.time()), 1e-6)
        rec.pop("pulling", None)
        rec.pop("fetch_failing_since", None)
        save(sp, rec)
        watch_import(rec, dest)
        ev.emit(rec, "delivered", "delivered", how=how, bytes=job.bytes, secs=round(secs, 1),
                mb_s=round(job.bytes / secs / 1e6, 1))
        be.log(f"[{cat}] '{title}' {how} -> {dest} ({job.bytes / 1e6:.0f} MB in {secs:.0f}s)")
        release_or_hold(be, cat, rec, sp, result)
        return
    be.log(f"[{cat}] '{title}' finished but result not visible yet; will retry")
    handle_missing_result(be, cat, rec, sp)


def check_failfast(be, cat, rec, sp, prog, now):
    """Fail-fast for dead public picks (see FAILFAST_*). True if the transfer was failed."""
    if FAILFAST == "off" or rec.get("private") or rec.get("failfast_flagged"):
        return False
    if now - rec.get("added", now) < FAILFAST_AFTER or (rec.get("last_progress") or 0) > 0:
        return False  # too early, or it has started: alive
    sw = be.swarm(rec)
    if sw is None:
        why = f"still 0% after {(now - rec['added']) / 60:.0f}min" if prog == 0 else None
    elif not sw.get("metadata"):
        why = f"no metadata after {(now - rec['added']) / 60:.0f}min"
    elif sw.get("seeds") == 0:
        why = "swarm reports 0 seeds"
    else:
        why = None
    if not why:
        return False
    if FAILFAST_DRY_RUN:
        rec["failfast_flagged"] = why
        save(sp, rec)
        ev.emit(rec, "would_fail_dead", None, reason=why)
        be.log(f"[{cat}] '{rec['title']}' looks DEAD ({why}) -> would fail & re-grab (dry-run)")
        return False
    if not may_fail(be, f"dead '{rec['title']}'"):
        return False
    be.log(f"[{cat}] '{rec['title']}' looks DEAD ({why}) -> failing & re-grabbing")
    be.cancel(rec)
    record_failure(be, cat, rec, "dead")
    retried = arr_mark_failed(cat, rec.get("release_name", rec["title"]), rec.get("history_ids"))
    rec.update(superseded=True, new_title=None, superseded_at=now, dead=True)
    save(sp, rec)
    ev.emit(rec, "failed", "cleaning_up", reason="dead", detail=why, rearr=retried)
    cleanup_local_partial(cat, rec["title"], old_release=rec.get("release_name"))
    return True


def check_slow(be, cat, rec, sp, prog, now):
    """The slow rule (see SLOW_*): track the bridge's own progress rate, and fail + re-grab a
    download that is hopeless -- or, in dry-run, just record that it would have."""
    if SLOW_RULE == "off" or (SLOW_RULE == "public" and rec.get("private")):
        return
    samples = rec.setdefault("rate", [])
    if not samples or now - samples[-1][0] >= 300:
        samples.append([now, prog])
        # keep ~1.5 windows of samples, always one old enough to span the window
        while len(samples) > 2 and now - samples[1][0] >= RATE_WINDOW:
            samples.pop(0)
        save(sp, rec)
    if now - rec.get("added", now) < SLOW_MIN_AGE:
        return
    why = None
    if prog < SLOW_MIN_PROGRESS:
        why = f"under {SLOW_MIN_PROGRESS:g}% after {(now - rec['added']) / 3600:.1f}h"
    else:
        t0, p0 = samples[0]
        span_h = (now - t0) / 3600
        rate = (prog - p0) / span_h if span_h >= 0.5 else None  # %/h over up to ~1h
        eta_h = (100 - prog) / rate if rate else float("inf") if rate is not None else None
        if eta_h is not None and eta_h > SLOW_MAX_ETA_H:
            if not rec.get("slow_since"):
                rec["slow_since"] = now
                save(sp, rec)
            elif now - rec["slow_since"] >= SLOW_CONFIRM:
                why = f"projected {eta_h:.0f}h left at {rate or 0:.2f}%/h"
        elif rec.get("slow_since"):
            rec.pop("slow_since")
            save(sp, rec)
    if not why:
        return
    if SLOW_DRY_RUN:
        if not rec.get("slow_flagged"):
            rec["slow_flagged"] = why
            save(sp, rec)
            ev.emit(rec, "would_fail_slow", None, reason=why, progress=prog)
            be.log(f"[{cat}] '{rec['title']}' is SLOW ({why}) -> would fail & re-grab (dry-run)")
        return
    if not may_fail(be, f"slow '{rec['title']}'"):
        return
    if keep_private_partial(be, cat, rec, sp, f"too slow ({why})"):
        return
    be.log(f"[{cat}] '{rec['title']}' too SLOW ({why}) -> failing & re-grabbing")
    be.cancel(rec)
    record_failure(be, cat, rec, "slow")
    retried = arr_mark_failed(cat, rec.get("release_name", rec["title"]), rec.get("history_ids"))
    rec.update(superseded=True, new_title=None, superseded_at=now, slow=True)
    save(sp, rec)
    ev.emit(rec, "failed", "cleaning_up", reason="slow", detail=why, rearr=retried)
    cleanup_local_partial(cat, rec["title"], old_release=rec.get("release_name"))


def note_unavailable(be, cat, rec, sp, why):
    now = time.time()
    if not rec.get("unavailable_since"):
        rec["unavailable_since"] = now
        sp.write_text(json.dumps(rec))
        ev.emit(rec, "unavailable", "waiting", why=why)
        be.log(f"[{cat}] '{rec['title']}' unavailable on {be.label} ({why}); waiting, deadlines frozen")
    elif now - rec["unavailable_since"] > UNAVAILABLE_ALERT and not rec.get("unavailable_alerted"):
        rec["unavailable_alerted"] = True
        sp.write_text(json.dumps(rec))
        notify(
            f"Not visible on {be.label}: {rec.get('release_name', rec['title'])}",
            f"{be.label} hasn't shown this transfer for {UNAVAILABLE_ALERT // 3600}h ({why}). "
            "Nothing was deleted or failed; check the seedbox.",
        )


def clear_unavailable(rec, sp):
    if rec.get("unavailable_since"):
        # the time it was invisible doesn't count against its deadlines
        gap = time.time() - rec.pop("unavailable_since")
        rec.pop("unavailable_alerted", None)
        ev.emit(rec, "visible_again", None, after_s=round(gap))
        for k in ("last_progress_at", "result_missing_since", "fetch_failing_since"):
            if rec.get(k):
                rec[k] += gap
        sp.write_text(json.dumps(rec))


def run_forever():
    log(f"blackhole bridge starting; polling every {POLL}s; routes {ROUTES or 'NONE'}")
    for name in dict.fromkeys(n for _, n in ROUTES):
        try:
            backends.get(name).startup()
        except Exception as e:
            log(f"backend '{name}' failed to load: {e!r}")
    while True:
        try:
            process_new_magnets()
            check_completions()
            check_imports()
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    run_forever()
