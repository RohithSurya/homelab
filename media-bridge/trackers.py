"""
Extra public trackers for PUBLIC torrents, from ngosang/trackerslist (a daily-tested list).

Scraped sites' magnets (movierulz, tamilmv) often carry few or dead trackers, so swarms are found
slowly or not at all. Adding the list's best ~20 trackers gives the backend more places to find
peers. Announcing happens from Seedr / the seedbox, never from the Jetson.

NEVER for a private torrent: announcing a private infohash to public trackers leaks it outside
the private tracker, and trackers ban for that. bridge.py only calls this for drops that are
public by every check (no private flag in the .torrent, indexer not a SEED_RULES tracker).

The list is fetched at most once per PUBLIC_TRACKERS_REFRESH (plain HTTPS to GitHub) and cached
in /config; a failed fetch keeps the cached copy, and with no cache nothing is added.
"""

import os
import pathlib
import threading
import time
import urllib.parse

import requests

from common import clog

ENABLED = os.environ.get("PUBLIC_EXTRA_TRACKERS", "on").lower() not in ("off", "false", "0")
URL = os.environ.get(
    "PUBLIC_TRACKERS_URL",
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
)
CACHE = pathlib.Path(os.environ.get("PUBLIC_TRACKERS_CACHE", "/config/trackers_best.txt"))
REFRESH = int(os.environ.get("PUBLIC_TRACKERS_REFRESH", "86400"))
SCHEMES = ("udp://", "http://", "https://", "wss://")

_lock = threading.Lock()
_list = None
_loaded_at = 0.0


def log(msg):
    clog("bridge", msg)


def _parse(text):
    out = []
    for line in text.splitlines():
        t = line.strip()
        if t.startswith(SCHEMES) and t not in out:
            out.append(t)
    return out


def get():
    """The current list (possibly empty). Never raises."""
    global _list, _loaded_at
    if not ENABLED:
        return []
    with _lock:
        if _list is not None and time.time() - _loaded_at < REFRESH:
            return _list
        try:
            fresh = CACHE.exists() and time.time() - CACHE.stat().st_mtime < REFRESH
            if not fresh:
                r = requests.get(URL, timeout=20)
                r.raise_for_status()
                got = _parse(r.text)
                if got:
                    CACHE.write_text("\n".join(got) + "\n")
                    log(f"public tracker list refreshed: {len(got)} trackers")
            _list = _parse(CACHE.read_text()) if CACHE.exists() else []
        except Exception as e:
            log(f"    (warn) public tracker list fetch failed ({e}); using {'cache' if CACHE.exists() else 'none'}")
            try:
                _list = _parse(CACHE.read_text()) if CACHE.exists() else []
            except OSError:
                _list = []
        _loaded_at = time.time()
        return _list


def add_to_magnet(magnet, trackers):
    """magnet with every tracker it doesn't already have appended as &tr=. Returns (magnet, n added)."""
    if not trackers:
        return magnet, 0
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    have = {t.strip() for t in q.get("tr", [])}
    new = [t for t in trackers if t not in have]
    if not new:
        return magnet, 0
    return magnet + "".join("&tr=" + urllib.parse.quote(t, safe="") for t in new), len(new)
