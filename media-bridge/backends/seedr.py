"""
Seedr backend for bridge.py (Seedr cloud torrenting over its REST API).

  add magnet        -> POST /rest/transfer/magnet  (field: magnet)
  add .torrent      -> POST /rest/transfer/file    (field: file; same response shape)
  progress          -> root listing: an in-progress entry lives in root["torrents"]; when done
                       it becomes a top-level folder in root["folders"] (multi-file) or a bare
                       root file in root["files"] (single-file)
  fetch             -> GET /rest/file/{id} (302 -> CDN bytes), FTPS fallback (seedr_ftp)
  delete            -> DELETE /rest/folder/{id} | /rest/file/{id} | /rest/transfer/{id}

API contract verified live against the account (Seedr does not publish a schema):
  root/subfolder listing -> {folders:[{id, path, size}], files:[{id, name, size}],
                             torrents:[{id, name, progress, hash, ...}]}
  add response           -> {user_torrent_id, title, torrent_hash, success}
Auth: HTTP Basic. Password is provided base64-encoded (SEEDR_PASSWORD_B64) to
survive Docker/shell '$' interpolation; plain SEEDR_PASSWORD also accepted.
"""

import base64
import os
import pathlib
import time

import requests

import seedr_ftp
from backends.base import (
    DONE,
    DOWNLOADING,
    NOT_VISIBLE,
    Backend,
    Exists,
    Full,
    Rejected,
)
from common import clog, name_matches

EMAIL = os.environ["SEEDR_EMAIL"]
_pw_b64 = os.environ.get("SEEDR_PASSWORD_B64")
PASSWORD = (
    base64.b64decode(_pw_b64).decode() if _pw_b64 else os.environ["SEEDR_PASSWORD"]
)
BASE = "https://www.seedr.cc/rest"
FETCH_SKIP_MAX = int(
    os.environ.get("FETCH_SKIP_MAX", str(1 << 20))
)  # <1 MB: skip an unfetchable junk sidecar instead of failing the whole folder
# A private-tracker result stays on Seedr this long after delivery so Seedr keeps seeding it
# (the plan seeds 2:1 or 48h, and only while the files exist). 49h = 48h + margin; delivery
# is already after completion, so the tracker never sees less than 48h.
SEED_HOLD = int(os.environ.get("SEED_HOLD", "176400"))
# What the Seedr plan itself seeds a private torrent for, at most (Pro: 2:1 or 48h).
SEEDR_MAX_SEED_HOURS = float(os.environ.get("SEEDR_MAX_SEED_HOURS", "48"))

# Seedr Basic-auth lives on this session only -- never reuse it for anything else.
SESSION = requests.Session()
SESSION.auth = (EMAIL, PASSWORD)
SESSION.headers["User-Agent"] = "seedr-bridge/1.0"


def log(msg):
    clog("seedr", msg)


def api(method, path, **kw):
    r = SESSION.request(method, f"{BASE}{path}", timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r


def list_folder(folder_id=""):
    return api("GET", f"/folder/{folder_id}" if folder_id != "" else "/folder").json()


def add_magnet(magnet):
    return api("POST", "/transfer/magnet", data={"magnet": magnet}).json()


def add_torrent_file(path):
    # Same response shape as add_magnet. Uploading the file (not a synthesized magnet) keeps
    # the info dict intact -- incl. the private flag -- and the passkeyed announce list.
    with open(path, "rb") as f:
        return api(
            "POST",
            "/transfer/file",
            files={"file": (path.name, f, "application/x-bittorrent")},
        ).json()


def download_file(file_id, dest: pathlib.Path, remote=None, size=None):
    """HTTPS (CDN) first; on any failure fall back to FTP for the same file (seedr_ftp:
    serves files the CDN 404s on, and resumes at a byte offset). A .part left by an
    earlier attempt goes straight to FTP so it is continued instead of truncated."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    ftp_ok = remote is not None and seedr_ftp.enabled()
    if size and tmp.exists() and tmp.stat().st_size == size:
        pass  # finished last time, died before the rename -> nothing left to fetch
    elif ftp_ok and tmp.exists() and tmp.stat().st_size:
        log(f"    resuming {dest.name} over FTP at {tmp.stat().st_size:,} B")
        seedr_ftp.download(remote, tmp)
    else:
        try:
            with SESSION.get(
                f"{BASE}/file/{file_id}", stream=True, timeout=900, allow_redirects=True
            ) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            if not ftp_ok:
                raise
            log(f"    HTTPS fetch of {dest.name} failed ({e}) -> FTP fallback")
            seedr_ftp.download(remote, tmp)
    got = tmp.stat().st_size
    if size and got != size:
        if got > size:  # corrupt, not merely short -> don't resume onto it
            tmp.unlink()
        raise RuntimeError(f"{dest.name}: got {got:,} B, expected {size:,} B")
    tmp.rename(dest)
    log(f"    downloaded {dest.name} ({dest.stat().st_size:,} bytes)")


def fetch_folder_recursive(folder_id, dest_root: pathlib.Path, remote=None):
    # remote = this folder's FTP path, for download_file's fallback; the top call takes
    # it from the listing (a top-level folder's path is its name at the FTP root).
    listing = list_folder(folder_id)
    if remote is None:
        remote = "/" + listing.get("path", "")
    for f in listing.get("files", []):
        dest = dest_root / f["name"]
        # Resume support: if a previous pass already pulled this file in full,
        # skip it. Without this, one dropped connection on a big 4K file made
        # the whole folder re-download from scratch (thrashing 20+ GB packs).
        size = f.get("size")
        if dest.exists() and size and dest.stat().st_size == size:
            continue
        try:
            download_file(f["id"], dest, f"{remote}/{f['name']}", size)
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
        # a subfolder's "path" is its FULL path from the Seedr root ('Top/Sub/Deeper'),
        # not its own name -- joining it whole nested the torrent's tree inside itself
        name = sub["path"].rsplit("/", 1)[-1]
        fetch_folder_recursive(sub["id"], dest_root / name, f"{remote}/{name}")


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


def _delete_result(result):
    if result is None:
        return
    if result["kind"] == "folder":
        delete_folder(result["id"])
    else:
        delete_file(result["id"])


class SeedrBackend(Backend):
    name = "seedr"
    label = "Seedr"
    tag = "seedr"
    max_seed_hours = SEEDR_MAX_SEED_HOURS
    full_retries = 5
    fetch_fail_log = "Seedr CDN failing"
    fetch_fail_note = "Seedr reported it complete but its CDN kept 404ing"

    def __init__(self):
        self.root = {}
        self.folders = {}
        self.torrents_by_id = {}
        self.torrents_by_name = {}
        self.held = set()

    def startup(self):
        try:
            root = list_folder()
            used = root.get("space_used", 0) / 1e9
            cap = root.get("space_max", 0) / 1e9
            log(f"Seedr auth OK; storage {used:.1f}/{cap:.1f} GB used")
        except Exception as e:
            log(f"FATAL: cannot reach Seedr REST API: {e}")

    def add(self, drop, magnet):
        try:
            if drop.suffix == ".torrent":
                resp = add_torrent_file(drop)
            else:
                resp = add_magnet(magnet)
        except requests.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code == 413:
                try:
                    r = list_folder()
                    free = r.get("space_max", 0) - r.get("space_used", 0)
                except Exception:
                    free = 0
                # plenty of free space yet still 413 => the torrent itself is bigger than
                # the account; it will never fit.
                raise Full(free, never_fits=free > 20e9) from e
            if code == 409:
                raise Exists() from e
            raise
        if not resp.get("success", True):
            raise Rejected(resp)
        return {
            "title": resp.get("title") or drop.stem,
            "user_torrent_id": resp.get("user_torrent_id"),
            "torrent_hash": resp.get("torrent_hash"),
        }

    def locate(self, btih, title):
        # Matched by infohash against the in-progress list (exact, unlike the name matcher);
        # if it already finished it is no longer a torrent, so fall back to name_matches over
        # the folders and bare root files -- the same two shapes status() handles.
        try:
            root = list_folder()
        except Exception as e:
            log(f"    409 but listing Seedr failed ({e}); will retry next loop")
            return None
        for t in root.get("torrents", []):
            if btih and (t.get("hash") or "").lower() == btih:
                keys = {"user_torrent_id": t.get("id"), "torrent_hash": t.get("hash")}
                return keys, f"in progress {t.get('progress', 0):.0f}%"
        keys = {"user_torrent_id": None, "torrent_hash": btih or None}
        for f in root.get("folders", []):
            if name_matches(f.get("path", ""), title):
                return keys, "already complete (folder)"
        for f in root.get("files", []):
            if name_matches(f.get("name", ""), title):
                return keys, "already complete (root file)"
        log("    409 but no matching Seedr transfer found; will retry next loop")
        return None

    def begin_loop(self, records):
        self.root = list_folder()
        self.folders = {f["path"]: f["id"] for f in self.root.get("folders", [])}
        # In-progress transfers. Seedr keeps a torrent here (and surfaces its
        # destination folder early, populating files as they finish) until it
        # reaches 100%, at which point it disappears from this list. So the ONLY
        # reliable "done" signal is the torrent leaving root["torrents"] -- NOT the
        # mere existence of the folder (that bug grabbed a half-finished folder).
        self.torrents_by_id = {t.get("id"): t for t in self.root.get("torrents", [])}
        self.torrents_by_name = {t.get("name"): t for t in self.root.get("torrents", [])}
        # Results held for private-tracker seeding: no name matching may ever touch them.
        self.held = {r.get("hold_folder_id") for _, r in records if r.get("hold_until")}
        self.held |= {r.get("hold_file_id") for _, r in records if r.get("hold_until")}
        self.held.discard(None)

    def status(self, rec):
        title = rec["title"]
        t = self.torrents_by_id.get(rec.get("user_torrent_id")) or self.torrents_by_name.get(title)
        if t is not None:
            return DOWNLOADING, round(t.get("progress", 0), 1)
        # finished (gone from in-progress) -> locate its result.
        # (a) multi-file / wrapped torrents land as a top-level FOLDER
        fid = self.folders.get(title)
        if fid is not None and fid not in self.held:
            return DONE, {"kind": "folder", "id": fid, "name": title}
        for name, fid in self.folders.items():  # tolerate Seedr sanitizing / site-prefixing
            if fid not in self.held and name_matches(name, title):
                return DONE, {"kind": "folder", "id": fid, "name": name}
        # (b) bare single-file torrents land as a ROOT FILE (no folder)
        for f in self.root.get("files", []):
            if f["id"] not in self.held and name_matches(os.path.splitext(f.get("name", ""))[0], title):
                return DONE, {"kind": "file", "id": f["id"], "name": f["name"], "size": f.get("size")}
        return NOT_VISIBLE, None

    def fetch(self, rec, result, dest_dir):
        if result["kind"] == "folder":
            fetch_folder_recursive(result["id"], dest_dir)
        else:
            download_file(result["id"], dest_dir / result["name"], f"/{result['name']}", result.get("size"))

    def cancel(self, rec):
        tid = rec.get("user_torrent_id")
        if tid is not None:
            delete_transfer(tid)

    def discard(self, rec, result):
        _delete_result(result)

    def start_hold(self, rec, result):
        # Seedr seeds only while the files exist and trackers punish hit-and-runs, so keep
        # the result until hold_until.
        until = time.time() + SEED_HOLD
        rec.update(
            hold_until=until,
            hold_folder_id=result["id"] if result and result["kind"] == "folder" else None,
            hold_file_id=result["id"] if result and result["kind"] == "file" else None,
        )
        return until

    def hold_over(self, rec):
        return time.time() >= rec["hold_until"]

    def end_hold(self, rec):
        if rec.get("hold_folder_id") is not None:
            delete_folder(rec["hold_folder_id"])
        if rec.get("hold_file_id") is not None:
            delete_file(rec["hold_file_id"])

    def reap(self, rec):
        # A superseded/stalled/missing release's Seedr folder often appears late; reap
        # anything that is it -- never the replacement's, never a held result.
        title, new_title = rec["title"], rec.get("new_title")
        for name, fid in list(self.folders.items()):
            if fid in self.held:
                continue
            if name_matches(name, title) and not (new_title and name_matches(name, new_title)):
                log(f"    [override] cleaning leftover Seedr folder '{name}'")
                delete_folder(fid)
        for f in self.root.get("files", []):
            if f["id"] in self.held:
                continue
            nm = f.get("name", "")
            if name_matches(nm, title) and not (new_title and name_matches(nm, new_title)):
                delete_file(f["id"])
