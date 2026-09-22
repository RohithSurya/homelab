#!/usr/bin/env python3
"""
Seedr <-> Sonarr/Radarr blackhole bridge.

Flow (Torrent Blackhole pattern):
  1. Sonarr/Radarr write <release>.magnet into /data/blackhole/{sonarr,radarr}/
  2. We add the magnet to Seedr      -> POST /rest/transfer/magnet  (field: magnet)
  3. We poll the root listing until the transfer finishes
     (an in-progress entry lives in root["torrents"]; when done it becomes a
      top-level folder in root["folders"] whose "path" == the transfer title).
  4. We download the finished files   -> GET /rest/file/{id} (302 -> CDN bytes)
     into /data/downloads/{sonarr,radarr}/<title>/  (the *arr "watch" folder).
  5. We delete the item from Seedr     -> DELETE /rest/folder/{id}   to free space.

API contract verified live against the account (Seedr does not publish a schema):
  root/subfolder listing -> {folders:[{id, path, size}], files:[{id, name, size}],
                             torrents:[{id, name, progress, ...}]}
  add magnet response    -> {user_torrent_id, title, torrent_hash, success}
Auth: HTTP Basic. Password is provided base64-encoded (SEEDR_PASSWORD_B64) to
survive Docker/shell '$' interpolation; plain SEEDR_PASSWORD also accepted.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import time
import pathlib
import urllib.parse
import requests

from common import clog, norm, ntfy

FAIL_COUNTS = {}  # magnet filename -> consecutive 413 attempts (in-memory)
TOMBSTONE_GRACE = (
    600  # secs to keep reaping a superseded release's late-appearing Seedr folder
)


def log(msg):
    clog("seedr", msg)


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


EMAIL = os.environ["SEEDR_EMAIL"]
_pw_b64 = os.environ.get("SEEDR_PASSWORD_B64")
PASSWORD = (
    base64.b64decode(_pw_b64).decode() if _pw_b64 else os.environ["SEEDR_PASSWORD"]
)
POLL = int(os.environ.get("POLL_INTERVAL", "30"))

BASE = "https://www.seedr.cc/rest"
DATA = pathlib.Path("/data")
STATE_DIR = DATA / "blackhole" / ".bridge-state"
CATEGORIES = ["sonarr", "radarr"]

# Stalled-download handling: if a transfer's progress hasn't advanced for this long,
# fail it in Radarr/Sonarr (-> blocklist + auto re-grab a different release) and notify.
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "2700"))  # 45 min
FETCH_SKIP_MAX = int(
    os.environ.get("FETCH_SKIP_MAX", str(1 << 20))
)  # <1 MB: skip an unfetchable junk sidecar instead of failing the whole folder
# Fetch-failure handling: a transfer Seedr reports complete but whose media bytes its CDN
# won't serve (a persistent 404 from a storage node) would otherwise retry-fetch forever,
# hogging Seedr space and never failing in the *arr. After this long failing to fetch, give
# up: purge it from Seedr, fail it in Radarr/Sonarr (-> blocklist + re-grab a different
# release) and notify. The post-completion analogue of STALL_TIMEOUT.
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "5400"))  # 90 min
# Missing-result handling: a transfer that left root["torrents"] (= finished) but whose
# folder/file never shows up (Seedr deduped or dropped it -- seen when a single-episode
# torrent finished alongside a pack holding the same file) would otherwise log "result not
# visible yet" every poll forever. After this long, tombstone it and fail it in the *arr.
RESULT_TIMEOUT = int(os.environ.get("RESULT_TIMEOUT", "1800"))  # 30 min
ARR = {
    "radarr": (os.environ.get("RADARR_URL"), os.environ.get("RADARR_API_KEY")),
    "sonarr": (os.environ.get("SONARR_URL"), os.environ.get("SONARR_API_KEY")),
}

SESSION = requests.Session()
SESSION.auth = (EMAIL, PASSWORD)
SESSION.headers["User-Agent"] = "seedr-bridge/1.0"


def api(method, path, **kw):
    r = SESSION.request(method, f"{BASE}{path}", timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r


def list_folder(folder_id=""):
    return api("GET", f"/folder/{folder_id}" if folder_id != "" else "/folder").json()


def add_magnet(magnet):
    return api("POST", "/transfer/magnet", data={"magnet": magnet}).json()


def _bdecode(data, i):
    """Minimal bencode decoder -> (value, next_index). Bytes stay bytes."""
    c = data[i : i + 1]
    if c == b"i":
        e = data.index(b"e", i)
        return int(data[i + 1 : e]), e + 1
    if c == b"l":
        i += 1
        lst = []
        while data[i : i + 1] != b"e":
            v, i = _bdecode(data, i)
            lst.append(v)
        return lst, i + 1
    if c == b"d":
        i += 1
        d = {}
        while data[i : i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            d[k] = v
        return d, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        ln = int(data[i:colon])
        start = colon + 1
        return data[start : start + ln], start + ln
    raise ValueError(f"bad bencode byte {c!r} at offset {i}")


def magnet_from_torrent(path):
    """Build a magnet URI from a .torrent file. Some indexers (e.g. YTS) hand
    Radarr/Sonarr a real .torrent rather than a magnet, which the blackhole saves
    as <release>.torrent. Seedr's add endpoint is magnet-only, so we synthesize one:
    btih = SHA1 of the *raw* bencoded info-dict bytes (re-encoding could reorder keys
    and change the hash), name from info['name'], trackers from announce/announce-list."""
    data = path.read_bytes()
    if data[:1] != b"d":
        raise ValueError("not a bencoded dict")
    i = 1
    info_raw = name = None
    trackers = []
    while data[i : i + 1] != b"e":
        k, i = _bdecode(data, i)
        if k == b"info":
            start = i
            v, i = _bdecode(data, i)
            info_raw = data[start:i]
            name = v.get(b"name")
        else:
            v, i = _bdecode(data, i)
            if k == b"announce" and isinstance(v, bytes):
                trackers.append(v)
            elif k == b"announce-list" and isinstance(v, list):
                trackers += [t for tier in v for t in tier if isinstance(t, bytes)]
    if info_raw is None:
        raise ValueError("no info dict")
    btih = hashlib.sha1(info_raw).hexdigest()
    dn = (name or path.stem.encode()).decode("utf-8", "replace")
    q = urllib.parse.quote_plus
    parts = [f"xt=urn:btih:{btih}", f"dn={q(dn)}"]  # xt urn must stay literal
    parts += [f"tr={q(t.decode('utf-8', 'replace'))}" for t in trackers]
    return "magnet:?" + "&".join(parts)


def download_file(file_id, dest: pathlib.Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with SESSION.get(
        f"{BASE}/file/{file_id}", stream=True, timeout=900, allow_redirects=True
    ) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
    tmp.rename(dest)
    log(f"    downloaded {dest.name} ({dest.stat().st_size:,} bytes)")


def fetch_folder_recursive(folder_id, dest_root: pathlib.Path):
    listing = list_folder(folder_id)
    for f in listing.get("files", []):
        dest = dest_root / f["name"]
        # Resume support: if a previous pass already pulled this file in full,
        # skip it. Without this, one dropped connection on a big 4K file made
        # the whole folder re-download from scratch (thrashing 20+ GB packs).
        size = f.get("size")
        if dest.exists() and size and dest.stat().st_size == size:
            continue
        try:
            download_file(f["id"], dest)
        except Exception as e:
            # Junk sidecars (YTSProxies.com.txt, "Torrent Downloaded From*.txt",
            # RARBG.nfo, ...) sometimes 404 on Seedr's CDN. They're not media and
            # the *arr import ignores them, so a small file we can't fetch must
            # NOT poison the whole folder (which would block delete_folder + the
            # import forever and re-thrash the big mkv). Skip it; re-raise on any
            # substantial file so real media still benefits from retry/resume.
            if size and size < FETCH_SKIP_MAX:
                log(f"    skipping unfetchable sidecar {f['name']} ({size:,} B): {e}")
                continue
            raise
    for sub in listing.get("folders", []):
        fetch_folder_recursive(sub["id"], dest_root / sub["path"])


def delete_folder(folder_id):
    try:
        api("DELETE", f"/folder/{folder_id}")
    except Exception as e:
        log(f"    (warning) could not delete Seedr folder {folder_id}: {e}")


def delete_file(file_id):
    try:
        api("DELETE", f"/file/{file_id}")
    except Exception as e:
        log(f"    (warning) could not delete Seedr file {file_id}: {e}")


def delete_transfer(tid):
    # in-progress torrents are removed via /transfer/{user_torrent_id}
    # (a finished one becomes a folder, removed via delete_folder instead).
    try:
        api("DELETE", f"/transfer/{tid}")
    except Exception as e:
        log(f"    (warning) could not delete Seedr transfer {tid}: {e}")


def _name_matches(seedr_name, title):
    # True when seedr_name and title are (almost certainly) the SAME release.
    # norm() drops punctuation, so Seedr's sanitization (dashes/spaces/appended group
    # tags) mostly reduces to the shorter name appearing verbatim inside the longer.
    # It must be a SUBSTRING, not a prefix: Seedr names the result after the torrent's
    # internal name, which for scraped sites carries a leading site tag the magnet's
    # `dn=` (and hence our stored title) lacks -- e.g. Seedr's
    # 'www.8MovieRulz.com - Mental Madhilo (2017) 1080p ... ESub.mkv' vs our
    # 'Mental Madhilo (2017) 1080p ...'. We compare the FULL shorter string -- NOT a
    # fixed-length prefix -- so two DIFFERENT releases of the same movie (which share
    # only the 'title+year' head, e.g. ...DTS vs ...BRRip) do NOT match. This
    # distinction is what the supersede reaper's safety guard relies on.
    a, b = norm(seedr_name), norm(title)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 18 and shorter in longer


def cleanup_local_partial(cat, old_title, new_title=None, old_release=None):
    # remove a superseded release's partially-fetched download dir. The dir is named after
    # the *arr release (see check_completions) -- or, for dirs written before that, after the
    # Seedr folder (often sanitized) -- so match either name fuzzily.
    base = DATA / "downloads" / cat
    if not base.exists():
        return
    for d in base.iterdir():
        if not d.is_dir():
            continue
        if new_title and _name_matches(
            d.name, new_title
        ):  # never the replacement's dir
            continue
        if _name_matches(d.name, old_title) or (
            old_release and _name_matches(d.name, old_release)
        ):
            shutil.rmtree(d, ignore_errors=True)
            log(f"    [override] removed local partial '{d.name}'")


def purge_superseded(cat, new_key, new_name, root):
    """Mark every in-progress Radarr release of the SAME movie as `new_key` (but a
    DIFFERENT release than `new_name`) for deferred cleanup: delete its transfer now
    (frees in-progress space), drop any local partial, and rewrite its state file as a
    tombstone so check_completions reaps the (often late-appearing) Seedr folder.
    Returns the number of releases superseded."""
    if not new_key or not STATE_DIR.exists():
        return 0
    n = 0
    for sp in STATE_DIR.glob("*.json"):
        try:
            rec = json.loads(sp.read_text())
        except Exception:
            continue
        if rec.get("category") != cat or rec.get("superseded"):
            continue
        if norm(rec.get("title", "")) == norm(new_name):  # same release -> leave it
            continue
        if movie_key(rec.get("title", "")) != new_key:  # different movie -> leave it
            continue
        tid = rec.get("user_torrent_id")
        if tid is not None:
            delete_transfer(tid)
        cleanup_local_partial(cat, rec["title"], new_name, rec.get("release_name"))
        rec.update(superseded=True, new_title=new_name, superseded_at=time.time())
        sp.write_text(json.dumps(rec))  # tombstone -> reaped by check_completions
        n += 1
        log(
            f"    [override] superseded '{rec['title']}' (transfer {tid}); deferring folder cleanup"
        )
    return n


def notify(title, body):
    ntfy(title, body, priority="high", tags="warning")


def arr_mark_failed(cat, release_name):
    """Find the most recent 'grabbed' history record for release_name and mark it failed
    -> Radarr/Sonarr blocklists it and (autoRedownloadFailed) grabs a different release.
    Uses plain requests, NOT SESSION (which carries Seedr Basic-auth we must not leak)."""
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


def handle_stalled(cat, rec, sp, prog):
    title = rec["title"]
    release = rec.get("release_name", title)
    log(
        f"[{cat}] '{title}' STALLED on Seedr ({prog}% for >{STALL_TIMEOUT // 60}min) -> failing & retrying"
    )
    tid = rec.get("user_torrent_id")
    if tid is not None:
        delete_transfer(tid)  # free the dead transfer
    retried = arr_mark_failed(cat, release)
    notify(
        f"Stalled: {release}",
        f"Stuck at {prog}% for {STALL_TIMEOUT // 60} min on Seedr. "
        + (
            "Asked Radarr/Sonarr to grab a different release."
            if retried
            else "Could not reach Radarr/Sonarr -- override it manually."
        ),
    )
    # tombstone -> existing reaper cleans any partial folder/local dir and we stop re-failing
    rec.update(superseded=True, new_title=None, superseded_at=time.time(), stalled=True)
    sp.write_text(json.dumps(rec))
    cleanup_local_partial(cat, title, old_release=release)


def handle_missing_result(cat, rec, sp):
    """A finished transfer whose result never appeared in Seedr. Start a deadline on the
    first miss; once RESULT_TIMEOUT elapses, fail it in Radarr/Sonarr and tombstone it, so
    a late-appearing result is reaped instead of looping forever. A successful match
    unlinks the state file, so the deadline never outlives a delivery."""
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
    log(
        f"[{cat}] '{title}' finished but result MISSING from Seedr for >{RESULT_TIMEOUT // 60}min -> failing"
    )
    retried = arr_mark_failed(cat, release)
    notify(
        f"Missing result: {release}",
        f"Seedr finished it but never produced a file for {RESULT_TIMEOUT // 60} min. "
        + (
            "Marked failed in Radarr/Sonarr (it re-grabs if still wanted)."
            if retried
            else "Could not reach Radarr/Sonarr -- check it manually."
        ),
    )
    rec.update(superseded=True, new_title=None, superseded_at=now, missing_result=True)
    sp.write_text(json.dumps(rec))
    cleanup_local_partial(cat, title, old_release=release)


def handle_fetch_failure(cat, rec, sp, err, folder_id=None, file_id=None):
    """A transfer Seedr marked complete but whose bytes its CDN keeps 404ing on fetch.
    Start a deadline on first failure; once FETCH_TIMEOUT elapses, stop retrying forever:
    purge the dead Seedr result, fail it in Radarr/Sonarr (-> blocklist + auto re-grab a
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
    log(
        f"[{cat}] '{title}' UNFETCHABLE (Seedr CDN failing >{FETCH_TIMEOUT // 60}min: {err}) -> failing & retrying"
    )
    if folder_id is not None:
        delete_folder(folder_id)
    if file_id is not None:
        delete_file(file_id)
    retried = arr_mark_failed(cat, release)
    notify(
        f"Unfetchable: {release}",
        f"Seedr reported it complete but its CDN kept 404ing for {FETCH_TIMEOUT // 60} min. "
        + (
            "Asked Radarr/Sonarr to grab a different release."
            if retried
            else "Could not reach Radarr/Sonarr -- override it manually."
        ),
    )
    # tombstone -> reaper cleans any leftover Seedr footprint / local partial and we stop retrying
    rec.update(superseded=True, new_title=None, superseded_at=now, unfetchable=True)
    sp.write_text(json.dumps(rec))
    cleanup_local_partial(cat, title, old_release=release)


def state_file(category, title):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = base64.urlsafe_b64encode(f"{category}|{title}".encode()).decode()
    return STATE_DIR / f"{safe}.json"


def _magnet_btih(magnet):
    """Infohash from a magnet's xt=urn:btih: (hex, lowercased). Base32 form is converted."""
    m = re.search(r"xt=urn:btih:([0-9a-zA-Z]+)", magnet or "")
    if not m:
        return ""
    h = m.group(1)
    if len(h) == 32:
        try:
            h = base64.b32decode(h.upper()).hex()
        except Exception:
            return ""
    return h.lower()


def adopt_existing(cat, mfile, magnet, nkey):
    """Seedr answered 409 Conflict: it ALREADY holds this torrent -- an out-of-band add
    (adding the magnet by hand on seedr.cc is a normal thing to do) or an earlier loop
    whose response we lost. A 409 has no state file behind it, and check_completions()
    only ever looks at state files, so the result would never be fetched while the magnet
    409s again on every single poll -- forever. Adopt the transfer instead: write exactly
    the record a successful add would have.

    Matched by infohash against the in-progress list (exact, unlike the name matcher);
    if it already finished it is no longer a torrent, so fall back to _name_matches over
    the folders and bare root files -- the same two shapes check_completions() handles.
    Returns True if the drop was adopted (and the magnet consumed)."""
    btih = _magnet_btih(magnet)
    try:
        root = list_folder()
    except Exception as e:
        log(f"    409 but listing Seedr failed ({e}); will retry next loop")
        return False
    title, where = mfile.stem, ""
    tid = thash = None
    for t in root.get("torrents", []):
        if btih and (t.get("hash") or "").lower() == btih:
            tid, thash = t.get("id"), t.get("hash")
            where = f"in progress {t.get('progress', 0):.0f}%"
            break
    if not where:
        # already complete -> a top-level folder, or a bare root file for single-file torrents
        for f in root.get("folders", []):
            if _name_matches(f.get("path", ""), title):
                where = "already complete (folder)"
                break
        if not where:
            for f in root.get("files", []):
                if _name_matches(f.get("name", ""), title):
                    where = "already complete (root file)"
                    break
    if not where:
        log("    409 but no matching Seedr transfer found; will retry next loop")
        return False
    if nkey:
        purge_superseded(cat, nkey, mfile.stem, root)
    rec = {
        "category": cat,
        "title": title,
        "release_name": mfile.stem,
        "user_torrent_id": tid,
        "torrent_hash": thash or btih or None,
        "added": time.time(),
    }
    state_file(cat, title).write_text(json.dumps(rec))
    mfile.unlink()
    log(f"    already on Seedr ({where}) -> adopted as '{title}'")
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
            if mfile.suffix == ".torrent":
                # Indexers that hand the *arr a real .torrent (YTS, etc.) land here;
                # Seedr only takes magnets, so synthesize one from the file.
                try:
                    magnet = magnet_from_torrent(mfile)
                except Exception as e:
                    log(f"[{cat}] cannot parse {mfile.name} ({e}); skipping")
                    continue
            else:
                magnet = mfile.read_text().strip()
                if not magnet.startswith("magnet:"):
                    continue
            # movie identity (Radarr only) -> lets a new release supersede an old
            # in-progress 'zombie' transfer of the SAME movie.
            nkey = movie_key(mfile.stem) if cat == "radarr" else None
            log(f"[{cat}] adding {mfile.name}")
            try:
                resp = add_magnet(magnet)
            except requests.HTTPError as e:
                if (
                    getattr(e, "response", None) is not None
                    and e.response.status_code == 413
                ):
                    try:
                        r = list_folder()
                        free = r.get("space_max", 0) - r.get("space_used", 0)
                    except Exception:
                        r, free = {}, 0
                    # override: a same-movie zombie is hogging Seedr -> free it and
                    # retry the add next loop instead of dropping this one as too large.
                    if nkey and purge_superseded(cat, nkey, mfile.stem, r) > 0:
                        FAIL_COUNTS.pop(mfile.name, None)
                        log(
                            f"    [override] freed Seedr space from old release; will retry add for {mfile.name}"
                        )
                        continue
                    n = FAIL_COUNTS.get(mfile.name, 0) + 1
                    FAIL_COUNTS[mfile.name] = n
                    # plenty of free space yet still 413 => the torrent itself is
                    # bigger than the account; it will never fit -> drop it.
                    if free > 20e9 or n >= 5:
                        log(
                            f"    '{mfile.name}' does not fit Seedr ({free / 1e9:.0f}GB free) -> dropping (too large)"
                        )
                        mfile.unlink()
                        FAIL_COUNTS.pop(mfile.name, None)
                    else:
                        log(
                            f"    add 413 (Seedr full, {free / 1e9:.0f}GB free); attempt {n}/5, will retry"
                        )
                    continue
                if (
                    getattr(e, "response", None) is not None
                    and e.response.status_code == 409
                    and adopt_existing(cat, mfile, magnet, nkey)
                ):
                    continue
                log(f"    add failed ({e}); will retry next loop")
                continue
            except Exception as e:
                log(f"    add failed ({e}); will retry next loop")
                continue
            FAIL_COUNTS.pop(mfile.name, None)
            if not resp.get("success", True):
                log(f"    Seedr rejected magnet: {resp}; dropping {mfile.name}")
                mfile.unlink()
                continue
            title = resp.get("title") or mfile.stem
            # override: drop any old in-progress release of the same movie.
            if nkey:
                purge_superseded(cat, nkey, mfile.stem, list_folder())
            rec = {
                "category": cat,
                "title": title,
                "release_name": mfile.stem,
                "user_torrent_id": resp.get("user_torrent_id"),
                "torrent_hash": resp.get("torrent_hash"),
                "added": time.time(),
            }
            state_file(cat, title).write_text(json.dumps(rec))
            mfile.unlink()
            log(f"    queued on Seedr as '{title}'")


def check_completions():
    if not STATE_DIR.exists() or not any(STATE_DIR.glob("*.json")):
        return
    root = list_folder()
    folders = {f["path"]: f["id"] for f in root.get("folders", [])}
    # In-progress transfers. Seedr keeps a torrent here (and surfaces its
    # destination folder early, populating files as they finish) until it
    # reaches 100%, at which point it disappears from this list. So the ONLY
    # reliable "done" signal is the torrent leaving root["torrents"] -- NOT the
    # mere existence of the folder (that bug grabbed a half-finished folder).
    torrents_by_id = {t.get("id"): t for t in root.get("torrents", [])}
    torrents_by_name = {t.get("name"): t for t in root.get("torrents", [])}
    for sp in sorted(STATE_DIR.glob("*.json")):
        rec = json.loads(sp.read_text())
        title, cat = rec["title"], rec["category"]
        # superseded release -> reap its (often late-appearing) Seedr footprint, never
        # the replacement's, until the grace window closes.
        if rec.get("superseded"):
            new_title = rec.get("new_title")
            for name, fid in list(folders.items()):
                if _name_matches(name, title) and not (
                    new_title and _name_matches(name, new_title)
                ):
                    log(f"    [override] cleaning leftover Seedr folder '{name}'")
                    delete_folder(fid)
            for f in root.get("files", []):
                nm = f.get("name", "")
                if _name_matches(nm, title) and not (
                    new_title and _name_matches(nm, new_title)
                ):
                    delete_file(f["id"])
            cleanup_local_partial(cat, title, new_title, rec.get("release_name"))
            if time.time() - rec.get("superseded_at", 0) > TOMBSTONE_GRACE:
                sp.unlink()
                log(f"    [override] cleanup window closed for '{title}'")
            continue
        tid = rec.get("user_torrent_id")
        t = torrents_by_id.get(tid) or torrents_by_name.get(title)
        if t is not None:
            prog, now = round(t.get("progress", 0), 1), time.time()
            log(f"[{cat}] '{title}' downloading on Seedr ({prog}%)")
            if rec.get("last_progress") is None or prog > rec["last_progress"] + 0.05:
                rec.update(
                    last_progress=prog, last_progress_at=now
                )  # advanced -> reset stall clock
                sp.write_text(json.dumps(rec))
            elif (
                now - rec.get("last_progress_at", rec.get("added", now)) > STALL_TIMEOUT
            ):
                handle_stalled(cat, rec, sp, prog)
            continue  # still in progress -> do NOT fetch yet
        # torrent finished (gone from in-progress) -> locate its result.
        # (a) multi-file / wrapped torrents land as a top-level FOLDER
        folder_id = folders.get(title)
        if folder_id is None:  # tolerate Seedr sanitizing / site-prefixing the name
            for name, fid in folders.items():
                if _name_matches(name, title):
                    folder_id, title = fid, name
                    break
        # Deliver under the release name the *arr GRABBED, not Seedr's name for the result: the
        # *arr maps a watch-folder item to its series/movie by parsing the folder name, and
        # Seedr's name is the torrent's raw internal one ('www.1TamilMV.promo - Heart Beat
        # (2026) S03 EP (01-04) ...'), which can map to nothing -- or to the wrong show.
        local = rec.get("release_name") or title
        if folder_id is not None:
            dest = DATA / "downloads" / cat / local
            log(f"[{cat}] '{title}' complete -> fetching folder to {dest}")
            try:
                fetch_folder_recursive(folder_id, dest)
                delete_folder(folder_id)
                sp.unlink()
                log(f"[{cat}] '{title}' delivered.")
                log(f"[{cat}] '{title}' downloaded to {dest}")
            except Exception as e:
                log(f"    fetch failed for '{title}': {e}; will retry")
                handle_fetch_failure(cat, rec, sp, str(e), folder_id=folder_id)
            continue
        # (b) bare single-file torrents land as a ROOT FILE (no folder)
        match = None
        for f in root.get("files", []):
            if _name_matches(os.path.splitext(f.get("name", ""))[0], title):
                match = f
                break
        if match is not None:
            dest = DATA / "downloads" / cat / local / match["name"]
            log(f"[{cat}] '{title}' complete (single file) -> fetching {match['name']}")
            try:
                download_file(match["id"], dest)
                delete_file(match["id"])
                sp.unlink()
                log(f"[{cat}] '{title}' delivered.")
                log(f"[{cat}] '{title}' downloaded to {dest}")
            except Exception as e:
                log(f"    fetch failed for '{title}': {e}; will retry")
                handle_fetch_failure(cat, rec, sp, str(e), file_id=match["id"])
            continue
        log(f"[{cat}] '{title}' finished but result not visible yet; will retry")
        handle_missing_result(cat, rec, sp)


def run_forever():
    log(f"seedr-bridge starting; polling every {POLL}s")
    try:
        root = list_folder()
        used = root.get("space_used", 0) / 1e9
        cap = root.get("space_max", 0) / 1e9
        log(f"Seedr auth OK; storage {used:.1f}/{cap:.1f} GB used")
    except Exception as e:
        log(f"FATAL: cannot reach Seedr REST API: {e}")
    while True:
        try:
            process_new_magnets()
            check_completions()
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    run_forever()
