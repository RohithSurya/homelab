"""
Seedbox backend for bridge.py: qBittorrent on a remote box (Pulsed Media PMSS today), reached
ONLY over SSH with two restricted keys from the vault, never with the account password:

  SEEDBOX_SSH_KEY_QBIT   authorized_keys: restrict,port-forwarding,permitopen="127.0.0.1:*",
                         command="/bin/false"  -> a tunnel to qBittorrent's WebUI on 127.0.0.1
  SEEDBOX_SSH_KEY_SFTP   authorized_keys: restrict,command="/usr/lib/openssh/sftp-server -R"
                         -> read-only SFTP: pull finished files, read ~/.quota and the port file
  SEEDBOX_QBIT_PASSWORD  qBittorrent's own WebUI password (NOT the account password). A password
                         change in the Pulsed panel resets it to the account password: the first
                         refused login then stops all logins for LOGIN_BACKOFF (qBittorrent bans
                         127.0.0.1 after repeated failures, which would lock the user's WebUI too).

The box's SSH host key must already be pinned (media-bridge/config/seedbox_known_hosts, or the
vault's SEEDBOX_SSH_HOSTKEY); an unknown or changed key is refused, never learned.

qBittorrent WebUI API over the tunnel:
  add          POST /api/v2/torrents/add  (paused/stopped=false, no share limits, bridge category)
  progress     GET  /api/v2/torrents/info?hashes=...   (one batched call per poll)
  files        GET  /api/v2/torrents/files?hash=...    -> pulled one by one over SFTP
  delete       POST /api/v2/torrents/delete  deleteFiles=true
Space comes from ~/.quota (PMSS writes the user's repquota line every minute): qBittorrent's
free_space_on_disk is the whole shared disk and would let the quota overflow.

Seeding holds are decided from qBittorrent's own per-torrent seeding_time / ratio against the rule
bridge.py stored in the state file (SEED_RULES). A torrent missing from the listing is NEVER a
finished hold, and never deleted: it is reported UNAVAILABLE and the bridge waits.
"""

import io
import json
import os
import posixpath
import re
import socket
import threading
import time

import requests

import vault
from backends import follow
from backends.base import (
    DONE,
    DOWNLOADING,
    UNAVAILABLE,
    Backend,
    Exists,
    Full,
    Rejected,
)
from common import clog, ntfy
from torrentfile import magnet_meta, parse_torrent, torrent_meta

HOST = os.environ.get("SEEDBOX_SSH_HOST", "")
SSH_PORT = int(os.environ.get("SEEDBOX_SSH_PORT", "22"))
USER = os.environ.get("SEEDBOX_SSH_USER", "")
QBIT_USER = os.environ.get("SEEDBOX_QBIT_USER") or USER
KNOWN_HOSTS = os.environ.get("SEEDBOX_KNOWN_HOSTS", "/config/seedbox_known_hosts")
PORT_FILE = os.environ.get("SEEDBOX_QBIT_PORT_FILE", ".qbittorrentPort")  # relative to the SFTP home
QUOTA_FILE = os.environ.get("SEEDBOX_QUOTA_FILE", ".quota")
CATEGORY_PREFIX = os.environ.get("SEEDBOX_CATEGORY_PREFIX", "bridge-")
TAG = "media-bridge"
GiB = 1024**3
QUOTA_FALLBACK = float(os.environ.get("SEEDBOX_QUOTA_GB", "230")) * GiB  # if ~/.quota is unreadable
FREE_MARGIN = float(os.environ.get("SEEDBOX_FREE_MARGIN_GB", "20")) * GiB  # never fill past this
UNKNOWN_SIZE = float(os.environ.get("SEEDBOX_UNKNOWN_SIZE_GB", "40")) * GiB  # a magnet without xl=
SEED_MARGIN_HOURS = float(os.environ.get("SEED_MARGIN_HOURS", "3"))
LOGIN_BACKOFF = int(os.environ.get("SEEDBOX_LOGIN_BACKOFF", "1800"))
SFTP_TIMEOUT = int(os.environ.get("SEEDBOX_SFTP_TIMEOUT", "120"))
PREFETCH_REQUESTS = int(os.environ.get("SEEDBOX_PREFETCH_REQUESTS", "64"))  # bounds memory per stream
# One SSH stream is capped by window / RTT: every SSH stack defaults to a 2 MiB channel window, and at
# ~110 ms Helsinki -> Jetson that is ~12 MB/s. Pull connections advertise a bigger window and ask for
# OpenSSH's largest read (SFTP_MAX_READ_LENGTH = 261120; never more: a clamped reply leaves a gap that
# makes paramiko give up prefetching), so in-flight = PREFETCH_REQUESTS x 255 KiB. Measured 2026-09-26:
# one tuned stream 36 MB/s vs 13 default, at half the CPU per GB; AES-GCM runs on the Jetson's AES unit.
SFTP_WINDOW = int(float(os.environ.get("SEEDBOX_SFTP_WINDOW_MB", "16")) * 1024**2)
SFTP_REQUEST = min(int(os.environ.get("SEEDBOX_SFTP_REQUEST", "261120")), 261120)
SFTP_CIPHER = os.environ.get("SEEDBOX_SFTP_CIPHER", "aes128-gcm@openssh.com")  # preferred, not forced
# Parallel SFTP streams per large file, each on its own SSH connection. With the window above the
# limit is the box's shared uplink (~40 MB/s evenings, far less at busy hours), which 1-2 tuned
# streams already fill; more only add connections (Pulsed's KB: 5-8 per user at most).
PULL_SEGMENTS = int(os.environ.get("PULL_SEGMENTS", "2"))
SEGMENT_MIN = int(float(os.environ.get("PULL_SEGMENT_MIN_MB", "64")) * 1024**2)  # smaller files: 1 stream
# Small files (scene RAR volumes: dozens of ~50 MB files) this many at a time, one connection each:
# per-file setup costs round trips, so one at a time ran at 11.7 MB/s vs 36.6 four at a time.
PULL_PARALLEL_FILES = int(os.environ.get("PULL_PARALLEL_FILES", "4"))
# Follow mode (backends/follow.py): at most this much per step, so another transfer's final pull
# never waits long behind one; and at completion, re-read pieces that failed their hash (libtorrent
# reported them before they reached disk) up to FOLLOW_PASSES times, FOLLOW_RETRY_WAIT s apart.
FOLLOW_STEP = int(float(os.environ.get("FOLLOW_STEP_MB", "2048")) * 1024**2)
FOLLOW_PASSES = int(os.environ.get("FOLLOW_PASSES", "3"))
FOLLOW_RETRY_WAIT = float(os.environ.get("FOLLOW_RETRY_WAIT", "10"))
QUOTA_REFRESH = 120
DEFAULT_RULE = {"hours": 336.0, "ratio": None}  # a held rec that somehow lacks its rule
CHUNK = 1 << 20  # pwrite batch
# Per read() from paramiko: the file's own SFTPFile.MAX_REQUEST_SIZE when it has one. Asking for more
# (the old 1 MiB) made BufferedFile.read concatenate its 32 KiB responses one by one into a growing
# bytes object -- ~16 MiB of copying per MiB pulled, all under the GIL.
READ_SIZE = 32768
# Brief states where progress is 1.0 but the files aren't settled yet.
SETTLING = {"moving", "checkingUP", "checkingResumeData", "allocating", "checkingDL"}
STOPPED_UP = {"pausedUP", "stoppedUP", "queuedUP"}
STOPPED_DL = {"pausedDL", "stoppedDL", "queuedDL"}
BROKEN = {"error", "missingFiles", "unknown"}


def log(msg):
    clog("seedbox", msg)


def _local_path(dest_dir, rel):
    """dest_dir/<rel>, refusing anything that would land outside dest_dir: file names come from the
    torrent, i.e. from whoever made it."""
    if not rel or any(p in ("", ".", "..") or "\0" in p for p in rel):
        raise RuntimeError(f"unsafe path in torrent: {'/'.join(rel)!r}")
    local = dest_dir.joinpath(*rel)
    if not local.resolve().is_relative_to(dest_dir.resolve()):
        raise RuntimeError(f"unsafe path in torrent: {'/'.join(rel)!r}")
    return local


def _sftp_transport(paramiko, sock):
    """Client transport for pulls: big receive window, SFTP_CIPHER first in the offer."""
    t = paramiko.Transport(sock, default_window_size=SFTP_WINDOW)
    opts = t.get_security_options()
    if SFTP_CIPHER in opts.ciphers:
        opts.ciphers = (SFTP_CIPHER,) + tuple(c for c in opts.ciphers if c != SFTP_CIPHER)
    return t


def _sftp_client(paramiko, t):
    sftp = paramiko.SFTPClient.from_transport(t, window_size=SFTP_WINDOW)
    sftp.get_channel().settimeout(SFTP_TIMEOUT)
    return sftp


def _open_remote(sftp, remote):
    """remote file for reading, with requests of SFTP_REQUEST (paramiko's own default is 32 KiB)."""
    rf = sftp.open(remote, "rb")
    if hasattr(rf, "MAX_REQUEST_SIZE"):
        rf.MAX_REQUEST_SIZE = SFTP_REQUEST
    return rf


class _AnyOf:
    """is_set() when any of the given events is: one stop signal for helpers that take one Event."""

    def __init__(self, *events):
        self.events = [e for e in events if e is not None]

    def is_set(self):
        return any(e.is_set() for e in self.events)


def _save_map(mapf, size, segs):
    """Atomic: a crash mid-write must never leave a torn map behind."""
    tmp = mapf.with_name(mapf.name + ".tmp")
    tmp.write_text(json.dumps({"size": size, "segs": segs}))
    os.replace(tmp, mapf)


def _abandon(sftps, files):
    """After an aborted pull (cancel or error): paramiko's prefetch thread waits for a free request
    slot in a busy loop that never checks whether its file or channel is gone, so it would spin
    forever. Close the channels (the caller is giving up on them anyway), then empty each file's
    slot table: the thread wakes, fails its next send on the closed channel and exits."""
    for sftp in sftps:
        try:
            sftp.close()
        except Exception:
            pass
    for rf in files:
        lock = getattr(rf, "_prefetch_lock", None)
        if lock is not None:
            with lock:
                rf._prefetch_extents.clear()


def _quiet_prefetch_exit(args, _prev=threading.excepthook):
    # that failed send is how _abandon ends the thread: expected, not worth a traceback
    name = args.thread.name if args.thread is not None else ""
    if name.endswith("(_prefetch_thread)") and issubclass(args.exc_type, (OSError, EOFError)):
        return
    _prev(args)


threading.excepthook = _quiet_prefetch_exit


def parse_quota(text):
    """(used bytes, soft quota bytes) from PMSS's ~/.quota (a repquota/quota -s snapshot):
    '/dev/md0  24604K  230G  288G  ...'."""
    unit = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("/dev/"):
            vals = []
            for p in parts[1:3]:
                m = re.fullmatch(r"([\d.]+)([KMGT]?)\*?", p)
                if not m:
                    break
                vals.append(float(m.group(1)) * unit.get(m.group(2) or "K", 1024))
            if len(vals) == 2:
                return int(vals[0]), int(vals[1])
    raise ValueError("no quota line found")


class Remote:
    """SSH transports (one per key), the SFTP client, and a local TCP forwarder that tunnels
    HTTP to qBittorrent's WebUI on the box's 127.0.0.1."""

    def __init__(self):
        self._lock = threading.RLock()
        self._transports = {}
        self._sftp = None
        self._qbit_port = None
        self._listener = None
        self.local_port = None

    # --- SSH ---------------------------------------------------------------------------------

    def _pinned_key(self, paramiko):
        known = paramiko.HostKeys()
        if os.path.exists(KNOWN_HOSTS):
            known.load(KNOWN_HOSTS)
        entry = known.lookup(HOST if SSH_PORT == 22 else f"[{HOST}]:{SSH_PORT}")
        if not entry:
            try:  # rebuild the pin from the vault copy (e.g. a fresh config volume)
                line = vault.get("SEEDBOX_SSH_HOSTKEY")
                with open(KNOWN_HOSTS, "w") as f:
                    f.write(line.strip() + "\n")
                os.chmod(KNOWN_HOSTS, 0o600)
                known.load(KNOWN_HOSTS)
                entry = known.lookup(HOST if SSH_PORT == 22 else f"[{HOST}]:{SSH_PORT}")
            except (vault.VaultError, OSError):
                entry = None
        if not entry:
            raise RuntimeError(f"no pinned SSH host key for {HOST}; refusing to connect")
        return entry

    def _transport(self, kind):
        import paramiko  # only this backend needs it

        with self._lock:
            t = self._transports.get(kind)
            if t is not None and t.is_active():
                return t
            pinned = self._pinned_key(paramiko)
            sock = socket.create_connection((HOST, SSH_PORT), timeout=30)
            t = paramiko.Transport(sock)
            t.banner_timeout = 30
            t.start_client(timeout=30)
            key = t.get_remote_server_key()
            if pinned.get(key.get_name()) != key:
                t.close()
                raise RuntimeError(
                    f"seedbox SSH host key CHANGED for {HOST} (now {key.get_name()} "
                    f"{key.fingerprint}); refusing to connect"
                )
            secret = "SEEDBOX_SSH_KEY_QBIT" if kind == "qbit" else "SEEDBOX_SSH_KEY_SFTP"
            t.auth_publickey(USER, paramiko.Ed25519Key.from_private_key(io.StringIO(vault.get(secret))))
            t.set_keepalive(30)
            self._transports[kind] = t
            if kind == "sftp":
                self._sftp = None
            return t

    def sftp(self):
        import paramiko

        with self._lock:
            t = self._transport("sftp")
            if self._sftp is None or self._sftp.get_channel().closed:
                self._sftp = paramiko.SFTPClient.from_transport(t)
                self._sftp.get_channel().settimeout(SFTP_TIMEOUT)
            return self._sftp

    def read_small(self, path):
        with self.sftp().open(path, "r") as f:
            return f.read().decode(errors="replace")

    def open_sftp(self):
        """A fresh SSH connection + SFTP client with the read-only key, owned by the caller (the
        pull worker): never shared with the bridge thread's cached one. Caller closes both."""
        import paramiko

        pinned = self._pinned_key(paramiko)
        sock = socket.create_connection((HOST, SSH_PORT), timeout=30)
        t = _sftp_transport(paramiko, sock)
        try:
            t.banner_timeout = 30
            t.start_client(timeout=30)
            key = t.get_remote_server_key()
            if pinned.get(key.get_name()) != key:
                raise RuntimeError(f"seedbox SSH host key CHANGED for {HOST}; refusing to connect")
            t.auth_publickey(USER, paramiko.Ed25519Key.from_private_key(io.StringIO(vault.get("SEEDBOX_SSH_KEY_SFTP"))))
            return t, _sftp_client(paramiko, t)
        except Exception:
            t.close()
            raise

    def close(self):
        with self._lock:
            for t in self._transports.values():
                try:
                    t.close()
                except Exception:
                    pass
            self._transports.clear()
            self._sftp = None

    # --- tunnel to qBittorrent ------------------------------------------------------------------

    def qbit_port(self, refresh=False):
        if refresh or not self._qbit_port:
            self._qbit_port = int(self.read_small(PORT_FILE).strip())
        return self._qbit_port

    def base_url(self):
        with self._lock:
            if self._listener is None:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.bind(("127.0.0.1", 0))  # inside the container only
                srv.listen(16)
                self._listener, self.local_port = srv, srv.getsockname()[1]
                threading.Thread(target=self._accept, name="seedbox-tunnel", daemon=True).start()
        return f"http://127.0.0.1:{self.local_port}"

    def _accept(self):
        while True:
            cs, peer = self._listener.accept()
            try:
                ch = self._transport("qbit").open_channel(
                    "direct-tcpip", ("127.0.0.1", self.qbit_port()), peer, timeout=30
                )
            except Exception as e:
                log(f"tunnel to qBittorrent failed ({e}); re-reading its port next time")
                self._qbit_port = None
                cs.close()
                continue
            for a, b in ((cs, ch), (ch, cs)):
                threading.Thread(target=_pump, args=(a, b), daemon=True).start()


def _pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except Exception:
        pass
    finally:
        for x in (a, b):
            try:
                x.close()
            except Exception:
                pass


class QbitBackend(Backend):
    name = "seedbox"
    label = "seedbox"
    tag = "seedbox"
    max_seed_hours = None
    full_retries = None  # a full seedbox is busy seeding, not too small: wait for space
    fetch_fail_log = "seedbox SFTP fetch failing"
    fetch_fail_note = "The seedbox reported it complete but pulling it over SFTP kept failing"

    def __init__(self):
        if not (HOST and USER):
            raise RuntimeError("SEEDBOX_SSH_HOST / SEEDBOX_SSH_USER are not set")
        self.remote = Remote()
        self.s = None
        self.torrents = {}
        self.live_hashes = set()
        self.reserved = 0
        self.login_blocked_until = 0
        self._quota = None  # (used, limit, read_at)

    # --- qBittorrent WebUI API ------------------------------------------------------------------

    def _session(self):
        if self.s is None:
            base = self.remote.base_url()
            self.s = requests.Session()
            self.s.headers["Referer"] = base  # qBittorrent's CSRF check wants a same-origin Referer
            self.base = base + "/api/v2"
        return self.s

    def _login(self):
        now = time.time()
        if now < self.login_blocked_until:
            raise RuntimeError(
                f"qBittorrent login paused until {time.strftime('%H:%M', time.localtime(self.login_blocked_until))} "
                "after a refused login"
            )
        s = self._session()
        try:
            pw = vault.get("SEEDBOX_QBIT_PASSWORD")
        except vault.VaultError as e:
            raise RuntimeError(f"can't read the qBittorrent password from the vault: {e}") from None
        r = s.post(f"{self.base}/auth/login", data={"username": QBIT_USER, "password": pw}, timeout=30)
        if r.status_code == 403 or r.text.strip() != "Ok.":
            # Stop at the FIRST refusal: qBittorrent bans 127.0.0.1 after repeated failures, and
            # every client (incl. the user's WebUI through lighttpd) is 127.0.0.1 to it.
            self.login_blocked_until = now + LOGIN_BACKOFF
            log(f"qBittorrent REFUSED the login ({r.status_code} {r.text.strip()[:30]!r}); no retry for {LOGIN_BACKOFF // 60}min")
            ntfy(
                "Seedbox: qBittorrent login refused",
                "media-bridge's qBittorrent password was refused (a password change in the Pulsed panel "
                "resets it to the account password). No new logins for "
                f"{LOGIN_BACKOFF // 60} min. Update SEEDBOX_QBIT_PASSWORD in the vault or reset it in qBittorrent.",
                priority="high",
                tags="warning",
            )
            raise RuntimeError("qBittorrent login refused")

    def _api(self, method, path, **kw):
        s = self._session()
        kw.setdefault("timeout", 60)
        for attempt in (1, 2):
            try:
                r = s.request(method, f"{self.base}{path}", **kw)
                if r.status_code == 403:  # no session yet, or it expired
                    self._login()
                    r = s.request(method, f"{self.base}{path}", **kw)
                break
            except requests.ConnectionError:
                # qBittorrent closes idle keep-alive connections (e.g. during a long SFTP pull),
                # which the next request on the pooled connection sees as "RemoteDisconnected";
                # or PMSS moved qBittorrent to another port. Re-read the port, retry once.
                self.remote.qbit_port(refresh=True)
                if attempt == 2:
                    raise
        r.raise_for_status()
        return r

    def _info(self, hashes=None, **params):
        if hashes is not None:
            if not hashes:
                return []
            params["hashes"] = "|".join(hashes)
        return self._api("GET", "/torrents/info", params=params).json()

    def _find(self, btih):
        """qBittorrent's listing entry for a v1 infohash, or None. qBittorrent on libtorrent 2
        keys a hybrid v1+v2 torrent by its v2 hash cut to 40 hex chars, so `hashes=<v1>` finds
        nothing for it (2026-10-29: a hybrid Heart Beat pack finished on the box while every
        re-add answered 'Fails.' as a duplicate the bridge couldn't see). Falls back to matching
        `infohash_v1` across the whole listing; the entry's `hash` is the id every API call needs."""
        btih = btih.lower()
        ts = self._info([btih])
        if ts:
            return ts[0]
        for t in self._info():
            if (t.get("infohash_v1") or "").lower() == btih:
                return t
        return None

    def _delete(self, h):
        if not h:
            return
        try:
            self._api("POST", "/torrents/delete", data={"hashes": h, "deleteFiles": "true"})
        except Exception as e:
            log(f"    (warning) could not delete torrent {h} from the seedbox: {e}")

    def _resume(self, h):
        # qBittorrent 5 renamed resume -> start; try the old name first (the box runs 4.5)
        for path in ("/torrents/resume", "/torrents/start"):
            try:
                self._api("POST", path, data={"hashes": h})
                return True
            except requests.HTTPError as e:
                if getattr(e.response, "status_code", None) != 404:
                    raise
        return False

    # --- space ------------------------------------------------------------------------------------

    def _quota_state(self, force=False):
        now = time.time()
        if force or self._quota is None or now - self._quota[2] > QUOTA_REFRESH:
            try:
                used, limit = parse_quota(self.remote.read_small(QUOTA_FILE))
            except Exception as e:
                log(f"    (warning) can't read {QUOTA_FILE} ({e}); assuming full quota use is unknown")
                used, limit = None, int(QUOTA_FALLBACK)
            self._quota = (used, limit, now)
        return self._quota

    def _free(self):
        """Bytes that can still be committed: quota - used - bytes our downloads still need to
        write - what this loop already added. None if the quota can't be read."""
        used, limit, _ = self._quota_state()
        if used is None:
            return None
        # every torrent on the box, not just ours: manual/ratio-building ones fill the quota too
        pending = sum(max(0, t.get("amount_left", 0)) for t in self._info())
        return limit - used - pending - self.reserved

    # --- contract --------------------------------------------------------------------------------

    def startup(self):
        try:
            ver = self._api("GET", "/app/version").text.strip()
            used, limit, _ = self._quota_state(force=True)
            q = f"{used / GiB:.1f}/{limit / GiB:.0f} GiB quota used" if used is not None else "quota unreadable"
            log(f"seedbox qBittorrent {ver} OK via SSH tunnel to {HOST}; {q}")
        except Exception as e:
            log(f"FATAL: cannot reach the seedbox qBittorrent via {HOST}: {e}")

    def add(self, drop, magnet):
        cat = drop.parent.name
        if drop.suffix == ".torrent":
            btih, tname, size, _ = torrent_meta(drop)
        else:
            btih, tname, size, _ = magnet_meta(magnet)
        if btih and self._find(btih):
            raise Exists()
        need = size or UNKNOWN_SIZE
        free = self._free()
        if free is None:
            raise RuntimeError("seedbox quota unreadable; not adding until it is")
        if need > free - FREE_MARGIN:
            _, limit, _ = self._quota_state()
            raise Full(max(free, 0), never_fits=need > limit - FREE_MARGIN)
        data = {
            "category": CATEGORY_PREFIX + cat,
            "tags": TAG,
            "paused": "false",  # qBittorrent 4.x
            "stopped": "false",  # qBittorrent 5.x
            "ratioLimit": "-1",  # never let a global/category share limit stop or remove it
            "seedingTimeLimit": "-1",
        }
        try:
            if drop.suffix == ".torrent":
                files = {"torrents": (drop.name, drop.read_bytes(), "application/x-bittorrent")}
                r = self._api("POST", "/torrents/add", data=data, files=files)
            else:
                r = self._api("POST", "/torrents/add", data=dict(data, urls=magnet))
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code == 409:  # qBittorrent 5.2+: nothing added (duplicate)
                raise Exists() from e
            if code == 415:
                raise Rejected("qBittorrent: not a valid torrent") from e
            raise
        body = r.text.strip()
        ok = body == "Ok."
        if not ok and body.startswith("{"):  # qBittorrent 5.2+ answers JSON
            try:
                ok = json.loads(body).get("success_count", 0) > 0
            except ValueError:
                ok = False
        if not ok:
            if btih and self._find(btih):
                raise Exists()
            raise RuntimeError(f"qBittorrent refused the add ({body[:60]!r})")
        t = None
        for _ in range(10):  # adds are asynchronous for magnets
            if not btih:
                break
            t = self._find(btih)
            if t:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("qBittorrent accepted the add but doesn't list the torrent")
        self.reserved += need
        # Titled after the *arr's release name (the drop): supersede's movie_key and the logs work
        # from it. The torrent's own name is kept too. torrent_hash = qBittorrent's id (differs
        # from the v1 btih for hybrid torrents); btih = v1, for the bridge's duplicate check.
        qid = (t["hash"].lower() if t else btih) or None
        return {"title": drop.stem, "torrent_hash": qid, "btih": btih or None, "torrent_name": tname, "size": size}

    def locate(self, btih, title):
        try:
            t = self._find(btih) if btih else None
        except Exception as e:
            log(f"    already on the seedbox but listing it failed ({e}); will retry next loop")
            return None
        if not t:
            log("    seedbox refused a duplicate but doesn't list it; will retry next loop")
            return None
        where = "complete" if t.get("progress", 0) >= 1 else f"in progress {t.get('progress', 0) * 100:.0f}%"
        return {"torrent_hash": t["hash"].lower(), "btih": btih, "torrent_name": t.get("name")}, where

    def begin_loop(self, records):
        self.reserved = 0
        live = [r for _, r in records if r.get("torrent_hash")]
        hashes = [r["torrent_hash"].lower() for r in live]
        self.torrents = {t["hash"].lower(): t for t in self._info(hashes)}
        self.live_hashes = {r["torrent_hash"].lower() for r in live if not r.get("superseded")}
        # qBittorrent listing none of several tracked torrents = it is restarting / reloading its
        # session, not that they all vanished: fail the loop so nothing is judged on it.
        if len(hashes) >= 2 and not self.torrents:
            raise RuntimeError(f"qBittorrent lists none of {len(hashes)} tracked torrents (restarting?)")

    def _torrent(self, rec):
        return self.torrents.get((rec.get("torrent_hash") or "").lower())

    def status(self, rec):
        t = self._torrent(rec)
        if t is None:
            return UNAVAILABLE, "not in qBittorrent's list"
        state = t.get("state", "")
        if state in BROKEN:
            return UNAVAILABLE, f"qBittorrent state '{state}'"
        prog = t.get("progress", 0)
        if prog < 1:
            if state in STOPPED_DL:  # something stopped it; a stopped torrent would just "stall"
                log(f"    '{rec['title']}' was {state} -> resuming")
                self._resume(t["hash"])
            return DOWNLOADING, round(prog * 100, 1)
        if state in SETTLING:
            return DOWNLOADING, 100.0
        return DONE, {"name": t.get("name", rec["title"]), "hash": t["hash"], "save_path": t.get("save_path", "")}

    def swarm(self, rec):
        t = self._torrent(rec)
        if t is None:
            return None
        # num_complete = seeds the trackers report for the whole swarm; num_seeds = connected now
        seeds = max(t.get("num_complete", -1), t.get("num_seeds", 0))
        return {"metadata": t.get("state") != "metaDL", "seeds": seeds if seeds >= 0 else None}

    def extra_trackers(self, rec, trackers):
        h = rec.get("torrent_hash")
        if not (h and trackers):
            return 0
        try:
            self._api("POST", "/torrents/addTrackers", data={"hash": h, "urls": "\n".join(trackers)})
            return len(trackers)
        except Exception as e:
            log(f"    (warning) could not add public trackers to {h}: {e}")
            return 0

    def partial_bytes(self, rec):
        t = self._torrent(rec)
        return None if t is None else t.get("completed", t.get("downloaded", 0))

    def prepare_fetch(self, rec, result):
        """Bridge thread: the file list from qBittorrent -> a plain plan the pull worker can run
        without touching the API session."""
        files = [
            f
            for f in self._api("GET", "/torrents/files", params={"hash": result["hash"]}).json()
            if f.get("priority", 1) != 0  # a deselected file was never downloaded
        ]
        if not files:
            raise RuntimeError("qBittorrent lists no files for this torrent")
        # Deliver the torrent's CONTENTS into the dest dir, as the Seedr backend does: strip the
        # torrent's own root folder when every file sits under one.
        parts = [f["name"].split("/") for f in files]
        strip = 1 if all(len(p) > 1 for p in parts) and len({p[0] for p in parts}) == 1 else 0
        return {
            "name": result.get("name"),
            "files": [
                {"remote": posixpath.join(result["save_path"], f["name"]), "rel": p[strip:], "size": f.get("size")}
                for f, p in zip(files, parts)
            ],
            "size": sum(f.get("size", 0) for f in files),
        }

    # --- follow mode (see backends/follow.py) ----------------------------------------------------

    follows = True

    def _follow_layout(self, h):
        """(layout, piece hashes) from the torrent itself, or None if it can't be followed."""
        r = self._api_soft("GET", "/torrents/export", params={"hash": h})
        if r is None:
            return None
        _, info, _ = parse_torrent(r.content)
        if b"pieces" not in info or b"piece length" not in info:
            return None  # v2-only torrent: no v1 piece hashes to verify against
        props = self._api("GET", "/torrents/properties", params={"hash": h}).json()
        skip = {f["name"] for f in self._api("GET", "/torrents/files", params={"hash": h}).json()
                if f.get("priority", 1) == 0}
        return follow.layout(info, props["save_path"], skip), info[b"pieces"]

    def _api_soft(self, method, path, **kw):
        """_api, but None on a 404/409 (an endpoint this qBittorrent lacks, a vanished hash)."""
        try:
            return self._api(method, path, **kw)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 409):
                return None
            raise

    def prepare_follow(self, rec, stage):
        h = (rec.get("torrent_hash") or "").lower()
        if not h:
            return None
        st = follow.load(stage)
        if st is not None and st.get("hash") != h:
            st = None  # another torrent's leftovers: the step resets them
        init = None
        if st is None:
            init = self._follow_layout(h)
            if init is None:
                return None
            lay = init[0]
            want = follow.needed(dict(lay))
            verified = None
        else:
            lay, want, verified = st, follow.needed(st), st["verified"]
        states = self._api("GET", "/torrents/pieceStates", params={"hash": h}).json()
        new = [i for i, s in enumerate(states) if s == 2 and i in want and not (verified and verified[i])]
        return {"kind": "follow", "hash": h, "init": init, "pieces": new,
                "new_bytes": len(new) * lay["piece_len"]}

    def _follow_fetch(self, st, stage, pieces, cancel):
        conns = []
        try:
            for _ in range(max(1, PULL_SEGMENTS)):
                conns.append(self.remote.open_sftp())
            try:
                return follow.fetch(st, stage, pieces, [c[1] for c in conns], _open_remote, PREFETCH_REQUESTS, cancel)
            finally:
                follow.save(stage, st)  # whatever was verified (and fsynced) counts, even on error
        finally:
            for t, sftp in conns:
                for x in (sftp, t):
                    try:
                        x.close()
                    except Exception:
                        pass

    def _follow_step(self, rec, plan, stage, cancel):
        st = follow.load(stage)
        if st is None or st.get("hash") != plan["hash"]:
            if plan.get("init") is None:
                return None
            follow.discard(stage, st)
            lay, hashes = plan["init"]
            for f in lay["files"]:
                if follow.writable(f):
                    _local_path(stage, f["rel"])  # names come from the torrent: refuse escapes
            st = follow.init(stage, plan["hash"], lay, hashes)
        todo, size = [], 0
        for i in plan["pieces"]:
            if not st["verified"][i]:
                todo.append(i)
                size += st["piece_len"]
                if size >= FOLLOW_STEP:
                    break
        if not todo:
            return {"verified": 0, "bad": 0, "bytes": 0}
        ok, bad, got = self._follow_fetch(st, stage, todo, cancel)
        return {"verified": ok, "bad": bad, "bytes": got, "left": len(follow.missing(st))}

    def _follow_finish(self, rec, st, stage, cancel):
        """At completion: the pieces follow steps didn't get, then the final names."""
        t0, got = time.time(), 0
        for attempt in range(FOLLOW_PASSES):
            todo = follow.missing(st)
            if not todo or (cancel is not None and cancel.is_set()):
                break
            if attempt:
                time.sleep(FOLLOW_RETRY_WAIT)  # hash failures: let the box flush those pieces
            got += self._follow_fetch(st, stage, todo, cancel)[2]
        if cancel is not None and cancel.is_set():
            return
        left = follow.missing(st)
        if left:
            raise RuntimeError(f"{len(left)} pieces failed verification after {FOLLOW_PASSES} passes")
        follow.finalize(stage, st)
        secs = max(time.time() - t0, 1e-6)
        log(f"    follow: completed '{rec.get('title')}' with {got:,} bytes after the download ({got / secs / 1e6:.1f} MB/s)")

    def pull(self, rec, plan, dest_dir, cancel=None):
        """Pull worker thread: every file of the plan over SFTP. Big files one at a time, each in
        PULL_SEGMENTS parallel streams; small files PULL_PARALLEL_FILES at a time. Every stream
        has its own SSH connection. A follow step (plan["kind"] == "follow") pulls finished pieces
        of a torrent that is still downloading; a final pull whose stage holds follow state only
        fills in what the steps didn't get."""
        if isinstance(plan, dict) and plan.get("kind") == "follow":
            return self._follow_step(rec, plan, dest_dir, cancel)
        st = follow.load(dest_dir)
        if st is not None:
            if st.get("hash") == (rec.get("torrent_hash") or "").lower():
                try:
                    return self._follow_finish(rec, st, dest_dir, cancel)
                except Exception as e:
                    # never let follow mode cost a delivery: drop what it pulled, pull normally
                    log(f"    follow: finishing '{rec.get('title')}' failed ({e}); pulling it the normal way")
            follow.discard(dest_dir, st)  # another torrent's leftovers, or a failed follow
        conns = []

        def streams(n):
            while len(conns) < n:
                conns.append(self.remote.open_sftp())
            return [c[1] for c in conns[:n]]

        try:
            big, small = [], []
            for f in plan["files"]:  # every path is checked before anything is fetched
                local = _local_path(dest_dir, f["rel"])
                resumed = local.with_suffix(local.suffix + ".part.map").exists()  # cut into ranges already
                (big if resumed or (f["size"] or 0) >= SEGMENT_MIN else small).append((f, local))
            for f, local in big:
                if cancel is not None and cancel.is_set():
                    return
                self._get_segmented(streams(max(1, PULL_SEGMENTS)), f["remote"], local, f["size"], cancel)
            if small and not (cancel is not None and cancel.is_set()):
                self._get_many(streams(max(1, min(PULL_PARALLEL_FILES, len(small)))), small, cancel)
        finally:
            for t, sftp in conns:
                for x in (sftp, t):
                    try:
                        x.close()
                    except Exception:
                        pass

    def fetch(self, rec, result, dest_dir):
        self.pull(rec, self.prepare_fetch(rec, result), dest_dir)

    def cancel(self, rec):
        self._delete(rec.get("torrent_hash"))

    def discard(self, rec, result):
        self._delete(rec.get("torrent_hash"))

    def start_hold(self, rec, result):
        return None  # decided per poll by hold_over() from qBittorrent's own counters

    def hold_over(self, rec):
        t = self._torrent(rec)
        if t is None:
            return None  # can't see it: keep the hold, never guess "done"
        state = t.get("state", "")
        if state in BROKEN:
            return None
        rule = rec.get("seed_rule") or DEFAULT_RULE
        seeded = t.get("seeding_time", 0)
        if seeded >= (rule["hours"] + SEED_MARGIN_HOURS) * 3600:
            return True
        if rule.get("ratio") and t.get("ratio", 0) >= rule["ratio"]:
            return True
        # Something (a share limit, a manual pause) stopped it seeding early: its seeding_time
        # would never grow and the tracker would log a hit-and-run.
        if state in STOPPED_UP or (rec.get("no_deliver") and state in STOPPED_DL):
            log(f"[{rec['category']}] '{rec['title']}' stopped ({state}) at {seeded / 3600:.1f}h of "
                f"{rule['hours']:g}h -> resuming")
            try:
                self._resume(t["hash"])
            except Exception as e:
                log(f"    (warning) could not resume it: {e}")
        return False

    def end_hold(self, rec):
        self._delete(rec.get("torrent_hash"))

    def reap(self, rec):
        # hash-exact, and never a torrent a live (non-tombstoned) record still owns -- e.g. the
        # same release grabbed again inside the tombstone's grace window
        h = (rec.get("torrent_hash") or "").lower()
        if h and h in self.torrents and h not in self.live_hashes:
            self._delete(h)

    # --- SFTP pull --------------------------------------------------------------------------------

    def _get_segmented(self, sftps, remote, local, size, cancel=None):
        """remote -> local split into byte ranges pulled in parallel, one SFTP client per stream
        (a client is never shared between threads), written in place (os.pwrite) into a
        preallocated <local>.part. Progress per range is kept in <local>.part.map, saved only
        after the data it claims is fsynced, so a restart -- even after a power cut -- resumes
        every range where it stopped. A resumed map with more ranges than clients runs them in
        turn. The first failing stream stops its siblings."""
        if local.exists() and local.stat().st_size == size:
            return
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(local.suffix + ".part")
        mapf = local.with_suffix(local.suffix + ".part.map")
        segs = None
        if mapf.exists() and tmp.exists():
            try:
                m = json.loads(mapf.read_text())
                if m.get("size") == size:
                    segs = m["segs"]
            except (OSError, ValueError):
                segs = None
        if segs is None:
            n = len(sftps)
            step = -(-size // n)
            segs = [[i * step, min(size, (i + 1) * step), i * step] for i in range(n) if i * step < size]
            with open(tmp, "wb") as f:
                f.truncate(size)  # sparse preallocation; ranges are filled in place
            _save_map(mapf, size, segs)
        todo = [s for s in segs if s[2] < s[1]]
        done0 = sum(s[2] - s[0] for s in segs)
        if done0:
            log(f"    resuming {local.name} at {done0:,} of {size:,} B")
        lock = threading.Lock()
        errors = []
        stop = threading.Event()  # set by the first failing stream
        pending = list(todo)  # ranges no stream has taken yet
        opened = []  # remote file handles, for _abandon after an abort
        fd = os.open(tmp, os.O_RDWR)
        t0 = time.time()

        def save():
            with lock:
                os.fsync(fd)  # data first: the map must never claim bytes still in the page cache
                _save_map(mapf, size, segs)

        def aborted():
            return stop.is_set() or (cancel is not None and cancel.is_set())

        def pull_range(seg, sftp):
            with _open_remote(sftp, remote) as rf:
                opened.append(rf)
                rsize = getattr(rf, "MAX_REQUEST_SIZE", READ_SIZE)
                rf.seek(seg[2])
                rf.prefetch(seg[1], max_concurrent_requests=PREFETCH_REQUESTS)  # arg = END offset
                buf, since_save = bytearray(), 0
                while seg[2] + len(buf) < seg[1] and not aborted():
                    chunk = rf.read(min(rsize, seg[1] - seg[2] - len(buf)))
                    if not chunk:
                        raise RuntimeError(f"short read at {seg[2] + len(buf):,}")
                    buf += chunk
                    if len(buf) >= CHUNK or seg[2] + len(buf) >= seg[1]:
                        os.pwrite(fd, buf, seg[2])
                        seg[2] += len(buf)  # progress = bytes already written, never just read
                        since_save += len(buf)
                        buf.clear()
                        if since_save >= 64 * CHUNK:
                            save()
                            since_save = 0
                if buf:  # stopped with bytes read but not yet written: keep them
                    os.pwrite(fd, buf, seg[2])
                    seg[2] += len(buf)

        def worker(sftp):
            try:
                while not aborted():
                    with lock:
                        if not pending:
                            return
                        seg = pending.pop(0)
                    pull_range(seg, sftp)
            except Exception as e:
                errors.append(e)
                stop.set()

        streams = sftps[:max(1, min(len(sftps), len(todo)))]
        try:
            threads = [threading.Thread(target=worker, args=(sftp,), daemon=True) for sftp in streams]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        finally:
            try:
                save()
            finally:
                os.close(fd)
            if aborted():
                _abandon(sftps, opened)
        if errors:
            raise RuntimeError(f"{local.name}: {errors[0]}")
        if cancel is not None and cancel.is_set():
            return
        if any(s[2] < s[1] for s in segs) or tmp.stat().st_size != size:
            raise RuntimeError(f"{local.name}: incomplete after pull")
        tmp.rename(local)
        mapf.unlink(missing_ok=True)
        secs = max(time.time() - t0, 1e-6)
        log(f"    downloaded {local.name} ({size:,} bytes, {len(streams)} streams, {(size - done0) / secs / 1e6:.1f} MB/s)")

    def _get_many(self, sftps, files, cancel=None):
        """[(plan file, local path)] pulled one per stream at a time, each with _get; the first
        failure stops the others and is raised."""
        lock, errors, stop = threading.Lock(), [], threading.Event()
        halt = _AnyOf(cancel, stop)
        pending = list(files)

        def worker(sftp):
            try:
                while not halt.is_set():
                    with lock:
                        if not pending:
                            return
                        f, local = pending.pop(0)
                    self._get(sftp, f["remote"], local, f["size"], halt)
            except Exception as e:
                errors.append(e)
                stop.set()

        threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in sftps]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            raise errors[0]

    def _get(self, sftp, remote, local, size, cancel=None):
        """remote -> local via <local>.part, resuming at the .part's size; verified by size. On
        cancel the .part stays for the next attempt to resume."""
        if size is None:
            size = sftp.stat(remote).st_size
        if local.exists() and local.stat().st_size == size:
            return  # fetched in full on an earlier pass
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(local.suffix + ".part")
        off = 0
        if tmp.exists():
            st = tmp.stat()
            off = st.st_size
            # corrupt (longer than the file), or sparse -- a segmented pull's preallocated .part
            # whose map is gone, where the size says nothing about what was written: never resume
            if off > size or st.st_blocks * 512 < off:
                tmp.unlink()
                off = 0
        if off:
            log(f"    resuming {local.name} at {off:,} B")
        t0 = time.time()
        if off < size:
            opened = []
            try:
                with _open_remote(sftp, remote) as rf, open(tmp, "ab") as out:
                    opened.append(rf)
                    rsize = getattr(rf, "MAX_REQUEST_SIZE", READ_SIZE)
                    rf.seek(off)
                    # prefetch's argument is the file's SIZE (it reads from the current offset to
                    # it); the request cap bounds memory on the Jetson.
                    rf.prefetch(size, max_concurrent_requests=PREFETCH_REQUESTS)
                    while not (cancel is not None and cancel.is_set()):
                        chunk = rf.read(rsize)
                        if not chunk:
                            break
                        out.write(chunk)
            except Exception:
                _abandon([sftp], opened)
                raise
            if cancel is not None and cancel.is_set():
                _abandon([sftp], opened)
                return
        got = tmp.stat().st_size
        if got != size:
            raise RuntimeError(f"{local.name}: got {got:,} B, expected {size:,} B")
        tmp.rename(local)
        secs = max(time.time() - t0, 1e-6)
        log(f"    downloaded {local.name} ({size:,} bytes, {(size - off) / secs / 1e6:.1f} MB/s)")
