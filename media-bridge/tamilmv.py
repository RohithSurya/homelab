#!/usr/bin/env python3
"""
tamilmv / tamilblasters crawler -> Torznab feed for Prowlarr (host :8005).

Why this exists: these two IPBoard forums (Tamil/Telugu/Hindi/Malayalam/Kannada/English
movies + web-series) were previously scraped by the compiled Rust spiders inside the
MediaFusion image. We retired that whole 4-container stack; this reimplements just the two
tamil scrapers ourselves -- the same pattern as movierulz.py -- and drops MediaFusion.

Design (mirrors movierulz.py; stateless parse helpers are imported from it):
  * run_crawler()   -- per site: resolve the live domain, walk each forum's topic listing via
                       FlareSolverr, fetch topic pages, extract magnets, upsert into sqlite.
  * serve_torznab() -- stdlib HTTP server (:8005) answering t=caps / search / movie / tvsearch.

Domain rotation: tamilmv/tamilblasters change domains often. The seed domains AND the per-forum
maps come from MediaFusion's UPSTREAM GitHub scraper_config.json (TAMILMV_CONFIG_URL), refreshed
periodically and cached locally. The live domain is found by following the seed's 301 redirect
(plain HTTP -- the redirect resolves at Cloudflare's edge, no FlareSolverr needed); only the
content pages are CF-walled and go through FlareSolverr, exactly like movierulz.

Scope: MOVIES + full-season packs only (single episodes / "Part" forms are skipped) -- the
classify()/normalize_title() guards are reused verbatim from movierulz.
"""
import email.utils
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import requests

from common import clog, ntfy
# Reuse movierulz's stateless helpers (importing movierulz only builds its regexes; its threads
# are started separately by main.py, so the import has no side effects beyond that).
from movierulz import (
    fetch, parse_magnet, search_terms, query_matches, torznab_category, error_xml, MAGNET_RE,
)

# ---- config ----
CONFIG_URL = os.environ.get(
    "TAMILMV_CONFIG_URL",
    "https://raw.githubusercontent.com/mhdzumair/MediaFusion/main/resources/json/scraper_config.json",
)
CONFIG_CACHE = os.environ.get("TAMILMV_CONFIG_CACHE", "/config/tamilmv_config.json")
CONFIG_REFRESH = int(os.environ.get("TAMILMV_CONFIG_REFRESH", "86400"))  # re-pull GitHub config daily
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://gluetun:8191").rstrip("/")
APIKEY = os.environ.get("TAMILMV_APIKEY", "")
PORT = int(os.environ.get("TAMILMV_PORT", "8005"))
CRAWL_INTERVAL = int(os.environ.get("TAMILMV_CRAWL_INTERVAL", "3600"))
MAX_PAGES = int(os.environ.get("TAMILMV_MAX_PAGES", "5"))   # per-forum page cap (bounds each pass)
MIN_PAGES = int(os.environ.get("TAMILMV_MIN_PAGES", "1"))   # scan at least this many pages per forum
FAKE_SEEDERS = int(os.environ.get("FAKE_SEEDERS", "50"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "3"))
EMPTY_RETRY_DAYS = float(os.environ.get("TAMILMV_EMPTY_RETRY_DAYS", "3"))
# Cooldown between "domain dead" ntfy pushes for the SAME site. A dead seed fails on every crawl
# pass, so an ungated alert fires ~24x/day forever and drowns the topic (it buried DIUN's daily
# image-update batch until DIUN was moved to its own topic). Logs stay per-pass; only ntfy is
# throttled. 0 disables the cooldown (alert every pass).
DEAD_ALERT_INTERVAL = float(os.environ.get("TAMILMV_DEAD_ALERT_INTERVAL", "86400"))
DB_PATH = os.environ.get("TAMILMV_DB", "/config/tamilmv.db")

# The two sites we handle, mapped to their key in the upstream config. Per-site seed overrides
# (for emergencies when the GitHub seed itself dies) come from *_SEED_OVERRIDE env vars.
SITES = {
    "tamilmv": os.environ.get("TAMILMV_SEED_OVERRIDE", ""),
    "tamil_blasters": os.environ.get("TAMILBLASTERS_SEED_OVERRIDE", ""),
}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Full topic URL (with slug) on a forum listing, keyed on the numeric topic id. A descriptive slug
# (>=1 letter) is required so pinned nav links like '/topic/183-0/' are ignored.
TOPIC_RE = re.compile(
    r'https?://[^"\'<>\s]*?/index\.php\?/forums/topic/(\d+)-([a-z0-9][^"\'<>\s]*)', re.I)

# module-level parsed config: {site: {"homepage": str, "forums": [(forum_path, language), ...]}}
CONFIG = {}
CONFIG_TS = 0.0


def log(msg):
    clog("tamilmv", msg)


# ---- GitHub config (seeds + forum maps) ----
def _parse_site_cfg(raw):
    """Normalize a site block from scraper_config.json into {homepage, forums:[(path,lang)]}.
    Handles BOTH shapes: tamilmv uses 'supported_search_forums' ({path:{language,media_type}});
    tamil_blasters uses 'catalogs' ({language:{mediatype: path | [paths]}})."""
    homepage = (raw.get("homepage") or "").rstrip("/")
    forums = []
    ssf = raw.get("supported_search_forums")
    if isinstance(ssf, dict):
        for path, meta in ssf.items():
            forums.append((path, (meta or {}).get("language", "unknown")))
    cats = raw.get("catalogs")
    if isinstance(cats, dict):
        for lang, groups in cats.items():
            if not isinstance(groups, dict):
                continue
            for val in groups.values():
                for path in (val if isinstance(val, list) else [val]):
                    if isinstance(path, str) and path:
                        forums.append((path, lang))
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
            ntfy("tamilmv: no config", "GitHub fetch and cache both failed; crawler idle",
                 priority="high", tags="warning")
        return bool(CONFIG)
    CONFIG = {site: _parse_site_cfg(raw.get(site, {})) for site in SITES}
    CONFIG_TS = time.time()
    return True


def resolve_base(seed):
    """Follow the seed's 301 chain to the current live domain (plain HTTP -- edge redirect, not
    CF-walled). Returns 'scheme://host' or None."""
    if not seed:
        return None
    try:
        r = requests.get(seed, allow_redirects=True, timeout=15, headers={"User-Agent": UA})
        if r.status_code < 400:
            p = urlparse(r.url)
            return f"{p.scheme}://{p.netloc}"
    except Exception as e:
        log(f"  redirect resolve failed for {seed}: {e}")
    return None


def site_base(site):
    """Live base URL for a site, with recovery: override/seed -> redirect; if dead, force a GitHub
    config refresh for a newer seed and retry; still dead -> ntfy + None (skip this site).

    The dead-site ntfy is rate-limited to one push per DEAD_ALERT_INTERVAL per site, and the
    timer is RESET to 0 on every successful resolve. That gives transition semantics like
    daddylive's precheck: a site that recovers and then dies again alerts immediately instead
    of waiting out a window opened by the previous outage."""
    key = f"dead_alert:{site}"
    seed = SITES.get(site) or CONFIG.get(site, {}).get("homepage")
    base = resolve_base(seed)
    if base:
        set_meta(key, 0)
        return base
    log(f"  {site}: seed {seed!r} not resolving; refreshing config")
    load_config(force=True)
    seed2 = SITES.get(site) or CONFIG.get(site, {}).get("homepage")
    base = resolve_base(seed2)
    if base:
        set_meta(key, 0)
        return base
    log(f"  {site}: seed {seed2!r} still dead after config refresh")
    last = float(get_meta(key) or 0)
    if time.time() - last >= DEAD_ALERT_INTERVAL:
        ntfy(f"tamilmv: {site} domain dead",
             f"seed {seed2!r} no longer redirects; update *_SEED_OVERRIDE or wait for upstream config",
             priority="high", tags="warning")
        set_meta(key, time.time())
    return base


# ---- sqlite (own DB; posts keyed by (site, post_id) since topic ids collide across sites) ----
DB_LOCK = threading.Lock()
_INIT_LOCK = threading.Lock()
_conn = None


def db():
    global _conn
    if _conn is not None:
        return _conn
    with _INIT_LOCK:
        if _conn is not None:
            return _conn
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.commit()
        _conn = conn
    return _conn


def get_meta(key):
    with DB_LOCK:
        row = db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_meta(key, value):
    with DB_LOCK:
        db().execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))
        db().commit()


def post_needs_fetch(site, post_id):
    """New topic: yes. Already scraped WITH magnets: no. Scraped with ZERO magnets (page published
    before its magnets landed): retry until EMPTY_RETRY_DAYS after first-seen."""
    with DB_LOCK:
        row = db().execute("SELECT scraped_at, magnets FROM posts WHERE site=? AND post_id=?",
                           (site, post_id)).fetchone()
    if row is None:
        return True
    if row["magnets"] > 0 or EMPTY_RETRY_DAYS <= 0:
        return False
    return (time.time() - (row["scraped_at"] or 0)) < EMPTY_RETRY_DAYS * 86400


def mark_post(site, post_id, url, magnets):
    """INSERT OR IGNORE keeps scraped_at as the stable first-seen time (anchors the empty-post retry
    window to discovery); magnets is the raw count on the page and only ratchets up (never down)."""
    with DB_LOCK:
        db().execute(
            "INSERT OR IGNORE INTO posts(site, post_id, url, scraped_at, magnets) VALUES (?,?,?,?,?)",
            (site, post_id, url, time.time(), magnets))
        db().execute("UPDATE posts SET magnets=? WHERE site=? AND post_id=? AND magnets<?",
                     (magnets, site, post_id, magnets))
        db().commit()


def upsert_release(rec):
    with DB_LOCK:
        # OR IGNORE keeps the original added_at -> stable pubDate for Prowlarr.
        db().execute(
            """INSERT OR IGNORE INTO releases
               (btih, site, post_id, title, kind, season, language, year, quality, size, magnet, added_at)
               VALUES (:btih,:site,:post_id,:title,:kind,:season,:language,:year,:quality,:size,:magnet,:added_at)""",
            rec)
        db().commit()


# ---- crawl ----
def process_topic(site, post_id, url, language):
    status, html = fetch(url)
    time.sleep(REQUEST_DELAY)
    if status != 200 or not html:
        return 0
    magnets = MAGNET_RE.findall(html)
    n = 0
    for m in magnets:
        rec = parse_magnet(m, post_id, language)
        if rec:
            rec["site"] = site
            upsert_release(rec)
            n += 1
    mark_post(site, post_id, url, len(magnets))
    return n


def crawl_forum(site, base, forum_path, language, known_before):
    """Walk one forum's topic listing (newest first), page 1..MAX_PAGES. Incremental: stop once a
    whole page is topics we already knew before this pass (MIN_PAGES floor guards the first page)."""
    posts = releases = 0
    for page in range(1, MAX_PAGES + 1):
        url = f"{base}/index.php?/forums/forum/{forum_path}/page/{page}/"
        status, html = fetch(url)
        time.sleep(REQUEST_DELAY)
        if status != 200 or not html:
            break
        page_topics, seen = [], set()
        for m in TOPIC_RE.finditer(html):
            pid = int(m.group(1))
            if pid in seen:
                continue
            seen.add(pid)
            page_topics.append((pid, m.group(0)))
        if not page_topics:
            break
        for pid, purl in page_topics:
            if post_needs_fetch(site, pid):
                releases += process_topic(site, pid, purl, language)
                posts += 1
        if page >= MIN_PAGES and all(pid in known_before for pid, _ in page_topics):
            break
    return posts, releases


def crawl_site(site):
    if not load_config():
        return
    base = site_base(site)
    if not base:
        return
    with DB_LOCK:
        known = {r[0] for r in db().execute("SELECT post_id FROM posts WHERE site=?", (site,)).fetchall()}
    forums = CONFIG.get(site, {}).get("forums", [])
    log(f"{site}: crawling {len(forums)} forum(s) at {base}")
    tp = tr = 0
    for forum_path, language in forums:
        try:
            p, r = crawl_forum(site, base, forum_path, language, known)
            tp += p
            tr += r
        except Exception as e:
            log(f"  forum {forum_path} error: {e}")
    log(f"{site}: done -- {tp} topics fetched, {tr} releases indexed")


def run_crawler():
    db()  # init schema
    if not any(SITES.values()) and not load_config():
        log("no config available -- crawler idle; Torznab feed still serves")
        return
    log(f"crawler starting; both sites every {CRAWL_INTERVAL}s via {FLARESOLVERR_URL}")
    while True:
        try:
            for site in SITES:
                crawl_site(site)
        except Exception as e:
            log(f"crawl error: {e}")
        time.sleep(CRAWL_INTERVAL)


# ---- Torznab (own server on :8005) ----
def caps_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<caps>\n'
        '  <server title="tamilmv"/>\n'
        '  <limits max="100" default="100"/>\n'
        '  <searching>\n'
        '    <search available="yes" supportedParams="q"/>\n'
        '    <movie-search available="yes" supportedParams="q,year"/>\n'
        '    <tv-search available="yes" supportedParams="q,season,ep"/>\n'
        '  </searching>\n'
        '  <categories>\n'
        '    <category id="2000" name="Movies">\n'
        '      <subcat id="2030" name="Movies/SD"/>\n'
        '      <subcat id="2040" name="Movies/HD"/>\n'
        '    </category>\n'
        '    <category id="5000" name="TV">\n'
        '      <subcat id="5030" name="TV/SD"/>\n'
        '      <subcat id="5040" name="TV/HD"/>\n'
        '    </category>\n'
        '  </categories>\n'
        '</caps>\n'
    )


def query_releases(t, q, season, cats):
    with DB_LOCK:
        rows = db().execute("SELECT * FROM releases ORDER BY added_at DESC").fetchall()
    terms = search_terms(q)   # shared with movierulz: alias-aware, never matches-everything
    out = []
    for r in rows:
        if t == "movie" and r["kind"] != "movie":
            continue
        if t == "tvsearch" and r["kind"] != "series":
            continue
        if cats:
            fam = 5000 if r["kind"] == "series" else 2000
            sub = torznab_category(r)
            if not any(c == fam or c == sub for c in cats):
                continue
        if not query_matches(terms, r["title"]):
            continue
        if season and r["kind"] == "series" and r["season"] and int(season) != r["season"]:
            continue
        out.append(r)
    return out[:100]


def results_xml(rows):
    items = []
    for r in rows:
        magnet = r["magnet"]
        size = r["size"] or 0
        cat = torznab_category(r)
        pub = email.utils.formatdate(r["added_at"], usegmt=True)
        items.append(
            "    <item>\n"
            f"      <title>{escape(r['title'])}</title>\n"
            f'      <guid isPermaLink="false">{r["btih"]}</guid>\n'
            f"      <pubDate>{pub}</pubDate>\n"
            f"      <size>{size}</size>\n"
            f"      <link>{escape(magnet)}</link>\n"
            f'      <enclosure url="{escape(magnet)}" length="{size}" type="application/x-bittorrent"/>\n'
            f'      <torznab:attr name="category" value="{cat}"/>\n'
            f'      <torznab:attr name="magneturl" value="{escape(magnet)}"/>\n'
            f'      <torznab:attr name="seeders" value="{FAKE_SEEDERS}"/>\n'
            f'      <torznab:attr name="peers" value="{FAKE_SEEDERS}"/>\n'
            f'      <torznab:attr name="size" value="{size}"/>\n'
            "    </item>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">\n'
        '  <channel>\n'
        '    <title>tamilmv</title>\n'
        '    <description>tamilmv/tamilblasters torznab feed</description>\n'
        '    <link>http://media-bridge:8005/torznab</link>\n'
        + "".join(items) +
        '  </channel>\n'
        '</rss>\n'
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.rstrip("/").endswith("/api") and "/torznab" not in parsed.path:
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
        try:
            rows = query_releases(t, q, season, cats)
            self._send(results_xml(rows))
        except Exception as e:
            log(f"torznab query error: {e}")
            self._send(error_xml(900, "internal error"))


def serve_torznab():
    db()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"torznab server listening on :{PORT} (apikey {'set' if APIKEY else 'DISABLED'})")
    httpd.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_crawler, daemon=True).start()
    serve_torznab()
