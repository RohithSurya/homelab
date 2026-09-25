#!/usr/bin/env python3
"""
media-bridge entrypoint.

Runs six formerly-separate custom services as supervised daemon threads inside
one container (all off-VPN, on the default bridge network):
  * bridge   -- Sonarr/Radarr blackhole bridge (30s poll) over pluggable download
               backends (backends/: Seedr today); BRIDGE_ROUTE picks one per drop
  * reaper   -- Jellyfin "watched movie" -> Radarr reaper   (6h)
  * movierulz-- 5movierulz crawler + Torznab feed for Prowlarr (crawl ~1h, HTTP server)
  * tamilmv  -- tamilmv/tamilblasters forum crawler + Torznab feed :8005 (replaces MediaFusion)
  * daddylive-- DaddyLive resolver: M3U/EPG + on-demand stream resolve for Jellyfin (HTTP server)
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
import reaper
import movierulz
import tamilmv
import daddylive
import youtube
from common import clog, ntfy


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


# DaddyLive is switched off (2026-09-24) but kept in the tree; DADDYLIVE_ENABLED=true brings it back.
DADDYLIVE_ENABLED = os.environ.get("DADDYLIVE_ENABLED", "true").lower() == "true"


def main():
    parts = ["bridge", "reaper", "movierulz", "tamilmv"] + (["daddylive"] if DADDYLIVE_ENABLED else []) + ["youtube"]
    clog("main", "media-bridge starting: " + " + ".join(parts))
    supervise("bridge", bridge.run_forever)
    supervise("reaper", reaper.run_forever)
    supervise("movierulz-crawler", movierulz.run_crawler)
    supervise("movierulz-torznab", movierulz.serve_torznab)
    supervise("tamilmv-crawler", tamilmv.run_crawler)
    supervise("tamilmv-torznab", tamilmv.serve_torznab)
    if DADDYLIVE_ENABLED:
        supervise("daddylive-refresher", daddylive.run_refresher)
        supervise("daddylive-precheck", daddylive.run_precheck)
        supervise("daddylive-server", daddylive.serve)
    else:
        clog("main", "daddylive disabled (DADDYLIVE_ENABLED=false)")
    supervise("youtube-worker", youtube.run_worker)
    supervise("youtube-server", youtube.serve)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
