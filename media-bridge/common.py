#!/usr/bin/env python3
"""Shared helpers for the media-bridge components (seedr, reaper, movierulz)."""
import os
import re
import time
import requests

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def clog(tag, msg):
    """Timestamped, component-tagged log line (e.g. '[12:00:00] [seedr] ...')."""
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def name_matches(result_name, title):
    # True when result_name and title are (almost certainly) the SAME release.
    # norm() drops punctuation, so a download service's sanitization (dashes/spaces/appended
    # group tags) mostly reduces to the shorter name appearing verbatim inside the longer.
    # It must be a SUBSTRING, not a prefix: Seedr names the result after the torrent's
    # internal name, which for scraped sites carries a leading site tag the magnet's
    # `dn=` (and hence our stored title) lacks -- e.g. Seedr's
    # 'www.8MovieRulz.com - Mental Madhilo (2017) 1080p ... ESub.mkv' vs our
    # 'Mental Madhilo (2017) 1080p ...'. We compare the FULL shorter string -- NOT a
    # fixed-length prefix -- so two DIFFERENT releases of the same movie (which share
    # only the 'title+year' head, e.g. ...DTS vs ...BRRip) do NOT match. This
    # distinction is what the supersede reaper's safety guard relies on.
    a, b = norm(result_name), norm(title)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 18 and shorter in longer


def ntfy(title, body, priority="default", tags="information"):
    """Best-effort ntfy push; silently no-ops if NTFY_TOPIC is unset."""
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode(),
                      headers={"Title": title, "Priority": priority, "Tags": tags}, timeout=15)
    except Exception as e:
        clog("ntfy", f"notify failed: {e}")
