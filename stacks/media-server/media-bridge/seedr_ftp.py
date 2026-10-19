"""
Seedr FTP(S) fallback transport for seedr.py's downloads.

Seedr's HTTPS download (GET /rest/file/{id} -> 302 -> rd*.seedr.cc CDN) sometimes 404s
persistently on one file of a finished transfer while the bytes sit fine in Seedr's
storage (hit 2026-09-23: Heart Beat S03E08 404'd on every poll, then FETCH_TIMEOUT would
have discarded the whole pack). FTP reads that storage directly, so it serves those files,
at the same speed (~38 MB/s measured). It also resumes at a byte offset (REST), which the
HTTPS path cannot: a dropped connection there restarts the file from 0.

FTP only moves bytes -- adding magnets, progress, completion and deletes stay on the REST
API. Paths mirror Seedr's folder tree: /<top folder path>/<sub>/<file name>.
Explicit FTPS (AUTH TLS + PROT P). Disabled when SEEDR_FTP_HOST is empty.
"""

import base64
import ftplib
import os
import pathlib

from common import clog

HOST = os.environ.get("SEEDR_FTP_HOST", "")
USER = os.environ.get("SEEDR_FTP_USER") or os.environ.get("SEEDR_EMAIL", "")
# Dedicated FTP password (Seedr Account -> Security) if set, else the account password.
# base64 for the same '$'-interpolation reason as SEEDR_PASSWORD_B64.
_pw_b64 = os.environ.get("SEEDR_FTP_PASSWORD_B64") or os.environ.get(
    "SEEDR_PASSWORD_B64"
)
PASSWORD = base64.b64decode(_pw_b64).decode() if _pw_b64 else ""
RETRIES = int(os.environ.get("SEEDR_FTP_RETRIES", "3"))  # reconnects per file, each resuming


def log(msg):
    clog("seedr", msg)


def enabled():
    return bool(HOST and USER and PASSWORD)


def _connect():
    f = ftplib.FTP_TLS(HOST, timeout=120)
    f.login(USER, PASSWORD)
    f.prot_p()
    f.encoding = "utf-8"
    return f


def download(remote_path, tmp: pathlib.Path):
    """Fetch remote_path into tmp, appending from tmp's current size (REST) so a partial
    left by an earlier attempt -- HTTPS or FTP -- is continued, not restarted. Reconnects
    up to RETRIES times on a dropped connection."""
    size = lambda: tmp.stat().st_size if tmp.exists() else 0  # noqa: E731
    for attempt in range(1, RETRIES + 1):
        off, f = size(), None
        try:
            f = _connect()
            with open(tmp, "ab") as out:
                f.retrbinary(
                    f"RETR {remote_path}", out.write, blocksize=1 << 20, rest=off or None
                )
            return
        except (OSError, EOFError, ftplib.Error) as e:
            # A 5xx before any byte moved (550 no such file) can't fix itself -> raise now.
            # But Seedr ALSO reports a dropped data connection as '550 Issue during
            # transfer', so a 5xx after progress is a drop like any other -> resume.
            if attempt == RETRIES or (isinstance(e, ftplib.error_perm) and size() == off):
                raise
            log(f"    FTP dropped on {tmp.name} at {size():,} B ({e}); resuming")
        finally:
            # close(), not quit(): QUIT reads the pending reply, and after a drop that is
            # the 550 above -- which would replace the real error
            if f is not None:
                f.close()
