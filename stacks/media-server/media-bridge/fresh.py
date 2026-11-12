#!/usr/bin/env python3
"""
fresh -- "Latest Releases": a weekly set of new Indian movies, picked by an LLM, kept a month.

Division of labour (user decisions 2026-11-11):
  * THIS module (deterministic, no LLM): builds the candidate list from the movierulz + tamilmv
    scraper DBs, rejects theatre prints / foreign dubs by rule, looks each movie up (TMDB via
    Radarr), picks the release(s) per the language + quality rules, downloads the approved ones
    through the bridge (its own blackhole category `fresh` -- Radarr never grabs, never
    re-searches), places the files into a Radarr movie folder (unmonitored, movies-new), keeps
    the Jellyfin collection "Latest Releases" in sync and expires movies after FRESH_KEEP_DAYS.
  * Hermes (on the Jetson, cron, Claude Sonnet with web search; Muse Glimmer fallback): reads
    GET /fresh/candidates, researches buzz, POSTs /fresh/decisions. Twice a week it sends the
    proposals over iMessage; the user's "ok 3 5" becomes POST /fresh/approve. (mediaops proxies
    these calls; this server is LAN-only like the rest of :8003.)

Rules (see CLAUDE.md "fresh"):
  * Indian films only (any Indian language); no foreign film dubbed into an Indian language.
  * No CAM/HDTS/TC/DVDScr/PreDVD. HC-ESub is fine on a genuine OTT/BluRay source.
  * Quality: 1080p <= FRESH_SIZE_CAP_GB (multi-audio: twice that), else the smallest bigger 1080p,
    else 720p, else SD, and 4K only when nothing else exists.
  * Language: one multi-audio file holding the original language AND Telugu if one exists;
    otherwise a Telugu file plus an original-language file. Telugu is always the default: it
    gets the bare file name (Jellyfin lists that version first and plays it), and in a
    multi-audio .mkv its track is flagged default (mkvpropedit). A movie available only as a
    dub into a third language waits ("waiting_lang") for its original or Telugu.
  * FRESH_WEEKLY (15) movies per calendar week (Mon-Sun, local time); a two-file movie is one;
    movies the LLM flags big may go FRESH_OVERFLOW (5) over.
  * Expiry: FRESH_KEEP_DAYS (30) after placement, deleted through Radarr (files + folder) --
    unless ANY Jellyfin user (guest included) has it favourited or partly watched, or it is kept.
"""
import datetime
import html
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import threading
import time

import requests

from common import clog, norm, ntfy

RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")

DB_PATH = os.environ.get("FRESH_DB", "/config/fresh.db")
MOVIERULZ_DB = os.environ.get("MOVIERULZ_DB", "/config/movierulz.db")
TAMILMV_DB = os.environ.get("TAMILMV_DB", "/config/tamilmv.db")
DATA = pathlib.Path(os.environ.get("FRESH_DATA", "/data"))
BLACKHOLE = DATA / "blackhole" / "fresh"
DELIVERED = DATA / "downloads" / "fresh"
ROOT = os.environ.get("FRESH_ROOT", "/data/media/movies-new")
PROFILE = os.environ.get("FRESH_PROFILE", "Any")

WEEKLY = int(os.environ.get("FRESH_WEEKLY", "15"))
OVERFLOW = int(os.environ.get("FRESH_OVERFLOW", "5"))
# New releases land on Fridays (user, 2026-11-12): from FRIDAY_FROM (0=Mon) to Sunday the week's
# cap grows by FRIDAY_EXTRA, so Friday's releases still find room after the week's first 15.
FRIDAY_EXTRA = int(os.environ.get("FRESH_FRIDAY_EXTRA", "5"))
FRIDAY_FROM = int(os.environ.get("FRESH_FRIDAY_FROM", "4"))
WINDOW_DAYS = int(os.environ.get("FRESH_WINDOW_DAYS", "21"))      # first good rip seen within
SIZE_CAP = float(os.environ.get("FRESH_SIZE_CAP_GB", "5")) * 1e9
GOOD_SCORE = float(os.environ.get("FRESH_GOOD_SCORE", "6"))      # below: a "fill" pick
FILL_WEEKDAY = int(os.environ.get("FRESH_FILL_WEEKDAY", "4"))    # auto mode: fills start Fri (0=Mon)
KEEP_DAYS = float(os.environ.get("FRESH_KEEP_DAYS", "30"))
EXPIRE_MAX = int(os.environ.get("FRESH_EXPIRE_MAX", "5"))
EXPIRE_DRY_RUN = os.environ.get("FRESH_EXPIRE_DRY_RUN", "true").lower() not in ("false", "0", "no")
MODE_DEFAULT = os.environ.get("FRESH_MODE", "approval")          # approval | auto (stored in meta)
COLLECTION = os.environ.get("FRESH_COLLECTION", "Latest Releases")
POLL = int(os.environ.get("FRESH_POLL", "300"))
BUILD_EVERY = int(os.environ.get("FRESH_BUILD_EVERY", "3600"))
MAX_TRIES = int(os.environ.get("FRESH_MAX_TRIES", "3"))           # failed downloads per version
DROP_TIMEOUT = float(os.environ.get("FRESH_DROP_TIMEOUT_H", "48")) * 3600
INFO_FETCH_MAX = int(os.environ.get("FRESH_INFO_FETCH_MAX", "20"))  # movierulz pages per build
WAIT_DAYS = int(os.environ.get("FRESH_WAIT_DAYS", "60"))          # waiting_lang / watchlist give up
PROPOSAL_DAYS = int(os.environ.get("FRESH_PROPOSAL_DAYS", "14"))  # unanswered proposals drop
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts")

RA = {"X-Api-Key": RADARR_API_KEY}
JF = {"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'}
_lock = threading.RLock()
_conn = None


def log(msg):
    clog("fresh", msg)


# ----------------------------------------------------------------------------- release parsing
LANGS = {  # code -> display name. Indian languages + English (for detecting foreign films)
    "tel": "Telugu", "tam": "Tamil", "mal": "Malayalam", "hin": "Hindi", "kan": "Kannada",
    "ben": "Bengali", "mar": "Marathi", "pan": "Punjabi", "guj": "Gujarati", "ori": "Odia",
    "bho": "Bhojpuri", "tul": "Tulu", "eng": "English",
}
INDIAN = set(LANGS) - {"eng"}
# TMDB/Radarr originalLanguage names -> code
RADARR_LANG = {"Telugu": "tel", "Tamil": "tam", "Malayalam": "mal", "Hindi": "hin", "Kannada": "kan",
               "Bengali": "ben", "Marathi": "mar", "Punjabi": "pan", "Gujarati": "guj", "Oriya": "ori",
               "Odia": "ori", "Bhojpuri": "bho", "English": "eng"}
_TOKEN_LANG = {"tel": "tel", "telugu": "tel", "tam": "tam", "tamil": "tam", "mal": "mal",
               "malayalam": "mal", "hin": "hin", "hindi": "hin", "kan": "kan", "kannada": "kan",
               "eng": "eng", "english": "eng", "ben": "ben", "bengali": "ben", "mar": "mar",
               "marathi": "mar", "pun": "pan", "punjabi": "pan", "guj": "guj", "gujarati": "guj",
               "bhojpuri": "bho", "tulu": "tul", "odia": "ori", "oriya": "ori"}
_WORD_LANG = re.compile(r"\b(telugu|tamil|malayalam|hindi|kannada|bengali|marathi|punjabi|gujarati|"
                        r"bhojpuri|odia|tulu|english)\b", re.I)
THEATRE = re.compile(r"\b(cam|camrip|hdcam|hd-?ts|ts|telesync|tc|hd-?tc|telecine|pre-?dvd|pdvd|"
                     r"dvd-?scr|dvdscreener|scr|screener|hq\s*ts|hq\s*cam)(rip)?\b", re.I)
PACK = re.compile(r"(\d{3,4}p|4k)\s*(,|&)\s*(\d{3,4}p|4k)|\+\s*rips\b", re.I)
DISC = re.compile(r"\b(dvd9|dvd5|bdiso|iso|bd25|bd50)\b", re.I)
HC = re.compile(r"\bhc[-\s]*e?subs?\b|\bhardsub|\bhc\b", re.I)


def _source(t):
    tl = t.lower()
    if "true web-dl" in tl or "true webdl" in tl:
        return "TRUE WEB-DL", 5
    if re.search(r"\bweb-?dl\b", tl):
        return "WEB-DL", 4
    if re.search(r"\bblu-?ray\b|\bbdrip\b|\bbr-?rip\b|\bbd-?rip\b", tl):
        return "BluRay", 4
    if re.search(r"\bweb-?rip\b", tl):
        return "WEBRip", 3
    if re.search(r"\bhd-?rip\b", tl):
        return "HDRip", 2
    if "dvd remux" in tl or "dvdrip" in tl:
        return "DVDRip", 1
    return "unknown", 0


def _resolution(t, size):
    tl = t.lower()
    if re.search(r"\b(2160p|4k|uhd)\b", tl):
        return 2160
    if "1080p" in tl:
        return 1080
    if "720p" in tl:
        return 720
    if re.search(r"\b(480p|576p|360p)\b", tl):
        return 480
    return 480  # these sites name SD rips by size only ("HDRip - x264 - AAC - 400MB")


def audio_langs(title, fallback=None):
    """Audio languages of a release from its name: the [Tel + Tam + Hin] bracket, else the
    language words outside it, else the scraper's language (the forum it came from). Returns
    (set of codes, guessed: bool)."""
    t = title.replace("\xa0", " ")
    langs = set()
    for br in re.findall(r"\[([^\]]+)\]", t):
        toks = [x.strip().lower() for x in re.split(r"[+&/,]", br)]
        got = {_TOKEN_LANG[x] for x in toks if x in _TOKEN_LANG}
        if got and len(got) == len([x for x in toks if x]):  # every token is a language
            langs |= got
    if langs:
        return langs, False
    head = re.sub(r"\[[^\]]*\]", " ", t)
    words = {_TOKEN_LANG[w.lower()] for w in _WORD_LANG.findall(head)}
    if words:
        return words, False
    fb = _TOKEN_LANG.get((fallback or "").lower())
    return ({fb} if fb else set()), True


def parse_title(title):
    """'The Dark Heaven (2026) TRUE WEB-DL ...' -> ('The Dark Heaven', 2026) or (None, None)."""
    t = title.replace("\xa0", " ").strip()
    m = re.match(r"^(.*?)\s*\((\d{4})\)", t)
    if not m or not m.group(1).strip():
        return None, None
    return m.group(1).strip(" -"), int(m.group(2))


def parse_release(title, size, language=None):
    """Classify one scraped release. Returns a dict; 'reject' is None for usable ones."""
    t = title.replace("\xa0", " ")
    src, src_rank = _source(t)
    res = _resolution(t, size)
    langs, guessed = audio_langs(t, language)
    rel = {"title": t.strip(), "size": size or 0, "source": src, "src_rank": src_rank, "res": res,
           "langs": sorted(langs), "langs_guessed": guessed, "hc": bool(HC.search(t)),
           "hevc": bool(re.search(r"\b(hevc|x265|h\.?265)\b", t, re.I)), "reject": None,
           # a '[Tel + Tam + Hin + Eng]' dub pack vs a plain 'Tamil TRUE WEB-DL' (an original's own rip)
           "bracket": bool(re.search(r"\[[^\]]*\+[^\]]*\]", t))}
    tl = t.lower()
    if THEATRE.search(t) or "hq clean audio" in tl:
        rel["reject"] = "theatre print"
    elif "hq clean" in tl and src_rank < 4:
        # 'HQ Clean' = theatre audio, cleaned. On a TRUE WEB-DL / BluRay it's harmless wording;
        # on an 'HDRip' it marks a print released weeks before any OTT (Jailer 2, Oct 15).
        rel["reject"] = "theatre print (HQ Clean)"
    elif PACK.search(t):
        rel["reject"] = "multi-resolution pack"
    elif DISC.search(t) and src != "DVDRip":
        rel["reject"] = "disc image"
    elif src_rank == 0:
        rel["reject"] = "unknown source"
    return rel


def tier(rel, multi=False):
    """Lower is better: 1080p within the cap, bigger 1080p, 720p, SD, then 4K (only if alone).
    multi=True: this file is to cover two languages at once, so it may be twice the size. A
    multi-audio file used for ONE language is judged like any single file (a 9 GB five-language
    pack must not beat a 3 GB Telugu rip for a Telugu original)."""
    cap = SIZE_CAP * (2 if multi else 1)
    if rel["res"] == 1080:
        return 0 if rel["size"] <= cap * 1.05 else 1
    return {720: 2, 480: 3, 2160: 4}.get(rel["res"], 5)


def rank_key(rel, multi=False):
    """Sort key within the usable releases of one language option (min = best)."""
    t = tier(rel, multi)
    size_pref = rel["size"] if t == 1 else -rel["size"]  # above the cap: smallest; within: biggest
    return (t, -rel["src_rank"], rel["hc"], rel["hevc"], rel.get("private", False), size_pref)


def plan_versions(releases, orig):
    """Pick what to download for one movie. `releases` = usable parsed releases (with 'btih').
    Returns {'status': 'ok'|'waiting_lang'|'no_release', 'versions': [{role, langs, rel}], 'why'}.
    role 'primary' = the default version (Telugu when there is one), 'secondary' = original."""
    good = [r for r in releases if not r.get("reject")]
    if not good:
        return {"status": "no_release", "versions": [], "why": "no usable HD release yet"}
    want = {"tel", orig} if orig else {"tel"}

    def best(cands, multi=False):
        return min(cands, key=lambda r: rank_key(r, multi)) if cands else None

    if orig == "tel" or not orig:
        tel = best([r for r in good if "tel" in r["langs"]])
        if tel:
            return {"status": "ok", "versions": [{"role": "primary", "lang": "tel", "rel": tel}],
                    "why": "Telugu original" if orig == "tel" else "Telugu (original unknown)"}
        return {"status": "waiting_lang", "versions": [], "why": "no Telugu release yet"}
    multi = best([r for r in good if want <= set(r["langs"])], multi=True)
    tel = best([r for r in good if "tel" in r["langs"]])
    org = best([r for r in good if orig in r["langs"]])
    if multi and (tel is None or org is None or tier(multi, True) <= max(tier(tel), tier(org))):
        return {"status": "ok", "versions": [{"role": "primary", "lang": "tel", "rel": multi}],
                "why": f"one multi-audio file ({'+'.join(multi['langs'])})"}
    out = []
    if tel:
        out.append({"role": "primary", "lang": "tel", "rel": tel})
    if org:
        out.append({"role": "secondary" if tel else "primary", "lang": orig, "rel": org})
    if not out:
        have = sorted({lang for r in good for lang in r["langs"]})
        return {"status": "waiting_lang", "versions": [],
                "why": f"only {', '.join(LANGS.get(x, x) for x in have) or 'unknown'} audio; "
                       f"waiting for {LANGS.get(orig, orig)} or Telugu"}
    why = " + ".join(f"{LANGS.get(v['lang'], v['lang'])} file" for v in out)
    return {"status": "ok", "versions": out, "why": why}


def file_name(folder, role, lang, ext):
    """Primary = bare folder name (Jellyfin's default version, listed first even at a lower
    resolution -- tested on 12.4); other versions '<folder> - <Language>'."""
    if role == "primary":
        return f"{folder}{ext}"
    return f"{folder} - {LANGS.get(lang, lang.title())}{ext}"


def week_id(ts=None):
    d = datetime.datetime.fromtimestamp(ts if ts is not None else time.time())
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def safe_stem(name):
    s = re.sub(r"[/\\\x00-\x1f]", " ", name.replace("\xa0", " "))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:180]


# ----------------------------------------------------------------------------- state
def _db():
    global _conn
    if _conn is None:
        pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS movies(
                id INTEGER PRIMARY KEY, mkey TEXT UNIQUE, title TEXT, year INTEGER,
                tmdb INTEGER, orig TEXT, facts TEXT, info TEXT, releases TEXT, plan TEXT,
                first_seen REAL, first_good REAL, status TEXT, status_why TEXT, updated REAL,
                verdict TEXT, score REAL, big INTEGER DEFAULT 0, reason TEXT, flags TEXT,
                model TEXT, decided_at REAL, approved_at REAL, notified_at REAL,
                started_at REAL, week TEXT, placed_at REAL, radarr_id INTEGER, radarr_added INTEGER DEFAULT 0,
                jf_id TEXT, keep INTEGER DEFAULT 0, expired_at REAL);
            CREATE TABLE IF NOT EXISTS versions(
                id INTEGER PRIMARY KEY, movie_id INTEGER, role TEXT, lang TEXT, title TEXT,
                btih TEXT, magnet TEXT, url TEXT, size INTEGER, private INTEGER DEFAULT 0,
                stem TEXT, state TEXT, tries INTEGER DEFAULT 0, bad TEXT DEFAULT '[]',
                error TEXT, path TEXT, dropped_at REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS blocks(mkey TEXT PRIMARY KEY, title TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE INDEX IF NOT EXISTS movies_status ON movies(status);
        """)
        _conn = c
    return _conn


def q(sql, args=()):
    with _lock:
        return _db().execute(sql, args).fetchall()


def x(sql, args=()):
    with _lock:
        cur = _db().execute(sql, args)
        return cur.lastrowid


def meta_get(k, default=None):
    r = q("SELECT v FROM meta WHERE k=?", (k,))
    return r[0]["v"] if r else default


def meta_set(k, v):
    x("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, str(v)))


def mode():
    m = meta_get("mode", MODE_DEFAULT)
    return m if m in ("approval", "auto") else "approval"


def set_status(mid, status, why=None, **extra):
    cols = {"status": status, "status_why": why, "updated": time.time(), **extra}
    x(f"UPDATE movies SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), mid))


# ----------------------------------------------------------------------------- APIs
def radarr(method, path, **kw):
    r = requests.request(method, f"{RADARR_URL}/api/v3/{path}", headers=RA, timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r.json() if r.text.strip() else None


def jellyfin(method, path, **kw):
    r = requests.request(method, f"{JELLYFIN_URL}/{path}", headers=JF, timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r.json() if r.text.strip() else None


def lookup(title, year):
    """TMDB facts via Radarr for 'title year'. Returns (best match or None, alternatives)."""
    try:
        res = radarr("GET", "movie/lookup", params={"term": f"{title} {year}"}) or []
    except Exception as e:
        log(f"  lookup '{title} ({year})' failed: {e}")
        return None, []

    def facts(m):
        im = (m.get("ratings") or {}).get("imdb") or {}
        return {"tmdb": m.get("tmdbId"), "title": m.get("title"), "year": m.get("year"),
                "orig": orig_code(m),
                "in_cinemas": (m.get("inCinemas") or "")[:10] or None,
                "digital": (m.get("digitalRelease") or "")[:10] or None,
                "imdb": im.get("value"), "imdb_votes": im.get("votes"),
                "popularity": round(m.get("popularity") or 0, 1), "genres": m.get("genres") or [],
                "runtime": m.get("runtime"), "certification": m.get("certification"),
                "overview": (m.get("overview") or "")[:240], "radarr_id": m.get("id") or None,
                "has_file": bool(m.get("hasFile")), "monitored": m.get("monitored")}
    def orig_code(m):
        name = (m.get("originalLanguage") or {}).get("name")
        return RADARR_LANG.get(name, (name or "").lower() or None)
    near = [m for m in res if m.get("year") and abs(m["year"] - year) <= 1]
    nt = norm(title)
    exact = [m for m in near if norm(m.get("title")) == nt or norm(m.get("originalTitle")) == nt
             or any(norm(a.get("title")) == nt for a in m.get("alternateTitles") or [])]
    best = (exact or [])[:1]
    alts = [facts(m) for m in (exact[1:] + [m for m in near if m not in exact])][:3]
    return (facts(best[0]) if best else None), alts


INFO_FIELDS = ("Directed by", "Written by", "Starring by", "Genres", "Categories", "Country",
               "Language", "Quality")


def parse_info_block(page):
    """movierulz 'Movie Information' block -> {field: value}."""
    i = page.find("Movie Information")
    if i < 0:
        return {}
    t = re.sub(r"<[^>]+>", "\n", page[i:i + 6000])
    t = html.unescape(t)
    lines = [ln.strip() for ln in t.split("\n") if ln.strip() and ln.strip() != ","]
    out, cur = {}, None
    for ln in lines[1:]:
        if ln.rstrip(":") in INFO_FIELDS and ln.endswith(":"):
            cur = ln.rstrip(":")
            out[cur] = []
            continue
        if ln.startswith("Synopsis") or ln.startswith("Player") or ln == "Play":
            break
        if cur:
            out[cur].append(ln.strip(" ,"))
    return {k: ", ".join(v for v in vals if v) for k, vals in out.items()}


def fetch_info(url):
    try:
        import movierulz  # FlareSolverr helper (lazy: needs its env only when used)
        status, page = movierulz.fetch(url)
        return parse_info_block(page) if status == 200 else {}
    except Exception as e:
        log(f"  info block fetch failed for {url}: {e}")
        return {}


# ----------------------------------------------------------------------------- candidates
def scraped_releases(since):
    """Movie releases from both scraper DBs added since `since` -> {mkey: [release dicts]}."""
    out = {}
    srcs = [("movierulz", MOVIERULZ_DB,
             "SELECT r.btih, r.title, r.language, r.year, r.size, r.magnet, r.added_at, p.url "
             "FROM releases r LEFT JOIN posts p ON p.post_id=r.post_id WHERE r.kind='movie'"),
            ("tamilmv", TAMILMV_DB,
             "SELECT r.btih, r.title, r.language, r.year, r.size, r.magnet, r.added_at, p.url "
             "FROM releases r LEFT JOIN posts p ON p.site=r.site AND p.post_id=r.post_id "
             "WHERE r.kind='movie' AND r.site != 'tamil_blasters'")]
    for site, path, sql in srcs:
        if not os.path.exists(path):
            continue
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            rows = c.execute(sql + " AND r.added_at > ?", (since,)).fetchall()
            c.close()
        except Exception as e:
            log(f"  reading {site} failed: {e}")
            continue
        for btih, title, lang, year, size, magnet, added, url in rows:
            name, yr = parse_title(title or "")
            if not name:
                continue
            rel = parse_release(title, size, lang)
            rel.update(btih=btih, magnet=magnet, added=added, site=site, post=url, year=yr, name=name)
            out.setdefault(f"{norm(name)}{yr}", []).append(rel)
    return out


def native_rip(rels):
    """Is there a plain single-language Indian rip ('X (2026) Tamil TRUE WEB-DL ...')? Originals
    get those; foreign films only appear as dub packs ('[Tel + Tam + Hin + Eng]') or English."""
    return any(not r["bracket"] and not r["langs_guessed"] and len(r["langs"]) == 1
               and r["langs"][0] in INDIAN for r in rels)


def foreign_reason(info, orig, rels):
    """Why this is a foreign film (never downloaded), or None. Country from the movierulz page is
    authoritative; else a non-Indian TMDB original language with no native Indian rip (TMDB's
    title search can match the wrong film -- The Grandmaster -- so that alone is not enough)."""
    if info.get("Country") and "india" not in info["Country"].lower():
        return f"foreign film ({info['Country']})"
    if orig and orig not in INDIAN and not native_rip(rels):
        return f"foreign film (original language {LANGS.get(orig, orig)})"
    return None


def merge_duplicates():
    """Two titles for one film (Telugu title vs Tamil title, 'SI' vs 'Sub Inspector'): rows with
    the same TMDB id are one movie. The oldest row keeps the decision; the others become
    'duplicate' and lend it their releases (so a Telugu rip under the Telugu title counts)."""
    rows = q("SELECT id, tmdb, status FROM movies WHERE tmdb IS NOT NULL AND status NOT IN ('expired','removed') "
             "ORDER BY id")
    by = {}
    for r in rows:
        by.setdefault(r["tmdb"], []).append(r)
    for rs in by.values():
        if len(rs) < 2:
            continue
        live = [r for r in rs if r["status"] in ("active", "done")]
        canon = (live or [r for r in rs if r["status"] != "duplicate"] or rs)[0]["id"]
        for r in rs:
            if r["id"] == canon:
                continue
            if r["status"] not in ("duplicate", "active", "done"):
                set_status(r["id"], "duplicate", f"same film as #{canon}")
            if r["id"] in _RELS:
                seen = {rel_["btih"] for rel_ in _RELS.get(canon, [])}
                _RELS.setdefault(canon, []).extend(rel_ for rel_ in _RELS[r["id"]] if rel_["btih"] not in seen)


def dedupe(rels):
    seen, out = set(), []
    for r in sorted(rels, key=lambda r: r["added"] or 0):
        if r["btih"] in seen:
            continue
        seen.add(r["btih"])
        out.append(r)
    return out


def build_candidates(now=None):
    """Refresh the movies table from the scraper DBs. New movies become 'new' (awaiting the LLM);
    known ones get their release list + plan refreshed (a Telugu/original file may appear later)."""
    now = now or time.time()
    year_now = datetime.date.fromtimestamp(now).year
    groups = scraped_releases(now - max(WINDOW_DAYS, WAIT_DAYS) * 86400)
    blocked = {r["mkey"] for r in q("SELECT mkey FROM blocks")}
    fetched, made = 0, 0
    for mkey, rels in groups.items():
        rels = dedupe(rels)
        name, yr = rels[0]["name"], rels[0]["year"]
        if yr not in (year_now, year_now - 1):
            continue
        good = [r for r in rels if not r["reject"]]
        first_good = min((r["added"] for r in good), default=None)
        row = q("SELECT * FROM movies WHERE mkey=?", (mkey,))
        if not row:
            # a new movie: only if it surfaced recently (a good rip, or a theatre print to watch)
            first = min(r["added"] for r in rels)
            # Recent = its LATEST usable rip is recent (or, theatre-only, its latest print). An early
            # pre-OTT HDRip must not hide the real OTT release weeks later: Meesaya Murukku 2's first
            # rip was a Tamil HDRip on Oct 15, its TRUE WEB-DL multi-audio on Nov 5 -- keyed on the
            # first rip, it was never a candidate (2026-11-12).
            latest = max(r["added"] for r in (good or rels))
            if latest < now - WINDOW_DAYS * 86400:
                continue
            best, alts = lookup(name, yr)
            info = {}
            post = next((r["post"] for r in rels if r["site"] == "movierulz" and r["post"]), None)
            if post and fetched < INFO_FETCH_MAX:
                info = fetch_info(post)
                fetched += 1
            facts = {"match": best, "alternatives": alts}
            orig = (best or {}).get("orig")
            status, why = "new", None
            if mkey in blocked:
                status, why = "blocked", "blocked by you"
            elif best and best.get("has_file"):
                status, why = "in_library", "already in the library"
            elif best and best.get("radarr_id") and best.get("monitored"):
                status, why = "in_library", "already requested in Radarr"
            elif foreign_reason(info, orig, rels):
                status, why = "foreign", foreign_reason(info, orig, rels)
            mid = x("INSERT INTO movies(mkey, title, year, tmdb, orig, facts, info, first_seen, first_good, "
                    "status, status_why, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mkey, name, yr, (best or {}).get("tmdb"), orig, json.dumps(facts), json.dumps(info),
                     first, first_good, status, why, now))
            made += 1
            row = q("SELECT * FROM movies WHERE id=?", (mid,))
        m = row[0]
        if m["status"] == "new":  # later rips can reveal a dub pack as the only form
            why = foreign_reason(json.loads(m["info"] or "{}"), m["orig"], rels)
            if why:
                set_status(m["id"], "foreign", why)
        slim = [{k: r[k] for k in ("title", "size", "source", "res", "langs", "hc", "reject", "site", "btih", "added")}
                for r in rels]
        x("UPDATE movies SET releases=?, first_good=COALESCE(first_good, ?) WHERE id=?",
          (json.dumps(slim), first_good, m["id"]))
        _RELS[m["id"]] = rels
    merge_duplicates()
    for m in q("SELECT * FROM movies WHERE status NOT IN ('expired','removed','duplicate')"):
        plan = current_plan(m)
        if plan:
            x("UPDATE movies SET plan=? WHERE id=?", (json.dumps(_plan_json(plan)), m["id"]))
    if made:
        log(f"candidates: {made} new movie(s)")
    meta_set("built_at", now)
    return made


_RELS = {}  # movie id -> full release dicts (with magnets) from the last build (in memory)


def _plan_json(plan):
    return {"status": plan["status"], "why": plan["why"],
            "versions": [{"role": v["role"], "lang": v["lang"], "title": v["rel"]["title"],
                          "size_gb": round(v["rel"]["size"] / 1e9, 2), "btih": v["rel"]["btih"],
                          "site": v["rel"].get("site")} for v in plan["versions"]]}


def current_plan(m):
    """Re-plan from the in-memory release list (has magnets); excludes releases that failed."""
    rels = _RELS.get(m["id"])
    if rels is None:
        return None
    bad = set()
    for v in q("SELECT bad FROM versions WHERE movie_id=?", (m["id"],)):
        bad |= set(json.loads(v["bad"] or "[]"))
    good = [r for r in rels if not r["reject"] and r["btih"] not in bad]
    return plan_versions(good, m["orig"])


# ----------------------------------------------------------------------------- decisions/approval
def candidates_for_llm():
    """Movies awaiting a decision, compact, for the LLM."""
    out = []
    for m in q("SELECT * FROM movies WHERE status='new' ORDER BY first_good DESC, first_seen DESC"):
        facts = json.loads(m["facts"] or "{}")
        plan = json.loads(m["plan"] or "{}")
        rels = json.loads(m["releases"] or "[]")
        avail = sorted({f"{'+'.join(r['langs']) or '?'} {r['res']}p {r['source']}" for r in rels if not r["reject"]})
        rejected = sorted({f"{r['reject']}" for r in rels if r["reject"]})
        out.append({"id": m["id"], "title": m["title"], "year": m["year"], "tmdb_match": facts.get("match"),
                    "tmdb_alternatives": facts.get("alternatives"), "movierulz_info": json.loads(m["info"] or "{}"),
                    "available": avail, "rejected_releases": rejected, "plan": plan,
                    "first_good_rip": _day(m["first_good"]),
                    "latest_good_rip": _day(max((r.get("added") or 0 for r in rels if not r["reject"]), default=0) or None),
                    "first_seen": _day(m["first_seen"])})
    return out


def _day(ts):
    return datetime.date.fromtimestamp(ts).isoformat() if ts else None


def apply_decisions(decisions, model=None):
    """Store the LLM's verdicts. download -> proposed (approval mode) / approved (auto);
    watchlist -> watchlist; skip -> skipped. tmdb/orig corrections re-plan the movie."""
    done = []
    for d in decisions:
        mid = int(d["id"])
        r = q("SELECT * FROM movies WHERE id=?", (mid,))
        if not r:
            continue
        m = r[0]
        verdict = (d.get("verdict") or "").lower()
        if verdict not in ("download", "skip", "watchlist"):
            raise ValueError(f"movie {mid}: verdict must be download|skip|watchlist")
        tmdb = d.get("tmdb_id") or m["tmdb"]
        orig = (d.get("orig_lang") or m["orig"] or "").lower()[:3] or None
        orig = _TOKEN_LANG.get(orig, orig)
        flags = d.get("flags") or []
        x("UPDATE movies SET verdict=?, score=?, big=?, reason=?, flags=?, model=?, decided_at=?, tmdb=?, orig=? "
          "WHERE id=?", (verdict, float(d.get("score") or 0), 1 if d.get("big") else 0,
                         (d.get("reason") or "")[:400], json.dumps(flags), model, time.time(), tmdb, orig, mid))
        if orig and orig not in INDIAN:
            set_status(mid, "foreign", f"original language {orig}")
        elif verdict == "skip":
            set_status(mid, "skipped", (d.get("reason") or "")[:200])
        elif verdict == "watchlist":
            set_status(mid, "watchlist", "waiting for an OTT/HD release")
        elif not tmdb:
            set_status(mid, "skipped", "no TMDB id -- identity not confirmed")
        else:
            m2 = q("SELECT * FROM movies WHERE id=?", (mid,))[0]
            plan = current_plan(m2)
            if plan:  # the original language may have been corrected: store the new plan
                x("UPDATE movies SET plan=? WHERE id=?", (json.dumps(_plan_json(plan)), mid))
            else:
                plan = {"status": json.loads(m2["plan"] or "{}").get("status")}
            if plan.get("status") == "waiting_lang":
                set_status(mid, "waiting_lang", plan.get("why"))
            elif mode() == "auto":
                set_status(mid, "approved", "auto mode", approved_at=time.time())
            else:
                set_status(mid, "proposed", None)
        done.append(mid)
    merge_duplicates()  # an LLM-corrected TMDB id can reveal two titles of one film
    if done:  # the Muse fallback job skips a day the Claude job already covered
        meta_set("last_decision", json.dumps({"at": time.time(), "model": model, "count": len(done)}))
    return done


def approve(ids, approve_=True):
    out = []
    for mid in ids:
        r = q("SELECT status FROM movies WHERE id=?", (int(mid),))
        if not r:
            continue
        if approve_:
            if r[0]["status"] in ("proposed", "skipped", "new", "rejected", "waiting_lang"):
                set_status(int(mid), "approved", "approved by you", approved_at=time.time())
                out.append(int(mid))
        elif r[0]["status"] in ("proposed", "approved", "new", "watchlist", "waiting_lang"):
            set_status(int(mid), "rejected", "rejected by you")
            out.append(int(mid))
    return out


def block(ids=(), titles=()):
    out = []
    for mid in ids:
        r = q("SELECT mkey, title FROM movies WHERE id=?", (int(mid),))
        if r:
            x("INSERT OR REPLACE INTO blocks VALUES (?,?,?)", (r[0]["mkey"], r[0]["title"], time.time()))
            if q("SELECT status FROM movies WHERE id=?", (int(mid),))[0]["status"] not in ("active", "done"):
                set_status(int(mid), "blocked", "blocked by you")
            out.append(r[0]["title"])
    for t in titles:
        name, yr = parse_title(t) if "(" in t else (t, None)
        for m in q("SELECT id, mkey, title, year FROM movies"):
            if norm(m["title"]) == norm(name) and (yr is None or m["year"] == yr):
                out += block([m["id"]])
    return out


def unblock(ids):
    for mid in ids:
        r = q("SELECT mkey FROM movies WHERE id=?", (int(mid),))
        if r:
            x("DELETE FROM blocks WHERE mkey=?", (r[0]["mkey"],))
            set_status(int(mid), "new", "unblocked")
    return list(ids)


# ----------------------------------------------------------------------------- weekly cap + start
def week_counts(wk):
    rows = q("SELECT big FROM movies WHERE week=?", (wk,))
    return len(rows), sum(1 for r in rows if r["big"])


def cap(now, big=False):
    """The week's movie cap at `now`: WEEKLY, +FRIDAY_EXTRA from Friday on, +OVERFLOW for big films."""
    friday = datetime.date.fromtimestamp(now).weekday() >= FRIDAY_FROM
    return WEEKLY + (FRIDAY_EXTRA if friday else 0) + (OVERFLOW if big else 0)


def slots(wk, big, now=None):
    n, _ = week_counts(wk)
    return cap(now if now is not None else time.time(), big) - n


def start_approved(now=None):
    """Start approved movies (and watchlist ones whose HD release appeared) within the weekly cap."""
    now = now or time.time()
    wk = week_id(now)
    weekday = datetime.date.fromtimestamp(now).weekday()
    for m in q("SELECT * FROM movies WHERE status='watchlist'"):
        plan = current_plan(m)
        if plan and plan["status"] == "ok":
            set_status(m["id"], "approved", "watchlist: HD release is out", approved_at=now, big=1)
    for m in q("SELECT * FROM movies WHERE status='waiting_lang' AND verdict='download'"):
        plan = current_plan(m)
        if plan and plan["status"] == "ok":  # the original or Telugu file appeared
            if mode() == "auto" or m["approved_at"]:
                set_status(m["id"], "approved", plan["why"], approved_at=m["approved_at"] or now)
            else:
                set_status(m["id"], "proposed", plan["why"])
    rows = q("SELECT * FROM movies WHERE status='approved' ORDER BY big DESC, score DESC, approved_at")
    started = []
    for m in rows:
        if slots(wk, bool(m["big"]), now) <= 0:
            continue
        fill = (m["score"] or 0) < GOOD_SCORE or "unconfirmed" in (m["flags"] or "")
        if mode() == "auto" and fill and not m["big"] and weekday < FILL_WEEKDAY:
            continue  # good picks first; fills only from FILL_WEEKDAY if slots remain
        plan = current_plan(m)
        if plan is None:
            continue  # releases not loaded yet (right after a restart): next pass
        if plan["status"] != "ok":
            set_status(m["id"], "waiting_lang" if plan["status"] == "waiting_lang" else "approved", plan["why"])
            continue
        for v in plan["versions"]:
            queue_version(m, v)
        set_status(m["id"], "active", plan["why"], started_at=now, week=wk)
        started.append(m["title"])
    if started:
        log(f"started {len(started)}: {', '.join(started)}")
    return started


def queue_version(m, v, row=None):
    """Drop one version into the bridge's `fresh` blackhole."""
    rel = v["rel"]
    stem = safe_stem(rel["title"])
    BLACKHOLE.mkdir(parents=True, exist_ok=True)
    if rel.get("torrent"):
        (BLACKHOLE / f"{stem}.torrent").write_bytes(rel["torrent"])
    else:
        (BLACKHOLE / f"{stem}.magnet").write_text(rel["magnet"])
    vals = (v["role"], v["lang"], rel["title"], rel["btih"], rel.get("magnet"), rel.get("url"), rel["size"],
            1 if rel.get("private") else 0, stem, "dropped", time.time(), time.time())
    if row:
        x("UPDATE versions SET role=?, lang=?, title=?, btih=?, magnet=?, url=?, size=?, private=?, stem=?, "
          "state=?, dropped_at=?, updated=?, error=NULL WHERE id=?", (*vals, row["id"]))
    else:
        x("INSERT INTO versions(movie_id, role, lang, title, btih, magnet, url, size, private, stem, state, "
          "dropped_at, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (m["id"], *vals))
    log(f"  queued [{v['role']} {v['lang']}] {stem}")


# ----------------------------------------------------------------------------- download tracking
def bridge_failed(stem, since):
    """Did the bridge give up on this drop? (bridge.db events: failed / dropped)."""
    try:
        c = sqlite3.connect(f"file:{os.environ.get('BRIDGE_DB', '/config/bridge.db')}?mode=ro", uri=True, timeout=5)
        r = c.execute("SELECT type, detail FROM events WHERE release=? AND ts>=? AND type IN ('failed','dropped') "
                      "ORDER BY ts DESC LIMIT 1", (stem, since - 60)).fetchone()
        c.close()
        return (r[0] + (f": {r[1]}" if r[1] else "")) if r else None
    except Exception:
        return None


def bridge_tracks(stem):
    """Is the drop still waiting in the blackhole or a live transfer?"""
    if any((BLACKHOLE / f"{stem}{s}").exists() for s in (".magnet", ".torrent")):
        return True
    sd = DATA / "blackhole" / ".bridge-state"  # one base64("<cat>|<title>").json per transfer
    for p in sd.glob("*.json") if sd.exists() else []:
        if p.name.startswith("."):
            continue
        try:
            rec = json.loads(p.read_text())
            if rec.get("category") == "fresh" and rec.get("release_name") == stem and not rec.get("superseded"):
                return True
        except Exception:
            continue
    return False


def check_versions(now=None):
    now = now or time.time()
    for v in q("SELECT v.*, m.title AS mtitle FROM versions v JOIN movies m ON m.id=v.movie_id WHERE v.state='dropped'"):
        dest = DELIVERED / v["stem"]
        if dest.exists():
            try:
                place(v, dest)
            except Exception as e:
                x("UPDATE versions SET error=?, updated=? WHERE id=?", (f"place: {e}"[:300], now, v["id"]))
                log(f"  placing '{v['stem']}' failed: {e!r}")
            continue
        why = bridge_failed(v["stem"], v["dropped_at"] or 0)
        if not why and not bridge_tracks(v["stem"]) and now - (v["dropped_at"] or now) > DROP_TIMEOUT:
            why = "not delivered within the timeout"
        if why:
            retry_version(v, why)
    # movies whose versions are all settled
    for m in q("SELECT * FROM movies WHERE status='active'"):
        vs = q("SELECT state FROM versions WHERE movie_id=?", (m["id"],))
        if vs and all(s["state"] in ("placed", "failed") for s in vs):
            if any(s["state"] == "placed" for s in vs):
                set_status(m["id"], "done", None, placed_at=m["placed_at"] or now)
            else:
                set_status(m["id"], "failed", "every download failed")
                ntfy(f"Latest Releases: {m['title']} failed", "Every release tried failed to download.",
                     priority="default", tags="warning")
    add_missing_versions()


def retry_version(v, why):
    bad = set(json.loads(v["bad"] or "[]")) | {v["btih"]}
    tries = (v["tries"] or 0) + 1
    x("UPDATE versions SET bad=?, tries=?, error=?, updated=? WHERE id=?",
      (json.dumps(sorted(bad)), tries, why[:300], time.time(), v["id"]))
    m = q("SELECT * FROM movies WHERE id=?", (v["movie_id"],))[0]
    log(f"  '{v['stem']}' failed ({why}); try {tries}/{MAX_TRIES}")
    if tries >= MAX_TRIES:
        x("UPDATE versions SET state='failed' WHERE id=?", (v["id"],))
        return
    plan = current_plan(m)
    nxt = next((p for p in (plan or {}).get("versions", []) if p["lang"] == v["lang"]), None) if plan else None
    if nxt is None:
        nxt = prowlarr_version(m, v["lang"], bad)
    if nxt is None:
        x("UPDATE versions SET state='failed' WHERE id=?", (v["id"],))
        return
    nxt["role"] = v["role"]
    queue_version(m, nxt, row=q("SELECT * FROM versions WHERE id=?", (v["id"],))[0])


def add_missing_versions():
    """A done movie whose second language file appeared later gets it (not counted again)."""
    for m in q("SELECT * FROM movies WHERE status IN ('done','active') AND expired_at IS NULL"):
        plan = current_plan(m)
        if not plan or plan["status"] != "ok":
            continue
        # every language ever queued counts (a failed one stays failed: no endless retries)
        have = {r["lang"] for r in q("SELECT lang FROM versions WHERE movie_id=?", (m["id"],))}
        for v in plan["versions"]:
            if v["lang"] in have or _has_multi(m["id"], v["lang"]):
                continue
            log(f"  {m['title']}: adding the {LANGS.get(v['lang'], v['lang'])} version that appeared")
            queue_version(m, v)
            if m["status"] == "done":
                set_status(m["id"], "active", "adding a version")


def _has_multi(mid, lang):
    for r in q("SELECT title FROM versions WHERE movie_id=? AND state!='failed'", (mid,)):
        langs, _ = audio_langs(r["title"])
        if lang in langs:
            return True
    return False


def prowlarr_version(m, lang, bad):
    """Fallback source: Prowlarr search, public indexers first, private ones last."""
    if not (PROWLARR_URL and PROWLARR_API_KEY):
        return None
    try:
        r = requests.get(f"{PROWLARR_URL}/api/v1/search", params={"query": f"{m['title']} {m['year']}", "type": "search",
                                                                  "categories": 2000, "limit": 100},
                         headers={"X-Api-Key": PROWLARR_API_KEY}, timeout=120)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        log(f"  prowlarr search for '{m['title']}' failed: {e}")
        return None
    cands = []
    for it in rows:
        name, yr = parse_title(it.get("title") or "")
        t = it.get("title") or ""
        if name is None:
            m2 = re.match(r"^(.*?)[ .](\d{4})[ .]", t)
            name, yr = (m2.group(1).replace(".", " "), int(m2.group(2))) if m2 else (None, None)
        if not name or norm(name) != norm(m["title"]) or yr != m["year"]:
            continue
        rel = parse_release(t, it.get("size") or 0, None)
        if rel["reject"] or lang not in rel["langs"] or (it.get("seeders") or 0) < 1:
            continue
        rel.update(btih=(it.get("infoHash") or it.get("guid") or t).lower(), magnet=it.get("magnetUrl"),
                   url=it.get("downloadUrl"), private=(it.get("indexerPrivacy") or it.get("privacy")) == "private",
                   site=it.get("indexer"))
        if rel["btih"] in bad:
            continue
        cands.append(rel)
    if not cands:
        return None
    best = min(cands, key=rank_key)  # private sorts after public within a tier
    if not best.get("magnet"):
        try:
            rr = requests.get(best["url"], timeout=60, allow_redirects=False)
            if rr.is_redirect and rr.headers.get("Location", "").startswith("magnet:"):
                best["magnet"] = rr.headers["Location"]
            else:
                rr.raise_for_status()
                best["torrent"] = rr.content
        except Exception as e:
            log(f"  prowlarr download of '{best['title']}' failed: {e}")
            return None
    log(f"  prowlarr fallback for {m['title']} [{lang}]: {best['title']} ({'private' if best.get('private') else 'public'})")
    return {"role": "primary", "lang": lang, "rel": best}


# ----------------------------------------------------------------------------- placement
def biggest_video(path):
    if path.is_file():
        return path if path.suffix.lower() in VIDEO_EXT else None
    vids = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT
            and "sample" not in p.name.lower() and "sample" not in str(p.parent).lower()]
    return max(vids, key=lambda p: p.stat().st_size) if vids else None


def mkv_tracks(path):
    out = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, timeout=120)
    if out.returncode not in (0, 1):
        raise RuntimeError(out.stderr.strip()[:200] or "mkvmerge failed")
    return json.loads(out.stdout).get("tracks", [])


def default_audio_args(tracks, lang="tel"):
    """mkvpropedit args making the first `lang` audio track the default (others not), or None."""
    audio = [t for t in tracks if t.get("type") == "audio"]
    lang2 = {"tel": "te", "tam": "ta", "mal": "ml", "hin": "hi", "kan": "kn"}.get(lang, lang)

    def is_lang(t):
        p = t.get("properties") or {}
        return p.get("language") == lang or (p.get("language_ietf") or "").split("-")[0] == lang2 or \
            lang in (p.get("track_name") or "").lower()[:3] or LANGS.get(lang, "").lower() in (p.get("track_name") or "").lower()
    idx = next((i for i, t in enumerate(audio) if is_lang(t)), None)
    if idx is None or len(audio) < 2:
        return None
    args = []
    for i in range(len(audio)):
        args += ["--edit", f"track:a{i + 1}", "--set", f"flag-default={1 if i == idx else 0}"]
    return args


def set_default_telugu(path):
    if path.suffix.lower() != ".mkv":
        return "not mkv"
    args = default_audio_args(mkv_tracks(path))
    if not args:
        return "no Telugu track / single audio"
    r = subprocess.run(["mkvpropedit", str(path), *args], capture_output=True, text=True, timeout=300)
    if r.returncode not in (0, 1):
        raise RuntimeError(r.stderr.strip()[:200] or r.stdout.strip()[:200])
    return "Telugu track set as default"


def ensure_radarr_movie(m):
    """The Radarr movie for this pick (added unmonitored, no search, in ROOT). Returns (id, path)."""
    if m["radarr_id"]:
        try:
            mv = radarr("GET", f"movie/{m['radarr_id']}")
            return mv["id"], mv["path"]
        except requests.HTTPError:
            pass
    found = radarr("GET", "movie/lookup/tmdb", params={"tmdbId": m["tmdb"]})
    if found.get("id"):
        x("UPDATE movies SET radarr_id=? WHERE id=?", (found["id"], m["id"]))
        return found["id"], found["path"]
    prof = next(p["id"] for p in radarr("GET", "qualityprofile") if p["name"] == PROFILE)
    found.update(qualityProfileId=prof, rootFolderPath=ROOT, monitored=False, minimumAvailability="released",
                 addOptions={"searchForMovie": False, "monitor": "none"})
    mv = radarr("POST", "movie", json=found)
    x("UPDATE movies SET radarr_id=?, radarr_added=1 WHERE id=?", (mv["id"], m["id"]))
    log(f"  added '{mv['title']} ({mv['year']})' to Radarr (unmonitored) at {mv['path']}")
    return mv["id"], mv["path"]


def place(v, dest):
    m = q("SELECT * FROM movies WHERE id=?", (v["movie_id"],))[0]
    video = biggest_video(dest)
    if video is None:
        raise RuntimeError("no video file in the download")
    rid, path = ensure_radarr_movie(m)
    folder = pathlib.Path(path)
    folder.mkdir(parents=True, exist_ok=True)
    langs, _ = audio_langs(v["title"])
    note = ""
    if len(langs) > 1 and "tel" in langs:
        try:
            note = set_default_telugu(video)
        except Exception as e:
            note = f"default track not set: {e}"
    role = v["role"]  # no Telugu yet -> the original is primary (bare name) until one arrives
    if role == "primary" and v["lang"] == "tel":
        # a Telugu file arriving after the original took the bare name: rename that one first
        for old in q("SELECT * FROM versions WHERE movie_id=? AND state='placed' AND role='primary' AND id!=?",
                     (m["id"], v["id"])):
            op = pathlib.Path(old["path"] or "")
            if op.exists():
                np_ = folder / file_name(folder.name, "secondary", old["lang"], op.suffix.lower())
                os.rename(op, np_)
                x("UPDATE versions SET role='secondary', path=? WHERE id=?", (str(np_), old["id"]))
    target = folder / file_name(folder.name, role, v["lang"], video.suffix.lower())
    os.replace(video, target)
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)
    x("UPDATE versions SET state='placed', path=?, updated=?, error=NULL WHERE id=?", (str(target), time.time(), v["id"]))
    if not m["placed_at"]:
        x("UPDATE movies SET placed_at=? WHERE id=?", (time.time(), m["id"]))
    log(f"  placed {target}" + (f" ({note})" if note else ""))
    try:
        radarr("POST", "command", json={"name": "RescanMovie", "movieId": rid})
        jellyfin("POST", "Library/Media/Updated", json={"Updates": [{"Path": str(folder), "UpdateType": "Created"}]})
    except Exception as e:
        log(f"  rescan notify failed (will show up on the next scan): {e}")


# ----------------------------------------------------------------------------- Jellyfin collection
def jf_movie_ids(paths):
    """Jellyfin item ids for movie folders -> {folder: item id}."""
    items = jellyfin("GET", "Items", params={"Recursive": "true", "IncludeItemTypes": "Movie",
                                             "Fields": "Path"}).get("Items", [])
    out = {}
    for it in items:
        p = it.get("Path") or ""
        for f in paths:
            if p.startswith(f.rstrip("/") + "/"):
                out[f] = it["Id"]
    return out


def collection_id(create_with=None):
    items = jellyfin("GET", "Items", params={"Recursive": "true", "IncludeItemTypes": "BoxSet"}).get("Items", [])
    for it in items:
        if it.get("Name") == COLLECTION:
            return it["Id"]
    if create_with:
        r = jellyfin("POST", "Collections", params={"Name": COLLECTION, "Ids": ",".join(create_with)})
        log(f"created Jellyfin collection '{COLLECTION}'")
        return r.get("Id")
    return None


def sync_collection():
    rows = q("SELECT m.id, m.radarr_id FROM movies m WHERE m.placed_at IS NOT NULL AND m.expired_at IS NULL "
             "AND m.radarr_id IS NOT NULL")
    folders = {}
    for r in rows:
        try:
            folders[radarr("GET", f"movie/{r['radarr_id']}")["path"]] = r["id"]
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                set_status(r["id"], "removed", "deleted from Radarr", expired_at=time.time())
    want = jf_movie_ids(list(folders))
    cid = collection_id(create_with=list(want.values()) if want else None)
    if not cid:
        return
    have = {it["Id"] for it in jellyfin("GET", "Items", params={"ParentId": cid}).get("Items", [])}
    add = set(want.values()) - have
    drop = have - set(want.values())
    if add:
        jellyfin("POST", f"Collections/{cid}/Items", params={"Ids": ",".join(add)})
    if drop:
        jellyfin("DELETE", f"Collections/{cid}/Items", params={"Ids": ",".join(drop)})
    for f, jid in want.items():
        x("UPDATE movies SET jf_id=? WHERE id=?", (jid, folders[f]))
    if add or drop:
        log(f"collection '{COLLECTION}': +{len(add)} -{len(drop)}")


# ----------------------------------------------------------------------------- expiry
def held_by_users(jf_ids):
    """{jellyfin item id: reason} for items ANY enabled user (guest included) has favourited or
    partly watched. Raises on any API error -- the caller then deletes nothing."""
    users = [u for u in jellyfin("GET", "Users") if not (u.get("Policy") or {}).get("IsDisabled")]
    if not users:
        raise RuntimeError("no Jellyfin users")
    held = {}
    for u in users:
        items = jellyfin("GET", f"Users/{u['Id']}/Items", params={"Ids": ",".join(jf_ids), "Fields": "UserData"})
        for it in items.get("Items", []):
            ud = it.get("UserData") or {}
            if ud.get("IsFavorite"):
                held[it["Id"]] = f"favourite of {u.get('Name')}"
            elif (ud.get("PlaybackPositionTicks") or 0) > 0 and not ud.get("Played"):
                held.setdefault(it["Id"], f"{u.get('Name')} is part-way through")
    return held


def expire(now=None):
    now = now or time.time()
    due = q("SELECT * FROM movies WHERE placed_at IS NOT NULL AND expired_at IS NULL AND keep=0 AND radarr_added=1 "
            "AND placed_at < ? AND status IN ('done','active','failed')", (now - KEEP_DAYS * 86400,))
    if not due:
        return []
    jids = [m["jf_id"] for m in due if m["jf_id"]]
    try:
        held = held_by_users(jids) if jids else {}
    except Exception as e:
        log(f"expiry skipped -- Jellyfin check failed, deleting nothing: {e}")
        return []
    gone = []
    for m in due:
        if not m["jf_id"]:
            continue  # never seen in Jellyfin: can't check favourites -> keep
        if m["jf_id"] in held:
            if m["status_why"] != held[m["jf_id"]]:
                x("UPDATE movies SET status_why=? WHERE id=?", (held[m["jf_id"]], m["id"]))
                log(f"  keeping '{m['title']}': {held[m['jf_id']]}")
            continue
        if len(gone) >= EXPIRE_MAX:
            break
        label = f"{m['title']} ({m['year']})"
        if EXPIRE_DRY_RUN:
            log(f"  WOULD expire {label} (dry-run)")
            gone.append(label)
            continue
        try:
            requests.delete(f"{RADARR_URL}/api/v3/movie/{m['radarr_id']}", headers=RA, timeout=60,
                            params={"deleteFiles": "true", "addImportExclusion": "false"}).raise_for_status()
        except Exception as e:
            log(f"  expiring {label} failed: {e}")
            continue
        set_status(m["id"], "expired", f"{KEEP_DAYS:g} days up", expired_at=now)
        log(f"  expired {label}")
        gone.append(label)
    if gone:
        ntfy(("WOULD expire" if EXPIRE_DRY_RUN else "Expired") + f" {len(gone)} Latest Release(s)",
             "\n".join(f"- {g}" for g in gone), priority="low", tags="wastebasket")
    return gone


def housekeeping(now=None):
    now = now or time.time()
    for m in q("SELECT id, status, decided_at, first_seen FROM movies WHERE status IN ('proposed','waiting_lang','watchlist')"):
        age = now - (m["decided_at"] or m["first_seen"] or now)
        limit = PROPOSAL_DAYS if m["status"] == "proposed" else WAIT_DAYS
        if age > limit * 86400:
            set_status(m["id"], "lapsed", f"{m['status']} for {limit} days")


# ----------------------------------------------------------------------------- digest/status
def digest(only_new=False):
    wk = week_id()
    n, _ = week_counts(wk)
    props = q("SELECT * FROM movies WHERE status='proposed' ORDER BY score DESC")
    if only_new:
        props = [p for p in props if not p["notified_at"]]
    lines = [f"🎬 Latest Releases — {mode()} mode, {n}/{cap(time.time())} this week"]
    if props:
        lines.append("Proposed (reply e.g. \"ok 12 14\", \"no 13\", \"block 15\"):")
        for p in props:
            plan = json.loads(p["plan"] or "{}")
            files = " + ".join(f"{LANGS.get(v['lang'], v['lang'])} {v['size_gb']}GB" for v in plan.get("versions", []))
            lines.append(f"#{p['id']} {p['title']} ({p['year']}) · {p['score']:g}/10 · {files}\n   {p['reason']}")
    else:
        lines.append("No new proposals.")
    since = time.time() - 4 * 86400
    recent = q("SELECT title, year, status FROM movies WHERE status IN ('active','done') AND started_at > ?", (since,))
    if recent:
        lines.append("Downloading/added: " + ", ".join(f"{r['title']}" for r in recent))
    wl = q("SELECT title FROM movies WHERE status='watchlist'")
    if wl:
        lines.append("Watchlist (grab when OTT): " + ", ".join(r["title"] for r in wl))
    return {"text": "\n".join(lines), "ids": [p["id"] for p in props], "mode": mode(), "week_count": n}


def mark_notified(ids):
    for i in ids:
        x("UPDATE movies SET notified_at=? WHERE id=?", (time.time(), int(i)))


def status_json():
    rows = q("SELECT id, title, year, status, status_why, verdict, score, big, reason, week, placed_at, keep "
             "FROM movies ORDER BY updated DESC LIMIT 300")
    return {"mode": mode(), "week": week_id(), "week_count": week_counts(week_id())[0], "weekly": cap(time.time()),
            "movies": [dict(r) for r in rows],
            "versions": [dict(r) for r in q("SELECT movie_id, role, lang, title, state, tries, error, path "
                                              "FROM versions ORDER BY updated DESC LIMIT 300")]}


def movie_json(mid):
    r = q("SELECT * FROM movies WHERE id=?", (mid,))
    if not r:
        return None
    d = dict(r[0])
    for k in ("facts", "info", "releases", "plan", "flags"):
        d[k] = json.loads(d[k] or "null")
    d["versions"] = [dict(v) for v in q("SELECT * FROM versions WHERE movie_id=?", (mid,))]
    return d


def page():
    s = status_json()
    e = html.escape
    def score(m):
        return "" if m["score"] is None else f"{m['score']:g}"
    rows = "".join(
        f"<tr><td>{m['id']}</td><td>{e(m['title'])} ({m['year']})</td><td>{e(m['status'])}</td>"
        f"<td>{score(m)}</td><td>{e(m['status_why'] or m['reason'] or '')}</td></tr>"
        for m in s["movies"])
    return (f"<!doctype html><title>Latest Releases</title><meta name=viewport content='width=device-width'>"
            f"<style>body{{font:14px system-ui;margin:16px}}td{{padding:3px 8px;border-bottom:1px solid #ddd;vertical-align:top}}</style>"
            f"<h1>Latest Releases</h1><p>{e(s['mode'])} mode · week {e(s['week'])}: {s['week_count']}/{s['weekly']}</p>"
            f"<table><tr><th>#</th><th>movie</th><th>status</th><th>score</th><th>why</th></tr>{rows}</table>")


# ----------------------------------------------------------------------------- HTTP (via youtube.py's server)
def handle(method, path, body=None):
    """(code, content-type, payload) for /fresh/*. body = parsed JSON for POST."""
    p = path.split("?")[0].rstrip("/")
    qs = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&") if "=" in kv) if "?" in path else {}
    j = "application/json"
    if method == "GET":
        if p == "/fresh":
            return 200, "text/html; charset=utf-8", page()
        if p == "/fresh/status.json":
            return 200, j, json.dumps(status_json(), default=str)
        if p == "/fresh/candidates":
            return 200, j, json.dumps({"week": week_id(), "week_count": week_counts(week_id())[0], "weekly": cap(time.time()),
                                       "mode": mode(), "last_decision": json.loads(meta_get("last_decision", "null")),
                                       "candidates": candidates_for_llm()}, default=str)
        if p == "/fresh/digest":
            return 200, j, json.dumps(digest(only_new=qs.get("new") == "1"))
        m = re.fullmatch(r"/fresh/movie/(\d+)", p)
        if m:
            d = movie_json(int(m.group(1)))
            return (200, j, json.dumps(d, default=str)) if d else (404, j, '{"error":"no such movie"}')
        return 404, j, '{"error":"not found"}'
    body = body or {}
    ids = [int(i) for i in body.get("ids", [])]
    if p == "/fresh/decisions":
        return 200, j, json.dumps({"stored": apply_decisions(body.get("decisions", []), body.get("model"))})
    if p == "/fresh/approve":
        return 200, j, json.dumps({"approved": approve(ids, True)})
    if p == "/fresh/reject":
        return 200, j, json.dumps({"rejected": approve(ids, False)})
    if p == "/fresh/block":
        return 200, j, json.dumps({"blocked": block(ids, body.get("titles", []))})
    if p == "/fresh/unblock":
        return 200, j, json.dumps({"unblocked": unblock(ids)})
    if p == "/fresh/keep":
        for i in ids:
            x("UPDATE movies SET keep=? WHERE id=?", (0 if body.get("keep") is False else 1, i))
        return 200, j, json.dumps({"kept": ids, "keep": body.get("keep", True)})
    if p == "/fresh/notified":
        mark_notified(ids)
        return 200, j, json.dumps({"notified": ids})
    if p == "/fresh/mode":
        if body.get("mode") not in ("approval", "auto"):
            return 400, j, '{"error":"mode must be approval|auto"}'
        meta_set("mode", body["mode"])
        return 200, j, json.dumps({"mode": body["mode"]})
    if p == "/fresh/run":
        threading.Thread(target=run_once, kwargs={"force_build": True}, daemon=True).start()
        return 202, j, '{"started":true}'
    return 404, j, '{"error":"not found"}'


# ----------------------------------------------------------------------------- loop
_run_lock = threading.Lock()


def run_once(force_build=False):
    if not _run_lock.acquire(blocking=False):
        return
    try:
        now = time.time()
        if force_build or not _RELS or now - float(meta_get("built_at", 0)) > BUILD_EVERY:
            build_candidates(now)
        start_approved(now)
        check_versions(now)
        housekeeping(now)
        try:
            sync_collection()
        except Exception as e:
            log(f"collection sync failed: {e}")
        if now - float(meta_get("expired_check", 0)) > 6 * 3600:
            meta_set("expired_check", now)
            expire(now)
    finally:
        _run_lock.release()


def run_forever():
    log(f"starting: {mode()} mode, {WEEKLY}/week (+{FRIDAY_EXTRA} from Friday, +{OVERFLOW} big), keep {KEEP_DAYS:g}d"
        f"{' (expiry DRY-RUN)' if EXPIRE_DRY_RUN else ''}, collection '{COLLECTION}'")
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"pass failed: {e!r}")
        time.sleep(POLL)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_candidates()
        print(json.dumps(candidates_for_llm(), indent=1, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(status_json(), indent=1, default=str))
    else:
        run_forever()
