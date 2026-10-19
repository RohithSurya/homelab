#!/usr/bin/env python3
"""
tamilmv crawler -> Torznab feed for Prowlarr (host :8005).

Why this exists: this IPBoard forum (Tamil/Telugu/Hindi/Malayalam/Kannada/English movies +
web-series) was previously scraped by a compiled Rust spider inside the MediaFusion image. We
retired that whole 4-container stack; this reimplements the scraper ourselves -- the same
pattern as movierulz.py -- and drops MediaFusion. (tamilblasters, the second site MediaFusion
covered, was dead from 2026-08-23 and was removed on 2026-10-19.)

Design: ARCHIVE everything, EXTRACT from the archive, SERVE only what is flagged served.
  * run_crawler()   -- hourly head walk of the release forums (newest topics, by START date),
                       the daily sitemap sweep and forum discovery, active-series re-reads.
  * run_backfill()  -- its own thread: a resumable walk of EVERY forum to its last page (release
                       forums first), plain HTTP only; pauses instead of falling back to
                       FlareSolverr, so a multi-day crawl can never starve movierulz.
  * every topic read is archived (all posts on its first page, gzipped, tamilmv_archive.db), then
    extract() pulls out torrents (magnets, mangled 'http://magnet?' links, .torrent attachments)
    and direct-download links. `python -m tamilmv reparse` re-runs extract() on the archive, so
    a parser fix never needs a re-crawl.
  * serve_torznab() -- stdlib HTTP server (:8005): Torznab (t=caps/search/movie/tvsearch) serving
                       torrents + flagged '[DL-LINK]' items from SERVED forums/topics only, the
                       topic pages (/t/<site>/<id>) that items link to, and /nograb (always 404).

Domain rotation: tamilmv changes domains often. The seed domain AND the per-forum
maps come from MediaFusion's UPSTREAM GitHub scraper_config.json (TAMILMV_CONFIG_URL), refreshed
periodically and cached locally. The live domain is found by following the seed's 301 redirect.

Fetching: the IPS pages, sitemap and attachments answer plain HTTP (no CF challenge, measured
2026-09-30), which is ~10x cheaper than FlareSolverr. Plain requests go through TAMILMV_HTTP_PROXY
(gluetun's built-in proxy -> VPN exit IP); FlareSolverr is the head walk's fallback on a challenge.
Without a proxy, plain HTTP is off: pages go through FlareSolverr, and attachments, the sitemap and
the backfill are skipped (media-bridge is off the VPN; it never fetches these sites from home).

Scope: MOVIES + full-season packs only (single episodes / "Part" forms are skipped) -- the
classify()/normalize_title() guards are reused verbatim from movierulz.
"""

import datetime
import email.utils
import gzip
import hashlib
import html as htmlmod
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import requests

from common import clog, ntfy
from torrentfile import parse_torrent

# Reuse movierulz's stateless helpers (importing movierulz only builds its regexes; its threads
# are started separately by main.py, so the import has no side effects beyond that).
from movierulz import (
    fetch,
    parse_magnet,
    search_terms,
    match_rank,
    page,
    page_args,
    PAGE_MAX,
    series_title,
    torznab_category,
    error_xml,
    nudge_sonarr,
    SONARR_NUDGE_DAYS,
    classify,
    normalize_title,
    MAGNET_RE,
    QUALITY_RE,
    SITE_PREFIX_RE,
)

# ---- config ----
CONFIG_URL = os.environ.get(
    "TAMILMV_CONFIG_URL",
    "https://raw.githubusercontent.com/mhdzumair/MediaFusion/main/resources/json/scraper_config.json",
)
CONFIG_CACHE = os.environ.get("TAMILMV_CONFIG_CACHE", "/config/tamilmv_config.json")
CONFIG_REFRESH = int(
    os.environ.get("TAMILMV_CONFIG_REFRESH", "86400")
)  # re-pull GitHub config daily
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://gluetun:8191").rstrip("/")
APIKEY = os.environ.get("TAMILMV_APIKEY", "")
PORT = int(os.environ.get("TAMILMV_PORT", "8005"))
CRAWL_INTERVAL = int(os.environ.get("TAMILMV_CRAWL_INTERVAL", "3600"))
MAX_PAGES = int(
    os.environ.get("TAMILMV_MAX_PAGES", "5")
)  # head walk: newest pages per forum
MIN_PAGES = int(
    os.environ.get("TAMILMV_MIN_PAGES", "1")
)  # scan at least this many pages per forum
FAKE_SEEDERS = int(os.environ.get("FAKE_SEEDERS", "50"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "3"))
EMPTY_RETRY_DAYS = float(os.environ.get("TAMILMV_EMPTY_RETRY_DAYS", "3"))
# A running series is ONE topic that grows; the listing can't be relied on to show it (see
# refresh_active_series). Re-read series topics that gained a release within ACTIVE_DAYS, each at
# most once per ACTIVE_REREAD seconds. 0 days disables.
ACTIVE_DAYS = float(os.environ.get("TAMILMV_ACTIVE_DAYS", "21"))
ACTIVE_REREAD = float(os.environ.get("TAMILMV_ACTIVE_REREAD", "10800"))
# Cooldown between "domain dead" ntfy pushes for the SAME site. A dead seed fails on every crawl
# pass, so an ungated alert fires ~24x/day forever and drowns the topic (it buried DIUN's daily
# image-update batch until DIUN was moved to its own topic). Logs stay per-pass; only ntfy is
# throttled. 0 disables the cooldown (alert every pass).
DEAD_ALERT_INTERVAL = float(os.environ.get("TAMILMV_DEAD_ALERT_INTERVAL", "86400"))
DB_PATH = os.environ.get("TAMILMV_DB", "/config/tamilmv.db")
# Raw topic HTML (gzipped), kept apart so the feed's DB stays small. ~4.5 KB per topic.
ARCHIVE_PATH = os.environ.get("TAMILMV_ARCHIVE_DB", "/config/tamilmv_archive.db")
# Plain HTTP (pages, sitemap, .torrent attachments) goes through this proxy -- gluetun's built-in
# one, so the VPN exit IP is used. Empty = plain HTTP off (FlareSolverr only; no attachments,
# sitemap or backfill).
HTTP_PROXY = os.environ.get("TAMILMV_HTTP_PROXY", "").strip()
PLAIN_DELAY = float(os.environ.get("TAMILMV_PLAIN_DELAY", "1"))
# Backfill thread: listing pages per step, and how long to back off when plain HTTP stops working
# (CF challenge, site down, proxy gone).
BACKFILL_CHUNK = int(os.environ.get("TAMILMV_BACKFILL_CHUNK", "5"))
BACKFILL_PAUSE = float(os.environ.get("TAMILMV_BACKFILL_PAUSE", "1800"))
# Topics of one listing page fetched in parallel by the backfill (measured 2026-09-30 through the
# proxy: 1 -> 1.62 s/topic, 2 -> 0.87, 3 -> 0.61, 4 -> 0.52 with request latency starting to rise).
BACKFILL_WORKERS = max(1, int(os.environ.get("TAMILMV_BACKFILL_WORKERS", "3")))
# Every per-topic problem (fetch, attachment, parse, audit) as one JSON line, for later review:
# `python -m tamilmv errors`. Rotates to .1 at ERRORS_MAX_MB.
ERRORS_PATH = os.environ.get("TAMILMV_ERRORS_LOG", "/config/tamilmv_errors.jsonl")
ERRORS_MAX_MB = float(os.environ.get("TAMILMV_ERRORS_MAX_MB", "50"))
# Sitemap safety net: every SITEMAP_INTERVAL s, fetch sitemap.php and read any topic we have never
# seen whose lastmod is within SITEMAP_DAYS. The same pass refreshes the forum list.
SITEMAP_INTERVAL = float(os.environ.get("TAMILMV_SITEMAP_INTERVAL", "86400"))
SITEMAP_DAYS = float(os.environ.get("TAMILMV_SITEMAP_DAYS", "30"))
# Served forums the upstream config doesn't map: "site:forum-path=language,..." (backfill order).
# language "auto" = a forum mixing languages (members' "Post & Share Here"): read from the title.
EXTRA_FORUMS = os.environ.get(
    "TAMILMV_EXTRA_FORUMS",
    "tamilmv:38-dvd9-dvd5=malayalam,tamilmv:60-dvd9-dvd5=hindi,tamilmv:13-dvd9-dvd5=tamil,"
    "tamilmv:92-post-share-here=auto",
)
# Where a browser reaches this server: the '[DL-LINK]' items and every release link their topic
# page here (Radarr/Sonarr "info" link). The Tailscale address works at home and away.
PUBLIC_URL = os.environ.get("TAMILMV_PUBLIC_URL", "http://100.122.87.20:8005").rstrip(
    "/"
)
LINKS_MAX = int(
    os.environ.get("TAMILMV_LINKS_MAX", "50")
)  # '[DL-LINK]' items per search
# Title flag of a direct-download item. Radarr/Sonarr parse '[DL-LINK]' as the release group (title,
# year, quality, language unaffected), and no real group is called that.
LINK_FLAG = "[DL-LINK]"
# Bump when extract() learns something new; posts read by an older parser are re-extracted from
# the archive (no fetch) as the walk reaches them, or all at once with `python -m tamilmv reparse`.
PARSER_V = 3
# Bump to make the backfill walk every forum from page 1 again (v2: topics read before the archive
# existed must be read once more).
WALK_EPOCH = 2

# The site we handle, mapped to its key in the upstream config, with its seed override (for
# emergencies when the GitHub seed itself dies). Only these sites are crawled AND served: rows of
# a removed site (tamil_blasters, 2026-10-19) stay in the DBs but never reach the feed.
SITES = {
    "tamilmv": os.environ.get("TAMILMV_SEED_OVERRIDE", ""),
}
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Full topic URL (with slug) on a forum listing, keyed on the numeric topic id. A descriptive slug
# (>=1 letter) is required so pinned nav links like '/topic/183-0/' are ignored.
TOPIC_RE = re.compile(
    r'https?://[^"\'<>\s]*?/index\.php\?/forums/topic/(\d+)-([a-z0-9][^"\'<>\s]*)', re.I
)

# module-level parsed config: {site: {"homepage": str, "forums": [(forum_path, language), ...]}}
CONFIG = {}
CONFIG_TS = 0.0


def log(msg):
    clog("tamilmv", msg)


# ---- GitHub config (seeds + forum maps) ----
def _parse_site_cfg(raw):
    """Normalize a site block from scraper_config.json into {homepage, forums:[(path,lang)]}
    (tamilmv's 'supported_search_forums': {path:{language,media_type}})."""
    homepage = (raw.get("homepage") or "").rstrip("/")
    forums = []
    ssf = raw.get("supported_search_forums")
    if isinstance(ssf, dict):
        for path, meta in ssf.items():
            forums.append((path, (meta or {}).get("language", "unknown")))
    # de-dupe (path, lang) while preserving order
    seen, uniq = set(), []
    for f in forums:
        if f[0] not in seen:
            seen.add(f[0])
            uniq.append(f)
    return {"homepage": homepage, "forums": uniq}


def load_config(force=False):
    """Refresh CONFIG from GitHub (cached to disk). Falls back to the on-disk cache when GitHub is
    unreachable; ntfy + keep whatever we have if neither works. Returns True if CONFIG is populated."""
    global CONFIG, CONFIG_TS
    if CONFIG and not force and (time.time() - CONFIG_TS) < CONFIG_REFRESH:
        return True
    raw = None
    try:
        r = requests.get(CONFIG_URL, timeout=20, headers={"User-Agent": UA})
        r.raise_for_status()
        raw = r.json()
        os.makedirs(os.path.dirname(CONFIG_CACHE), exist_ok=True)
        with open(CONFIG_CACHE, "w") as fh:
            json.dump(raw, fh)
        log("config refreshed from GitHub")
    except Exception as e:
        log(f"GitHub config fetch failed ({e}); trying cache")
        try:
            with open(CONFIG_CACHE) as fh:
                raw = json.load(fh)
        except Exception:
            raw = None
    if raw is None:
        if not CONFIG:
            ntfy(
                "tamilmv: no config",
                "GitHub fetch and cache both failed; crawler idle",
                priority="high",
                tags="warning",
            )
        return bool(CONFIG)
    CONFIG = {site: _parse_site_cfg(raw.get(site, {})) for site in SITES}
    CONFIG_TS = time.time()
    return True


def forum_id(forum_path):
    """'25-hd-rips-dvd-rips-br-rips' -> '25' (the id is what breadcrumbs and URLs share)."""
    return str(forum_path).split("-", 1)[0]


def _extra_forums(site):
    out = []
    for item in EXTRA_FORUMS.split(","):
        s, _, rest = item.strip().partition(":")
        path, _, lang = rest.partition("=")
        if s == site and path and lang:
            out.append((path.strip(), lang.strip()))
    return out


def release_forums(site):
    """{forum id: (forum path, language)} for every forum whose topics are served by default: the
    upstream config's map plus TAMILMV_EXTRA_FORUMS (DVD9/DVD5 forums, members' share forum)."""
    out = {}
    for path, lang in CONFIG.get(site, {}).get("forums", []) + _extra_forums(site):
        out.setdefault(forum_id(path), (path, lang))
    return out


# ---- fetching: plain HTTP via the VPN proxy, FlareSolverr as the fallback ----
# Per-thread state: the requests session (the backfill fetches from several worker threads), the
# fetch mode and the role that error-log lines are tagged with. The backfill sets plain_only: it
# must never fall back to FlareSolverr (days of crawling would starve movierulz); it raises
# PlainUnavailable and pauses.
_CTX = threading.local()


def _session():
    s = getattr(_CTX, "session", None)
    if s is None:
        s = _CTX.session = requests.Session()
        s.headers["User-Agent"] = UA
        if HTTP_PROXY:
            s.proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
    return s


# A topic/listing fetch failure is TRANSIENT (retry the same page later; blocks the backfill page)
# or PERMANENT (the topic is gone; logged, the walk moves on).
TRANSIENT = {"network", "challenge", "rate_limited", "server_error"}


def fetch_error_type(status, text=""):
    if status is None:
        return "network"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    if (
        status in (403, 503)
        or "Just a moment" in (text or "")[:4000]
        or "challenge-platform" in (text or "")[:8000]
    ):
        return "challenge"
    if status in (404, 410):
        return "not_found"
    return "http_other"


class PlainUnavailable(Exception):
    """Plain HTTP stopped working (CF challenge, 429, 5xx, proxy/network error) in a plain-only
    thread: the backfill pauses and retries the same listing page."""

    def __init__(self, typ, status, url):
        super().__init__(f"{typ} ({status}) for {url}")
        self.typ, self.status, self.url = typ, status, url


def _challenged(status, text):
    return (
        status in (403, 503)
        or "Just a moment" in text[:4000]
        or "challenge-platform" in text[:8000]
    )


def fetch_plain(url, binary=False):
    """(status, text|bytes) over the proxy, or (None, None) when plain HTTP is off or fails."""
    if not HTTP_PROXY:
        return None, None
    try:
        r = _session().get(url, timeout=45)
        time.sleep(PLAIN_DELAY)
        if binary:
            return r.status_code, r.content
        r.encoding = r.encoding or "utf-8"
        return r.status_code, r.text
    except Exception as e:
        log(f"  plain fetch failed for {url}: {e}")
        return None, None


def fetch_page(url):
    """(status, html): plain HTTP first; FlareSolverr on a challenge, an error, or no proxy.
    In a plain_only thread a transient failure raises PlainUnavailable instead (a 404 of a deleted
    topic is returned as-is -- it must not pause the walk)."""
    status, text = fetch_plain(url)
    if status == 200 and text and not _challenged(status, text):
        return status, text
    if getattr(_CTX, "plain_only", False):
        typ = fetch_error_type(status, text)
        if typ in TRANSIENT:
            raise PlainUnavailable(typ, status, url)
        return status, text
    status, text = fetch(url)
    time.sleep(REQUEST_DELAY)
    return status, text


def resolve_base(seed):
    """Follow the seed's 301 chain to the current live domain (plain HTTP -- edge redirect, not
    CF-walled; through the VPN proxy when one is set). Returns 'scheme://host' or None."""
    if not seed:
        return None
    try:
        r = _session().get(seed, allow_redirects=True, timeout=15)
        if r.status_code < 400:
            p = urlparse(r.url)
            return f"{p.scheme}://{p.netloc}"
    except Exception as e:
        log(f"  redirect resolve failed for {seed}: {e}")
    return None


def site_base(site):
    """Live base URL for a site, with recovery: override/seed -> redirect; if dead, force a GitHub
    config refresh for a newer seed and retry; still dead -> ntfy + None (skip this site).
    A live base is remembered in meta 'base:<site>' (topic pages link the live domain from it).

    The dead-site ntfy is rate-limited to one push per DEAD_ALERT_INTERVAL per site, and the
    timer is RESET to 0 on every successful resolve, so a site that recovers and then dies
    again alerts immediately instead of waiting out a window opened by the previous outage."""
    key = f"dead_alert:{site}"
    seed = SITES.get(site) or CONFIG.get(site, {}).get("homepage")
    base = resolve_base(seed)
    if base:
        set_meta(key, 0)
        set_meta(f"base:{site}", base)
        return base
    log(f"  {site}: seed {seed!r} not resolving; refreshing config")
    load_config(force=True)
    seed2 = SITES.get(site) or CONFIG.get(site, {}).get("homepage")
    base = resolve_base(seed2)
    if base:
        set_meta(key, 0)
        set_meta(f"base:{site}", base)
        return base
    log(f"  {site}: seed {seed2!r} still dead after config refresh")
    last = float(get_meta(key) or 0)
    if time.time() - last >= DEAD_ALERT_INTERVAL:
        ntfy(
            f"tamilmv: {site} domain dead",
            f"seed {seed2!r} no longer redirects; update *_SEED_OVERRIDE or wait for upstream config",
            priority="high",
            tags="warning",
        )
        set_meta(key, time.time())
    return base


# ---- sqlite (own DB; posts keyed by (site, post_id) since topic ids collide across sites) ----
DB_LOCK = threading.Lock()
_INIT_LOCK = threading.Lock()
_conn = None


def _add_columns(conn, table, cols):
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, typ in cols:
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def db():
    global _conn
    if _conn is not None:
        return _conn
    with _INIT_LOCK:
        if _conn is not None:
            return _conn
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                site TEXT,
                post_id INTEGER,
                url TEXT,
                scraped_at REAL,
                magnets INTEGER DEFAULT 0,
                PRIMARY KEY (site, post_id)
            );
            CREATE TABLE IF NOT EXISTS releases (
                btih TEXT PRIMARY KEY,
                site TEXT,
                post_id INTEGER,
                title TEXT,
                kind TEXT,
                season INTEGER,
                language TEXT,
                year INTEGER,
                quality TEXT,
                size INTEGER,
                magnet TEXT,
                added_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_trel_added ON releases(added_at);
            CREATE INDEX IF NOT EXISTS idx_trel_post ON releases(site, post_id);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            -- .torrent attachments already downloaded (btih '' = not a usable public torrent),
            -- so a re-read topic never downloads the same file twice.
            CREATE TABLE IF NOT EXISTS attachments (
                site TEXT,
                att_id INTEGER,
                post_id INTEGER,
                btih TEXT,
                name TEXT,
                fetched_at REAL,
                PRIMARY KEY (site, att_id)
            );
            -- Direct-download / shortener links from a topic's OPENING post. Served only as
            -- '[DL-LINK]' items that can never be grabbed (0 seeders, /nograb = 404).
            CREATE TABLE IF NOT EXISTS weblinks (
                site TEXT,
                post_id INTEGER,
                url TEXT,
                host TEXT,
                label TEXT,
                size INTEGER,
                topic TEXT,
                language TEXT,
                found_at REAL,
                PRIMARY KEY (site, post_id, url)
            );
            -- Which topics a torrent was posted in (releases keeps ONE row per infohash -- the first
            -- topic -- so a re-post in another topic is recorded here, not as a duplicate release).
            CREATE TABLE IF NOT EXISTS release_topics (
                btih TEXT,
                site TEXT,
                post_id INTEGER,
                PRIMARY KEY (btih, site, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_rt_post ON release_topics(site, post_id);
            -- One-off error-log events already written (a re-read must not repeat them).
            CREATE TABLE IF NOT EXISTS logged (
                site TEXT,
                post_id INTEGER,
                type TEXT,
                key TEXT,
                PRIMARY KEY (site, post_id, type, key)
            );
            -- Every forum of a site (from its sitemap). serve NULL = automatic (served iff it is a
            -- release forum); 0/1 = a manual override that survives config changes.
            CREATE TABLE IF NOT EXISTS forums (
                site TEXT,
                forum_id TEXT,
                path TEXT,
                serve INTEGER,
                seen_at REAL,
                PRIMARY KEY (site, forum_id)
            );
            """
        )
        # posts.serve: per-topic override of the forum's serve flag (NULL = follow the forum).
        _add_columns(
            conn,
            "posts",
            (
                ("forum", "TEXT"),
                ("posted_at", "REAL"),
                ("parsed_v", "INTEGER DEFAULT 0"),
                ("title", "TEXT"),
                ("serve", "INTEGER"),
                ("gone", "INTEGER DEFAULT 0"),
                ("pending", "INTEGER DEFAULT 0"),
                ("last_error", "TEXT"),
            ),
        )
        _add_columns(conn, "attachments", (("size", "INTEGER"),))
        _add_columns(
            conn,
            "weblinks",
            (
                ("lid", "TEXT"),
                ("btih", "TEXT"),
                ("title", "TEXT"),
                ("kind", "TEXT"),
                ("season", "INTEGER"),
                ("quality", "TEXT"),
            ),
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wl_lid ON weblinks(lid)")
        conn.commit()
        _conn = conn
    return _conn


def get_meta(key):
    with DB_LOCK:
        row = db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_meta(key, value):
    with DB_LOCK:
        db().execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value))
        )
        db().commit()


# ---- archive (raw topic HTML, gzipped; separate file) ----
ARCH_LOCK = threading.Lock()
_aconn = None


def adb():
    global _aconn
    if _aconn is not None:
        return _aconn
    with _INIT_LOCK:
        if _aconn is not None:
            return _aconn
        os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
        conn = sqlite3.connect(ARCHIVE_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS pages (
                            site TEXT, post_id INTEGER, url TEXT, html BLOB, saved_at REAL,
                            PRIMARY KEY (site, post_id))""")
        conn.commit()
        _aconn = conn
    return _aconn


def archive_put(site, post_id, url, page):
    with ARCH_LOCK:
        adb().execute(
            "INSERT OR REPLACE INTO pages(site, post_id, url, html, saved_at) VALUES (?,?,?,?,?)",
            (site, post_id, url, gzip.compress(page.encode("utf-8")), time.time()),
        )
        adb().commit()


def archive_get(site, post_id):
    """(url, html, saved_at) or None."""
    with ARCH_LOCK:
        row = (
            adb()
            .execute(
                "SELECT url, html, saved_at FROM pages WHERE site=? AND post_id=?",
                (site, post_id),
            )
            .fetchone()
        )
    if not row:
        return None
    return (
        row["url"],
        gzip.decompress(row["html"]).decode("utf-8", "replace"),
        row["saved_at"],
    )


def archive_saved_at(site, post_id):
    with ARCH_LOCK:
        row = (
            adb()
            .execute(
                "SELECT saved_at FROM pages WHERE site=? AND post_id=?", (site, post_id)
            )
            .fetchone()
        )
    return row[0] if row else None


def archived(site, post_id):
    with ARCH_LOCK:
        return (
            adb()
            .execute("SELECT 1 FROM pages WHERE site=? AND post_id=?", (site, post_id))
            .fetchone()
            is not None
        )


# ---- error log (JSONL) ----
_ERR_LOCK = threading.Lock()
ONE_OFF = {
    "not_torrent",
    "private_torrent",
    "magnet_unparsed",
    "magnet_skipped",
    "empty",
    "extract_error",
}


def log_error(
    site,
    post_id,
    typ,
    stage,
    outcome,
    url=None,
    status=None,
    detail="",
    forum=None,
    dedupe_key=None,
    **extra,
):
    """Append one JSON line to ERRORS_PATH. One-off findings (ONE_OFF types) are written once per
    (topic, type, dedupe_key); fetch failures are events and are written every time. Never raises:
    the log must not break the crawl."""
    try:
        if typ in ONE_OFF and post_id is not None:
            with DB_LOCK:
                cur = db().execute(
                    "INSERT OR IGNORE INTO logged(site, post_id, type, key) VALUES (?,?,?,?)",
                    (site, post_id, typ, str(dedupe_key or "")),
                )
                db().commit()
            if cur.rowcount == 0:
                return
        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "site": site,
            "post_id": post_id,
            "url": url,
            "forum": forum,
            "thread": getattr(_CTX, "role", "head"),
            "stage": stage,
            "type": typ,
            "status": status,
            "detail": (detail or "")[:500],
            "outcome": outcome,
        }
        rec.update(extra)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _ERR_LOCK:
            os.makedirs(os.path.dirname(ERRORS_PATH) or ".", exist_ok=True)
            try:
                if os.path.getsize(ERRORS_PATH) > ERRORS_MAX_MB * 1e6:
                    os.replace(ERRORS_PATH, ERRORS_PATH + ".1")
            except OSError:
                pass
            with open(ERRORS_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as e:
        log(f"  error-log write failed: {e!r}")


# ---- serve flags ----
def forum_overrides():
    """{(site, forum id): 0/1} manual forum serve overrides."""
    with DB_LOCK:
        return {
            (r["site"], r["forum_id"]): r["serve"]
            for r in db().execute(
                "SELECT site, forum_id, serve FROM forums WHERE serve IS NOT NULL"
            )
        }


def is_served(site, forum, post_serve, overrides=None, released=None):
    """Topic-level override > forum-level override > automatic (a release forum). Topics whose
    forum is unknown (read before forums were recorded) came from release forums: served. With no
    config loaded yet every forum counts as served rather than emptying the feed."""
    if post_serve is not None:
        return bool(post_serve)
    if not forum:
        return True
    ov = (overrides if overrides is not None else forum_overrides()).get((site, forum))
    if ov is not None:
        return bool(ov)
    if not CONFIG.get(site, {}).get("forums"):
        return True
    return forum in (released if released is not None else release_forums(site))


def upsert_forums(site, entries):
    """entries: [(forum id, path)] from the sitemap; new rows get serve NULL (automatic)."""
    with DB_LOCK:
        for fid, path in entries:
            db().execute(
                "INSERT INTO forums(site, forum_id, path, serve, seen_at) VALUES (?,?,?,NULL,?) "
                "ON CONFLICT(site, forum_id) DO UPDATE SET path=excluded.path, "
                "seen_at=excluded.seen_at",
                (site, fid, path, time.time()),
            )
        db().commit()


def known_forums(site):
    with DB_LOCK:
        return [
            (r["forum_id"], r["path"])
            for r in db().execute(
                "SELECT forum_id, path FROM forums WHERE site=?", (site,)
            )
        ]


def forum_language(site, forum):
    return release_forums(site).get(forum, (None, "auto"))[1]


def backfill_order(site):
    """[(forum path, language)]: config (release) forums first, then TAMILMV_EXTRA_FORUMS in their
    listed order, then every other known forum by id -- all of them are archived."""
    out, seen = [], set()
    for path, lang in CONFIG.get(site, {}).get("forums", []) + _extra_forums(site):
        if forum_id(path) not in seen:
            seen.add(forum_id(path))
            out.append((path, lang))
    for fid, path in sorted(
        known_forums(site), key=lambda x: int(x[0]) if x[0].isdigit() else 0
    ):
        if fid not in seen:
            seen.add(fid)
            out.append((path, "auto"))
    return out


def _slug(url):
    """The topic slug ('heart-beat-2026-s03-ep-01-32-...'), host-agnostic so a domain rotation
    doesn't read as a change; None if the URL isn't a topic URL."""
    m = TOPIC_RE.search(url or "")
    return m.group(2).rstrip("/").lower() if m else None


def topic_action(site, post_id, url=None):
    """What a listed topic needs: 'fetch', 'reparse' (re-extract from the archive, no network), or
    None. New topic, slug changed, never archived, or archived before a `recrawl --refetch`: fetch.
    Gone (404 / no posts on the page): nothing, until its slug changes (the audit gives it one more
    try). No torrents yet: fetch again until EMPTY_RETRY_DAYS after it was POSTED (a page published
    before its magnets landed). An attachment that failed to download, or an older parser: reparse.

    The slug check is what keeps a running series current: tamilmv posts a season as ONE topic and
    appends each new episode batch to it, retitling it as it grows ('S03 EP (01-04)' -> '... (01-32)',
    and the slug follows). Without it a topic was read once, at its first batch, and never again --
    Heart Beat S03 sat at episodes 1-4 in the feed while the topic held 1-32.

    The retry window is anchored on the post date, not on when we first saw it: the backfill finds
    thousands of years-old link-only topics, and anchoring on first-seen re-read each of them every
    pass for days."""
    with DB_LOCK:
        row = (
            db()
            .execute(
                "SELECT scraped_at, magnets, url, posted_at, parsed_v, gone, pending FROM posts "
                "WHERE site=? AND post_id=?",
                (site, post_id),
            )
            .fetchone()
        )
    if row is None:
        return "fetch"
    if url and _slug(url) and _slug(url) != _slug(row["url"]):
        return "fetch"
    if row["gone"]:
        return None
    saved = archive_saved_at(site, post_id)
    if saved is None or saved < float(get_meta(f"refetch_before:{site}") or 0):
        return "fetch"
    retry = (
        (row["magnets"] or 0) == 0
        and EMPTY_RETRY_DAYS > 0
        and (time.time() - (row["posted_at"] or row["scraped_at"] or 0))
        < EMPTY_RETRY_DAYS * 86400
    )
    if retry:
        return "fetch"
    if row["pending"] or (row["parsed_v"] or 0) < PARSER_V:
        return "reparse"
    return None


def post_needs_fetch(site, post_id, url=None):
    return topic_action(site, post_id, url) == "fetch"


def mark_post(
    site, post_id, url, magnets, forum=None, posted_at=None, title=None, pending=0
):
    """A successful read. INSERT OR IGNORE keeps scraped_at as the stable first-seen time; magnets
    (torrents found on the page: magnets + .torrent attachments) only ratchets up (never down). url
    is always refreshed: it is what topic_action compares the next listing's slug against. Clears
    gone/last_error; pending = an attachment still to download."""
    with DB_LOCK:
        db().execute(
            "INSERT OR IGNORE INTO posts(site, post_id, url, scraped_at, magnets) VALUES (?,?,?,?,?)",
            (site, post_id, url, time.time(), magnets),
        )
        db().execute(
            "UPDATE posts SET url=?, parsed_v=?, forum=coalesce(?, forum), posted_at=coalesce(?, posted_at), "
            "title=coalesce(?, title), gone=0, pending=?, last_error=NULL WHERE site=? AND post_id=?",
            (
                url,
                PARSER_V,
                forum,
                posted_at,
                title or None,
                1 if pending else 0,
                site,
                post_id,
            ),
        )
        db().execute(
            "UPDATE posts SET magnets=? WHERE site=? AND post_id=? AND magnets<?",
            (magnets, site, post_id, magnets),
        )
        db().commit()


def mark_gone(site, post_id, url, typ):
    """The topic can't be read (404 / no posts on the page): remember it so walks skip it."""
    with DB_LOCK:
        db().execute(
            "INSERT OR IGNORE INTO posts(site, post_id, url, scraped_at, magnets) VALUES (?,?,?,?,0)",
            (site, post_id, url, time.time()),
        )
        db().execute(
            "UPDATE posts SET gone=1, last_error=? WHERE site=? AND post_id=?",
            (typ, site, post_id),
        )
        db().commit()


def upsert_release(rec):
    """One row per infohash (the first topic keeps it: stable pubDate); every topic it appears in
    goes to release_topics."""
    with DB_LOCK:
        db().execute(
            "INSERT OR IGNORE INTO release_topics(btih, site, post_id) VALUES (?,?,?)",
            (rec["btih"], rec["site"], rec["post_id"]),
        )
        # OR IGNORE keeps the original added_at -> stable pubDate for Prowlarr.
        db().execute(
            """INSERT OR IGNORE INTO releases
               (btih, site, post_id, title, kind, season, language, year, quality, size, magnet, added_at)
               VALUES (:btih,:site,:post_id,:title,:kind,:season,:language,:year,:quality,:size,:magnet,:added_at)""",
            rec,
        )
        db().commit()


def link_id(site, post_id, url):
    return hashlib.sha1(f"{site}|{post_id}|{url}".encode()).hexdigest()[:16]


def replace_weblinks(site, post_id, links, topic, language):
    """A topic's links are re-derived on every extract (labels/ties improve with the parser), so the
    old set is replaced. found_at is kept for links seen before (stable pubDate)."""
    with DB_LOCK:
        c = db()
        old = {
            r["url"]: r["found_at"]
            for r in c.execute(
                "SELECT url, found_at FROM weblinks WHERE site=? AND post_id=?",
                (site, post_id),
            )
        }
        c.execute("DELETE FROM weblinks WHERE site=? AND post_id=?", (site, post_id))
        for ln in links:
            c.execute(
                "INSERT OR IGNORE INTO weblinks(site, post_id, url, host, label, size, topic, language, "
                "found_at, lid, btih, title, kind, season, quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    site,
                    post_id,
                    ln["url"],
                    ln["host"],
                    ln["label"],
                    ln["size"],
                    topic,
                    language,
                    old.get(ln["url"], time.time()),
                    link_id(site, post_id, ln["url"]),
                    ln.get("btih"),
                    ln.get("title"),
                    ln.get("kind"),
                    ln.get("season"),
                    ln.get("quality"),
                ),
            )
        c.commit()


# ---- topic parsing (pure functions; tests feed them synthetic IPS markup) ----
CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)'
    r'|<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']canonical',
    re.I,
)
# Posts from ~2023 carry magnets the forum mangled into links: 'http://magnet?xt=urn:btih:...'.
MANGLED_MAGNET_RE = re.compile(r'https?://magnet/?\?(xt=urn:btih:[^"\'<>\s]+)', re.I)
ARTICLE_RE = re.compile(r"<article\b", re.I)
ARTICLE_BLOCK_RE = re.compile(r"<article\b.*?</article>", re.I | re.S)
TIME_RE = re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.I)
OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']', re.I
)
BREADCRUMB_RE = re.compile(r'data-role=["\']breadcrumbList["\'](.*?)</ul>', re.I | re.S)
FORUM_LINK_RE = re.compile(r"/forums/forum/(\d+)-", re.I)
ANCHOR_RE = re.compile(r"<a\s([^>]*)>(.*?)</a>", re.I | re.S)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
ATTACH_RE = re.compile(r"attachment\.php\?id=(\d+)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
FILE_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", re.I)
VIDEO_EXT_RE = re.compile(r"\.(mkv|mp4|avi|m4v)\b", re.I)
YEAR_WORD_RE = re.compile(r"\b(19|20)\d{2}\b")
# Hosts in a post that are never a download: image hosts (screenshots), socials, the forum
# software's own credits, ads. Anything else linked from the opening post is kept as a web link.
NOT_DOWNLOAD_HOSTS = (
    "imageride.",
    "imgbb.",
    "ibb.co",
    "imgur.",
    "postimg.",
    "pixhost.",
    "imagebam.",
    "tmdb.org",
    "themoviedb.",
    "imdb.com",
    "t.me",
    "telegram.",
    "instagram.",
    "facebook.",
    "twitter.",
    "x.com",
    "whatsapp.",
    "youtube.",
    "youtu.be",
    "invisioncommunity.",
    "ipbmafia.",
    "streamdady.",
    "llvpn.",
    "gmpg.org",
    "wikipedia.",
    "google.com",
    "reddit.",
    "linkedin.",
    "pinterest.",
    "tumblr.",
)
LANG_WORD_RE = re.compile(
    r"\b(tamil|telugu|malayalam|kannada|hindi|bengali|marathi|punjabi|english)\b", re.I
)


def _text(fragment):
    return re.sub(r"\s+", " ", htmlmod.unescape(TAG_RE.sub(" ", fragment))).strip()


def first_post(page):
    """The topic's opening post (IPS: the first <article>...</article>) -- the uploader's, not a
    reply, and not the page's share buttons after it."""
    m = ARTICLE_RE.search(page)
    if not m:
        return ""
    end = page.find("</article>", m.start())
    return page[m.start() : end if end > 0 else len(page)]


def topic_forum(page):
    """Id of the forum the topic sits in: the last forum link of the breadcrumb."""
    m = BREADCRUMB_RE.search(page)
    ids = FORUM_LINK_RE.findall(m.group(1)) if m else []
    return ids[-1] if ids else None


def topic_posted_at(page):
    m = TIME_RE.search(first_post(page))
    if not m:
        return None
    try:
        return datetime.datetime.fromisoformat(
            m.group(1).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def lang_from_title(title):
    m = LANG_WORD_RE.search(title or "")
    return m.group(1).lower() if m else "unknown"


def topic_title(page):
    m = OG_TITLE_RE.search(page)
    return htmlmod.unescape(m.group(1)).strip() if m else ""


def archive_view(page):
    """What is archived of a topic page: title, canonical URL, breadcrumb and every post on it (all
    extract() needs, ~4.5 KB gzipped); the page chrome, ads and scripts around them are dropped."""
    title = topic_title(page)
    m = CANONICAL_RE.search(page)
    canon = m and (m.group(1) or m.group(2))
    b = BREADCRUMB_RE.search(page)
    q = htmlmod.escape
    return (
        f'<meta property="og:title" content="{q(title)}">\n'
        + (f'<link rel="canonical" href="{q(canon)}">\n' if canon else "")
        + (
            f'<nav><ul data-role="breadcrumbList">{b.group(1)}</ul></nav>\n'
            if b
            else ""
        )
        + "\n".join(ARTICLE_BLOCK_RE.findall(page))
    )


def find_magnets(page):
    """Every magnet on the page, the mangled 'http://magnet?xt=' form rewritten to 'magnet:?'; one
    per infohash, page order."""
    found = MAGNET_RE.findall(page) + [
        "magnet:?" + m for m in MANGLED_MAGNET_RE.findall(page)
    ]
    out, seen = [], set()
    for m in found:
        m = htmlmod.unescape(m)
        bt = re.search(r"btih:([0-9a-zA-Z]+)", m)
        key = bt.group(1).lower() if bt else m
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


def _dn(magnet):
    dn = re.search(r"[?&]dn=([^&]+)", magnet)
    return urllib.parse.unquote(dn.group(1)).strip() if dn else ""


def _magnet_names(magnets):
    names = set()
    for m in magnets:
        dn = re.search(r"[?&]dn=([^&]+)", m)
        if (
            dn
        ):  # '+' is a space in some magnets and a literal (DD+5.1) in others: keep both
            names.add(urllib.parse.unquote(dn.group(1)).strip().lower())
            names.add(urllib.parse.unquote_plus(dn.group(1)).strip().lower())
    return names


def _magnet_meta(magnet):
    """(btih lowercased hex or as given, dn, size or 0)."""
    bt = re.search(r"btih:([0-9a-zA-Z]+)", magnet)
    xl = re.search(r"[?&]xl=(\d+)", magnet)
    return (
        (bt.group(1).lower() if bt else ""),
        _dn(magnet),
        (int(xl.group(1)) if xl else 0),
    )


def find_torrent_attachments(page):
    """[(att_id, url, file name)] for every .torrent attachment link on the page."""
    out, seen = [], set()
    for attrs, inner in ANCHOR_RE.findall(page):
        href = HREF_RE.search(attrs)
        if not href:
            continue
        url = htmlmod.unescape(href.group(1))
        m = ATTACH_RE.search(url)
        name = _text(inner)
        is_torrent = name.lower().endswith(".torrent") or re.search(
            r'data-fileext=["\']torrent', attrs, re.I
        )
        if m and is_torrent and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((int(m.group(1)), url, name))
    return out


def magnet_from_torrent_bytes(data):
    """(btih, magnet) for a public .torrent's bytes; None if it isn't one (or is private: a private
    torrent must never be turned into a public magnet)."""
    try:
        info_raw, info, trackers = parse_torrent(data)
    except Exception:
        return None
    if info.get(b"private") == 1:
        return None
    btih = hashlib.sha1(info_raw).hexdigest()
    if b"length" in info:
        size = info[b"length"]
    else:
        size = sum(f.get(b"length", 0) for f in info.get(b"files", []))
    name = (info.get(b"name") or b"").decode("utf-8", "replace")
    q = urllib.parse.quote
    parts = [f"xt=urn:btih:{btih}", f"dn={q(name, safe='')}", f"xl={size}"]
    parts += [
        f"tr={q(t.decode('utf-8', 'replace'), safe='')}"
        for t in dict.fromkeys(trackers)
    ]
    return btih, "magnet:?" + "&".join(parts)


def _size_bytes(text):
    found = FILE_SIZE_RE.findall(text or "")
    if not found:
        return 0
    val, unit = found[-1]
    return int(float(val) * (1e9 if unit.upper() == "GB" else 1e6))


def _generic_label(label):
    """'DIRECT LINK', 'Download', 'Click here', 'G-Drive'...: says nothing about the file."""
    return (
        len(label) <= 30
        and not FILE_SIZE_RE.search(label)
        and not VIDEO_EXT_RE.search(label)
        and not YEAR_WORD_RE.search(label)
    )


def find_weblinks(page, own_host, att_map=None):
    """Download/shortener links in the OPENING post only (replies post links to other movies).

    Label = the link text; the text just before the link when the text is the bare URL; and for a
    generic button ('DIRECT LINK') the name of the torrent right above it -- tamilmv lists each
    file as .torrent + MAGNET + DIRECT LINK, so that button IS that torrent's file, and the link
    records the torrent's btih and size. att_map: {attachment id: (btih, size)} of downloaded
    .torrent attachments (their btih is only known after the download)."""
    att_map = att_map or {}
    post = first_post(page)
    out, seen = [], set()
    prev_end = 0
    last = None  # (name, btih, size) of the most recent torrent in the post
    for m in ANCHOR_RE.finditer(post):
        ctx_start, prev_end = max(prev_end, m.start() - 600), m.end()
        attrs, inner = m.group(1), m.group(2)
        href = HREF_RE.search(attrs)
        if not href:
            continue
        url = htmlmod.unescape(href.group(1)).strip()
        mangled = MANGLED_MAGNET_RE.match(url)
        if url.lower().startswith("magnet:") or mangled:
            bt, dn, size = _magnet_meta(
                "magnet:?" + mangled.group(1) if mangled else url
            )
            last = (dn, bt, size)
            continue
        att = ATTACH_RE.search(url)
        if att:
            name = _text(inner)
            if name.lower().endswith(".torrent"):
                bt, size = att_map.get(int(att.group(1)), ("", 0))
                last = (name[: -len(".torrent")].strip(), bt, size)
            continue
        host = (urlparse(url).hostname or "").lower()
        if (
            not url.lower().startswith(("http://", "https://"))
            or not host
            or url in seen
            or host == own_host
            or host.endswith("." + own_host)
            or any(h in host for h in NOT_DOWNLOAD_HOSTS)
            or re.search(r"<img\b", inner, re.I)
        ):
            continue
        seen.add(url)
        label = _text(inner)
        btih = None
        size = 0
        if (
            label
            and not label.startswith(("http://", "https://"))
            and _generic_label(label)
            and last
        ):
            label, btih, size = last[0], (last[1] or None), last[2]
        elif not label or label == url or label.startswith(("http://", "https://")):
            ctx = post[ctx_start : m.start()]
            if ">" in ctx and ("<" not in ctx or ctx.index(">") < ctx.index("<")):
                ctx = ctx[ctx.index(">") + 1 :]  # cut into the middle of a tag
            label = _text(ctx)[-200:]
        out.append(
            {
                "url": url,
                "host": host,
                "label": label,
                "size": size or _size_bytes(label),
                "btih": btih,
            }
        )
    return out


def link_title(label, topic):
    """Release-style title for a web link: the file name in the label (without extension) when
    there is one, else the label if it names a year, else the topic's title."""
    s = (label or "").strip()
    m = VIDEO_EXT_RE.search(s)
    if m:
        s = s[: m.start()]
        tok = re.search(
            r"(\S{12,})$", s
        )  # 'Raja Rani (2014) 1080p - Raja.Rani.2014.1080p...'
        if tok and tok.group(1).count(".") >= 3:
            s = tok.group(1)
    elif not YEAR_WORD_RE.search(s):
        s = topic or s
    s = SITE_PREFIX_RE.sub("", s)
    s = re.sub(
        r"^\s*(?:\d+\)\s*|@\S+\s+-\s+)", "", s
    )  # '1) Title', '@Uploader - Title'
    return s.strip(" -|:")


def annotate_link(link, topic):
    """Title/kind/season/quality the feed serves a link under (same movie/series rules as torrents)."""
    title = link_title(link["label"], topic)
    kind, season = classify(title)
    qm = QUALITY_RE.search(title) or QUALITY_RE.search(link["label"] or "")
    link.update(
        title=normalize_title(title, kind) if kind != "skip" else title,
        kind=kind,
        season=season,
        quality=qm.group(1).lower() if qm else "",
    )
    return link


# ---- crawl ----
def _attachment_map(site, post_id):
    """{att_id: (btih, size)} of this topic's downloaded attachments."""
    with DB_LOCK:
        return {
            r["att_id"]: (r["btih"] or "", r["size"] or 0)
            for r in db().execute(
                "SELECT att_id, btih, size FROM attachments WHERE site=? AND post_id=?",
                (site, post_id),
            )
        }


def _attachment_known(site, att_id):
    with DB_LOCK:
        return (
            db()
            .execute(
                "SELECT 1 FROM attachments WHERE site=? AND att_id=?", (site, att_id)
            )
            .fetchone()
            is not None
        )


def _record_attachment(site, att_id, post_id, btih, name, size=0):
    with DB_LOCK:
        db().execute(
            "INSERT OR REPLACE INTO attachments(site, att_id, post_id, btih, name, fetched_at, size) "
            "VALUES (?,?,?,?,?,?,?)",
            (site, att_id, post_id, btih, name, time.time(), size),
        )
        db().commit()


def _attachment_reason(data):
    try:
        info = parse_torrent(data)[1]
    except Exception:
        return "not_torrent"
    return "private_torrent" if info.get(b"private") == 1 else "not_torrent"


def extract(site, post_id, url, page, language=None):
    """Store what a (live or archived) topic page offers -- releases (magnets, mangled magnets,
    .torrent attachments; an attachment whose file name matches a magnet's dn is the same torrent
    and is not downloaded) and web links -- for EVERY forum; whether any of it is served is decided
    when the feed is queried (is_served). language None = from the forum map, 'auto' = from the
    title. Problems go to the error log. Returns the number of releases found."""
    m = CANONICAL_RE.search(page)
    canon = m and (m.group(1) or m.group(2))
    if canon and _slug(canon):
        url = canon
    forum = topic_forum(page)
    posted_at = topic_posted_at(page)
    title = topic_title(page)
    if language is None:
        language = forum_language(site, forum)
    if language == "auto":
        language = lang_from_title(title)

    magnets = find_magnets(page)
    n = 0
    for mg in magnets:
        rec = parse_magnet(mg, post_id, language)
        if rec:
            rec["site"] = site
            upsert_release(rec)
            n += 1
        else:
            bt, dn, _ = _magnet_meta(mg)
            valid = re.fullmatch(r"[0-9a-f]{40}|[0-9a-z]{32}", bt or "")
            log_error(
                site,
                post_id,
                "magnet_skipped" if valid else "magnet_unparsed",
                "extract",
                "recorded",
                url,
                detail=(dn or mg)[:300],
                forum=forum,
                dedupe_key=bt or mg[:80],
            )

    names = _magnet_names(magnets)
    attachments = find_torrent_attachments(page)
    pending = 0
    for att_id, att_url, att_name in attachments:
        if att_name.lower()[: -len(".torrent")].strip() in names or _attachment_known(
            site, att_id
        ):
            continue
        st, data = fetch_plain(att_url, binary=True)
        if st != 200 or not data:
            # plain HTTP off, or a transient error: the topic stays 'pending' and is re-extracted
            # (from the archive) on its next visit, which retries only this download
            pending = 1
            log_error(
                site,
                post_id,
                "attachment_failed",
                "attachment",
                "pending",
                att_url,
                st,
                att_name,
                forum=forum,
                att_id=att_id,
            )
            continue
        got = magnet_from_torrent_bytes(data)
        size = _magnet_meta(got[1])[2] if got else 0
        _record_attachment(site, att_id, post_id, got[0] if got else "", att_name, size)
        if got:
            rec = parse_magnet(got[1], post_id, language)
            if rec:
                rec["site"] = site
                upsert_release(rec)
                n += 1
            else:
                log_error(
                    site,
                    post_id,
                    "magnet_skipped",
                    "attachment",
                    "recorded",
                    att_url,
                    detail=att_name,
                    forum=forum,
                    dedupe_key=got[0],
                )
        else:
            log_error(
                site,
                post_id,
                _attachment_reason(data),
                "attachment",
                "recorded",
                att_url,
                st,
                att_name,
                forum=forum,
                dedupe_key=att_id,
                att_id=att_id,
            )

    own_host = (urlparse(url).hostname or "").lower()
    links = [
        annotate_link(ln, title)
        for ln in find_weblinks(page, own_host, _attachment_map(site, post_id))
    ]
    replace_weblinks(site, post_id, links, title, language)
    mark_post(
        site,
        post_id,
        url,
        len(magnets) + len(attachments),
        forum,
        posted_at,
        title,
        pending,
    )
    if not magnets and not attachments and not links and is_served(site, forum, None):
        log_error(
            site,
            post_id,
            "empty",
            "extract",
            "recorded",
            url,
            detail=f"no torrents or links: {title}"[:300],
            forum=forum,
        )
    return n


def _safe_extract(site, post_id, url, page, language):
    try:
        return extract(site, post_id, url, page, language)
    except PlainUnavailable:
        raise
    except Exception as e:
        # a parser bug: record the topic (so the walk moves on) and the error; fix + `reparse`
        log_error(
            site,
            post_id,
            "extract_error",
            "extract",
            "recorded",
            url,
            detail=repr(e),
            dedupe_key=repr(e)[:80],
        )
        mark_post(site, post_id, url, 0)
        with DB_LOCK:
            db().execute(
                "UPDATE posts SET parsed_v=0, last_error='extract_error' WHERE site=? AND post_id=?",
                (site, post_id),
            )
            db().commit()
        return 0


def process_topic(site, post_id, url, language=None):
    """Fetch one topic, archive it, extract from it. Returns the number of releases found.
    Transient failures (network/challenge/429/5xx) are logged 'retry' and -- in the plain-only
    backfill -- re-raised so the whole listing page is retried; a topic that isn't there (404, a
    page without posts) is logged and marked gone so the walk moves on."""
    try:
        status, page = fetch_page(url)
    except PlainUnavailable as e:
        log_error(site, post_id, e.typ, "fetch", "retry", url, e.status, str(e))
        raise
    if status != 200 or not page:
        typ = fetch_error_type(status, page)
        if typ in TRANSIENT:
            log_error(site, post_id, typ, "fetch", "retry", url, status)
        else:
            mark_gone(site, post_id, url, typ)
            log_error(site, post_id, typ, "fetch", "gone", url, status)
        return 0
    if not ARTICLE_RE.search(
        page
    ):  # a moved/hidden stub (redirects home), an error page
        mark_gone(site, post_id, url, "no_article")
        log_error(
            site,
            post_id,
            "no_article",
            "fetch",
            "gone",
            url,
            status,
            f"no <article> on the page: {topic_title(page) or 'untitled'}"[:300],
        )
        return 0
    view = archive_view(page)
    m = CANONICAL_RE.search(view)
    archive_put(site, post_id, (m and (m.group(1) or m.group(2))) or url, view)
    return _safe_extract(site, post_id, url, view, language)


def reparse_topic(site, post_id, language=None):
    """Re-extract one topic from its archived copy (no page fetch; retries pending attachments)."""
    got = archive_get(site, post_id)
    if not got:
        return 0
    return _safe_extract(site, post_id, got[0], got[1], language)


def run_topics(site, items, workers=1):
    """Process [(post_id, url, action, language)] with `workers` threads ('fetch' | 'reparse' |
    'force' = fetch regardless). Returns (topics fetched, releases). Every item is processed or the
    call raises: the first PlainUnavailable stops the not-yet-started items and is re-raised after
    the running ones finish, so the caller retries the whole batch (done items are cheap no-ops the
    second time: topic_action returns None for them). Worker threads inherit the caller's fetch
    mode and log role."""
    plain_only = getattr(_CTX, "plain_only", False)
    role = getattr(_CTX, "role", "head")
    stop = threading.Event()

    def work(item):
        _CTX.plain_only, _CTX.role = plain_only, role
        if stop.is_set():
            return None
        pid, url, action, language = item
        try:
            if action == "reparse":
                return ("reparse", reparse_topic(site, pid, language))
            return ("fetch", process_topic(site, pid, url, language))
        except PlainUnavailable:
            stop.set()
            raise

    if workers <= 1 or len(items) <= 1:
        results = [work(it) for it in items]
    else:
        failure = None
        results = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in [ex.submit(work, it) for it in items]:
                try:
                    results.append(fut.result())
                except Exception as e:
                    failure = failure or e
        if failure:
            raise failure
    done = [r for r in results if r]
    return sum(1 for k, _ in done if k == "fetch"), sum(n for _, n in done)


def listing_url(base, forum_path, page):
    """Forum listing sorted by topic START date, newest first. The default (last-post) order moves
    old topics around as they get replies, so 'stop at a known page' and 'resume at page N' are
    only sound on this order."""
    seg = f"page/{page}/" if page > 1 else ""
    return f"{base}/index.php?/forums/forum/{forum_path}/{seg}&sortby=start_date&sortdirection=desc"


def read_listing(base, forum_path, page, site=None):
    """([(topic id, url)], total pages) or (None, 0) on failure. IPS answers a page past the end
    with page 1, so callers must stop at the page count, never on 'empty page'."""
    url = listing_url(base, forum_path, page)
    try:
        status, page_html = fetch_page(url)
    except PlainUnavailable as e:
        log_error(
            site,
            None,
            e.typ,
            "listing",
            "retry",
            url,
            e.status,
            str(e),
            forum=forum_id(forum_path),
        )
        raise
    if status != 200 or not page_html:
        log_error(
            site,
            None,
            "listing_failed",
            "listing",
            "retry",
            url,
            status,
            fetch_error_type(status, page_html),
            forum=forum_id(forum_path),
        )
        return None, 0
    counts = [int(x) for x in re.findall(r'data-pages=["\'](\d+)', page_html)]
    topics, seen = [], set()
    for m in TOPIC_RE.finditer(page_html):
        pid = int(m.group(1))
        if pid not in seen:
            seen.add(pid)
            topics.append((pid, m.group(0)))
    return topics, (max(counts) if counts else 1)


def _read_topics(site, topics, language, workers=1):
    items = []
    for pid, purl in topics:
        action = topic_action(site, pid, purl)
        if action:
            items.append((pid, purl, action, language))
    return run_topics(site, items, workers)


def crawl_forum(site, base, forum_path, language, known_before):
    """Head walk of one forum: pages 1..MAX_PAGES, stopping at the first page whose topics were all
    known before this pass (floored by MIN_PAGES) or at the last page."""
    posts = releases = 0
    for page in range(1, MAX_PAGES + 1):
        topics, pages = read_listing(base, forum_path, page, site)
        if not topics:
            break
        p, r = _read_topics(site, topics, language)
        posts += p
        releases += r
        if page >= pages or (
            page >= MIN_PAGES and all(pid in known_before for pid, _ in topics)
        ):
            break
    return posts, releases


def reset_walk_if_needed():
    """A WALK_EPOCH bump restarts the backfill of every forum from page 1 (once)."""
    if get_meta("walk_epoch") == str(WALK_EPOCH):
        return False
    with DB_LOCK:
        db().execute(
            "DELETE FROM meta WHERE key LIKE 'walk:%' OR key LIKE 'walkdone:%'"
        )
        db().commit()
    set_meta("walk_epoch", WALK_EPOCH)
    log(f"backfill: walk epoch {WALK_EPOCH} -- every forum is walked again from page 1")
    return True


def backfill(site, base, forums, budget, workers=1):
    """Resumable full walk of every forum, at most `budget` listing pages per call, each page's
    topics fetched by `workers` threads. Progress per forum lives in meta 'walk:<site>:<forum id>'
    (next page) and moves ONLY after every topic of the page was processed -- a failure anywhere on
    the page (PlainUnavailable) leaves it where it was, so the page is read again. Last page read ->
    'walkdone:<site>:<forum id>' = 1. A resume re-reads one page (new topics push the list down; a
    deleted one pulls it up by a slot)."""
    posts = releases = pages_read = 0
    for forum_path, language in forums:
        fid = forum_id(forum_path)
        if pages_read >= budget:
            break
        if get_meta(f"walkdone:{site}:{fid}") == "1":
            continue
        page = max(1, int(get_meta(f"walk:{site}:{fid}") or 1) - 1)
        while pages_read < budget:
            topics, pages = read_listing(base, forum_path, page, site)
            pages_read += 1
            if topics is None:
                break
            p, r = _read_topics(site, topics, language, workers)
            posts += p
            releases += r
            if page >= pages:
                set_meta(f"walkdone:{site}:{fid}", 1)
                log(f"  backfill: forum {forum_path} complete ({pages} pages)")
                break
            page += 1
            set_meta(f"walk:{site}:{fid}", page)
    return posts, releases, pages_read


def backfill_remaining(site, forums):
    return sum(
        1 for path, _ in forums if get_meta(f"walkdone:{site}:{forum_id(path)}") != "1"
    )


SITEMAP_TOPIC_RE = re.compile(
    r"<loc>([^<]*/forums/topic/(\d+)-[^<]*)</loc>\s*<lastmod>([^<]+)</lastmod>", re.I
)
SITEMAP_FORUM_RE = re.compile(r"<loc>[^<]*/forums/forum/((\d+)-[^/<]*)/?</loc>", re.I)


def discover_forums(site, base):
    """Record every forum the site lists in its sitemap (all of them are archived by the backfill;
    which are served is decided by is_served). Returns the number found."""
    st, body = fetch_plain(f"{base}/sitemap.php?file=sitemap_content_forums_Forum")
    if st != 200 or not body:
        return 0
    entries = [(fid, path) for path, fid in SITEMAP_FORUM_RE.findall(body)]
    if entries:
        upsert_forums(site, entries)
    return len(entries)


def sitemap_topics(site, base, now=None):
    """{topic id: (url, lastmod ts)} for every topic in the site's sitemap, or None if unavailable."""
    now = now or time.time()
    status, index = fetch_plain(f"{base}/sitemap.php")
    if status != 200 or not index:
        log(f"  sitemap: index unavailable ({status})")
        return None
    files = [
        htmlmod.unescape(u)
        for u in re.findall(r"<loc>([^<]*forums_Topic[^<]*)</loc>", index)
    ]
    entries = {}
    for f in files:
        st, body = fetch_plain(f)
        if st != 200 or not body:
            log(f"  sitemap: {f} unavailable ({st})")
            return None
        for loc, pid, lastmod in SITEMAP_TOPIC_RE.findall(body):
            try:
                ts = datetime.datetime.fromisoformat(lastmod.strip()).timestamp()
            except ValueError:
                ts = now
            entries[int(pid)] = (htmlmod.unescape(loc), ts)
    return entries


def sitemap_sweep(site, base, now=None):
    """Safety net for the walk: read every topic in the IPS sitemap we have never seen whose lastmod
    is within SITEMAP_DAYS -- catches a topic moved into a forum after the walk passed it, or one a
    listing hiccup skipped. The same daily pass refreshes the forum list. Needs plain HTTP."""
    now = now or time.time()
    if (
        not HTTP_PROXY
        or now - float(get_meta(f"sitemap:{site}") or 0) < SITEMAP_INTERVAL
    ):
        return 0, 0
    role, _CTX.role = getattr(_CTX, "role", "head"), "sitemap"
    try:
        nforums = discover_forums(site, base)
        entries = sitemap_topics(site, base, now)
        if entries is None:
            return 0, 0
        with DB_LOCK:
            known = {
                r[0]
                for r in db().execute("SELECT post_id FROM posts WHERE site=?", (site,))
            }
        todo = [
            (pid, loc)
            for pid, (loc, ts) in sorted(entries.items(), reverse=True)
            if pid not in known and now - ts < SITEMAP_DAYS * 86400
        ]
        posts = releases = 0
        for pid, loc in todo:
            releases += process_topic(site, pid, loc, None)
            posts += 1
        set_meta(f"sitemap:{site}", now)
        log(
            f"  sitemap: {nforums} forums, {len(entries)} topics listed, {len(todo)} unseen within "
            f"{SITEMAP_DAYS:g}d read, {releases} releases"
        )
        return posts, releases
    finally:
        _CTX.role = role


def audit(site, base, workers=1):
    """Completeness check after a full walk: every topic in the sitemap (all forums, no time window)
    that isn't archived, is gone, or has a pending attachment gets one more try (gone ones too, in
    case a hiccup marked them wrongly). Whatever still fails is logged 'audit_missing'.
    Returns (listed, archived before, tried, still missing) or None when the sitemap is unavailable."""
    role, _CTX.role = getattr(_CTX, "role", "head"), "audit"
    try:
        entries = sitemap_topics(site, base)
        if entries is None:
            return None
        with DB_LOCK:
            state = {
                r["post_id"]: (r["gone"], r["pending"])
                for r in db().execute(
                    "SELECT post_id, gone, pending FROM posts WHERE site=?", (site,)
                )
            }
        items, have = [], 0
        for pid, (loc, _) in sorted(entries.items(), reverse=True):
            gone, pending = state.get(pid, (0, 0))
            is_archived = archived(site, pid)
            have += 1 if is_archived else 0
            if not is_archived or gone:
                items.append((pid, loc, "force", None))
            elif pending:
                items.append((pid, loc, "reparse", None))
        for i in range(
            0, len(items), 25
        ):  # batches: a transient failure retries 25, not all
            run_topics(site, items[i : i + 25], workers)
        missing = 0
        for pid, loc, _, _ in items:
            with DB_LOCK:
                row = (
                    db()
                    .execute(
                        "SELECT gone, pending, last_error FROM posts WHERE site=? AND post_id=?",
                        (site, pid),
                    )
                    .fetchone()
                )
            if not archived(site, pid) or (row and (row["gone"] or row["pending"])):
                missing += 1
                log_error(
                    site,
                    pid,
                    "audit_missing",
                    "audit",
                    "recorded",
                    loc,
                    detail=(row["last_error"] if row else "never read")
                    or "pending attachment",
                    dedupe_key=get_meta("walk_epoch"),
                )
        log(
            f"audit {site}: {len(entries)} topics in the sitemap, {have} archived before, "
            f"{len(items)} retried, {missing} still missing (see the error log)"
        )
        return len(entries), have, len(items), missing
    finally:
        _CTX.role = role


def refresh_active_series(site, base):
    """Re-read series topics that gained a release in the last ACTIVE_DAYS, wherever they are.

    The slug check in post_needs_fetch only fires if the crawl SEES the topic, and it doesn't:
    crawl_forum stops at the first page of already-known topics, and a season topic started
    weeks ago sits on page 2+ even while it grows. Heart Beat S03 (topic 199088) went
    ep-01-32 -> ep-01-36 on page 2 of the web-series forum and a full pass fetched 0 topics.
    So this fetches the topic directly: the stored slug still resolves (the forum serves the
    topic by id), and process_topic adopts the canonical URL the page reports. ~20 topics at
    21 days, each at most every ACTIVE_REREAD s -- a few fetches per hourly pass."""
    if ACTIVE_DAYS <= 0:
        return 0, 0
    now = time.time()
    with DB_LOCK:
        rows = (
            db()
            .execute(
                """SELECT p.post_id, p.url, max(r.language) AS language FROM posts p
               JOIN releases r ON r.site = p.site AND r.post_id = p.post_id
               WHERE p.site = ? AND r.kind = 'series'
               GROUP BY p.post_id HAVING max(r.added_at) > ?""",
                (site, now - ACTIVE_DAYS * 86400),
            )
            .fetchall()
        )
    posts = releases = 0
    for r in rows:
        key = f"reread:{site}:{r['post_id']}"
        if now - float(get_meta(key) or 0) < ACTIVE_REREAD:
            continue
        slug = _slug(r["url"])
        if not slug:
            continue
        # current domain + stored slug: the domain rotates, the topic id doesn't
        url = f"{base}/index.php?/forums/topic/{r['post_id']}-{slug}/"
        with DB_LOCK:
            before = (
                db()
                .execute(
                    "SELECT count(*) FROM releases WHERE site=? AND post_id=?",
                    (site, str(r["post_id"])),
                )
                .fetchone()[0]
            )
        process_topic(site, r["post_id"], url, r["language"])
        with DB_LOCK:
            after = (
                db()
                .execute(
                    "SELECT count(*) FROM releases WHERE site=? AND post_id=?",
                    (site, str(r["post_id"])),
                )
                .fetchone()[0]
            )
        set_meta(key, now)
        posts += 1
        if after > before:
            releases += after - before
            log(f"  active series {r['post_id']}: +{after - before} new release(s)")
    return posts, releases


def _active_role(fn):
    def wrapped(*a, **kw):
        role, _CTX.role = getattr(_CTX, "role", "head"), "active"
        try:
            return fn(*a, **kw)
        finally:
            _CTX.role = role

    return wrapped


refresh_active_series = _active_role(refresh_active_series)


def crawl_site(site):
    """One hourly pass over a site:
      1. head walk   -- every release forum, pages 1..MAX_PAGES by start date, incremental;
      2. sitemap     -- daily: forum list + unseen topics from sitemap.php, SITEMAP_DAYS window;
      3. active series re-reads (series topics that grow in place).
    The full walk of every forum is run_backfill's job (its own thread)."""
    if not load_config():
        return
    base = site_base(site)
    if not base:
        return
    with DB_LOCK:
        known = {
            r[0]
            for r in db()
            .execute("SELECT post_id FROM posts WHERE site=?", (site,))
            .fetchall()
        }
    forums = list(release_forums(site).values())
    log(
        f"{site}: head walk of {len(forums)} release forum(s) at {base} "
        f"(plain HTTP {'via ' + HTTP_PROXY if HTTP_PROXY else 'off'})"
    )
    tp = tr = 0
    for forum_path, language in forums:
        try:
            p, r = crawl_forum(site, base, forum_path, language, known)
            tp += p
            tr += r
        except Exception as e:
            log(f"  forum {forum_path} error: {e}")
    sp = sr = 0
    try:
        sp, sr = sitemap_sweep(site, base)
    except Exception as e:
        log(f"  sitemap error: {e}")
    try:
        ap, ar = refresh_active_series(site, base)
    except Exception as e:
        ap = ar = 0
        log(f"  active-series refresh error: {e}")
    log(
        f"{site}: done -- {tp} topics fetched, {tr} releases indexed; "
        f"sitemap: {sp} topics, {sr} releases; active series: {ap} re-read, {ar} new"
    )


def recent_series_titles():
    """Served titles' source for nudge_sonarr: series releases indexed in the last SONARR_NUDGE_DAYS."""
    with DB_LOCK:
        rows = (
            db()
            .execute(
                "SELECT DISTINCT title FROM releases WHERE kind='series' AND added_at > ?",
                (time.time() - SONARR_NUDGE_DAYS * 86400,),
            )
            .fetchall()
        )
    return [r[0] for r in rows]


def run_crawler():
    db()  # init schema
    adb()
    _CTX.role = "head"
    if not any(SITES.values()) and not load_config():
        log("no config available -- crawler idle; Torznab feed still serves")
        return
    log(
        f"crawler starting; both sites every {CRAWL_INTERVAL}s (plain HTTP {HTTP_PROXY or 'off'}, "
        f"fallback {FLARESOLVERR_URL})"
    )
    while True:
        try:
            for site in SITES:
                crawl_site(site)
        except Exception as e:
            log(f"crawl error: {e}")
        try:
            nudge_sonarr(recent_series_titles())
        except Exception as e:
            log(f"sonarr nudge error: {e}")
        time.sleep(CRAWL_INTERVAL)


def run_backfill():
    """Walk every forum of every site to its last page, continuously, BACKFILL_WORKERS topics at a
    time; when a site's walk is complete, run the audit once (per walk epoch); then check every 10
    min (a `recrawl` resets progress; the head walk and sitemap keep things current). Plain HTTP
    only: on a challenge/outage it pauses BACKFILL_PAUSE s rather than falling back to FlareSolverr.
    The live base is re-resolved at most hourly (a dead site costs two timeouts an hour)."""
    db()
    adb()
    if not HTTP_PROXY:
        log("backfill: off (needs TAMILMV_HTTP_PROXY)")
        while True:
            time.sleep(86400)
    _CTX.plain_only, _CTX.role = True, "backfill"
    reset_walk_if_needed()
    log(
        f"backfill: {BACKFILL_WORKERS} worker(s), {BACKFILL_CHUNK} listing pages per step"
    )
    bases = {}  # site -> (base or None, resolved at)
    stats = {"pages": 0, "topics": 0, "releases": 0, "logged": time.time()}
    while True:
        worked = False
        for site in SITES:
            try:
                if not load_config():
                    continue
                base, at = bases.get(site, (None, 0))
                if time.time() - at > 3600:
                    base = site_base(site)
                    bases[site] = (base, time.time())
                if not base:
                    continue
                if not known_forums(site):
                    discover_forums(site, base)
                forums = backfill_order(site)
                if not backfill_remaining(site, forums):
                    if get_meta(f"audit:{site}") != get_meta("walk_epoch"):
                        if audit(site, base, BACKFILL_WORKERS) is not None:
                            set_meta(f"audit:{site}", get_meta("walk_epoch"))
                    continue
                p, r, n = backfill(site, base, forums, BACKFILL_CHUNK, BACKFILL_WORKERS)
                worked = worked or n > 0
                stats["pages"] += n
                stats["topics"] += p
                stats["releases"] += r
                if time.time() - stats["logged"] >= 600:
                    log(
                        f"backfill {site}: {stats['pages']} pages, {stats['topics']} topics read, "
                        f"{stats['releases']} releases in the last {int((time.time() - stats['logged']) / 60)} min; "
                        f"{backfill_remaining(site, forums)} of {len(forums)} forum(s) left"
                    )
                    stats.update(pages=0, topics=0, releases=0, logged=time.time())
            except PlainUnavailable as e:
                log(
                    f"backfill: plain HTTP unavailable ({e}); pausing {BACKFILL_PAUSE:g}s"
                )
                bases.pop(site, None)
                time.sleep(BACKFILL_PAUSE)
            except Exception as e:
                log(f"backfill {site} error: {e!r}")
                time.sleep(60)
        if not worked:
            time.sleep(600)


# ---- Torznab (own server on :8005) ----
def caps_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<caps>\n"
        '  <server title="tamilmv"/>\n'
        '  <limits max="100" default="100"/>\n'
        "  <searching>\n"
        '    <search available="yes" supportedParams="q"/>\n'
        '    <movie-search available="yes" supportedParams="q,year"/>\n'
        '    <tv-search available="yes" supportedParams="q,season,ep"/>\n'
        "  </searching>\n"
        "  <categories>\n"
        '    <category id="2000" name="Movies">\n'
        '      <subcat id="2030" name="Movies/SD"/>\n'
        '      <subcat id="2040" name="Movies/HD"/>\n'
        "    </category>\n"
        '    <category id="5000" name="TV">\n'
        '      <subcat id="5030" name="TV/SD"/>\n'
        '      <subcat id="5040" name="TV/HD"/>\n'
        "    </category>\n"
        "  </categories>\n"
        "</caps>\n"
    )


def _served_rows(sql, args=()):
    """Rows of `sql` (which must select p.forum AS pforum, p.serve AS pserve) that are served."""
    with DB_LOCK:
        rows = db().execute(sql, args).fetchall()
    overrides = forum_overrides()
    released = {site: release_forums(site) for site in SITES}
    return [
        r
        for r in rows
        if r["site"] in SITES
        and is_served(
            r["site"], r["pforum"], r["pserve"], overrides, released.get(r["site"], {})
        )
    ]


def _wanted(t, kind, row_cats, cats, season, row_season):
    if t == "movie" and kind != "movie":
        return False
    if t == "tvsearch" and kind != "series":
        return False
    if cats:
        fam, sub = row_cats
        if not any(c == fam or c == sub for c in cats):
            return False
    if season and kind == "series" and row_season and int(season) != row_season:
        return False
    return True


def query_releases(t, q, season, cats, offset=0, limit=PAGE_MAX):
    rows = _served_rows(
        "SELECT r.*, p.forum AS pforum, p.serve AS pserve FROM releases r "
        "LEFT JOIN posts p ON p.site = r.site AND p.post_id = r.post_id ORDER BY r.added_at DESC"
    )
    terms = search_terms(
        q
    )  # shared with movierulz: alias-aware, never matches-everything
    out = []
    for r in rows:
        fam = 5000 if r["kind"] == "series" else 2000
        if not _wanted(
            t, r["kind"], (fam, torznab_category(r)), cats, season, r["season"]
        ):
            continue
        rank = match_rank(terms, r["title"])
        if rank is None:
            continue
        out.append((rank, r))
    return page(out, offset, limit)


def query_links(t, q, season, cats, offset=0):
    """Served direct-download links matching a SEARCH (never the empty-query RSS feed), on the
    first page only -- later pages would repeat them."""
    if not (q or "").strip() or offset:
        return []
    rows = _served_rows(
        "SELECT w.*, p.forum AS pforum, p.serve AS pserve FROM weblinks w "
        "LEFT JOIN posts p ON p.site = w.site AND p.post_id = w.post_id "
        "WHERE w.lid IS NOT NULL AND w.kind IN ('movie', 'series') ORDER BY w.found_at DESC"
    )
    terms = search_terms(q)
    out = []
    for r in rows:
        fam = 5000 if r["kind"] == "series" else 2000
        if not _wanted(
            t, r["kind"], (fam, torznab_category(r)), cats, season, r["season"]
        ):
            continue
        rank = match_rank(terms, r["title"] or "")
        if rank is None:
            continue
        out.append((rank, r))
    return page(out, 0, LINKS_MAX)


DVD_DISC_RE = re.compile(r"\bDVD\s?-?\s?[59]\b", re.I)


def served_title(title, kind, magnet=None):
    """Title as served: Sonarr's show year for series, and 'DVD9/DVD5' -> 'DVD Remux' when the
    torrent is ONE video file (a remux, importable). Radarr reads 'DVD9' as a raw disc image and
    rejects it ('Raw Bluray/DVD release') -- right for an ISO/VIDEO_TS torrent, which keeps it."""
    t = series_title(title) if kind == "series" else title
    if DVD_DISC_RE.search(t) and (
        magnet is None or VIDEO_EXT_RE.search(_dn(magnet)[-6:])
    ):
        t = DVD_DISC_RE.sub("DVD Remux", t)
    return t


def topic_url(site, post_id):
    return f"{PUBLIC_URL}/t/{urllib.parse.quote(site)}/{post_id}"


def results_xml(rows, links=(), feed_base=None):
    """Torznab items. Releases carry their topic page as <comments> (Prowlarr -> Radarr/Sonarr
    'info' link). Links are '[DL-LINK] <title>' items that can't be taken: 0 seeders (every
    indexer minimum is >= 1, so Radarr/Sonarr always reject them) and a download URL that answers
    404 -- read by Radarr/Prowlarr as 'release no longer available', not an indexer failure."""
    feed_base = (feed_base or PUBLIC_URL).rstrip("/")
    items = []
    for r in rows:
        magnet = r["magnet"]
        size = r["size"] or 0
        cat = torznab_category(r)
        pub = email.utils.formatdate(r["added_at"], usegmt=True)
        items.append(
            "    <item>\n"
            f"      <title>{escape(served_title(r['title'], r['kind'], magnet))}</title>\n"
            f'      <guid isPermaLink="false">{r["btih"]}</guid>\n'
            f"      <pubDate>{pub}</pubDate>\n"
            f"      <size>{size}</size>\n"
            f"      <comments>{escape(topic_url(r['site'], r['post_id']))}</comments>\n"
            f"      <link>{escape(magnet)}</link>\n"
            f'      <enclosure url="{escape(magnet)}" length="{size}" type="application/x-bittorrent"/>\n'
            f'      <torznab:attr name="category" value="{cat}"/>\n'
            f'      <torznab:attr name="magneturl" value="{escape(magnet)}"/>\n'
            f'      <torznab:attr name="seeders" value="{FAKE_SEEDERS}"/>\n'
            f'      <torznab:attr name="peers" value="{FAKE_SEEDERS}"/>\n'
            f'      <torznab:attr name="size" value="{size}"/>\n'
            "    </item>\n"
        )
    for w in links:
        size = w["size"] or 0
        cat = torznab_category(w)
        nograb = f"{feed_base}/nograb/{w['lid']}.torrent"
        items.append(
            "    <item>\n"
            f"      <title>{escape(LINK_FLAG + ' ' + served_title(w['title'], w['kind']))}</title>\n"
            f'      <guid isPermaLink="false">dl-link-{w["lid"]}</guid>\n'
            f"      <pubDate>{email.utils.formatdate(w['found_at'], usegmt=True)}</pubDate>\n"
            f"      <size>{size}</size>\n"
            f"      <comments>{escape(topic_url(w['site'], w['post_id']) + '#l' + w['lid'])}</comments>\n"
            f"      <link>{escape(nograb)}</link>\n"
            f'      <enclosure url="{escape(nograb)}" length="{size}" type="application/x-bittorrent"/>\n'
            f'      <torznab:attr name="category" value="{cat}"/>\n'
            '      <torznab:attr name="seeders" value="0"/>\n'
            '      <torznab:attr name="peers" value="0"/>\n'
            f'      <torznab:attr name="size" value="{size}"/>\n'
            "    </item>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">\n'
        "  <channel>\n"
        "    <title>tamilmv</title>\n"
        "    <description>tamilmv torznab feed</description>\n"
        "    <link>http://media-bridge:8005/torznab</link>\n"
        + "".join(items)
        + "  </channel>\n"
        "</rss>\n"
    )


# ---- topic page (/t/<site>/<id>): the info link of every item ----
# Scraped HTML is shown with scripts impossible (CSP) and stripped anyway; images blocked (they'd
# tell third-party image hosts who is looking).
PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
_DROP_BLOCK_RE = re.compile(
    r"<(script|style|iframe|object|embed|noscript|template|svg)\b.*?</\1\s*>",
    re.I | re.S,
)
_DROP_TAG_RE = re.compile(
    r"</?(script|iframe|object|embed|link|meta|base|form|input|button|textarea|"
    r"select|frame|frameset|svg)\b[^>]*>",
    re.I,
)
_EVENT_ATTR_RE = re.compile(r"""\s+on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_JS_URL_RE = re.compile(
    r"""(href|src|action)\s*=\s*(["'])\s*(?:javascript|data|vbscript):[^"']*\2""", re.I
)
_SITE_HOST_RE = {
    "tamilmv": re.compile(r"https?://(?:www\.)?1tamilmv\.[a-z]+", re.I),
}


def sanitize(html_, site=None, base=None):
    s = _DROP_BLOCK_RE.sub("", html_)
    s = _DROP_TAG_RE.sub("", s)
    s = _EVENT_ATTR_RE.sub("", s)
    s = _JS_URL_RE.sub(r'\1="#"', s)
    if base and site in _SITE_HOST_RE:
        s = _SITE_HOST_RE[site].sub(base, s)  # old domains -> the live one
    return s


def _fmt_size(n):
    return f"{n / 1e9:.2f} GB" if n and n >= 1e9 else (f"{n / 1e6:.0f} MB" if n else "")


def topic_page(site, post_id):
    """(status, html) of a topic's page: direct links, live tamilmv link, torrents, saved copy."""
    with DB_LOCK:
        post = (
            db()
            .execute("SELECT * FROM posts WHERE site=? AND post_id=?", (site, post_id))
            .fetchone()
        )
        rels = (
            db()
            .execute(
                "SELECT * FROM releases WHERE btih IN (SELECT btih FROM release_topics WHERE site=? AND post_id=?) "
                "OR (site=? AND post_id=?) ORDER BY size DESC",
                (site, post_id, site, post_id),
            )
            .fetchall()
        )
        links = (
            db()
            .execute(
                "SELECT * FROM weblinks WHERE site=? AND post_id=? ORDER BY rowid",
                (site, post_id),
            )
            .fetchall()
        )
    if post is None or site not in SITES:
        return 404, "<!doctype html><meta charset=utf-8><p>Unknown topic."
    q = htmlmod.escape
    arch = archive_get(site, post_id)
    base = get_meta(f"base:{site}")
    slug = _slug(post["url"])
    live = (
        f"{base}/index.php?/forums/topic/{post_id}-{slug}/"
        if base and slug
        else post["url"]
    )
    served = is_served(site, post["forum"], post["serve"])
    title = (
        post["title"] or (topic_title(arch[1]) if arch else "") or f"topic {post_id}"
    )
    rel_names = {r["btih"]: r["title"] for r in rels}
    posted = (
        datetime.datetime.fromtimestamp(
            post["posted_at"], datetime.timezone.utc
        ).strftime("%Y-%m-%d")
        if post["posted_at"]
        else "?"
    )
    out = [
        "<!doctype html><html><head><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        "<meta name=referrer content=no-referrer>",
        f"<title>{q(title)}</title><style>",
        ":root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--hl:#fff4c2;--acc:#0a66c2}",
        "@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f7;--mut:#a1a1a6;",
        "--line:#2c2c2e;--hl:#3a3212;--acc:#5aa9ff}}",
        "body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;",
        "max-width:980px;margin:0 auto;padding:16px}a{color:var(--acc)}",
        "h1{font-size:20px;margin:0 0 6px}h2{font-size:16px;margin:22px 0 8px}",
        ".mut{color:var(--mut)}table{border-collapse:collapse;width:100%}",
        "td{border-top:1px solid var(--line);padding:6px 8px 6px 0;vertical-align:top;",
        "overflow-wrap:anywhere}tr:target{background:var(--hl)}",
        ".post{border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}",
        ".post img{display:none}.tag{font-size:12px;border:1px solid var(--line);",
        "border-radius:4px;padding:0 5px;margin-left:6px}</style></head><body>",
        f"<h1>{q(title)}</h1>",
        f"<p class=mut>{q(site)} topic {post_id} · forum {q(post['forum'] or '?')} · posted {posted}"
        f"<span class=tag>{'served' if served else 'not served'}</span>"
        + ("<span class=tag>gone</span>" if post["gone"] else "")
        + ("<span class=tag>attachment pending</span>" if post["pending"] else "")
        + (
            f"<span class=tag>{q(post['last_error'])}</span>"
            if post["last_error"]
            else ""
        )
        + "</p>",
        f"<p><a href='{q(live)}' target=_blank rel='noopener noreferrer'>Open on tamilmv ↗</a>"
        + (f" <span class=mut>(live domain {q(base)})</span>" if base else "")
        + "</p>",
        "<h2>Direct links</h2>",
    ]
    if links:
        out.append("<table>")
        for w in links:
            same = rel_names.get(w["btih"]) if w["btih"] else None
            out.append(
                f"<tr id='l{q(w['lid'] or '')}'><td><a href='{q(w['url'])}' target=_blank "
                f"rel='noopener noreferrer'>{q(w['host'])} ↗</a></td>"
                f"<td>{q(w['label'] or '')}"
                + (
                    f"<br><span class=mut>same file as torrent: {q(same)}</span>"
                    if same
                    else ""
                )
                + f"</td><td class=mut>{_fmt_size(w['size'])}</td></tr>"
            )
        out.append("</table>")
    else:
        out.append("<p class=mut>None.</p>")
    out.append("<h2>Torrents</h2>")
    if rels:
        out.append("<table>")
        for r in rels:
            out.append(
                f"<tr><td><a href='{q(r['magnet'])}'>magnet</a></td><td>{q(r['title'])}</td>"
                f"<td class=mut>{_fmt_size(r['size'])}</td></tr>"
            )
        out.append("</table>")
    else:
        out.append("<p class=mut>None.</p>")
    out.append("<h2>Saved copy</h2>")
    if arch:
        saved = datetime.datetime.fromtimestamp(
            arch[2], datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        out.append(
            f"<p class=mut>saved {saved} from {q(arch[0])}</p>"
            f"<div class=post>{sanitize(arch[1], site, base)}</div>"
        )
    else:
        out.append("<p class=mut>Not archived yet (the backfill will get to it).</p>")
    out.append("</body></html>")
    return 200, "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, code=200, ctype="application/xml; charset=utf-8", csp=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if csp:
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _feed_base(self):
        host = self.headers.get("Host", "")
        return (
            f"http://{host}"
            if re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?", host)
            else PUBLIC_URL
        )

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/nograb/"):
            # a '[DL-LINK]' item was force-grabbed: 404 = 'release no longer available'
            self._send(
                "not a torrent: open the item's info link for the direct download",
                404,
                "text/plain; charset=utf-8",
            )
            return
        m = re.fullmatch(r"/t/([a-z_]+)/(\d+)/?", path)
        if m:
            try:
                code, body = topic_page(m.group(1), int(m.group(2)))
            except Exception as e:
                log(f"topic page error: {e!r}")
                code, body = 500, "<!doctype html><meta charset=utf-8><p>error"
            self._send(body, code, "text/html; charset=utf-8", PAGE_CSP)
            return
        if not path.rstrip("/").endswith("/api") and "/torznab" not in path:
            self._send(error_xml(200, "not found"), 404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        t = (qs.get("t", ["caps"])[0]).lower()
        if t == "caps":
            self._send(caps_xml())
            return
        if APIKEY and qs.get("apikey", [""])[0] != APIKEY:
            self._send(error_xml(100, "Incorrect user credentials"))
            return
        if t not in ("search", "movie", "tvsearch"):
            self._send(error_xml(202, f"No such function ({t})"))
            return
        q = qs.get("q", [""])[0]
        season = qs.get("season", [""])[0]
        cats = []
        for c in qs.get("cat", []):
            cats += [int(x) for x in c.split(",") if x.strip().isdigit()]
        offset, limit = page_args(qs)
        try:
            rows = query_releases(t, q, season, cats, offset, limit)
            links = query_links(t, q, season, cats, offset)
            self._send(results_xml(rows, links, self._feed_base()))
        except Exception as e:
            log(f"torznab query error: {e!r}")
            self._send(error_xml(900, "internal error"))


def serve_torznab():
    db()
    adb()
    load_config()  # serve flags need the forum map; the crawler thread may not have loaded it yet
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(
        f"torznab server listening on :{PORT} (apikey {'set' if APIKEY else 'DISABLED'}; "
        f"topic pages at {PUBLIC_URL}/t/...)"
    )
    httpd.serve_forever()


def print_links(query):
    """`python -m tamilmv links <words>`: stored web links whose topic matches every word."""
    words = [w for w in re.split(r"\s+", query.lower()) if w]
    with DB_LOCK:
        rows = (
            db()
            .execute("SELECT * FROM weblinks ORDER BY post_id DESC, rowid")
            .fetchall()
        )
    last = None
    for r in rows:
        hay = f"{r['topic']} {r['label']}".lower()
        if not all(w in hay for w in words):
            continue
        if (r["site"], r["post_id"]) != last:
            last = (r["site"], r["post_id"])
            print(
                f"\n[{r['site']} {r['post_id']}] {r['topic']}  ({r['language']})  "
                f"{topic_url(r['site'], r['post_id'])}"
            )
        size = f"{r['size'] / 1e9:.1f} GB  " if r["size"] else ""
        print(f"  {size}{r['url']}\n      {r['label'][:160]}")


def reparse_all(site=None):
    """`python -m tamilmv reparse [site]`: re-run extract() over the archive (no page fetches)."""
    load_config()
    with ARCH_LOCK:
        keys = [
            (r["site"], r["post_id"])
            for r in adb().execute(
                "SELECT site, post_id FROM pages" + (" WHERE site=?" if site else ""),
                (site,) if site else (),
            )
            if r["site"] in SITES
        ]
    n = 0
    for i, (s, pid) in enumerate(keys, 1):
        n += reparse_topic(s, pid)
        if i % 500 == 0:
            print(f"  {i}/{len(keys)} topics re-extracted", flush=True)
    print(f"reparsed {len(keys)} topics, {n} releases")


def _parse_topic_arg(arg):
    """'174730', a topic URL, or '174730-slug' -> (post id, url path suffix)."""
    m = re.search(r"topic/(\d+)-?([^/?#]*)", arg) or re.fullmatch(
        r"(\d+)-?([^/?#]*)", arg.strip()
    )
    if not m:
        raise SystemExit(f"not a topic id or URL: {arg}")
    return int(m.group(1)), m.group(2)


def refetch_cli(args, site="tamilmv"):
    """`python -m tamilmv refetch <topic id | URL> ...`: fetch now (FlareSolverr fallback allowed),
    re-archive, re-extract, and print what the topic yields -- searchable immediately."""
    load_config()
    _CTX.role = "manual"
    base = get_meta(f"base:{site}") or site_base(site)
    for arg in args:
        pid, slug = _parse_topic_arg(arg)
        with DB_LOCK:
            row = (
                db()
                .execute(
                    "SELECT url FROM posts WHERE site=? AND post_id=?", (site, pid)
                )
                .fetchone()
            )
        slug = slug or _slug(row["url"] if row else "") or "x"
        url = f"{base}/index.php?/forums/topic/{pid}-{slug}/"
        n = process_topic(site, pid, url)
        with DB_LOCK:
            post = (
                db()
                .execute("SELECT * FROM posts WHERE site=? AND post_id=?", (site, pid))
                .fetchone()
            )
            rels = (
                db()
                .execute(
                    "SELECT title, size FROM releases WHERE btih IN "
                    "(SELECT btih FROM release_topics WHERE site=? AND post_id=?)",
                    (site, pid),
                )
                .fetchall()
            )
            links = (
                db()
                .execute(
                    "SELECT title, host, size FROM weblinks WHERE site=? AND post_id=?",
                    (site, pid),
                )
                .fetchall()
            )
        state = (
            ("GONE (" + (post["last_error"] or "") + ")")
            if post and post["gone"]
            else "ok"
        )
        print(
            f"[{site} {pid}] {state} forum={post['forum'] if post else '?'} "
            f"served={is_served(site, post['forum'], post['serve']) if post else '?'} "
            f"new releases={n}  {topic_url(site, pid)}\n  {post['title'] if post else ''}"
        )
        for r in rels:
            print(f"    torrent  {_fmt_size(r['size']):>9}  {r['title']}")
        for w in links:
            print(f"    link     {_fmt_size(w['size']):>9}  {w['host']}  {w['title']}")


def recrawl_cli(args, site="tamilmv"):
    """`python -m tamilmv recrawl [--forum ID ...] [--refetch]`: restart the backfill walk (all
    forums, or the given ones) and its final audit; the running backfill thread picks it up within
    10 min. Without --refetch only new / not-archived / changed topics are fetched on the way; with
    it every topic archived before now is downloaded again."""
    forums = [a for a in args if a.isdigit()]
    with DB_LOCK:
        if forums:
            for fid in forums:
                db().execute(
                    "DELETE FROM meta WHERE key IN (?, ?)",
                    (f"walk:{site}:{fid}", f"walkdone:{site}:{fid}"),
                )
        else:
            db().execute(
                "DELETE FROM meta WHERE key LIKE ? OR key LIKE ?",
                (f"walk:{site}:%", f"walkdone:{site}:%"),
            )
        db().execute("DELETE FROM meta WHERE key=?", (f"audit:{site}",))
        db().commit()
    if "--refetch" in args:
        set_meta(f"refetch_before:{site}", time.time())
    print(
        f"recrawl: {'forums ' + ', '.join(forums) if forums else 'all forums'} of {site} reset"
        + (" (every topic will be downloaded again)" if "--refetch" in args else "")
        + "; the backfill thread starts within 10 min"
    )


def errors_cli(args):
    """`python -m tamilmv errors [--type T] [--since 24h|7d] [--unresolved]`: summarize the error log."""
    typ = args[args.index("--type") + 1] if "--type" in args else None
    since = 0
    if "--since" in args:
        v = args[args.index("--since") + 1]
        since = time.time() - float(v[:-1]) * {"h": 3600, "d": 86400}[v[-1]]
    unresolved_only = "--unresolved" in args
    rows = []
    for path in (ERRORS_PATH + ".1", ERRORS_PATH):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass

    def ts(r):
        return (
            datetime.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )

    def unresolved(r):
        if r.get("post_id") is None:
            return r.get("outcome") == "retry"
        with DB_LOCK:
            p = (
                db()
                .execute(
                    "SELECT gone, pending, last_error FROM posts WHERE site=? AND post_id=?",
                    (r["site"], r["post_id"]),
                )
                .fetchone()
            )
        if r["type"] in ONE_OFF:
            return True  # findings: resolved only by fixing the parser/rules
        return (
            p is None
            or bool(p["gone"] or p["pending"])
            or not archived(r["site"], r["post_id"])
        )

    rows = [r for r in rows if (not typ or r["type"] == typ) and ts(r) >= since]
    if unresolved_only:
        rows = [r for r in rows if unresolved(r)]
    by = {}
    for r in rows:
        by.setdefault(r["type"], []).append(r)
    day = time.time() - 86400
    print(f"{len(rows)} entries in {ERRORS_PATH}" + (f" (type {typ})" if typ else ""))
    for t_, items in sorted(by.items(), key=lambda kv: -len(kv[1])):
        open_n = sum(1 for r in items if unresolved(r))
        print(
            f"\n{t_}: {len(items)} total, {sum(1 for r in items if ts(r) >= day)} in 24h, {open_n} unresolved"
        )
        for r in items[-5:]:
            where = (
                f"topic {r['post_id']}"
                if r.get("post_id")
                else f"forum {r.get('forum')}"
            )
            print(
                f"   {r['ts']} {where} [{r['stage']}/{r['outcome']}] {r.get('detail', '')[:100]}"
            )
            if r.get("url"):
                print(f"      {r['url'][:150]}")


if __name__ == "__main__":
    import sys

    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "links":
        print_links(" ".join(rest))
    elif cmd == "reparse":
        reparse_all(rest[0] if rest else None)
    elif cmd == "refetch":
        refetch_cli(rest)
    elif cmd == "recrawl":
        recrawl_cli(rest)
    elif cmd == "errors":
        errors_cli(rest)
    else:
        threading.Thread(target=run_crawler, daemon=True).start()
        serve_torznab()
