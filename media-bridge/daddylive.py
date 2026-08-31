#!/usr/bin/env python3
"""
DaddyLive (DLHD) -> Jellyfin Live TV resolver.

Why this exists: DaddyLive has no usable off-the-shelf integration left -- MediaFusion's
Rust build never ported its live scrapers, and Cabernet's DaddyLive plugin was taken down.
Community M3U feeds publish bare *.newkso.ru URLs that are Cloudflare-walled (403 even with
the right headers). But DaddyLive's OWN player flow resolves to a different, token-authed CDN
(phantemlis.top, segments on Cloudflare R2) that needs NO headers and is reachable server-side.
So we resolve channels exactly the way the site's player does, on demand, and hand Jellyfin a
plain M3U + XMLTV.

Resolution chain (per channel id), all plain server-side HTTP (NOT Cloudflare-walled):
    {DLHD_BASE}/{folder}/stream-{id}.php   (folder fallback: stream,cast,watch,plus,casting,player)
      -> iframe src -> {embed_host}/premiumtv/daddy3.php?id={id}   (embed host ROTATES)
      -> page JS: source: window.atob('<base64>')
      -> decode -> https://...phantemlis.../premium{id}/index.m3u8?md5v1=..&md5v2=..&expires=..
The md5/expires tokens are self-authorizing (no Referer/UA), minted fresh per fetch (~74min TTL),
so /live/{id} resolves on demand and 302-redirects -- Jellyfin plays it directly.

Channel metadata + EPG come from the public nightah/daddylive feed (its premium{N} == our id);
we keep its names/logos/tvg-id/group + EPG and just swap the dead newkso URL for our resolver.

Two threads (started from main.py):
  * run_refresher() -- periodically pull the nightah M3U -> channel table (sqlite).
  * serve()         -- stdlib HTTP server: /daddylive.m3u, /epg.xml, /live/{id}.

Network: runs OFF the VPN (default bridge net) -> egresses via the host's home IP, which reaches
both the dlhd player pages and the phantemlis/R2 CDN. No FlareSolverr needed (these are not
Cloudflare-walled, unlike newkso).
"""
import base64
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from common import clog

# ---- config ----
DLHD_BASE = os.environ.get("DLHD_BASE", "").rstrip("/")            # required; set in .env (out of source)
M3U_URL = os.environ.get(
    "DADDYLIVE_M3U_URL",
    "https://raw.githubusercontent.com/nightah/daddylive/main/daddylive-channels.m3u8")
EPG_URL = os.environ.get(
    "DADDYLIVE_EPG_URL",
    "https://raw.githubusercontent.com/nightah/daddylive/main/epgs/daddylive-channels-epg.xml")
# Fallback base only. Both the M3U and the /hls rewrites now derive their base from the
# request's own Host header (Handler._request_base_url()), so a client reaching the box on
# either NIC gets URLs back on that same NIC. This is used only where there is no request
# to read: the precheck thread, and requests that arrive without a Host header.
PUBLIC_URL = os.environ.get("DADDYLIVE_PUBLIC_URL", "http://127.0.0.1:8004").rstrip("/")
PORT = int(os.environ.get("DADDYLIVE_PORT", "8004"))
REFRESH = int(os.environ.get("DADDYLIVE_REFRESH", "3600"))
DB_PATH = os.environ.get("DADDYLIVE_DB", "/config/daddylive.db")

# Favorite channels to keep pre-resolved & validated so they open instantly and never hand
# Jellyfin a dead player. Comma-separated channel ids in DADDYLIVE_FAVORITES (e.g. "54,51,60").
FAVORITES = [int(x) for x in re.findall(r'\d+', os.environ.get("DADDYLIVE_FAVORITES", ""))]
PRECHECK = int(os.environ.get("DADDYLIVE_PRECHECK", "240"))  # re-test favorites every N seconds

# Public player-page folders that all serve stream-{id}.php (per dlhd.st/api.php). Tried in order
# so a single down/blocked folder doesn't kill a channel.
FOLDERS = [f.strip() for f in os.environ.get(
    "DADDYLIVE_FOLDERS", "cast,stream,watch,plus,casting,player").split(",") if f.strip()]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")
HTTP_TIMEOUT = float(os.environ.get("DADDYLIVE_TIMEOUT", "12"))

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

# ---- regexes ----
# nightah feed: #EXTINF:-1 group-title="X" tvg-id="Y" tvg-logo="Z",Name  /  then a premium{N} URL.
ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
PREMIUM_RE = re.compile(r'premium(\d+)', re.I)
IFRAME_RE = re.compile(r'<iframe[^>]*\bsrc=["\']([^"\']+)["\']', re.I)
ATOB_RE = re.compile(r'atob\(\s*["\']([A-Za-z0-9+/=]+)["\']\s*\)')
HLS_URL_RE = re.compile(r'https?://[^\s"\']+\.m3u8(?:\?[^\s"\']*)?', re.I)
WIKI_SCRIPT_RE = re.compile(
    r'<script[^>]*\bsrc=["\'](?P<src>(?:https?:)?//[^"\']*/wiki\.js(?:\?[^"\']*)?)["\']',
    re.I,
)
FID_RE = re.compile(r'\bfid\s*=\s*["\'](?P<fid>[^"\']+)["\']')
JOIN_RETURN_RE = re.compile(
    r'return\s*\(\s*\[(?P<chars>(?:\s*"[^"]*"\s*,?)*)\]\.join\(\s*""\s*\)',
    re.S,
)
CHAR_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
# cdnlivetv/tezgoal "oplayer" players (DaddyLive Player 5) build the m3u8 as
#   source:{src: VAR}   where   VAR = f(a)+f(b)+...   and each a,b,... = "<base64>"
# (f is an atob wrapper). We reassemble VAR by base64-decoding each part in order.
OPLAYER_SRC_RE = re.compile(r'source\s*:\s*\{\s*src\s*:\s*(?P<var>\w+)')
OPLAYER_CALLARG_RE = re.compile(r'\w+\(\s*(\w+)\s*\)')
# lovetier.bz players (DaddyLive Player 6, reached via popcdn.day) embed the playlist as a plain
# JSON literal: streamUrl: "https:\/\/…\/index.m3u8?token=…". Just unescape and take it.
STREAMURL_RE = re.compile(r'streamUrl\s*:\s*"(?P<url>https?:[^"]+\.m3u8[^"]*)"')
# Clappr embed players (DaddyLive Player 2, the `cast` folder → rotating *.pages.dev/embed/<slug>) build
# the URL by string concatenation with no literal .m3u8 in the HTML:
#   var CDN_BASE = 'https://<host>/';  ...  launchPlayer(CDN_BASE + id + '.m3u8')
# where id = ?id= query, else the path segment after /embed/, else the last path segment. We read
# CDN_BASE from the page and the slug from the embed URL, then assemble CDN_BASE + slug + '.m3u8'.
CDNBASE_RE = re.compile(r'''CDN_BASE\s*=\s*['"]([^'"]+)['"]''')


def log(msg):
    clog("daddylive", msg)


def _split_extinf(line):
    """
    Split an EXTINF line into attribute text and display name.

    The upstream feed can contain commas inside quoted attribute values
    (for example tvg-id="FOX.(KTTV).Los.Angeles,.CA.us"), so a plain
    regex split on the first comma is incorrect.
    """
    if not line.startswith("#EXTINF:"):
        return None, None
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            return line[:i], line[i + 1 :]
    return None, None


# ---- sqlite ----
DB_LOCK = threading.Lock()
_conn = None


def db():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id          INTEGER PRIMARY KEY,
                name        TEXT,
                logo        TEXT,
                tvg_id      TEXT,
                group_title TEXT,
                updated_at  REAL
            );
            """
        )
        _conn.commit()
    return _conn


def upsert_channel(rec):
    with DB_LOCK:
        db().execute(
            """INSERT INTO channels (id, name, logo, tvg_id, group_title, updated_at)
               VALUES (:id,:name,:logo,:tvg_id,:group_title,:updated_at)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, logo=excluded.logo, tvg_id=excluded.tvg_id,
                 group_title=excluded.group_title, updated_at=excluded.updated_at""",
            rec)
        db().commit()


def all_channels():
    with DB_LOCK:
        return db().execute(
            "SELECT * FROM channels ORDER BY group_title, name").fetchall()


# ---- refresher: nightah M3U -> channel table ----
def parse_feed(text):
    """Yield channel records {id,name,logo,tvg_id,group_title} from a nightah-style M3U."""
    pending = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            attrs_part, name_part = _split_extinf(line)
            if not attrs_part:
                pending = None
                continue
            attrs = dict(ATTR_RE.findall(attrs_part))
            pending = {
                "name": (name_part or "").strip(),
                "logo": attrs.get("tvg-logo", ""),
                "tvg_id": attrs.get("tvg-id", ""),
                "group_title": attrs.get("group-title", ""),
            }
        elif line and not line.startswith("#"):
            if pending is not None:
                pm = PREMIUM_RE.search(line)
                if pm:
                    pending["id"] = int(pm.group(1))
                    pending["updated_at"] = time.time()
                    yield pending
            pending = None
        # ignore #EXTVLCOPT / #EXTM3U / blanks


def refresh_once():
    r = SESSION.get(M3U_URL, timeout=30)
    r.raise_for_status()
    n = 0
    for rec in parse_feed(r.text):
        upsert_channel(rec)
        n += 1
    log(f"refreshed channel list: {n} channels from feed")


def run_refresher():
    db()  # init schema
    if not DLHD_BASE:
        log("DLHD_BASE is not set -- resolver disabled (set it in .env)")
        return
    log(f"refresher starting; channel list every {REFRESH}s from {M3U_URL}")
    while True:
        try:
            refresh_once()
        except Exception as e:
            log(f"refresh error: {e}")
        time.sleep(REFRESH)


# ---- resolver: channel id -> fresh playable token URL ----
def _extract_embed(html, base):
    """Pick the iframe that points at the daddy3 player embed; absolutize it."""
    for src in IFRAME_RE.findall(html):
        if re.search(r'(daddy|premiumtv|\.php\?id=|/embed/)', src, re.I):
            return urllib.parse.urljoin(base, src)
    # fall back to the first iframe if none matched the heuristic
    m = IFRAME_RE.search(html)
    return urllib.parse.urljoin(base, m.group(1)) if m else None


def _request_headers(referer=None):
    headers = {}
    if referer:
        headers["Referer"] = referer
        parts = urllib.parse.urlsplit(referer)
        if parts.scheme and parts.netloc:
            headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
    return headers


def _drop_referer(headers):
    """Strip Referer/Origin — segment CDNs are self-authorizing and some reject a Referer."""
    if not headers:
        return headers
    return {k: v for k, v in headers.items() if k not in ("Referer", "Origin")}


def _normalize_candidate_url(candidate, base_url):
    candidate = (candidate or "").strip()
    if not candidate:
        return None
    if candidate.startswith(("http://", "https://", "//", "/", "./", "../")):
        return urllib.parse.urljoin(base_url, candidate)
    return None


def _extract_oplayer_source(html):
    """Reassemble the m3u8 for a cdnlivetv/tezgoal oplayer page (DaddyLive Player 5), whose
    source is built by base64-decoding and concatenating a list of scrambled vars."""
    m = OPLAYER_SRC_RE.search(html)
    if not m:
        return None
    assign = re.search(rf'\b{m.group("var")}\s*=\s*([^;]+);', html)
    if not assign:
        return None
    parts = OPLAYER_CALLARG_RE.findall(assign.group(1))
    if not parts:
        return None
    out = []
    for name in parts:
        vm = re.search(rf'\b{name}\s*=\s*["\']([A-Za-z0-9+/=]+)["\']', html)
        if not vm:
            return None
        try:
            b = vm.group(1)
            out.append(base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "replace"))
        except Exception:
            return None
    url = "".join(out)
    return url if url.startswith("http") and ".m3u8" in url.split("?", 1)[0].lower() else None


def _extract_source(html, base_url):
    """Extract the first playable source URL from a player page."""
    for b64 in ATOB_RE.findall(html):
        try:
            dec = base64.b64decode(b64).decode("utf-8", "replace")
        except Exception:
            continue
        url = _normalize_candidate_url(dec, base_url)
        if url:
            return url
    for m in JOIN_RETURN_RE.finditer(html):
        try:
            chars = "".join(json.loads(f'"{c}"') for c in CHAR_RE.findall(m.group("chars")))
        except Exception:
            continue
        url = _normalize_candidate_url(chars, base_url)
        if url:
            return url
    oplayer = _extract_oplayer_source(html)
    if oplayer:
        return oplayer
    m = STREAMURL_RE.search(html)
    if m:
        return m.group("url").replace("\\/", "/")
    m = CDNBASE_RE.search(html)
    if m:
        slug = _embed_slug(base_url)
        if slug:
            return _normalize_candidate_url(m.group(1).rstrip("/") + "/" + slug + ".m3u8", base_url)
    m = HLS_URL_RE.search(html)
    if m:
        return m.group(0)
    return None


def _embed_slug(embed_url):
    """The stream slug a Clappr embed (Player 2) uses to build its m3u8, mirroring the page's
    getStreamId(): ?id= query, else the path segment after /embed/, else the last path segment."""
    parts = urllib.parse.urlsplit(embed_url)
    qid = urllib.parse.parse_qs(parts.query).get("id")
    if qid and qid[0]:
        return qid[0]
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        return None
    if "embed" in segs:
        i = segs.index("embed")
        if i + 1 < len(segs):
            return segs[i + 1]
    return segs[-1]


def _extract_wiki_embed(html, base_url):
    """Follow the wikisport -> wiki.php indirection used by some alternate players."""
    m = FID_RE.search(html)
    s = WIKI_SCRIPT_RE.search(html)
    if not (m and s):
        return None
    script_url = urllib.parse.urljoin(base_url, s.group("src"))
    fid = urllib.parse.quote(m.group("fid"))
    return urllib.parse.urljoin(script_url, f"wiki.php?player=desktop&live={fid}")


# Short-lived resolution cache: Jellyfin opens a channel several times in a row (probe,
# transcode, auto-retry after the first slow open times out -> "player error, then plays").
# Caching the resolved master URL for a few seconds collapses those into one live resolution,
# so the retry is instant. TTL stays well under the token life (~74min) and the R2 segment
# URLs (~120s) are refreshed on every /hls playlist reload anyway.
_RESOLVE_TTL = int(os.environ.get("DADDYLIVE_RESOLVE_TTL", "60"))
_resolve_cache = {}   # id -> (resolved dict, ts)
_folder_hint = {}     # id -> folder that resolved last time (tried first next time)
_cache_lock = threading.Lock()

# Favorites are kept warm by run_precheck(): a warm entry is a source that was validated within
# the last few precheck cycles, so /live/<fav> serves instantly from a known-live player.
_WARM_TTL = max(PRECHECK * 3, 300)
_warm = {}            # id -> (resolved dict, ts); maintained by run_precheck()


def _validate_source(url, headers):
    """Confirm a candidate player is actually live: descend master -> variant -> media playlist
    and require that a real media segment returns bytes. A source whose playlists load but whose
    segments are dead (a common DaddyLive failure) is rejected so we never hand Jellyfin a dead
    player and fall through to the next one instead."""
    def get_text(candidate):
        with SESSION.get(candidate, headers=headers or None, timeout=HTTP_TIMEOUT) as r:
            r.raise_for_status()
            return r.text, (r.headers.get("Content-Type") or "").lower()

    def has_bytes(candidate):
        # Segments live on self-authorizing CDNs; some (Player 2's picsart) 403 if a Referer is sent,
        # even though the playlist above them requires one -> fetch segments without the Referer.
        with SESSION.get(candidate, headers=_drop_referer(headers) or None,
                         stream=True, timeout=HTTP_TIMEOUT) as r:
            r.raise_for_status()
            return bool(next(r.iter_content(chunk_size=512), b""))

    def first_uri(body, base):
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return urllib.parse.urljoin(base, s)
        return None

    def is_playlist(body, ctype):
        return "mpegurl" in ctype or "#EXTM3U" in body[:512]

    try:
        body, ctype = get_text(url)
        if not is_playlist(body, ctype):
            return False
        base = url
        # Walk down nested playlists (master -> variant -> media); the first non-.m3u8 URI is a
        # real segment, which must return bytes. Cap the depth so a self-referential playlist can't
        # loop forever.
        for _ in range(3):
            nxt = first_uri(body, base)
            if not nxt:
                return True  # playlist present but no segments yet (live edge) -- accept
            if ".m3u8" in urllib.parse.urlsplit(nxt).path.lower():
                body, ctype = get_text(nxt)
                if not is_playlist(body, ctype):
                    return False
                base = nxt
                continue
            return has_bytes(nxt)
        return False
    except Exception:
        return False


def _resolve_from_page(page_url, referer):
    current_url = page_url
    current_referer = referer
    seen = set()
    for _ in range(4):
        if current_url in seen:
            return None
        seen.add(current_url)
        headers = _request_headers(current_referer)
        r = SESSION.get(current_url, headers=headers or None, timeout=HTTP_TIMEOUT)
        if r.status_code != 200 or not r.text:
            return None
        source = _extract_source(r.text, current_url)
        if source:
            source_headers = _request_headers(current_url)
            if _validate_source(source, source_headers):
                return {"url": source, "headers": source_headers}
            log(f"rejecting unplayable source from {current_url}: {source}")
        next_url = _extract_wiki_embed(r.text, current_url) or _extract_embed(r.text, current_url)
        if not next_url:
            return None
        current_referer = current_url
        current_url = next_url
    return None


def _resolve_live(channel_id):
    with _cache_lock:
        hint = _folder_hint.get(channel_id)
    folders = ([hint] + [f for f in FOLDERS if f != hint]) if hint else FOLDERS
    for folder in folders:
        page_url = f"{DLHD_BASE}/{folder}/stream-{channel_id}.php"
        try:
            resolved = _resolve_from_page(page_url, DLHD_BASE + "/")
            if resolved:
                with _cache_lock:
                    _folder_hint[channel_id] = folder
                return resolved
        except Exception as e:
            log(f"resolve {channel_id} via /{folder}/ failed: {e}")
            continue
    return None


def resolve(channel_id):
    """Return {"url", "headers"} for channel_id (cached ~TTL), or None if it can't resolve.
    Favorites are served from the warm cache first (kept live-validated by run_precheck)."""
    now = time.time()
    with _cache_lock:
        warm = _warm.get(channel_id)
        if warm and now - warm[1] < _WARM_TTL:
            return warm[0]
        hit = _resolve_cache.get(channel_id)
        if hit and now - hit[1] < _RESOLVE_TTL:
            return hit[0]
    resolved = _resolve_live(channel_id)
    if resolved:
        with _cache_lock:
            _resolve_cache[channel_id] = (resolved, now)
            if channel_id in FAVORITES:
                _warm[channel_id] = (resolved, now)
    return resolved


def run_precheck():
    """Periodically resolve+validate each favorite channel so it opens instantly and never hands
    Jellyfin a dead player. Keeps the warm cache fresh; logs only online/offline transitions."""
    if not FAVORITES:
        log("favorite precheck disabled (DADDYLIVE_FAVORITES empty)")
        return
    if not DLHD_BASE:
        return
    log(f"favorite precheck starting; {len(FAVORITES)} channel(s) every {PRECHECK}s")
    status = {}  # id -> bool, last known online state (for transition-only logging)
    while True:
        for cid in FAVORITES:
            try:
                resolved = _resolve_live(cid)
            except Exception as e:
                log(f"precheck {cid} error: {e}")
                resolved = None
            now = time.time()
            with _cache_lock:
                if resolved:
                    _warm[cid] = (resolved, now)
                else:
                    _warm.pop(cid, None)
            ok = resolved is not None
            if status.get(cid) != ok:
                log(f"favorite {cid} is now {'ONLINE' if ok else 'offline'}")
                status[cid] = ok
        time.sleep(PRECHECK)


# ---- M3U / EPG output ----
def build_m3u(base_url=None):
    base_url = (base_url or PUBLIC_URL).rstrip("/")
    lines = [f'#EXTM3U url-tvg="{base_url}/epg.xml"']
    for c in all_channels():
        name = c["name"] or f"Channel {c['id']}"
        lines.append(
            f'#EXTINF:-1 tvg-id="{c["tvg_id"] or ""}" '
            f'tvg-logo="{c["logo"] or ""}" group-title="{c["group_title"] or "DaddyLive"}",{name}')
        # Keep the legacy /live/{id} form stable because Jellyfin hashes the exact URL
        # into the channel id. Changing the host or adding a .m3u8 suffix breaks existing
        # Live TV items before playback even reaches the bridge.
        lines.append(f"{base_url}/live/{c['id']}")
    return "\n".join(lines) + "\n"


# ---- HLS rewriting proxy ----
# DaddyLive serves HLS segments with disguised extensions (.js/.zst/.jpg) that ffmpeg's HLS
# demuxer rejects ("not in allowed_segment_extensions"), and Jellyfin gives no way to pass
# -allowed_extensions ALL. So we proxy every playlist and rewrite sub-playlist/segment/key URIs
# to route back through us with clean .m3u8/.ts/.key suffixes, relaying the bytes.
URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
_hls_header_cache = {}
_HLS_HEADER_TTL = int(os.environ.get("DADDYLIVE_HLS_HEADER_TTL", "300"))


def _b64e(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def _remember_hls_headers(url, headers):
    if not headers:
        return
    _hls_header_cache[url] = (headers, time.time())


def _headers_for_upstream(url):
    hit = _hls_header_cache.get(url)
    if not hit:
        return None
    if time.time() - hit[1] >= _HLS_HEADER_TTL:
        _hls_header_cache.pop(url, None)
        return None
    return hit[0]


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _strip_png_decoy(data):
    """Some providers (Player 2's picsart CDN) disguise TS segments with a small real PNG header
    before the first MPEG-TS sync byte, which desyncs ffmpeg's demuxer at each segment boundary.
    If the payload starts with the PNG magic, trim to the first 0x47 that looks like a TS sync
    (0x47 also present at +188 and +376). Non-PNG segments are returned untouched."""
    if not data.startswith(_PNG_MAGIC):
        return data
    i = data.find(b"\x47")
    while i != -1 and i + 376 < len(data):
        if data[i + 188] == 0x47 and data[i + 376] == 0x47:
            return data[i:]
        i = data.find(b"\x47", i + 1)
    return data


def _proxy_uri(abs_url, force=None, headers=None, public=None):
    _remember_hls_headers(abs_url, headers)
    ext = force or ("m3u8" if ".m3u8" in abs_url.split("?", 1)[0].lower() else "ts")
    return f"{public or PUBLIC_URL}/hls/{_b64e(abs_url)}.{ext}"


def rewrite_playlist(text, base_url, headers=None, public=None):
    """Rewrite every URI in an HLS playlist to route back through /hls/<b64>.<ext>.

    `public` is the base the client itself dialled (Handler._request_base_url()), so the
    rewritten URLs come back on the same interface the request arrived on. The box is
    dual-homed; hardcoding one NIC's IP here made every segment URL unreachable whenever
    that NIC was down. Falls back to PUBLIC_URL when there is no request context.
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            if ("EXT-X-KEY" in s or "EXT-X-MEDIA" in s or "EXT-X-MAP" in s) and URI_ATTR_RE.search(s):
                def repl(m):
                    au = urllib.parse.urljoin(base_url, m.group(1))
                    force = "key" if "EXT-X-KEY" in s else None
                    return f'URI="{_proxy_uri(au, force, headers=headers, public=public)}"'
                out.append(URI_ATTR_RE.sub(repl, s))
            else:
                out.append(line)
        else:  # a URI line (variant playlist or media segment)
            out.append(_proxy_uri(urllib.parse.urljoin(base_url, s), headers=headers, public=public))
    return "\n".join(out) + "\n"


_epg_cache = {"at": 0.0, "body": b""}


def get_epg():
    """Pass-through of the upstream EPG, cached ~10min so Jellyfin guide refreshes don't hammer it."""
    now = time.time()
    if now - _epg_cache["at"] < 600 and _epg_cache["body"]:
        return _epg_cache["body"]
    r = SESSION.get(EPG_URL, timeout=30)
    r.raise_for_status()
    _epg_cache["at"] = now
    _epg_cache["body"] = r.content
    return r.content


# ---- HTTP server ----
HLS_CT = "application/vnd.apple.mpegurl"
LIVE_RE = re.compile(r'^/live/(\d+)(?:\.m3u8)?$')
HLS_RE = re.compile(r'^/hls/([A-Za-z0-9_-]+)\.(m3u8|ts|key)$')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default per-request stderr logging

    def _request_base_url(self):
        host = self.headers.get("Host")
        if not host:
            return PUBLIC_URL
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return f"{proto}://{host}".rstrip("/")

    def _send(self, body, code=200, ctype="text/plain; charset=utf-8", head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return False  # client (ffmpeg/browser) hung up mid-response -- harmless

    def _handle(self, head_only=False):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/daddylive.m3u", "/playlist.m3u", "/"):
            self._send(
                build_m3u(self._request_base_url()),
                ctype="application/x-mpegurl; charset=utf-8",
                head_only=head_only,
            )
            return
        if path in ("/epg.xml", "/xmltv.xml"):
            try:
                self._send(get_epg(), ctype="application/xml; charset=utf-8", head_only=head_only)
            except Exception as e:
                log(f"epg fetch failed: {e}")
                self._send("epg unavailable", 502, head_only=head_only)
            return
        m = LIVE_RE.match(path)
        if m:
            cid = int(m.group(1))
            try:
                resolved = resolve(cid)
            except Exception as e:
                log(f"resolve {cid} error: {e}")
                resolved = None
            if not resolved:
                self._send(f"channel {cid} unavailable", 502, head_only=head_only)
                return
            master = resolved["url"]
            headers = resolved.get("headers")
            try:  # fetch the resolved master playlist and rewrite it through our proxy
                r = SESSION.get(master, headers=headers or None, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                self._send(
                    rewrite_playlist(r.text, master, headers=headers,
                                     public=self._request_base_url()),
                    ctype=HLS_CT,
                    head_only=head_only,
                )
            except Exception as e:
                log(f"channel {cid} master fetch failed: {e}")
                self._send(f"channel {cid} unavailable", 502, head_only=head_only)
            return
        m = HLS_RE.match(path)
        if m:
            self._proxy_hls(m.group(1), m.group(2), head_only=head_only)
            return
        self._send("not found", 404, head_only=head_only)

    def do_GET(self):
        self._handle(head_only=False)

    def do_HEAD(self):
        self._handle(head_only=True)

    def _proxy_hls(self, b64, ext, head_only=False):
        try:
            upstream = _b64d(b64)
        except Exception:
            self._send("bad url", 400, head_only=head_only)
            return
        headers = _headers_for_upstream(upstream)
        try:
            if ext == "m3u8":  # nested playlist -> fetch and rewrite again
                r = SESSION.get(upstream, headers=headers or None, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                self._send(
                    rewrite_playlist(r.text, upstream, headers=headers,
                                     public=self._request_base_url()),
                    ctype=HLS_CT,
                    head_only=head_only,
                )
            else:              # segment (.ts) or key (.key) -> relay raw bytes
                seg_headers = headers if ext == "key" else _drop_referer(headers)
                r = SESSION.get(upstream, headers=seg_headers or None, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                ct = "application/octet-stream" if ext == "key" else "video/mp2t"
                data = r.content if ext == "key" else _strip_png_decoy(r.content)
                self._send(data, ctype=ct, head_only=head_only)
        except Exception as e:
            log(f"hls proxy fetch failed ({ext}): {e}")
            self._send("upstream error", 502, head_only=head_only)


def serve():
    db()  # ensure schema exists before serving
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"server listening on :{PORT} (m3u=/daddylive.m3u epg=/epg.xml stream=/live/<id>)")
    httpd.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_refresher, daemon=True).start()
    threading.Thread(target=run_precheck, daemon=True).start()
    serve()
