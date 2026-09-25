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


def grab_indexer(cat, release_name):
    """The indexer Radarr/Sonarr grabbed release_name from (its newest 'grabbed' history
    record), or None. Uses plain requests, like arr_mark_failed."""
    url, key = ARR.get(cat, (None, None))
    if not url or not key:
        return None
    try:
        h = requests.get(
            f"{url}/api/v3/history",
            params={"eventType": 1, "pageSize": 50, "sortKey": "date", "sortDirection": "descending"},
            headers={"X-Api-Key": key},
            timeout=30,
        )
        h.raise_for_status()
        nrel = norm(release_name)
        for r in h.json().get("records", []):
            if norm(r.get("sourceTitle", "")) == nrel:
                return (r.get("data") or {}).get("indexer")
    except Exception as e:
        log(f"    (warn) {cat} history lookup for the indexer failed: {e}")
    return None


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
    for base in (DATA / "downloads" / cat, INCOMING / cat):
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir():
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
    arr_mark_failed(cat, release)
    rec.update(held=True, no_deliver=True, hold_max_until=time.time() + PARTIAL_HOLD, partial_reason=why)
    sp.write_text(json.dumps(rec))
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
        cleanup_local_partial(cat, rec["title"], new_name, rec.get("release_name"))
        rec.update(superseded=True, new_title=new_name, superseded_at=time.time())
        sp.write_text(json.dumps(rec))  # tombstone -> reaped by check_completions
        n += 1
        tid = rec.get("user_torrent_id") or rec.get("torrent_hash")
        log(f"    [override] superseded '{rec['title']}' (transfer {tid}); deferring result cleanup")
    return n


def notify(title, body):
    ntfy(title, body, priority="high", tags="warning")


def arr_mark_failed(cat, release_name):
    """Find the most recent 'grabbed' history record for release_name and mark it failed
    -> Radarr/Sonarr blocklists it and (autoRedownloadFailed) grabs a different release.
    Uses plain requests, never a backend session (Seedr's carries Basic-auth we must not leak)."""
    url, key = ARR.get(cat, (None, None))
    if not url or not key:
        return False
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
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(until)) if until else "its seeding rule is met"
        be.log(f"[{cat}] '{rec['title']}' is private -> holding on {be.label} to seed until {when}")
        return
    be.discard(rec, result)
    sp.unlink()


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
    retried = arr_mark_failed(cat, release)
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
    retried = arr_mark_failed(cat, release)
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
    retried = arr_mark_failed(cat, release)
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


def adopt_existing(be, cat, mfile, magnet, nkey, private=False, rule=None, indexer=None):
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
    if private:
        rec.update(private=True, seed_rule=rule, indexer=indexer)
    state_file(cat, title).write_text(json.dumps(rec))
    mfile.unlink()
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
                # A magnet from a private tracker (an indexer with a SEED_RULES entry) must be held
                # like a .torrent one; nothing in a magnet says "private". One lookup per drop.
                if SEED_RULES and mfile.name not in INDEXERS:
                    INDEXERS[mfile.name] = grab_indexer(cat, mfile.stem)
                if tracker_rule_key(INDEXERS.get(mfile.name)):
                    private = True
            name = route(private)
            if name is None:
                log(f"[{cat}] no BRIDGE_ROUTE matches {mfile.name} ({'private' if private else 'public'}); leaving it")
                continue
            try:
                be = backends.get(name)
            except Exception as e:
                log(f"[{cat}] backend '{name}' unavailable ({e!r}); leaving {mfile.name}")
                continue
            rule = indexer = None
            if private:
                if mfile.name not in INDEXERS:
                    found = grab_indexer(cat, mfile.stem)
                    tries = INDEXER_TRIES.get(mfile.name, 0) + 1
                    if found is None and tries < INDEXER_MAX_TRIES:
                        # the *arr writes the drop just before its history row: ask again next poll
                        # rather than fall back to the strictest rule for good
                        INDEXER_TRIES[mfile.name] = tries
                        continue
                    INDEXERS[mfile.name] = found  # a real answer, or None after the retries
                indexer = INDEXERS[mfile.name]
                rule, how = seed_rule(indexer, hosts)
                cap = be.max_seed_hours
                if cap is not None and rule["hours"] > cap:
                    # Sending it would be a hit-and-run: the backend stops seeding first.
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
                    be.log(
                        f"    '{mfile.name}' does not fit {be.label} ({free / 1e9:.0f}GB free) -> dropping (too large)"
                    )
                    mfile.unlink()
                    FAIL_COUNTS.pop(mfile.name, None)
                elif limit is not None:
                    be.log(
                        f"    add refused ({be.label} full, {free / 1e9:.0f}GB free); attempt {n}/{limit}, will retry"
                    )
                elif n == 1 or n % 120 == 0:  # waits indefinitely -> log ~hourly, not every poll
                    be.log(
                        f"    {be.label} full ({free / 1e9:.0f}GB free) -> {mfile.name} waits in the blackhole for space"
                    )
                continue
            except Exists:
                if not adopt_existing(be, cat, mfile, magnet, nkey, private, rule, indexer):
                    be.log("    add failed (already exists, not adoptable yet); will retry next loop")
                continue
            except Rejected as e:
                be.log(f"    {be.label} rejected it: {e}; dropping {mfile.name}")
                mfile.unlink()
                continue
            except Exception as e:
                be.log(f"    add failed ({e}); will retry next loop")
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
            if private:
                rec.update(private=True, seed_rule=rule, indexer=indexer)
            state_file(cat, title).write_text(json.dumps(rec))
            mfile.unlink()
            be.log(f"    queued on {be.label} as '{title}'")


def check_completions():
    if not STATE_DIR.exists() or not any(STATE_DIR.glob("*.json")):
        return
    records = load_records()
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
            continue
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
            be.log(f"[{cat}] '{title}' seeding hold over -> removed from {be.label}")
        return
    # superseded release -> reap its (often late-appearing) footprint, never the
    # replacement's, until the grace window closes.
    if rec.get("superseded"):
        be.reap(rec)
        cleanup_local_partial(cat, title, rec.get("new_title"), rec.get("release_name"))
        if time.time() - rec.get("superseded_at", 0) > TOMBSTONE_GRACE:
            sp.unlink()
            be.log(f"    [override] cleanup window closed for '{title}'")
        return
    state, info = be.status(rec)
    if state == UNAVAILABLE:
        note_unavailable(be, cat, rec, sp, info)
        return  # never fail, tombstone or reap what the backend merely can't see right now
    clear_unavailable(rec, sp)
    if state == DOWNLOADING:
        prog, now = info, time.time()
        be.log(f"[{cat}] '{title}' downloading on {be.label} ({prog}%)")
        if rec.get("last_progress") is None or prog > rec["last_progress"] + 0.05:
            rec.update(last_progress=prog, last_progress_at=now)  # advanced -> reset stall clock
            sp.write_text(json.dumps(rec))
        elif now - rec.get("last_progress_at", rec.get("added", now)) > STALL_TIMEOUT:
            handle_stalled(be, cat, rec, sp, prog)
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
        be.log(f"[{cat}] '{result['name']}' complete -> fetching to {stage}")
        try:
            be.fetch(rec, result, stage)
            if not stage.exists():
                raise RuntimeError("fetch produced no files")
            how = deliver(stage, dest)
        except Exception as e:
            be.log(f"    fetch failed for '{title}': {e}; will retry")
            handle_fetch_failure(be, cat, rec, sp, str(e), result)
            return
        be.log(f"[{cat}] '{title}' {how} -> {dest}")
        release_or_hold(be, cat, rec, sp, result)
        return
    be.log(f"[{cat}] '{title}' finished but result not visible yet; will retry")
    handle_missing_result(be, cat, rec, sp)


def note_unavailable(be, cat, rec, sp, why):
    now = time.time()
    if not rec.get("unavailable_since"):
        rec["unavailable_since"] = now
        sp.write_text(json.dumps(rec))
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
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    run_forever()
