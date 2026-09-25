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
from torrentfile import magnet_meta, torrent_meta

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
# Parallel SFTP streams per large file, each on its own SSH connection (= its own TCP stream):
# one stream Helsinki -> Jetson measured 12.1 MB/s (window/RTT-bound), so throughput scales with
# streams until the link or the box's disk saturates. The box allows 10 sessions/connection and
# MaxStartups 60; Pulsed's KB suggests 5-8 threads at most.
PULL_SEGMENTS = int(os.environ.get("PULL_SEGMENTS", "4"))
SEGMENT_MIN = int(float(os.environ.get("PULL_SEGMENT_MIN_MB", "64")) * 1024**2)  # smaller files: 1 stream
QUOTA_REFRESH = 120
DEFAULT_RULE = {"hours": 336.0, "ratio": None}  # a held rec that somehow lacks its rule
CHUNK = 1 << 20
# Brief states where progress is 1.0 but the files aren't settled yet.
SETTLING = {"moving", "checkingUP", "checkingResumeData", "allocating", "checkingDL"}
STOPPED_UP = {"pausedUP", "stoppedUP", "queuedUP"}
STOPPED_DL = {"pausedDL", "stoppedDL", "queuedDL"}
BROKEN = {"error", "missingFiles", "unknown"}


def log(msg):
    clog("seedbox", msg)


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
        t = paramiko.Transport(sock)
        try:
            t.banner_timeout = 30
            t.start_client(timeout=30)
            key = t.get_remote_server_key()
            if pinned.get(key.get_name()) != key:
                raise RuntimeError(f"seedbox SSH host key CHANGED for {HOST}; refusing to connect")
            t.auth_publickey(USER, paramiko.Ed25519Key.from_private_key(io.StringIO(vault.get("SEEDBOX_SSH_KEY_SFTP"))))
            sftp = paramiko.SFTPClient.from_transport(t)
            sftp.get_channel().settimeout(SFTP_TIMEOUT)
            return t, sftp
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
        pending = sum(max(0, t.get("amount_left", 0)) for t in self._info(tag=TAG))
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
        if btih and self._info([btih]):
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
            if btih and self._info([btih]):
                raise Exists()
            raise RuntimeError(f"qBittorrent refused the add ({body[:60]!r})")
        for _ in range(10):  # adds are asynchronous for magnets
            if not btih or self._info([btih]):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("qBittorrent accepted the add but doesn't list the torrent")
        self.reserved += need
        # Titled after the *arr's release name (the drop): supersede's movie_key and the logs work
        # from it. The torrent's own name is kept too.
        return {"title": drop.stem, "torrent_hash": btih, "torrent_name": tname, "size": size}

    def locate(self, btih, title):
        try:
            ts = self._info([btih]) if btih else []
        except Exception as e:
            log(f"    already on the seedbox but listing it failed ({e}); will retry next loop")
            return None
        if not ts:
            log("    seedbox refused a duplicate but doesn't list it; will retry next loop")
            return None
        t = ts[0]
        where = "complete" if t.get("progress", 0) >= 1 else f"in progress {t.get('progress', 0) * 100:.0f}%"
        return {"torrent_hash": btih, "torrent_name": t.get("name")}, where

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

    def pull(self, rec, plan, dest_dir, cancel=None):
        """Pull worker thread: every file of the plan over SFTP, big ones in PULL_SEGMENTS
        parallel streams, each stream on its own SSH connection."""
        conns = []
        try:
            for f in plan["files"]:
                if cancel is not None and cancel.is_set():
                    return
                local = dest_dir.joinpath(*f["rel"])
                size = f["size"]
                n = PULL_SEGMENTS if size and size >= SEGMENT_MIN else 1
                while len(conns) < n:
                    conns.append(self.remote.open_sftp())
                if n == 1 and not local.with_suffix(local.suffix + ".part.map").exists():
                    self._get(conns[0][1], f["remote"], local, size)
                else:
                    self._get_segmented([c[1] for c in conns[:max(n, 1)]], f["remote"], local, size, cancel)
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
        """remote -> local split into len(sftps) byte ranges pulled in parallel, written in place
        (os.pwrite) into a preallocated <local>.part. Progress per range is kept in
        <local>.part.map, so a restart resumes every range where it stopped."""
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
            mapf.write_text(json.dumps({"size": size, "segs": segs}))
        todo = [s for s in segs if s[2] < s[1]]
        done0 = sum(s[2] - s[0] for s in segs)
        if done0:
            log(f"    resuming {local.name} at {done0:,} of {size:,} B")
        lock = threading.Lock()
        errors = []
        fd = os.open(tmp, os.O_RDWR)
        t0 = time.time()

        def save():
            with lock:
                mapf.write_text(json.dumps({"size": size, "segs": segs}))

        def worker(seg, sftp):
            try:
                with sftp.open(remote, "rb") as rf:
                    rf.seek(seg[2])
                    rf.prefetch(seg[1], max_concurrent_requests=PREFETCH_REQUESTS)  # arg = END offset
                    since_save = 0
                    while seg[2] < seg[1]:
                        if cancel is not None and cancel.is_set():
                            return
                        chunk = rf.read(min(CHUNK, seg[1] - seg[2]))
                        if not chunk:
                            raise RuntimeError(f"short read at {seg[2]:,}")
                        os.pwrite(fd, chunk, seg[2])
                        seg[2] += len(chunk)
                        since_save += len(chunk)
                        if since_save >= 64 * CHUNK:
                            save()
                            since_save = 0
            except Exception as e:
                errors.append(e)

        try:
            threads = [threading.Thread(target=worker, args=(seg, sftps[i % len(sftps)]), daemon=True)
                       for i, seg in enumerate(todo)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        finally:
            os.close(fd)
            save()
        if errors:
            raise RuntimeError(f"{local.name}: {errors[0]}")
        if cancel is not None and cancel.is_set():
            return
        if any(s[2] < s[1] for s in segs) or tmp.stat().st_size != size:
            raise RuntimeError(f"{local.name}: incomplete after pull")
        tmp.rename(local)
        mapf.unlink(missing_ok=True)
        secs = max(time.time() - t0, 1e-6)
        log(f"    downloaded {local.name} ({size:,} bytes, {len(todo)} streams, {(size - done0) / secs / 1e6:.1f} MB/s)")

    def _get(self, sftp, remote, local, size):
        """remote -> local via <local>.part, resuming at the .part's size; verified by size."""
        if size is None:
            size = sftp.stat(remote).st_size
        if local.exists() and local.stat().st_size == size:
            return  # fetched in full on an earlier pass
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(local.suffix + ".part")
        off = tmp.stat().st_size if tmp.exists() else 0
        if off > size:  # corrupt, not merely short -> don't resume onto it
            tmp.unlink()
            off = 0
        if off:
            log(f"    resuming {local.name} at {off:,} B")
        t0 = time.time()
        if off < size:
            with sftp.open(remote, "rb") as rf, open(tmp, "ab") as out:
                rf.seek(off)
                # prefetch's argument is the file's SIZE (it reads from the current offset to it);
                # the request cap bounds memory on the Jetson.
                rf.prefetch(size, max_concurrent_requests=PREFETCH_REQUESTS)
                while True:
                    chunk = rf.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
        got = tmp.stat().st_size
        if got != size:
            raise RuntimeError(f"{local.name}: got {got:,} B, expected {size:,} B")
        tmp.rename(local)
        secs = max(time.time() - t0, 1e-6)
        log(f"    downloaded {local.name} ({size:,} bytes, {(size - off) / secs / 1e6:.1f} MB/s)")
