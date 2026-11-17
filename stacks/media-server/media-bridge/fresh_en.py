#!/usr/bin/env python3
"""
fresh_en -- "Latest Hollywood": new English-language movies, picked by an LLM once they are out on
digital/OTT (or disc), downloaded automatically, kept a month. The Hollywood sibling of fresh.py.

It is the SAME engine (fresh.py), loaded a second time as its own module with VARIANT=fresh-en:
own DB (config/fresh_en.db), own bridge category (blackhole/fresh-en, downloads/fresh-en), own
Jellyfin collection ("Latest Hollywood"), own HTTP prefix (/fresh-en on :8003), own FRESH_EN_* env.
This file only swaps the Hollywood-specific hooks:

  * Sources (feed first, gated by metadata; checked live 2026-11-16, see CLAUDE.md):
      - Prowlarr RSS harvest of the public indexers -> `feed` table (the main rip watcher: P2P groups
        post WEB-DLs the day a film hits streaming);
      - targeted Prowlarr searches for films a calendar says are out (TMDB discover digital/physical
        releases, srrDB scene pres with their IMDb id, whentostream.com's RSS headlines) -> the
        `calendar` table, re-searched every 6h for 14 days ("streaming, but no rip yet");
      - Telugu dub packs of the same films from the movierulz/tamilmv DBs (+ a daily search).
  * Rules (user, 2026-11-16): English original only; the English file is the main one (bare name)
    and a Telugu file is added as a second one whenever it exists (never waited for); 4K first --
    1080p now and swapped for 4K in place when one appears; REMUX / full disc only for films the
    LLM flags big (a full disc is remuxed to one lossless .mkv); auto mode, 7/week (+3 big).
"""
import datetime
import html
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

import requests

from common import norm


def _load_engine():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("fresh_en_engine", os.path.join(here, "fresh.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.VARIANT = "fresh-en"
    mod.ENV_PREFIX = "FRESH_EN_"
    mod.DEFAULTS = {"TITLE": "Latest Hollywood", "DB": "/config/fresh_en.db", "MODE": "auto",
                    "WEEKLY": "7", "OVERFLOW": "3", "FRIDAY_EXTRA": "0", "INFO_FETCH_MAX": "0",
                    "MIN_FREE_GB": "300", "WINDOW_DAYS": "30"}
    spec.loader.exec_module(mod)
    sys.modules["fresh_en_engine"] = mod
    return mod


E = _load_engine()
log = E.log
_env = E._env

# ----------------------------------------------------------------------------- config
RSS_EVERY = int(_env("RSS_EVERY", "7200"))
# public Prowlarr indexers harvested: YTS, 1337x, TPB, Knaben, Uindex, TorrentDownload, LimeTorrents
RSS_INDEXERS = [int(i) for i in _env("RSS_INDEXERS", "1,18,2,15,8,9,13").split(",") if i.strip()]
CAP_4K = float(_env("4K_CAP_GB", "30")) * 1e9
HD_CAP = float(_env("HD_CAP_GB", "12")) * 1e9
REMUX_CAP = float(_env("REMUX_CAP_GB", "100")) * 1e9
MIN_SEEDERS = int(_env("MIN_SEEDERS", "3"))
# A private-tracker release ranks this many tiers lower: a good public 1080p beats a private 4K
# (public first -- private picks seed for days on the 230 GB seedbox, 336h on unknown trackers),
# but a private 4K still beats a low-bitrate or tiny public file. 0 = tier first, private as a tiebreak.
PRIVATE_PENALTY = int(_env("PRIVATE_PENALTY", "2"))
UPGRADE_DAYS = float(_env("UPGRADE_DAYS", "30"))
TELUGU_DAYS = float(_env("TELUGU_DAYS", "30"))
RIP_WAIT_DAYS = float(_env("RIP_WAIT_DAYS", "14"))
RIP_SEARCH_EVERY = int(_env("RIP_SEARCH_EVERY", "21600"))
FOLLOW_SEARCH_EVERY = int(_env("FOLLOW_SEARCH_EVERY", "43200"))   # 4K upgrade + Telugu searches
PRIVATE_SEARCH_EVERY = int(_env("PRIVATE_SEARCH_EVERY", "86400"))
SEARCHES_PER_PASS = int(_env("SEARCHES_PER_PASS", "6"))
TMDB_EVERY = int(_env("TMDB_EVERY", "86400"))
TMDB_MIN_VOTES = int(_env("TMDB_MIN_VOTES", "20"))
SRRDB_EVERY = int(_env("SRRDB_EVERY", "3600"))                    # 0 = off
WTS_EVERY = int(_env("WTS_EVERY", "3600"))                        # 0 = off
WTS_FEED = _env("WTS_FEED", "https://whentostream.com/feed/")
FEED_KEEP_DAYS = float(_env("FEED_KEEP_DAYS", "120"))
UA = {"User-Agent": "media-bridge/1.0"}
TMDB = "https://api.themoviedb.org/3"
SRRDB = "https://api.srrdb.com/v1/search"

E.GOOD_TIER = -1  # Hollywood: quality tier first (4K first), public before private only within a tier

# ----------------------------------------------------------------------------- release parsing
# TRaSH-Guides release-group tiers (WEB tier 01/02, UHD/remux groups) and its low-quality list.
TIER1 = {g.lower() for g in (
    "ABBIE AJP69 APEX BLUTONiUM BYNDR CMRG CRFW CRUD FLUX GNOME HONE KiNGS Kitsune NOSiViD NTb NTG "
    "RAWR SiC TEPES TheFarm FraMeSToR CiNEPHiLES BiTOR PmP EPSiLON KRaLiMaRKo BLURANiUM WiLDCAT ZQ "
    "DON W4NK3R CtrlHD HQMUX").split()}
TIER2 = {g.lower() for g in "dB Flights MiU monkee MZABI PHOENiX playWEB SbR SMURF TOMMY XEBEC".split()}
LQ = {g.lower() for g in "YTS YIFY GalaxyRG PSA Pahe MeGusta ShAaNiG MkvCage EVO aXXo Ganool TGx".split()}

YEAR_CUT = re.compile(r"[\s.\[(_-](2160p|1080p|720p|576p|480p|4k|uhd|web[-.]?dl|web[-.]?rip|web|blu-?ray|bdrip|"
                      r"brrip|remux|hdrip|hdtv|x26[45]|h\.?26[45]|hevc|complete|proper|repack|imax|extended|"
                      r"unrated|hybrid)\b", re.I)
DISC_EN = re.compile(r"\b(complete[ .-]?(uhd[ .-]?)?blu-?ray|bdmv|br-?disk|bd-?(25|50|66|100)|iso)\b", re.I)
DUB_EN = re.compile(r"\b(dual[ .-]?lat(ino)?|latino|cinecalidad|ita|italian|mvo|dvo|avo|rus|russian|ukr|french|"
                    r"vff|vfq|vf2|vfi|truefrench|castellano|spanish|esp|german|deutsch|multi|dual|dublado|"
                    r"nordic|swedish|danish|dutch|polish|pol|turkish|portuguese|korean|japanese|chinese)\b", re.I)
EPISODE = re.compile(r"\bS\d{1,2}[ .]?E\d{1,3}\b|\bseason[ .]\d", re.I)
THREE_D = re.compile(r"\b3d\b|\bh-?sbs\b|\bh-?ou\b", re.I)
UPSCALE = re.compile(r"\b(upscaled?|ai[ .-]?upscale[d]?|ai|sdr[ .-]?to[ .-]?hdr|regrade[d]?|\d{2,3}\s?fps)\b", re.I)
SERVICE = re.compile(r"\b(amzn|atvp|dsnp|nf|hmax|max|pcok|pmtp|hulu|it|ma|stan|crav)\b(?=[ .-]+(web|dl))", re.I)


def parse_title_en(title):
    """Release name -> (title, year). 'Title (2026) ...' and scene 'Title.2026.1080p.WEB-DL...'."""
    t = html.unescape(title or "").replace("\xa0", " ")
    name, yr = E.parse_title(t)
    if name:
        return _strip_year(name, yr), yr
    m = YEAR_CUT.search(t)
    head = t[:m.start()] if m else t
    years = list(re.finditer(r"(?:^|[\s.(\[_-])((?:19|20)\d{2})(?=$|[\s.)\]_-])", head))
    if not years:
        return None, None
    y = years[-1]
    name = re.sub(r"[._]", " ", head[:y.start(1)]).strip(" -([")
    return (_strip_year(name, int(y.group(1))), int(y.group(1))) if name else (None, None)


def _strip_year(name, yr):
    """'Resident Evil 2026 (2026)' -> 'Resident Evil' (some uploaders repeat the year)."""
    s = re.sub(rf"[\s.(]+{yr}\)?$", "", name).strip()
    return s or name


def _res_en(t):
    """An explicit 2160p/1080p/... wins over a 'UHD'/'4K' tag ('UHD WEBRip 1080p' is 1080p)."""
    m = re.search(r"\b(2160|1080|720|576|480)p\b", t, re.I)
    if m:
        return {2160: 2160, 1080: 1080, 720: 720}.get(int(m.group(1)), 480)
    return 2160 if re.search(r"\b(4k|uhd)\b", t, re.I) else 480


def _source_en(tl, remux, disc):
    if remux:
        return "REMUX", 6
    if disc:
        return "DISC", 6
    if re.search(r"\bweb[-. ]?rip\b", tl):
        return "WEBRip", 3
    if re.search(r"\bweb[-. ]?dl\b|\bweb\b|\b(amzn|atvp|dsnp|nf|hmax|pcok|pmtp)\b", tl):
        return "WEB-DL", 5
    if re.search(r"\bblu-?ray\b|\bbdrip\b|\bbr-?rip\b|\bbd-?rip\b", tl):
        return "BluRay", 4
    if re.search(r"\bhd-?rip\b", tl):
        return "HDRip", 2
    if re.search(r"\bhdtv\b", tl):
        return "HDTV", 1
    return "unknown", 0


def _group(t):
    s = re.sub(r"\.(mkv|mp4|avi)\s*$", "", t.strip(), flags=re.I)
    s = re.sub(r"(\s*\[[^\]]*\])+\s*$", "", s)
    m = re.search(r"[-\s]([A-Za-z0-9]{2,16})\s*$", s)
    return m.group(1) if m else ""


def parse_release_en(title, size, language=None, seeders=None):
    """Classify one release for the Hollywood ladder; 'reject' is None for usable ones. Same base
    keys as fresh.parse_release (the engine reads title/size/source/res/langs/reject/btih...)."""
    t = html.unescape(title or "").replace("\xa0", " ")
    tl = t.lower()
    langs, guessed = E.audio_langs(t, language)
    remux = bool(re.search(r"\bremux\b", tl))
    disc = bool(DISC_EN.search(t)) and not remux
    src, src_rank = _source_en(tl, remux, disc)
    grp = _group(t)
    lq = grp.lower() in LQ or "yts" in tl or "yify" in tl
    dv = bool(re.search(r"\b(dv|dovi|dolby[ .-]?vision)\b", tl))
    hdr10p = bool(re.search(r"hdr10(\+|p\b|plus)", tl))
    hdr = bool(re.search(r"\bhdr(10)?\b", tl)) or hdr10p
    rel = {"title": t.strip(), "size": size or 0, "source": src, "src_rank": src_rank,
           "res": _res_en(t), "langs": sorted(langs), "langs_guessed": guessed,
           "hc": bool(E.HC.search(t)), "hevc": bool(re.search(r"\b(hevc|x265|h\.?265)\b", t, re.I)),
           "bracket": bool(re.search(r"\[[^\]]*\+[^\]]*\]", t)), "reject": None,
           "remux": remux, "disc": disc, "group": grp,
           "grp_rank": 3 if lq else 0 if grp.lower() in TIER1 else 1 if grp.lower() in TIER2 else 2,
           "hdr_rank": 0 if dv and hdr else 1 if hdr10p else 2 if hdr else 4 if dv else 3,
           "service": bool(SERVICE.search(t)),
           "audio": 0 if re.search(r"atmos|truehd|dts[-. ]?(x|hd)", tl) else
                    1 if re.search(r"ddp|dd\+|eac3|\bdd[ .]?5|ac3|\bdts\b|5[ .]1|7[ .]1|6ch", tl) else 2,
           "proper": bool(re.search(r"\b(proper|repack\d?|rerip)\b", tl)), "seeders": seeders}
    name, _ = parse_title_en(t)
    tail = t[len(name):] if name and t.startswith(name) else t
    other = set(langs) - {"eng"}
    if E.THEATRE.search(t) or "hq clean audio" in tl:
        rel["reject"] = "theatre print"
    elif src == "HDRip" or (re.search(r"\bhq\b", tl) and src_rank < 4):  # 'HQ Clean' on a BluRay is wording
        rel["reject"] = "pre-OTT print (HDRip/HQ)"
    elif EPISODE.search(t):
        rel["reject"] = "TV episode"
    elif THREE_D.search(t):
        rel["reject"] = "3D"
    elif UPSCALE.search(t):
        rel["reject"] = "upscale"
    elif E.PACK.search(t):
        rel["reject"] = "multi-resolution pack"
    elif "tel" not in langs and (other or DUB_EN.search(tail)):
        rel["reject"] = "non-English dub"
    elif rel["hc"]:
        rel["reject"] = "hardcoded subtitles"
    elif src_rank == 0:
        rel["reject"] = "unknown source"
    elif size and ((rel["res"] >= 1080 and size < 1e9) or (rel["res"] == 2160 and size < 3e9)):
        rel["reject"] = "too small for its resolution"
    elif seeders is not None and seeders < MIN_SEEDERS:
        rel["reject"] = "too few seeders"
    return rel


# Tiers (lower = better). 9 = never. A+ (0) = 4K REMUX/full disc, big films only; A (1) = good 4K;
# B (2) = 1080p 2-12 GB; C (3) = low-bitrate/LQ 4K; then small 1080p, oversize 1080p, 720p.
NEVER = 9


def tier_en(rel, big=False):
    s, r = rel["size"], rel["res"]
    if rel.get("remux") or rel.get("disc"):
        return 0 if big and r == 2160 and s <= REMUX_CAP else NEVER
    if r == 2160:
        if rel.get("grp_rank", 2) < 3 and 8e9 <= s <= CAP_4K:
            return 1
        return 3 if s <= CAP_4K else 7
    if r == 1080:
        return 2 if 2e9 <= s <= HD_CAP else 4 if s < 2e9 else 5
    if r == 720:
        return 6
    return NEVER


def rank_key_en(rel, big=False):
    t = tier_en(rel, big)
    size_pref = rel["size"] if t in (5, 7) else -rel["size"]  # oversize tiers: smallest; else biggest
    eff = t + PRIVATE_PENALTY if rel.get("private") and t < NEVER else t
    return (eff, bool(rel.get("private")), rel.get("grp_rank", 2), rel.get("hdr_rank", 3),
            -rel.get("src_rank", 0), not rel.get("service"), rel.get("audio", 2), not rel.get("proper"),
            size_pref, -(rel.get("seeders") or 0))


def _label(rel):
    bits = [f"{rel['res']}p" if rel["res"] != 2160 else "4K", rel["source"]]
    if rel.get("res") == 2160 and rel.get("hdr_rank", 3) < 3:
        bits.append("HDR")
    return " ".join(bits) + f" {rel['size'] / 1e9:.1f}GB"


def plan_for_en(releases, m):
    """English main file (bare name) + a Telugu file whenever one exists. Never waits."""
    big = bool(m["big"])
    usable = [r for r in releases if tier_en(r, big) < NEVER]
    eng = [r for r in usable if not r["bracket"] and "tel" not in r["langs"]
           and (not r["langs"] or "eng" in r["langs"])]
    tel = [r for r in usable if "tel" in r["langs"]]
    if not eng:
        return {"status": "no_release", "versions": [],
                "why": "no English release yet" + (" (Telugu only)" if tel else "")}
    prim = min(eng, key=lambda r: rank_key_en(r, big))
    out = [{"role": "primary", "lang": "eng", "rel": prim}]
    why = f"English {_label(prim)}"
    if tel:
        t = min(tel, key=lambda r: rank_key_en(r, big))
        out.append({"role": "secondary", "lang": "tel", "rel": t})
        why += f" + Telugu {_label(t)}"
    if tier_en(prim, big) > 1:
        why += " (4K later)"
    return {"status": "ok", "versions": out, "why": why}


def stale_reason_en(best, rels, now):
    """New = out on digital (else on disc) within WINDOW_DAYS; with no TMDB date, the first usable
    rip is. New 4K re-uploads and disc releases keep old films' latest rip recent: Frankenstein
    (digital Nov 2025) and KPop Demon Hunters showed up as candidates in the 2026-11-16 dry run."""
    cutoff = now - E.WINDOW_DAYS * 86400
    d = (best or {}).get("digital") or (best or {}).get("physical")
    if d:
        ts = _ts(d + "T00:00:00")
        return f"not new: out on digital/disc {d}" if ts and ts < cutoff else None
    first = min((r["added"] or 0 for r in rels if not r["reject"]), default=0)
    return f"not new: first rip {E._day(first)}" if first and first < cutoff else None


def foreign_reason_en(info, orig, rels):
    if not orig:
        return "no TMDB match -- can't confirm an English-language film"
    if orig != "eng":
        return f"not an English-language film ({E.LANGS.get(orig, orig)})"
    return None


# ----------------------------------------------------------------------------- own tables
_tables_ready = False


def _tables():
    global _tables_ready
    if _tables_ready:
        return
    with E._lock:
        E._db().executescript("""
            CREATE TABLE IF NOT EXISTS feed(
                guid TEXT PRIMARY KEY, title TEXT, size INTEGER, seeders INTEGER, indexer TEXT,
                private INTEGER DEFAULT 0, magnet TEXT, url TEXT, infohash TEXT, published REAL,
                first_seen REAL, via TEXT);
            CREATE INDEX IF NOT EXISTS feed_pub ON feed(published);
            CREATE TABLE IF NOT EXISTS calendar(
                tmdb INTEGER PRIMARY KEY, title TEXT, year INTEGER, date TEXT, source TEXT, added REAL,
                last_search REAL DEFAULT 0, last_private REAL DEFAULT 0, searches INTEGER DEFAULT 0,
                state TEXT DEFAULT 'waiting', alts TEXT);
            CREATE TABLE IF NOT EXISTS searches(k TEXT PRIMARY KEY, at REAL);
        """)
    _tables_ready = True


def due(key, every, now=None):
    """True (and marks it) when `key` last ran more than `every` seconds ago."""
    _tables()
    now = now or time.time()
    r = E.q("SELECT at FROM searches WHERE k=?", (key,))
    if r and now - r[0]["at"] < every:
        return False
    E.x("INSERT OR REPLACE INTO searches(k, at) VALUES (?, ?)", (key, now))
    return True


# ----------------------------------------------------------------------------- Prowlarr
_PRIVATE_IDS = {"at": 0, "ids": []}


def private_indexers():
    """Enabled private Prowlarr indexers (the local movierulz/tamilmv feeds excluded), cached 6h."""
    if time.time() - _PRIVATE_IDS["at"] < 6 * 3600:
        return _PRIVATE_IDS["ids"]
    r = requests.get(f"{E.PROWLARR_URL}/api/v1/indexer", headers={"X-Api-Key": E.PROWLARR_API_KEY}, timeout=60)
    r.raise_for_status()
    _PRIVATE_IDS.update(at=time.time(), ids=[i["id"] for i in r.json() if i.get("enable") and i.get("privacy") == "private"
                                             and i.get("name") not in ("movierulz", "tamilmv")])
    return _PRIVATE_IDS["ids"]


def _ts(iso):
    try:
        return datetime.datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def search_prowlarr(query, private=False, via="search"):
    """Search Prowlarr (public RSS indexers, or the private ones) and store every movie result in
    `feed`. Returns the raw rows. An empty query = each indexer's latest (RSS)."""
    if not (E.PROWLARR_URL and E.PROWLARR_API_KEY):
        return []
    _tables()
    ids = private_indexers() if private else RSS_INDEXERS
    if not ids:
        return []
    r = requests.get(f"{E.PROWLARR_URL}/api/v1/search", headers={"X-Api-Key": E.PROWLARR_API_KEY}, timeout=240,
                     params={"query": query, "type": "search", "categories": [2000], "limit": 100, "indexerIds": ids})
    r.raise_for_status()
    rows = r.json()
    now, new = time.time(), 0
    for it in rows:
        t = it.get("title") or ""
        guid = (it.get("infoHash") or it.get("guid") or t).lower()
        priv = private or E.is_private_hit(it)
        cur = E.q("SELECT guid FROM feed WHERE guid=?", (guid,))
        if cur:
            E.x("UPDATE feed SET seeders=? WHERE guid=?", (it.get("seeders"), guid))
            continue
        E.x("INSERT INTO feed(guid, title, size, seeders, indexer, private, magnet, url, infohash, published, "
            "first_seen, via) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (guid, t, it.get("size") or 0, it.get("seeders"), it.get("indexer"), 1 if priv else 0,
             it.get("magnetUrl"), it.get("downloadUrl"), (it.get("infoHash") or "").lower() or None,
             _ts(it.get("publishDate")), now, via))
        new += 1
    if new and via != "rss":
        E.meta_set("built_at", 0)  # a targeted search found something: rebuild on this pass
    return rows


def search_film(title, year, private=False):
    """Targeted search; returns how many results are this film (same title + year)."""
    rows = search_prowlarr(f"{title} {year}", private=private)
    key = f"{norm(title)}{year}"
    hits = 0
    for it in rows:
        n, y = parse_title_en(it.get("title") or "")
        if n and f"{norm(n)}{y}" == key:
            hits += 1
    return hits


def harvest():
    rows = search_prowlarr("", via="rss")
    log(f"rss: {len(rows)} release(s) from {len(RSS_INDEXERS)} public indexer(s)")


# ----------------------------------------------------------------------------- candidate source
_scraper_releases = E.scraped_releases  # the Indian DB reader (parses with parse_release_en below)


def scraped_releases_en(since):
    """{mkey: [releases]} from the feed table, plus Telugu packs of the same films from the
    movierulz/tamilmv DBs (only for films the feed already has -- Indian sites never start one)."""
    _tables()
    out = {}
    for r in E.q("SELECT * FROM feed WHERE COALESCE(published, first_seen) > ?", (since,)):
        name, yr = parse_title_en(r["title"])
        if not name:
            continue
        rel = parse_release_en(r["title"], r["size"], None, r["seeders"])
        rel.update(btih=r["infohash"] or r["guid"], magnet=r["magnet"], url=r["url"], private=bool(r["private"]),
                   added=r["published"] or r["first_seen"], site=r["indexer"], post=None, year=yr, name=name)
        out.setdefault(f"{norm(name)}{yr}", []).append(rel)
    try:
        for mkey, rels in _scraper_releases(since).items():
            tel = [r for r in rels if "tel" in r["langs"]]
            if tel and mkey in out:
                out[mkey].extend(tel)
    except Exception as e:
        log(f"  reading the Telugu sources failed: {e}")
    return out


# ----------------------------------------------------------------------------- calendars / watchers
def cal_add(tmdb, title, year, date, source):
    _tables()
    if not tmdb or not title or not year:
        return False
    if year < datetime.date.today().year - 1:
        return False  # a re-release / new disc of an old film, not a new one
    if E.q("SELECT 1 FROM calendar WHERE tmdb=?", (tmdb,)) or E.q("SELECT 1 FROM movies WHERE tmdb=?", (tmdb,)):
        return False
    E.x("INSERT INTO calendar(tmdb, title, year, date, source, added) VALUES (?,?,?,?,?,?)",
        (tmdb, title, year, date, source, time.time()))
    log(f"calendar: {title} ({year}) from {source}" + (f", out {date}" if date else ""))
    return True


def _english(m):
    return ((m.get("originalLanguage") or {}).get("name") or "") == "English"


def tmdb_token():
    """(headers, params) for TMDB: the v4 read token as a Bearer header (keeps the key out of URLs
    and logs), else the v3 key. Both live in Bitwarden (vault.py), never in .env."""
    try:
        import vault
        try:
            return {"Authorization": f"Bearer {vault.get('TMDB_READ_API_KEY')}"}, {}
        except vault.VaultError:
            return {}, {"api_key": vault.get("TMDB_API_KEY")}
    except Exception as e:
        log(f"tmdb: no key ({e}); feed-only")
        return None, None


def tmdb_discover(now=None):
    """English films with a US digital release in the window -> calendar. (Digital only: type 5 =
    disc pulled in old films getting a new disc edition -- ThanksKilling 2009, 2026-11-16.)"""
    hdr, params = tmdb_token()
    if hdr is None:
        return 0
    today = datetime.date.fromtimestamp(now or time.time())
    added = 0
    for page in (1, 2, 3):
        p = {"region": "US", "with_release_type": "4", "with_original_language": "en",
             "release_date.gte": (today - datetime.timedelta(days=E.WINDOW_DAYS)).isoformat(),
             "release_date.lte": today.isoformat(), "vote_count.gte": TMDB_MIN_VOTES,
             "sort_by": "popularity.desc", "page": page, **params}
        r = requests.get(f"{TMDB}/discover/movie", headers={**UA, **hdr}, params=p, timeout=30)
        r.raise_for_status()
        d = r.json()
        for it in d.get("results", []):
            yr = int((it.get("release_date") or "0")[:4] or 0)
            added += cal_add(it.get("id"), it.get("title"), yr, today.isoformat(), "tmdb")
        if page >= (d.get("total_pages") or 1):
            break
    log(f"tmdb: {added} new calendar film(s)")
    return added


def srrdb_watch(now=None):
    """Scene pres (srrDB: every release carries its IMDb id + an isForeign flag) -> calendar."""
    year_now = datetime.date.fromtimestamp(now or time.time()).year
    last = E.meta_get("srrdb_last", "")
    newest, looked, added = last, 0, 0
    seen = set()
    for cat in ("x264", "x265"):
        r = requests.get(f"{SRRDB}/category:{cat}/order:date-desc", headers=UA, timeout=30)
        r.raise_for_status()
        for it in r.json().get("results", []):
            d = it.get("date") or ""
            if d <= last:
                break
            newest = max(newest, d)
            if it.get("isForeign") == "yes" or not it.get("imdbId") or it["imdbId"] in seen:
                continue
            name, yr = parse_title_en(it.get("release") or "")
            rel = parse_release_en(it.get("release") or "", it.get("size") or 0)
            if not name or yr not in (year_now, year_now - 1) or rel["reject"] or rel["res"] < 1080:
                continue
            seen.add(it["imdbId"])
            if looked >= 20:
                continue
            looked += 1
            try:
                m = E.radarr("GET", "movie/lookup/imdb", params={"imdbId": f"tt{it['imdbId']}"}) or {}
            except Exception:
                continue
            if _english(m) and m.get("year") in (year_now, year_now - 1):
                added += cal_add(m.get("tmdbId"), m.get("title"), m.get("year"),
                                 datetime.date.fromtimestamp(now or time.time()).isoformat(), "srrdb")
    E.meta_set("srrdb_last", newest)
    return added


MONTHS = "January February March April May June July August September October November December".split()
DATE_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?")


def headline_title(h):
    """whentostream headlines put the film in capitals: "James McAvoy's CALIFORNIA SCHEMIN' Sets
    Digital Streaming Release Date" -> "California Schemin'"."""
    runs, cur = [], []
    for w in h.split():
        letters = [c for c in w if c.isalpha()]
        if letters and not any(c.islower() for c in letters) or (cur and re.fullmatch(r"[0-9&:+\-]+", w)):
            cur.append(w)
        else:
            runs.append(cur)
            cur = []
    runs.append(cur)
    runs = [" ".join(r).strip(" ,") for r in runs if sum(c.isalpha() for c in "".join(r)) >= 2]
    if not runs:
        return None
    t = max(runs, key=len).title()
    return re.sub(r"(['’])S\b", r"\1s", t)


def headline_date(text, now):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    today = datetime.date.fromtimestamp(now)
    y = int(m.group(3)) if m.group(3) else today.year
    try:
        d = datetime.date(y, MONTHS.index(m.group(1)) + 1, int(m.group(2)))
    except ValueError:
        return None
    if not m.group(3) and d < today - datetime.timedelta(days=60):
        d = d.replace(year=y + 1)  # "January 6" written in November
    return d.isoformat()


def whentostream_watch(now=None):
    """whentostream.com RSS: 'X Sets Digital Streaming Release Date' / 'Streaming Premiere Date'
    headlines -> calendar (title from the headline, date from the post, identity from TMDB)."""
    now = now or time.time()
    r = requests.get(WTS_FEED, headers=UA, timeout=30)
    r.raise_for_status()
    seen = set(json.loads(E.meta_get("wts_seen", "[]")))
    added = 0
    for item in ET.fromstring(r.content).iter("item"):
        link = (item.findtext("link") or "").strip()
        head = html.unescape(item.findtext("title") or "")
        if not link or link in seen:
            continue
        seen.add(link)
        if not re.search(r"streaming (release|premiere) date|digital (release|streaming)", head, re.I):
            continue
        name = headline_title(head)
        if not name:
            continue
        body = html.unescape(re.sub(r"<[^>]+>", " ", " ".join(
            (el.text or "") for el in item if el.tag.endswith(("description", "encoded")))))
        best, _ = E.lookup(name, datetime.date.fromtimestamp(now).year)
        if best and best.get("orig") == "eng":
            added += cal_add(best.get("tmdb"), best.get("title"), best.get("year"), headline_date(body, now),
                             "whentostream")
    E.meta_set("wts_seen", json.dumps(sorted(seen)[-400:]))
    return added


def calendar_pass(now=None):
    """Films a calendar says are out but with no rip found yet: search every RIP_SEARCH_EVERY for
    RIP_WAIT_DAYS (public; private at most daily and only while public finds nothing)."""
    _tables()
    now = now or time.time()
    today = datetime.date.fromtimestamp(now).isoformat()
    done = 0
    for c in E.q("SELECT * FROM calendar WHERE state='waiting' ORDER BY added"):
        if E.q("SELECT 1 FROM movies WHERE tmdb=?", (c["tmdb"],)):
            E.x("UPDATE calendar SET state='found' WHERE tmdb=?", (c["tmdb"],))
            continue
        start = max(c["added"], _ts(c["date"]) or 0) if c["date"] else c["added"]
        if c["date"] and c["date"] > today:
            continue  # not out yet
        if now - start > RIP_WAIT_DAYS * 86400:
            E.x("UPDATE calendar SET state='lapsed' WHERE tmdb=?", (c["tmdb"],))
            log(f"calendar: no rip of {c['title']} ({c['year']}) within {RIP_WAIT_DAYS:g} days; dropped")
            continue
        if now - (c["last_search"] or 0) < RIP_SEARCH_EVERY or done >= SEARCHES_PER_PASS:
            continue
        done += 1
        try:
            hits = search_film(c["title"], c["year"])
            priv = not hits and now - (c["last_private"] or 0) > PRIVATE_SEARCH_EVERY
            if priv:
                hits = search_film(c["title"], c["year"], private=True)
        except Exception as e:
            log(f"calendar: search for {c['title']} failed: {e}")
            continue
        E.x("UPDATE calendar SET last_search=?, searches=searches+1" + (", last_private=?" if priv else "") +
            (", state='found'" if hits else "") + " WHERE tmdb=?",
            (now, now, c["tmdb"]) if priv else (now, c["tmdb"]))
        if hits:
            log(f"calendar: {c['title']} ({c['year']}) is out -- {hits} release(s)")


def followup_searches(now=None):
    """Placed films still at 1080p: look for the 4K (12h; private daily). Placed films without a
    Telugu file: look for one (12h, public). Each for UPGRADE_DAYS / TELUGU_DAYS after placement."""
    now = now or time.time()
    done = 0
    # Candidates first: the RSS feed often holds one stray release of a film (a YTS 720p, a DUAL
    # dub), so every candidate gets its own search before (and while) it waits for a decision.
    for m in E.q("SELECT * FROM movies WHERE status IN ('new','approved','watchlist') AND tmdb IS NOT NULL "
                 "ORDER BY first_seen DESC"):
        if done >= SEARCHES_PER_PASS:
            return
        if not due(f"c:{m['id']}", FOLLOW_SEARCH_EVERY, now):
            continue
        done += 1
        try:
            search_film(m["title"], m["year"])
            if due(f"cp:{m['id']}", PRIVATE_SEARCH_EVERY, now):
                search_film(m["title"], m["year"], private=True)
        except Exception as e:
            log(f"  search for candidate {m['title']} failed: {e}")
    for m in E.q("SELECT * FROM movies WHERE status IN ('done','active') AND expired_at IS NULL "
                 "AND placed_at IS NOT NULL ORDER BY placed_at DESC"):
        if done >= SEARCHES_PER_PASS:
            break
        age = now - m["placed_at"]
        vs = E.q("SELECT * FROM versions WHERE movie_id=?", (m["id"],))
        try:
            if age < UPGRADE_DAYS * 86400 and _needs_upgrade(m, vs) and due(f"4k:{m['id']}", FOLLOW_SEARCH_EVERY, now):
                done += 1
                search_prowlarr(f"{m['title']} {m['year']} 2160p")
                if PRIVATE_PENALTY <= 1 and due(f"4kp:{m['id']}", PRIVATE_SEARCH_EVERY, now):
                    search_prowlarr(f"{m['title']} {m['year']} 2160p", private=True)  # only useful when private 4K can win
            if age < TELUGU_DAYS * 86400 and not any(v["lang"] == "tel" for v in vs) \
                    and due(f"tel:{m['id']}", FOLLOW_SEARCH_EVERY, now):
                done += 1
                search_prowlarr(f"{m['title']} {m['year']} Telugu")
        except Exception as e:
            log(f"  follow-up search for {m['title']} failed: {e}")


def prune(now=None):
    now = now or time.time()
    E.x("DELETE FROM feed WHERE COALESCE(published, first_seen) < ?", (now - FEED_KEEP_DAYS * 86400,))


# ----------------------------------------------------------------------------- 4K upgrade
def _placed_primary(vs):
    return [v for v in vs if v["role"] == "primary" and v["state"] == "placed"]


def _needs_upgrade(m, vs):
    cur = _placed_primary(vs)
    if not cur:
        return False
    return tier_en(parse_release_en(cur[0]["title"], cur[0]["size"]), bool(m["big"])) > 1


_orig_add_missing = E.add_missing_versions
_orig_retry = E.retry_version
_orig_place = E.place


def add_missing_versions_en():
    """The engine's late second file (Telugu) + the 4K upgrade of a placed 1080p main file."""
    _orig_add_missing()
    now = time.time()
    for m in E.q("SELECT * FROM movies WHERE status IN ('done','active') AND expired_at IS NULL "
                 "AND placed_at > ?", (now - UPGRADE_DAYS * 86400,)):
        vs = E.q("SELECT * FROM versions WHERE movie_id=?", (m["id"],))
        if not _needs_upgrade(m, vs):
            continue
        ups = [v for v in vs if v["role"] == "primary_upgrade"]
        if any(v["state"] == "dropped" for v in ups) or len(ups) >= E.MAX_TRIES:
            continue
        plan = E.current_plan(m)  # excludes every release that failed before
        if not plan or plan["status"] != "ok":
            continue
        best = plan["versions"][0]["rel"]
        cur = _placed_primary(vs)[0]
        if tier_en(best, bool(m["big"])) > 1 or best["btih"] == cur["btih"] or not E.disk_ok():
            continue
        try:
            E.queue_version(m, {"role": "primary_upgrade", "lang": "eng", "rel": best})
        except Exception as e:
            log(f"  {m['title']}: queueing the 4K upgrade failed ({e}); next pass")
            continue
        log(f"  {m['title']}: upgrading to {_label(best)}")
        if m["status"] == "done":
            E.set_status(m["id"], "active", "upgrading to 4K")


def retry_version_en(v, why):
    """A failed 4K upgrade is not retried with the next-best release (that could be another
    1080p): it is marked failed + bad, and the upgrade check picks the next 4K by itself."""
    if v["role"] != "primary_upgrade":
        return _orig_retry(v, why)
    bad = set(json.loads(v["bad"] or "[]")) | {v["btih"]}
    E.x("UPDATE versions SET bad=?, tries=tries+1, error=?, state='failed', updated=? WHERE id=?",
        (json.dumps(sorted(bad)), why[:300], time.time(), v["id"]))
    log(f"  4K upgrade '{v['stem']}' failed ({why})")


def place_en(v, dest):
    """Full discs become one .mkv first; a 4K upgrade replaces the main file under the same name
    (Jellyfin keeps the item and its watch state), then the old file and version are retired."""
    try:
        note = prepare_disc(dest)
        if note:
            log(f"  {v['stem']}: {note}")
    except Exception as e:
        log(f"  {v['stem']}: disc remux failed ({e}); trying another release")
        shutil.rmtree(dest, ignore_errors=True) if dest.is_dir() else dest.unlink(missing_ok=True)
        return retry_version_en(v, f"disc remux failed: {e}")
    if v["role"] != "primary_upgrade":
        return _orig_place(v, dest)
    old = _placed_primary(E.q("SELECT * FROM versions WHERE movie_id=?", (v["movie_id"],)))
    _orig_place({**dict(v), "role": "primary"}, dest)
    new_path = E.q("SELECT path FROM versions WHERE id=?", (v["id"],))[0]["path"]
    for o in old:
        op = pathlib.Path(o["path"] or "")
        if o["path"] and o["path"] != new_path and op.exists():
            op.unlink()
        E.x("UPDATE versions SET state='replaced', updated=? WHERE id=?", (time.time(), o["id"]))
    E.x("UPDATE versions SET role='primary' WHERE id=?", (v["id"],))
    log(f"  upgraded the main file -> {new_path}")


# ----------------------------------------------------------------------------- full discs -> .mkv
def _sevenzip():
    return shutil.which("7z") or shutil.which("7zz")


def prepare_disc(dest):
    """A delivered BDMV folder (or .iso) -> one lossless .mkv of the main title (mkvmerge picks up
    the playlist's streams; no re-encode), disc files deleted. Returns a note, or None if no disc."""
    iso = dest if dest.is_file() and dest.suffix.lower() == ".iso" else \
        next((p for p in dest.rglob("*.iso")), None) if dest.is_dir() else None
    bdmv = next((p for p in dest.rglob("BDMV") if p.is_dir()), None) if dest.is_dir() else None
    if not iso and not bdmv:
        return None
    work = dest if dest.is_dir() else dest.with_name(dest.name + ".disc")
    work.mkdir(exist_ok=True)
    tmp = None
    if iso and not bdmv:
        z = _sevenzip()
        if not z:
            raise RuntimeError("no 7z in the image to open the .iso")
        tmp = work / ".iso-extract"
        r = subprocess.run([z, "x", "-y", f"-o{tmp}", str(iso), "BDMV"], capture_output=True, text=True, timeout=7200)
        if r.returncode != 0:
            raise RuntimeError(f"7z: {(r.stderr or r.stdout).strip()[-200:]}")
        bdmv = next((p for p in tmp.rglob("BDMV") if p.is_dir()), None)
        if not bdmv:
            raise RuntimeError("no BDMV folder in the .iso")
    src, size = _main_title(bdmv)
    out = work / (dest.stem + ".mkv")
    r = subprocess.run(["mkvmerge", "-o", str(out), str(src)], capture_output=True, text=True, timeout=14400)
    if r.returncode not in (0, 1) or not out.exists() or out.stat().st_size < 0.5 * size:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"mkvmerge: {(r.stdout or r.stderr).strip()[-200:]}")
    for p in list(work.iterdir()):  # keep only the new .mkv
        if p != out:
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    if dest.is_file():
        dest.unlink()
        work.rename(dest)
    return f"full disc remuxed to one .mkv ({size / 1e9:.1f} GB title)"


def _main_title(bdmv):
    """The main feature: the playlist with the most bytes (mkvmerge -J), else the biggest .m2ts."""
    best, size = None, 0
    for pl in sorted((bdmv / "PLAYLIST").glob("*.mpls")) if (bdmv / "PLAYLIST").is_dir() else []:
        r = subprocess.run(["mkvmerge", "-J", str(pl)], capture_output=True, text=True, timeout=120)
        try:
            props = json.loads(r.stdout).get("container", {}).get("properties", {})
        except ValueError:
            continue
        if (props.get("playlist_size") or 0) > size:
            best, size = pl, props["playlist_size"]
    if best:
        return best, size
    m2ts = sorted((bdmv / "STREAM").glob("*.m2ts"), key=lambda p: p.stat().st_size) if (bdmv / "STREAM").is_dir() else []
    if not m2ts:
        raise RuntimeError("no playlist or stream in the BDMV folder")
    return m2ts[-1], m2ts[-1].stat().st_size


# ----------------------------------------------------------------------------- status page / HTTP
def page_extra_en():
    _tables()
    rows = E.q("SELECT * FROM calendar WHERE state='waiting' ORDER BY date DESC, added DESC LIMIT 100")
    if not rows:
        return ""
    e = html.escape
    tr = "".join(f"<tr><td>{e(r['title'])} ({r['year']})</td><td>{e(r['date'] or '?')}</td><td>{e(r['source'])}</td>"
                 f"<td>{r['searches']}</td></tr>" for r in rows)
    return (f"<h2>Out (or coming) but no rip yet</h2><table><tr><th>movie</th><th>out</th><th>from</th>"
            f"<th>searches</th></tr>{tr}</table>")


def calendar_json():
    _tables()
    return [dict(r) for r in E.q("SELECT * FROM calendar ORDER BY added DESC LIMIT 300")]


def handle(method, path, body=None):
    full = path.split("?")[0].rstrip("/")
    j = "application/json"
    if method == "GET" and full == "/fresh-en/calendar.json":
        return 200, j, json.dumps(calendar_json(), default=str)
    if method == "POST" and full == "/fresh-en/run":
        threading.Thread(target=one_pass, kwargs={"force": True}, daemon=True).start()
        return 202, j, '{"started":true}'
    return E.handle(method, path, body)


# ----------------------------------------------------------------------------- hooks + loop
E.scraped_releases = scraped_releases_en
E.parse_release = parse_release_en
E.foreign_reason = foreign_reason_en
E.orig_allowed = lambda orig: orig == "eng"
E.plan_for = plan_for_en
E.tier = lambda rel, multi=False: tier_en(rel)
E.rank_key = lambda rel, multi=False: rank_key_en(rel)
E.add_missing_versions = add_missing_versions_en
E.retry_version = retry_version_en
E.place = place_en
E.page_extra = page_extra_en
E.display_title = lambda name, best: (best or {}).get("title") or name
E.stale_reason = stale_reason_en

_pass_lock = threading.Lock()


def watchers(now=None):
    now = now or time.time()
    jobs = [("rss", RSS_EVERY, harvest), ("tmdb", TMDB_EVERY, lambda: tmdb_discover(now)),
            ("srrdb", SRRDB_EVERY, lambda: srrdb_watch(now)), ("wts", WTS_EVERY, lambda: whentostream_watch(now)),
            ("prune", 86400, lambda: prune(now))]
    for key, every, fn in jobs:
        if every > 0 and due(key, every, now):
            try:
                fn()
            except Exception as e:
                log(f"{key} failed: {e}")
    for fn in (calendar_pass, followup_searches):
        try:
            fn(now)
        except Exception as e:
            log(f"{fn.__name__} failed: {e!r}")


def one_pass(force=False):
    if not _pass_lock.acquire(blocking=False):
        return
    try:
        watchers()
        E.run_once(force_build=force)
    finally:
        _pass_lock.release()


def run_forever():
    log(f"starting: {E.mode()} mode, {E.WEEKLY}/week (+{E.OVERFLOW} big), keep {E.KEEP_DAYS:g}d"
        f"{' (expiry DRY-RUN)' if E.EXPIRE_DRY_RUN else ''}, collection '{E.COLLECTION}', "
        f"min free {E.MIN_FREE_GB:g} GB")
    while True:
        try:
            one_pass()
        except Exception as e:
            log(f"pass failed: {e!r}")
        time.sleep(E.POLL)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build":
        E.build_candidates()
        print(json.dumps(E.candidates_for_llm(), indent=1, default=str))
    elif cmd == "status":
        print(json.dumps(E.status_json(), indent=1, default=str))
    elif cmd == "watch":
        watchers()
        print(json.dumps(calendar_json(), indent=1, default=str))
    elif cmd == "calendar":
        print(json.dumps(calendar_json(), indent=1, default=str))
    else:
        run_forever()
