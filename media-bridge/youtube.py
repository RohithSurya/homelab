#!/usr/bin/env python3
"""
media-bridge youtube component: a web form + background worker that pulls a
full-movie **YouTube** upload into a Radarr movie's folder when no viable torrent
exists (old / regional / dubbed films).

Design:
  * off-VPN like the rest of media-bridge; reaches Radarr via http://gluetun:7878.
  * the download itself runs in a **disposable `jauderho/yt-dlp` container**
    (yt-dlp + ffmpeg bundled, multi-arch) spawned via the host Docker socket and
    **removed after each job** -- so ffmpeg/yt-dlp never bloat the media-bridge image.
  * downloads land in the chosen Radarr movie's folder; a RescanMovie makes Radarr
    track the file (it imports existing files regardless of quality profile), then a
    Jellyfin library refresh surfaces it for playback.
  * manual URL only (no auto-search) -- avoids grabbing trailers / wrong cuts.

Two supervised threads (see main.py): serve() (HTTP form on :8003) and run_worker()
(processes the sqlite job queue one at a time).
"""

import html
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import requests

from common import clog, ntfy

PORT = int(os.environ.get("YOUTUBE_PORT", "8003"))
RADARR_URL = os.environ.get("RADARR_URL")
RADARR_KEY = os.environ.get("RADARR_API_KEY")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL")
JELLYFIN_KEY = os.environ.get("JELLYFIN_API_KEY")

YTDLP_IMAGE = os.environ.get("YTDLP_IMAGE", "jauderho/yt-dlp:latest")
YTDLP_FORMAT = os.environ.get("YTDLP_FORMAT", "bv*+ba/b")
# highest quality: prefer highest resolution, then h264 (Jetson Direct Play), then aac audio, then bitrate.
YTDLP_SORT = os.environ.get("YTDLP_SORT", "res,vcodec:h264,acodec:aac,br")
# how often a moving tag (:latest) is re-pulled -- see _ensure_image().
YTDLP_PULL_INTERVAL = int(os.environ.get("YTDLP_PULL_INTERVAL", "21600"))

# media-bridge sees /data; sibling containers spawned via the host daemon need the HOST path.
DATA_CONTAINER = os.environ.get("DATA_CONTAINER_PATH", "/data")
DATA_HOST = os.environ.get("DATA_HOST_PATH")  # e.g. /home/rosurya/media-server/data

DB_PATH = os.environ.get("YOUTUBE_DB", "/config/youtube.db")
POLL = int(os.environ.get("YOUTUBE_POLL", "5"))

RA = {"X-Api-Key": RADARR_KEY}
# accept only youtube URLs; capture the 11-char id across watch/short/embed/youtu.be forms.
YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^ &]*&)*v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def log(msg):
    clog("youtube", msg)


def parse_video_id(url):
    m = YT_RE.search(url or "")
    return m.group(1) if m else None


# ---------------- sqlite job store ----------------
def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT, url TEXT,
            movie_id INTEGER, movie_title TEXT,
            dest TEXT, status TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER)"""
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return con


def _meta_get(k):
    con = _db()
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    con.close()
    return row["v"] if row else None


def _meta_set(k, v):
    con = _db()
    with con:
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))
    con.close()


def _set_status(job_id, status, error=None, dest=None):
    con = _db()
    with con:
        con.execute(
            "UPDATE jobs SET status=?, error=COALESCE(?,error), dest=COALESCE(?,dest), updated_at=? WHERE id=?",
            (status, error, dest, int(time.time()), job_id),
        )
    con.close()


def requeue(job_id):
    """Send a failed job back to the queue (clears the error; yt-dlp --continue resumes
    whatever partial survived). Only 'failed' rows -- never re-download a finished movie."""
    con = _db()
    with con:
        cur = con.execute(
            "UPDATE jobs SET status='queued', error=NULL, updated_at=?"
            " WHERE id=? AND status='failed'",
            (int(time.time()), job_id),
        )
    n = cur.rowcount
    con.close()
    return n


def recent_jobs(limit=25):
    con = _db()
    rows = con.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return rows


# ---------------- Radarr ----------------
def radarr_movies():
    r = requests.get(f"{RADARR_URL}/api/v3/movie", headers=RA, timeout=60)
    r.raise_for_status()
    ms = [
        {
            "id": m["id"],
            "title": m.get("title"),
            "year": m.get("year"),
            "path": m.get("path"),
        }
        for m in r.json()
    ]
    ms.sort(key=lambda m: (m["title"] or "").lower())
    return ms


def radarr_movie(movie_id):
    r = requests.get(f"{RADARR_URL}/api/v3/movie/{movie_id}", headers=RA, timeout=30)
    r.raise_for_status()
    return r.json()


def radarr_rescan(movie_id):
    requests.post(
        f"{RADARR_URL}/api/v3/command",
        headers=RA,
        json={"name": "RescanMovie", "movieId": movie_id},
        timeout=30,
    ).raise_for_status()


def jellyfin_refresh():
    if not (JELLYFIN_URL and JELLYFIN_KEY):
        return
    try:
        requests.post(
            f"{JELLYFIN_URL}/Library/Refresh",
            # Jellyfin 12 disabled legacy auth: ?api_key= and X-Emby-Token now 401.
            headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"'},
            timeout=30,
        )
    except Exception as e:
        log(f"(warn) jellyfin refresh failed: {e}")


# ---------------- enqueue ----------------
def enqueue(url, movie_id):
    vid = parse_video_id(url)
    if not vid:
        raise ValueError("not a recognized YouTube URL")
    m = radarr_movie(int(movie_id))
    title = f"{m.get('title')} ({m.get('year')})"
    now = int(time.time())
    con = _db()
    with con:
        con.execute(
            "INSERT INTO jobs(video_id,url,movie_id,movie_title,status,created_at,updated_at)"
            " VALUES(?,?,?,?, 'queued', ?, ?)",
            (vid, url, m["id"], title, now, now),
        )
    con.close()
    log(f"queued: {title} <- youtube {vid}")


# ---------------- worker ----------------
def _host_path(container_path):
    """Translate a /data/... path (what Radarr/media-bridge see) to the HOST path,
    required because a sibling container's bind-mount is resolved by the host daemon."""
    if not DATA_HOST:
        raise RuntimeError(
            "DATA_HOST_PATH not set; cannot mount into disposable container"
        )
    if not container_path.startswith(DATA_CONTAINER):
        raise RuntimeError(
            f"movie path {container_path!r} is not under {DATA_CONTAINER}"
        )
    return DATA_HOST + container_path[len(DATA_CONTAINER) :]


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip()


# ---------------- disposable-image freshness ----------------
# yt-dlp failures that are almost always "the image predates YouTube's latest player
# change" rather than anything wrong with the video or this code.
STALE_RE = re.compile(
    r"HTTP Error 403|Requested format is not available|PO Token", re.I
)


def _mutable_tag(ref):
    """True when the image ref names a moving tag (bare name or :latest), i.e. one whose
    content changes upstream. A pinned version or @sha256 digest is never re-pulled."""
    tag = ref.rsplit("/", 1)[-1]
    return "@" not in ref and (":" not in tag or tag.endswith(":latest"))


def _ytdlp_version(client):
    try:
        out = client.containers.run(
            YTDLP_IMAGE, command=["--version"], entrypoint="yt-dlp", remove=True
        )
        return out.decode(errors="replace").strip()
    except Exception as e:
        return f"(unknown: {e})"


def _ensure_image(client, force=False):
    """Keep the disposable yt-dlp image fresh.

    A moving tag is NOT self-updating: docker only fetches an image that is *absent*, so a
    once-pulled `:latest` stays frozen at its first-pull date forever. That is exactly how a
    months-old yt-dlp reaches this worker and every download dies with `HTTP Error 403`
    partway in (YouTube gates each player client behind a PO token as it discovers them; the
    fix always ships in a newer yt-dlp). DIUN cannot cover the gap either -- it watches
    *running* containers, and this one exists only while a job runs.

    A pull failure is non-fatal whenever a local copy exists: a registry blip or an offline
    box must not block a download that would otherwise work.
    """
    import docker  # local import so a missing SDK only fails the worker thread

    try:
        client.images.get(YTDLP_IMAGE)
        have = True
    except docker.errors.ImageNotFound:
        have = False

    if have and not force:
        if not _mutable_tag(YTDLP_IMAGE):
            return
        if time.time() - int(_meta_get("ytdlp_pulled_at") or 0) < YTDLP_PULL_INTERVAL:
            return

    try:
        log(f"pulling {YTDLP_IMAGE} ...")
        client.images.pull(YTDLP_IMAGE)
        _meta_set("ytdlp_pulled_at", int(time.time()))
        log(f"yt-dlp is now {_ytdlp_version(client)}")
    except Exception as e:
        if not have:
            raise
        log(f"(warn) could not refresh {YTDLP_IMAGE}, using local copy: {e}")


def _tail_lines(raw, n=15):
    """Last n meaningful lines of yt-dlp output. Progress spam ('[download] 12.3% of ...')
    otherwise fills the whole tail and buries the one ERROR line the UI and ntfy show."""
    lines = [
        ln.strip()
        for ln in raw.replace("\r", "\n").splitlines()
        if ln.strip() and not re.match(r"\[download\]\s+\d", ln.strip())
    ]
    return "\n".join(lines[-n:])


def _run_ytdlp(client, job, cmd, host_dir):
    """Run one disposable yt-dlp container to completion; return (exit code, log tail)."""
    c = client.containers.run(
        YTDLP_IMAGE,
        command=cmd,
        user="1000:1000",
        environment={"HOME": "/tmp"},
        volumes={host_dir: {"bind": "/out", "mode": "rw"}},
        detach=True,
    )
    try:
        for line in c.logs(stream=True, follow=True):
            txt = line.decode(errors="replace").rstrip()
            if txt:
                log(f"  [{job['id']}] {txt}")
        code = c.wait().get("StatusCode", 1)
        tail = c.logs(tail=200).decode(errors="replace")
    finally:
        try:
            c.remove(force=True)  # disposable: always delete the temp container
        except Exception as e:
            log(f"(warn) could not remove temp container: {e}")
    return code, tail


def _download(client, job):
    m = radarr_movie(job["movie_id"])
    movie_path = m.get("path")  # container path, e.g. /data/media/movies/Title (Year)
    host_dir = _host_path(movie_path)
    fname = f"{_safe(job['movie_title'])} WEB-DL [YouTube-{job['video_id']}].mkv"
    dest_container = f"{movie_path}/{fname}"
    log(f"downloading job {job['id']} -> {dest_container}")

    cmd = [
        "-f",
        YTDLP_FORMAT,
        "-S",
        YTDLP_SORT,
        "--merge-output-format",
        "mkv",
        "--no-playlist",
        "--newline",
        "--continue",
        "-o",
        f"/out/{fname}",
        job["url"],
    ]
    # ensure the target folder exists on disk (Radarr may not have created it yet)
    os.makedirs(movie_path, exist_ok=True)

    code, tail = _run_ytdlp(client, job, cmd, host_dir)
    if code != 0 and STALE_RE.search(tail):
        # 403 / no-usable-format / PO-token: YouTube moved and this image predates the fix.
        # Force a re-pull and give it exactly one more shot (--continue resumes the partial).
        log(f"job {job['id']}: looks like a stale yt-dlp -- refreshing image and retrying once")
        _ensure_image(client, force=True)
        code, tail = _run_ytdlp(client, job, cmd, host_dir)

    if code != 0:
        raise RuntimeError(f"yt-dlp exited {code}: {_tail_lines(tail)[-500:]}")
    if not os.path.exists(dest_container):
        raise RuntimeError("yt-dlp reported success but output file is missing")
    return dest_container


def _process(client, job):
    _set_status(job["id"], "downloading")
    try:
        dest = _download(client, job)
    except Exception as e:
        _set_status(job["id"], "failed", error=str(e)[:1000])
        log(f"job {job['id']} FAILED: {e}")
        ntfy(
            "media-bridge youtube: download failed",
            f"{job['movie_title']}\n{e}",
            priority="high",
            tags="warning",
        )
        return
    try:
        radarr_rescan(job["movie_id"])
        jellyfin_refresh()
    except Exception as e:
        log(f"(warn) post-download register failed for job {job['id']}: {e}")
    _set_status(job["id"], "done", dest=dest)
    log(f"job {job['id']} DONE: {dest}")
    ntfy(
        "media-bridge youtube: download complete",
        f"{job['movie_title']}",
        tags="white_check_mark",
    )


def run_worker():
    import docker

    client = docker.from_env()
    # pull up front so the first job doesn't stall silently, and refresh a stale :latest
    _ensure_image(client)

    # requeue any job interrupted by a restart (yt-dlp --continue resumes the partial)
    con = _db()
    with con:
        con.execute("UPDATE jobs SET status='queued' WHERE status='downloading'")
    con.close()

    log(f"worker up (image={YTDLP_IMAGE}, sort={YTDLP_SORT})")
    while True:
        con = _db()
        job = con.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        con.close()
        if job:
            _ensure_image(client)  # a queued job always runs on a recent yt-dlp
            _process(client, job)
        else:
            time.sleep(POLL)


# ---------------- HTTP form ----------------
_STATUS_COLOR = {
    "done": "#1a7f37",
    "failed": "#cf222e",
    "downloading": "#9a6700",
    "queued": "#57606a",
}


def _page():
    try:
        movies = radarr_movies()
    except Exception as e:
        movies = []
        movies_err = f"<p style='color:#cf222e'>Could not load Radarr movies: {html.escape(str(e))}</p>"
    else:
        movies_err = ""
    opts = "".join(
        f"<option value='{m['id']}'>{html.escape(m['title'] or '')} ({m['year']})</option>"
        for m in movies
    )
    rows = ""
    for j in recent_jobs():
        color = _STATUS_COLOR.get(j["status"], "#57606a")
        err = (
            f"<div style='color:#cf222e;font-size:12px;white-space:pre-wrap'>{html.escape(j['error'] or '')}</div>"
            if j["status"] == "failed" and j["error"]
            else ""
        )
        retry = (
            "<form method=POST action=/retry style='display:inline'>"
            f"<input type=hidden name=job_id value='{j['id']}'>"
            "<button class=retry type=submit>retry</button></form>"
            if j["status"] == "failed"
            else ""
        )
        rows += (
            f"<tr><td>{html.escape(j['movie_title'] or '')}</td>"
            f"<td><a href='{html.escape(j['url'] or '')}'>{html.escape(j['video_id'] or '')}</a></td>"
            f"<td style='color:{color};font-weight:600'>{html.escape(j['status'] or '')}{err}</td>"
            f"<td>{time.strftime('%m-%d %H:%M', time.localtime(j['updated_at'] or 0))}</td>"
            f"<td>{retry}</td></tr>"
        )
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>media-bridge · YouTube grabber</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}}
 h1{{font-size:1.3rem}} label{{display:block;margin:.6rem 0 .2rem;font-weight:600}}
 select,input{{width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box}}
 button{{margin-top:1rem;padding:.6rem 1.2rem;font-size:1rem;cursor:pointer}}
 table{{width:100%;border-collapse:collapse;margin-top:2rem;font-size:14px}}
 th,td{{text-align:left;padding:.4rem;border-bottom:1px solid #d0d7de;vertical-align:top}}
 .hint{{color:#57606a;font-size:13px}}
 button.retry{{margin:0;padding:.15rem .5rem;font-size:12px}}
</style></head><body>
<h1>YouTube → library grabber</h1>
<p class=hint>Pick a movie already added in Radarr, paste a YouTube URL. Highest quality is downloaded
into the movie's folder and registered with Radarr + Jellyfin.</p>
{movies_err}
<form method=POST action=/submit>
 <label>Movie (from Radarr)</label>
 <select name=movie_id required>{opts}</select>
 <label>YouTube URL</label>
 <input name=url placeholder='https://www.youtube.com/watch?v=...' required>
 <button type=submit>Queue download</button>
</form>
<table><thead><tr><th>Movie</th><th>Video</th><th>Status</th><th>Updated</th><th></th></tr></thead>
<tbody>{rows or "<tr><td colspan=5 class=hint>no jobs yet</td></tr>"}</tbody></table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet default logging
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/youtube"):
            self._send(200, _page())
        else:
            self._send(404, "not found")

    def do_POST(self):
        if self.path not in ("/submit", "/retry"):
            self._send(404, "not found")
            return
        n = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(n).decode())

        if self.path == "/retry":
            try:
                jid = int((form.get("job_id", [""])[0]).strip())
            except ValueError:
                self._send(400, "<p>Error: bad job id</p><a href=/>back</a>")
                return
            if requeue(jid):
                log(f"job {jid} requeued from the web form")
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        url = (form.get("url", [""])[0]).strip()
        movie_id = (form.get("movie_id", [""])[0]).strip()
        try:
            enqueue(url, movie_id)
        except Exception as e:
            self._send(400, f"<p>Error: {html.escape(str(e))}</p><a href=/>back</a>")
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


def serve():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"form serving on :{PORT}")
    srv.serve_forever()
