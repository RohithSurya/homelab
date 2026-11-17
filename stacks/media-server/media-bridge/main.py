#!/usr/bin/env python3
"""
media-bridge entrypoint.

Runs five custom services as supervised daemon threads inside
one container (all off-VPN, on the default bridge network):
  * bridge   -- Sonarr/Radarr blackhole bridge (30s poll) over pluggable download
               backends (backends/: Seedr, seedbox); BRIDGE_ROUTE picks one per drop.
               PULL_WORKERS puller threads pull finished transfers home in the background.
  * reaper   -- Jellyfin "watched movie" -> Radarr reaper   (6h)
  * movierulz-- 5movierulz crawler + Torznab feed for Prowlarr (crawl ~1h, HTTP server)
  * tamilmv  -- tamilmv forum crawler + Torznab feed :8005
  * fresh    -- Latest Releases: LLM-picked new Indian movies, kept a month (fresh.py; API /fresh on :8003)
  * youtube  -- web form (:8003) + worker: pull a YouTube full-movie into a Radarr movie
               folder via a disposable yt-dlp+ffmpeg container (no-torrent fallback)

Each unit runs in its own thread wrapped so a crash logs + ntfy-alerts + restarts
without taking down the others (threads are I/O-bound; requests releases the GIL,
so the bridge's large blocking downloads don't stall the Torznab server or crawler).
"""
import os
import threading
import time

import bridge
import puller
import reaper
import movierulz
import tamilmv
import youtube
import fresh
import fresh_en
from common import clog, ntfy
from components import COMPONENTS, enabled_components


def supervise(name, target):
    def wrap():
        while True:
            try:
                target()
                # run_forever() loops internally; returning means it stopped cleanly
                clog("main", f"{name} returned unexpectedly; restarting in 30s")
            except Exception as e:
                clog("main", f"{name} crashed: {e!r}; restarting in 30s")
                ntfy(f"media-bridge: {name} crashed", repr(e), priority="high", tags="warning")
            time.sleep(30)

    t = threading.Thread(target=wrap, name=name, daemon=True)
    t.start()
    return t


def main():
    parts = enabled_components(os.environ.get("MEDIA_BRIDGE_COMPONENTS", ""))
    off = [c for c in COMPONENTS if c not in parts]
    clog("main", "media-bridge starting: " + " + ".join(parts) + (f" (off: {', '.join(off)})" if off else ""))
    if "bridge" in parts:
        supervise("bridge", bridge.run_forever)
        for i in range(puller.WORKERS):  # pull finished transfers home in the background, side by side
            supervise(f"bridge-puller-{i + 1}", puller.run_worker)
    if "reaper" in parts:
        supervise("reaper", reaper.run_forever)
    if "movierulz" in parts:
        supervise("movierulz-crawler", movierulz.run_crawler)
        supervise("movierulz-torznab", movierulz.serve_torznab)
    if "tamilmv" in parts:
        supervise("tamilmv-crawler", tamilmv.run_crawler)
        supervise("tamilmv-backfill", tamilmv.run_backfill)  # full walk of every forum, plain HTTP only
        supervise("tamilmv-torznab", tamilmv.serve_torznab)
    if "youtube" in parts:
        supervise("youtube-worker", youtube.run_worker)
        supervise("youtube-server", youtube.serve)
    if "fresh" in parts:
        supervise("fresh", fresh.run_forever)  # Latest Releases; its API is served on the :8003 server
    if "fresh-en" in parts:
        supervise("fresh-en", fresh_en.run_forever)  # Latest Hollywood; API at /fresh-en on :8003
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
