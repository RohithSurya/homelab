#!/usr/bin/env python3
"""
movierulz crawler -> Torznab feed for Prowlarr.

Why this exists: the tamilmv/tamilblasters crawlers are compiled Rust spiders inside
the MediaFusion image and can't be taught a new site. movierulz is a WordPress-style
site, so we crawl it ourselves (through the existing FlareSolverr, since it's
Cloudflare-protected) and expose a minimal Torznab feed that Prowlarr consumes as a
generic Torznab indexer -- exactly how MediaFusion is wired in.

Two threads (started from main.py):
  * run_crawler()   -- periodically crawls all-language category pages via FlareSolverr,
                       extracts magnets, parses/classifies, upserts into sqlite.
  * serve_torznab() -- stdlib HTTP server answering t=caps / search / movie / tvsearch.

Scope: all languages; MOVIES + full-season packs only. Single-episode and "Part"
releases are skipped (no clean Sonarr mapping).

Network: this runs OFF the VPN (default bridge net). All site fetching is proxied
through FlareSolverr (which is inside the VPN), reached at FLARESOLVERR_URL
(default http://gluetun:8191) -- the same gluetun-hostname pattern seedr uses for *arr.
"""
import datetime
import email.utils
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import requests

from common import clog, norm, ntfy

# ---- config ----
BASE = os.environ.get("MOVIERULZ_BASE", "").rstrip("/")  # required; set in .env (kept out of source)
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://gluetun:8191").rstrip("/")
APIKEY = os.environ.get("MOVIERULZ_APIKEY", "")
PORT = int(os.environ.get("TORZNAB_PORT", "8002"))
CRAWL_INTERVAL = int(os.environ.get("CRAWL_INTERVAL", "3600"))
# Crawl the master /movies listing page-by-page (newest first) all the way to the last page, so
# EVERY language and type (incl. *-dubbed) is covered from one source -- nothing is left out.
# Stop at the first empty page (the true end) or, on incremental re-crawls, once a whole page is
# posts already seen before this pass (MIN_PAGES floor guards the curated first page from hiding
# new posts that sit deeper).
MOVIES_PATH = os.environ.get("MOVIERULZ_MOVIES_PATH", "/movies")
MIN_PAGES = int(os.environ.get("MIN_PAGES", "3"))     # always scan at least this many pages per pass
MAX_PAGES = int(os.environ.get("MAX_PAGES", "1000"))  # safety cap; real stop is the last/empty page
FAKE_SEEDERS = int(os.environ.get("FAKE_SEEDERS", "50"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "3"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "0"))   # 0 = keep forever
# The site often publishes a movie's post page hours before adding its magnet links, so a first scrape
# can find zero magnets. Re-fetch such magnet-less posts for this many days after they were first seen
# (anchored to first-seen, not last attempt) so late-added magnets are still picked up; 0 disables it.
EMPTY_RETRY_DAYS = float(os.environ.get("MOVIERULZ_EMPTY_RETRY_DAYS", "3"))
DB_PATH = os.environ.get("MOVIERULZ_DB", "/config/movierulz.db")
# Radarr is the source of alternate titles (see search_terms): a Tamil/Telugu film is listed on
# these sites under its dub title (Karuppu -> "Veerabhadrudu"), and Radarr searches the NATIVE-script
# alt title, which no release name carries. Reuses the seedr component's Radarr credentials.
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
ALIAS_REFRESH = int(os.environ.get("ALIAS_REFRESH", "900"))   # re-pull the Radarr movie list this often
# Sonarr is the source of each show's year (see series_title). Reuses seedr's Sonarr credentials.
SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
# Stale-feed alert: ntfy (ops topic) when no NEW post has been seen for this many days -- the crawler
# can run "successfully" while indexing nothing (a domain rotation went unnoticed for 10.7 days,
# 2026-09-20 -> 09-30). Repeats at most every STALE_ALERT_REPEAT s until posts flow again.
STALE_ALERT_DAYS = float(os.environ.get("MOVIERULZ_STALE_ALERT_DAYS", "4"))
STALE_ALERT_REPEAT = float(os.environ.get("MOVIERULZ_STALE_ALERT_REPEAT", "86400"))

# Best-effort language label parsed from the post slug (e.g. '...-telugu-7008.html' or
# '...-telugu-dubbed-7112.html'). Functionally optional -- the feed matches by title, not language.
LANG_RE = re.compile(
    r'-(telugu|tamil|malayalam|kannada|hindi|bengali|punjabi|english)(?:-dubbed)?-\d+\.html$', re.I)


def lang_from_slug(url):
    m = LANG_RE.search(url)
    return m.group(1).lower() if m else "unknown"


def log(msg):
    clog("movierulz", msg)


# ---- regexes ----
# Any internal post link, keyed on the trailing numeric id. Captures both URL schemes:
#   Form A: /peddi-2026-dvdscr-telugu-7017.html
#   Form B: /save-the-tigers-season-3-2026-telugu/movie-watch-online-free-7090.html
# Any movierulz host, not just BASE's: the domain rotates (.solutions -> .institute, 2026-09) and the
# old BASE keeps redirecting to the new one, so a BASE-only pattern matched ZERO post links and every
# pass reported "0 posts" with no error for 10.7 days. crawl_once logs the new host when it moves.
_HOST = r'(?:www\.)?\d*movierulz[a-z0-9-]*\.[a-z]{2,}(?:\.[a-z]{2,})?'
POST_RE = re.compile(r'https?://(' + _HOST + r')/[A-Za-z0-9/_-]+?-(\d+)\.html', re.I)
MAGNET_RE = re.compile(r'magnet:\?[^"\'<>\s]+')

# Episode-range pack, e.g. "S02 EP (01-08)" -> indexed as multi-episode "S02E01-E08".
# NOT as a bare "S02" season pack: sites post running seasons in chunks (Heart Beat S03 is
# 32 episodes, posted 4 at a time), and a bare "S02" tells Sonarr the chunk is the whole season.
SEASON_PACK = re.compile(r'\bS(\d{1,2})\s*EP\s*\(\s*(\d+)\s*-\s*(\d+)\s*\)', re.I)
# Split a normalized series title into show name / optional year / the rest (from an optional
# resolution + the SxxE marker on), e.g. 'Heart Beat (2026) 1080p S03E01-E04 ...'. See series_title().
SERIES_HEAD_RE = re.compile(
    r'^(?P<base>.+?)(?:\s*[(\[]?\b(?:19|20)\d{2}\b[)\]]?)?(?P<rest>(?:\s+\d{3,4}p)?\s+S\d{1,2}E\d.*)$', re.I)
# Other series markers (single ep "S04 EP09"/"S04E09", "S1P2"/"S1P-1-2", spelled-out
# "Season 3") -> NOT mappable to Sonarr cleanly, so these releases are skipped.
SERIES_ANY = re.compile(r'\bS\d{1,2}\s*(?:EP?\s*\d+|E\d+|P[\s\-\d])', re.I)
SEASON_WORD = re.compile(r'\bSeason\s*\d+\b', re.I)

QUALITY_RE = re.compile(r'\b(2160p|1080p|720p|480p)\b', re.I)
YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')
SITE_PREFIX_RE = re.compile(r'^\s*www\.\S+\s+-\s+', re.I)  # strip leading 'www.<domain> - ' (any spelling)
EXT_RE = re.compile(r'\.(mkv|mp4|avi)$', re.I)
SIZE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(GB|MB)\b', re.I)


# ---- sqlite ----
DB_LOCK = threading.Lock()
_ALIAS = {}            # unorm(title) -> [latin titles]; see _radarr_aliases()
_ALIAS_TS = 0.0
_ALIAS_LOCK = threading.Lock()
_SERIES_YEAR = {}      # unorm(show title) -> Sonarr year (None if ambiguous); see _sonarr_years()
_SERIES_YEAR_TS = 0.0
# Separate lock for lazy init only: the crawler and torznab threads both call db() at startup, so the
# init (incl. the one-shot ALTER migration below) must run exactly once. Not DB_LOCK -- callers like
# get_meta() hold DB_LOCK across their db() call, and threading.Lock isn't reentrant.
_INIT_LOCK = threading.Lock()
_conn = None


def db():
    global _conn
    if _conn is not None:
        return _conn
    with _INIT_LOCK:
        if _conn is not None:   # another thread finished init while we waited
            return _conn
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                post_id INTEGER PRIMARY KEY,
                url TEXT,
                scraped_at REAL,
                magnets INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS releases (
                btih TEXT PRIMARY KEY,
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
            CREATE INDEX IF NOT EXISTS idx_releases_added ON releases(added_at);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # migrate: older DBs lack posts.magnets (raw magnet count seen on the page). Backfill any post
        # that already produced a release as "had magnets" so only genuinely magnet-less pages get
        # retried -- a post with skipped-only magnets (single episodes/Parts) must NOT be retried.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "magnets" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN magnets INTEGER DEFAULT 0")
            conn.execute("UPDATE posts SET magnets=1 "
                         "WHERE post_id IN (SELECT DISTINCT post_id FROM releases)")
        conn.commit()
        _conn = conn   # publish only after schema+migration are fully applied
    return _conn


def get_meta(key):
    with DB_LOCK:
        row = db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_meta(key, value):
    with DB_LOCK:
        db().execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))
        db().commit()


def post_needs_fetch(post_id):
    """Whether crawl_once should (re)fetch this post. New posts: yes. Posts already scraped WITH
    magnets on the page: no (fully populated -- even if every magnet was a skipped episode/Part).
    Posts scraped with ZERO magnets (page published before its magnets were added): retry until
    EMPTY_RETRY_DAYS after first-seen, so late-added magnets are still indexed."""
    with DB_LOCK:
        row = db().execute("SELECT scraped_at, magnets FROM posts WHERE post_id=?",
                           (post_id,)).fetchone()
    if row is None:
        return True
    if row["magnets"] > 0 or EMPTY_RETRY_DAYS <= 0:
        return False
    return (time.time() - (row["scraped_at"] or 0)) < EMPTY_RETRY_DAYS * 86400


def mark_post(post_id, url, magnets):
    """Record the post as seen. INSERT OR IGNORE keeps scraped_at as the stable FIRST-seen time (so the
    empty-post retry window is anchored to discovery, not the last attempt); `magnets` is the raw magnet
    count on the page and, once >0, sticks (never downgraded) -- a page stops being retried the moment
    it is populated."""
    with DB_LOCK:
        db().execute("INSERT OR IGNORE INTO posts(post_id, url, scraped_at, magnets) VALUES (?,?,?,?)",
                     (post_id, url, time.time(), magnets))
        db().execute("UPDATE posts SET magnets=? WHERE post_id=? AND magnets<?",
                     (magnets, post_id, magnets))
        db().commit()


def upsert_release(rec):
    with DB_LOCK:
        # OR IGNORE keeps the original added_at -> stable pubDate for Prowlarr.
        db().execute(
            """INSERT OR IGNORE INTO releases
               (btih, post_id, title, kind, season, language, year, quality, size, magnet, added_at)
               VALUES (:btih,:post_id,:title,:kind,:season,:language,:year,:quality,:size,:magnet,:added_at)""",
            rec)
        db().commit()


# ---- FlareSolverr fetch ----
def fetch(url):
    """Return (http_status, html) for url via FlareSolverr, or (None, '') on failure."""
    try:
        r = requests.post(f"{FLARESOLVERR_URL}/v1",
                          json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
                          timeout=90)
        r.raise_for_status()
        sol = r.json().get("solution", {})
        return sol.get("status"), sol.get("response", "") or ""
    except Exception as e:
        log(f"  fetch failed for {url}: {e}")
        return None, ""


# ---- parsing ----
def size_from_name(name):
    m = SIZE_RE.search(name)
    if not m:
        return 0
    val = float(m.group(1))
    return int(val * (1e9 if m.group(2).upper() == "GB" else 1e6))


def classify(name):
    """Return (kind, season) where kind in {'movie','series','skip'}."""
    mp = SEASON_PACK.search(name)
    if mp:
        return "series", int(mp.group(1))
    if SERIES_ANY.search(name) or SEASON_WORD.search(name):
        return "skip", None
    return "movie", None


def normalize_title(name, kind):
    """Clean the dn= name into a release title Sonarr/Radarr can parse.
    For episode-range packs, 'Show (2026) S02 EP (01-08)' -> 'Show S02E01-E08'."""
    title = SITE_PREFIX_RE.sub("", name)
    title = EXT_RE.sub("", title)
    if kind == "series":
        title = SEASON_PACK.sub(
            lambda m: f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}-E{int(m.group(3)):02d}",
            title,
        )
    return title.strip()


def parse_magnet(m, post_id, language):
    """Parse one magnet into a release rec, or None if it should be skipped."""
    m = m.replace("&amp;", "&")
    bt = re.search(r'btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})', m)
    if not bt:
        return None
    btih = bt.group(1).lower()
    dn = re.search(r'[?&]dn=([^&]+)', m)
    raw_name = urllib.parse.unquote(dn.group(1)) if dn else ""
    xl = re.search(r'[?&]xl=(\d+)', m)

    kind, season = classify(raw_name)
    if kind == "skip":
        return None

    title = normalize_title(raw_name, kind)
    if not title:
        return None
    size = int(xl.group(1)) if xl else size_from_name(raw_name)
    qm = QUALITY_RE.search(raw_name)
    quality = qm.group(1).lower() if qm else ""
    ym = YEAR_RE.search(raw_name)
    year = int(ym.group(0)) if ym else None

    return {"btih": btih, "post_id": post_id, "title": title, "kind": kind,
            "season": season, "language": language, "year": year, "quality": quality,
            "size": size, "magnet": m, "added_at": time.time()}


def process_post(post_id, url, language):
    status, html = fetch(url)
    time.sleep(REQUEST_DELAY)
    if status != 200 or not html:
        return 0
    magnets = MAGNET_RE.findall(html)
    n = 0
    for m in magnets:
        rec = parse_magnet(m, post_id, language)
        if rec:
            upsert_release(rec)
            n += 1
    mark_post(post_id, url, len(magnets))   # store raw magnet count -> drives empty-post retry
    return n


def crawl_once():
    """Walk /movies/page/1 .. last page (newest first).
    Until the catalogue has been fully backfilled once (meta 'backfill_done'), keep paginating to the
    real last page every pass -- this makes the long first backfill RESUMABLE across restarts (already
    seen posts are skipped cheaply, so re-scanning the early pages is fast). After the backfill has
    reached the end once, switch to incremental: stop at the first page that is entirely posts known
    before this pass (MIN_PAGES floor guards the curated first page)."""
    total_posts = total_releases = 0
    backfilling = get_meta("backfill_done") != "1"
    with DB_LOCK:
        known_before = {r[0] for r in db().execute("SELECT post_id FROM posts").fetchall()}
    page, reached_end = 1, False
    hosts = set()
    seen_this_pass = set()
    while page <= MAX_PAGES:
        url = f"{BASE}{MOVIES_PATH}/page/{page}"
        status, html = fetch(url)
        time.sleep(REQUEST_DELAY)
        if status != 200 or not html:
            reached_end = True
            break
        page_ids, seen_on_page = [], set()
        for mobj in POST_RE.finditer(html):
            hosts.add(mobj.group(1).lower())
            pid = int(mobj.group(2))
            if pid in seen_on_page:
                continue
            seen_on_page.add(pid)
            page_ids.append((pid, mobj.group(0)))
        if not page_ids:
            reached_end = True   # past the last real page
            break
        for pid, purl in page_ids:
            if post_needs_fetch(pid):
                total_releases += process_post(pid, purl, lang_from_slug(purl))
                total_posts += 1
        # incremental mode only: once a whole page is posts we already had -- known before this pass, or
        # seen on an earlier page of it -- the rest downstream is older and already indexed -> stop.
        # "Earlier page" matters: every listing page carries a sidebar of the NEWEST posts, so with
        # known_before alone any new post made every page look new and each pass walked all ~200
        # pages (found 2026-09-30: page 100 had 19 of 36 ids first seen that pass).
        if not backfilling and page >= MIN_PAGES and all(
                pid in known_before or pid in seen_this_pass for pid, _ in page_ids):
            break
        seen_this_pass.update(pid for pid, _ in page_ids)
        page += 1
    if backfilling and reached_end:
        set_meta("backfill_done", "1")
        log("full catalogue backfill complete -> switching to incremental crawls")
    if RETENTION_DAYS > 0:
        cutoff = time.time() - RETENTION_DAYS * 86400
        with DB_LOCK:
            db().execute("DELETE FROM releases WHERE added_at < ?", (cutoff,))
            db().commit()
    base_host = urllib.parse.urlparse(BASE).netloc.lower()
    moved = sorted(h for h in hosts if h != base_host)
    if moved:
        log(f"domain moved: post links point at {', '.join(moved)} (MOVIERULZ_BASE is {base_host}; "
            f"crawling through the redirect -- update MOVIERULZ_BASE in .env)")
        set_meta("moved_to", ",".join(moved))
    else:
        set_meta("moved_to", "")
    set_meta("last_pass", f"{page} page(s), {total_posts} posts fetched, {total_releases} releases")
    log(f"crawl done: scanned {page} page(s), {total_posts} posts fetched, {total_releases} releases indexed")


def check_stale(now=None):
    """ntfy when no new post has been seen for STALE_ALERT_DAYS (posts.scraped_at = first-seen), at
    most every STALE_ALERT_REPEAT s; a fresh post resets it. Returns True if it alerted."""
    now = now or time.time()
    if STALE_ALERT_DAYS <= 0:
        return False
    with DB_LOCK:
        newest = db().execute("SELECT max(scraped_at) FROM posts").fetchone()[0]
    if newest is None or now - newest < STALE_ALERT_DAYS * 86400:
        set_meta("stale_alert", 0)
        return False
    if now - float(get_meta("stale_alert") or 0) < STALE_ALERT_REPEAT:
        return False
    days = (now - newest) / 86400
    moved = get_meta("moved_to") or ""
    ntfy(f"movierulz: no new posts for {days:.1f} days",
         f"MOVIERULZ_BASE={BASE}\n"
         + (f"post links now point at {moved} -- update MOVIERULZ_BASE in .env and recreate media-bridge\n"
            if moved else "check `docker compose logs flaresolverr` (ERR_NAME_NOT_RESOLVED = domain gone)\n")
         + f"last pass: {get_meta('last_pass') or '?'}",
         priority="high", tags="warning")
    set_meta("stale_alert", now)
    log(f"stale alert sent: newest post {days:.1f} days old")
    return True


def run_crawler():
    db()  # init schema
    if not BASE:
        log("MOVIERULZ_BASE is not set -- crawler disabled (set it in .env); Torznab feed still serves")
        return
    log(f"crawler starting; master /movies crawl every {CRAWL_INTERVAL}s, via {FLARESOLVERR_URL}")
    while True:
        try:
            crawl_once()
        except Exception as e:
            log(f"crawl error: {e}")
        try:
            check_stale()
        except Exception as e:
            log(f"stale check error: {e}")
        time.sleep(CRAWL_INTERVAL)


# ---- Torznab ----
def torznab_category(rec):
    hd = rec["quality"] in ("720p", "1080p", "2160p")
    if rec["kind"] == "series":
        return 5040 if hd else 5030
    return 2040 if hd else 2030


def caps_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<caps>\n'
        '  <server title="movierulz"/>\n'
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


def error_xml(code, desc):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<error code="{code}" description="{escape(desc)}"/>\n'


def _collapse(s):
    """norm() + collapse runs of the same char, so transliteration variants match:
    'Bangaaram' and 'Bangaram' both -> 'bangaram', 'Pedda'/'Peddha' -> 'peda'/'pedha'."""
    return re.sub(r"(.)\1+", r"\1", norm(s))


def title_matches(q, title):
    """True if every word of the query (collapsed) appears in the collapsed title.
    Token-based + dup-collapse tolerates word order, spacing, and doubled letters --
    the *arr still re-parse and filter results, so we can afford to be generous."""
    ct = _collapse(title)
    toks = [_collapse(w) for w in q.split()]
    toks = [w for w in toks if w]
    return all(w in ct for w in toks)


def unorm(s):
    """Unicode-aware norm(): casefold, drop combining marks and every non-alphanumeric.
    norm() keeps only [a-z0-9], so a native-script title collapses to '' -- useless as a key.
    Radarr strips the vowel signs when it builds its query ('கருப்பு' -> 'கர பப'), so dropping
    marks on both sides is what makes the two compare equal."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "").casefold() if c.isalnum())


def _latin_term(t):
    """True if a Radarr title is usable as a search term against our latin-named releases.
    norm() alone is NOT the test: it keeps [a-z0-9], so a native-script title carrying a digit
    ('3 பி.ஹெச்.கே' for 3BHK) passes it with just '3' left. As a term its only token is
    '3', which substring-matches almost every release title (sizes, years, bitrates) -- the same
    match-the-whole-catalogue bug search_terms() guards against, arriving through the alias map
    instead of the query. Titles that are legitimately all-digits ('1917') stay usable."""
    return bool(norm(t)) and not any(c.isalpha() and ord(c) > 127 for c in t)


def _radarr_aliases():
    """{unorm(any known title): [latin titles of that movie]} from Radarr, refreshed every
    ALIAS_REFRESH. Best-effort: on failure the map is left as-is and search falls back to the
    raw query. The timestamp is stamped BEFORE the fetch so a failing Radarr can't make every
    search pay the timeout."""
    global _ALIAS, _ALIAS_TS
    if not (RADARR_URL and RADARR_API_KEY):
        return _ALIAS
    with _ALIAS_LOCK:
        if time.time() - _ALIAS_TS < ALIAS_REFRESH:
            return _ALIAS
        _ALIAS_TS = time.time()
    try:
        r = requests.get(f"{RADARR_URL}/api/v3/movie",
                         headers={"X-Api-Key": RADARR_API_KEY}, timeout=20)
        r.raise_for_status()
        movies = r.json()
    except Exception as e:
        log(f"alias refresh failed: {e}")
        return _ALIAS
    alias = {}
    for m in movies:
        titles = [m.get("title"), m.get("originalTitle")]
        titles += [a.get("title") for a in (m.get("alternateTitles") or [])]
        titles = [t for t in titles if t]
        # only latin-script titles are usable as search terms -- our releases are named in latin
        latin = [t for t in titles if _latin_term(t)]
        if not latin:
            continue
        for t in titles:
            k = unorm(t)
            if not k:
                continue
            bucket = alias.setdefault(k, [])
            bucket += [lt for lt in latin if lt not in bucket]
    with _ALIAS_LOCK:
        _ALIAS = alias
    log(f"alias map refreshed: {len(alias)} title keys from {len(movies)} radarr movies")
    return alias


def _sonarr_years():
    """{unorm(show title): year} from Sonarr, refreshed every ALIAS_REFRESH. Best-effort, same
    contract as _radarr_aliases(): stale map on failure, timestamp stamped before the fetch.
    A title two library shows share maps to None (no rewrite -- we can't tell which one)."""
    global _SERIES_YEAR, _SERIES_YEAR_TS
    if not (SONARR_URL and SONARR_API_KEY):
        return _SERIES_YEAR
    with _ALIAS_LOCK:
        if time.time() - _SERIES_YEAR_TS < ALIAS_REFRESH:
            return _SERIES_YEAR
        _SERIES_YEAR_TS = time.time()
    try:
        r = requests.get(f"{SONARR_URL}/api/v3/series",
                         headers={"X-Api-Key": SONARR_API_KEY}, timeout=20)
        r.raise_for_status()
        shows = r.json()
    except Exception as e:
        log(f"sonarr year refresh failed: {e}")
        return _SERIES_YEAR
    years = {}
    for sh in shows:
        if not sh.get("year"):
            continue
        # Sonarr disambiguates as 'Dark Matter (2024)'; key on the bare name
        k = unorm(re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", sh.get("title") or ""))
        if k:
            years[k] = None if k in years and years[k] != sh["year"] else sh["year"]
    with _ALIAS_LOCK:
        _SERIES_YEAR = years
    return years


def series_title(title):
    """Pin a series release's year to the one Sonarr has for that show.
    Release names carry the SEASON's year ('Heart Beat (2026) S03 ...' for a 2024 show), and
    Sonarr only maps a titled-with-year release to a series of that year -> "Unknown Series".
    Dropping the year is no fix either: a bare name can collide with a global scene alias
    ('Heart Beat' is TVDB 74684, the 1992 UK 'Heartbeat'). Only show-name + the show's own year
    maps reliably, so rewrite (or insert) it for shows in the Sonarr library; leave others be."""
    m = SERIES_HEAD_RE.match(title)
    if not m:
        return title
    year = _sonarr_years().get(unorm(m["base"]))
    return f"{m['base']} ({year}){m['rest']}" if year else title


def search_terms(q):
    """Resolve an incoming Torznab query into the titles to match releases against.

    None  -> no text filter (empty q / RSS).
    []    -> match NOTHING. Critical: Radarr searches a movie's alternate titles, which for Indian
             films are native-script ('கருப்பு'). norm() reduces those to '', title_matches() then
             sees zero tokens, and `all([])` is True -> the query matched the WHOLE catalogue and
             the *arr got 100 unrelated releases per search.
    else  -> the query itself (when latin) plus every latin title Radarr knows for that movie, so
             the native-script query Radarr already sends finds the dub-titled release
             ('கருப்பு' -> Karuppu + Veerabhadrudu). A trailing year in the query is carried onto
             the alias terms so precision isn't lost."""
    q = (q or "").strip()
    if not q:
        return None
    ym = re.search(r"\b(?:19|20)\d{2}$", q)
    base, year = (q[:ym.start()].strip(), ym.group(0)) if ym else (q, "")
    aliases = _radarr_aliases().get(unorm(base)) or []
    terms = [f"{t} {year}".strip() for t in aliases]
    if norm(base):          # latin query: keep it, so unknown-to-Radarr searches still work
        terms.append(q)
    return list(dict.fromkeys(terms))


def query_matches(terms, title):
    """True if the release title matches any resolved search term (None = no text filter)."""
    if terms is None:
        return True
    return any(title_matches(t, title) for t in terms)


def query_releases(t, q, season, cats):
    """Select releases matching the search. Returns list of sqlite rows."""
    with DB_LOCK:
        # scan the whole catalogue (no LIMIT) so old back-catalog stays searchable; the token
        # matcher below is cheap and results are capped to 100.
        rows = db().execute("SELECT * FROM releases ORDER BY added_at DESC").fetchall()
    terms = search_terms(q)
    out = []
    for r in rows:
        # kind filter by search type
        if t == "movie" and r["kind"] != "movie":
            continue
        if t == "tvsearch" and r["kind"] != "series":
            continue
        # category filter (e.g. cat=2000 or 5000 families)
        if cats:
            fam = 5000 if r["kind"] == "series" else 2000
            sub = torznab_category(r)
            if not any(c == fam or c == sub for c in cats):
                continue
        # text filter (dup-collapsing, token-based -> matches transliteration variants),
        # widened to the movie's Radarr alternate titles
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
            f"      <title>{escape(series_title(r['title']) if r['kind'] == 'series' else r['title'])}</title>\n"
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
        '    <title>movierulz</title>\n'
        '    <description>movierulz torznab feed</description>\n'
        f'    <link>{escape(BASE)}</link>\n'
        + "".join(items) +
        '  </channel>\n'
        '</rss>\n'
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default per-request stderr logging

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
    db()  # ensure schema exists before serving
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"torznab server listening on :{PORT} (apikey {'set' if APIKEY else 'DISABLED'})")
    httpd.serve_forever()


if __name__ == "__main__":
    # manual run: crawl in a thread, serve in foreground
    threading.Thread(target=run_crawler, daemon=True).start()
    serve_torznab()
