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
from backends.base import DONE, DOWNLOADING, Exists, Full, Rejected
from common import clog, name_matches, norm, ntfy
from torrentfile import magnet_btih, magnet_from_torrent, torrent_is_private

FAIL_COUNTS = {}  # blackhole filename -> consecutive "backend full" attempts (in-memory)
TOMBSTONE_GRACE = (
    600  # secs to keep reaping a superseded release's late-appearing backend result
)
POLL = int(os.environ.get("POLL_INTERVAL", "30"))
DATA = pathlib.Path("/data")
STATE_DIR = DATA / "blackhole" / ".bridge-state"
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
    base = DATA / "downloads" / cat
    if not base.exists():
        return
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


def adopt_existing(be, cat, mfile, magnet, nkey, private=False):
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
        rec["private"] = True
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
                except Exception as e:
                    log(f"[{cat}] cannot parse {mfile.name} ({e}); skipping")
                    continue
            else:
                magnet = mfile.read_text().strip()
                if not magnet.startswith("magnet:"):
                    continue
            name = route(private)
            if name is None:
                log(f"[{cat}] no BRIDGE_ROUTE matches {mfile.name} ({'private' if private else 'public'}); leaving it")
                continue
            try:
                be = backends.get(name)
            except Exception as e:
                log(f"[{cat}] backend '{name}' unavailable ({e!r}); leaving {mfile.name}")
                continue
            # movie identity (Radarr only) -> lets a new release supersede an old
            # in-progress 'zombie' transfer of the SAME movie.
            nkey = movie_key(mfile.stem) if cat == "radarr" else None
            be.log(f"[{cat}] adding {mfile.name}" + (" (private)" if private else ""))
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
                # plenty of free space yet still full => the torrent itself is bigger than
                # the account; it will never fit -> drop it.
                if free > 20e9 or n >= 5:
                    be.log(
                        f"    '{mfile.name}' does not fit {be.label} ({free / 1e9:.0f}GB free) -> dropping (too large)"
                    )
                    mfile.unlink()
                    FAIL_COUNTS.pop(mfile.name, None)
                else:
                    be.log(
                        f"    add refused ({be.label} full, {free / 1e9:.0f}GB free); attempt {n}/5, will retry"
                    )
                continue
            except Exists:
                if not adopt_existing(be, cat, mfile, magnet, nkey, private):
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
            title = keys.get("title") or mfile.stem
            # override: drop any old in-progress release of the same movie.
            if nkey:
                purge_superseded(cat, nkey, mfile.stem)
            rec = {"category": cat, "title": title, "release_name": mfile.stem, "backend": be.name}
            rec.update({k: v for k, v in keys.items() if k != "title"})
            rec["added"] = time.time()
            if private:
                rec["private"] = True
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
        for sp, rec in recs:
            check_one(be, sp, rec)


def check_one(be, sp, rec):
    title, cat = rec["title"], rec["category"]
    # delivered private result -> keep seeding until the hold is over, then free the space.
    if held(rec):
        if be.hold_over(rec):
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
        dest = DATA / "downloads" / cat / (rec.get("release_name") or title)
        be.log(f"[{cat}] '{result['name']}' complete -> fetching to {dest}")
        try:
            be.fetch(rec, result, dest)
            release_or_hold(be, cat, rec, sp, result)
            be.log(f"[{cat}] '{title}' delivered.")
        except Exception as e:
            be.log(f"    fetch failed for '{title}': {e}; will retry")
            handle_fetch_failure(be, cat, rec, sp, str(e), result)
        return
    be.log(f"[{cat}] '{title}' finished but result not visible yet; will retry")
    handle_missing_result(be, cat, rec, sp)


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
