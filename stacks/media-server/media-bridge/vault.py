"""
Secrets from Bitwarden Secrets Manager (cloud), through the `bws` CLI baked into the image.

The only secret kept on the Jetson is the machine account's access token, in /config/bws_token
(media-bridge/config, mode 600, gitignored). Values are fetched on demand, cached in memory for
VAULT_TTL seconds, and never logged or written to disk. A vault outage must only affect callers
that need a secret (tracker checks, seedbox setup) -- never transfers already running -- so every
caller handles VaultError.

Named vault.py, not secrets.py: that would shadow the stdlib `secrets` module.

  python -m vault check    # which expected secret names exist (never prints values)
"""

import json
import os
import subprocess
import sys
import threading
import time

TOKEN_FILE = os.environ.get("BWS_TOKEN_FILE", "/config/bws_token")
SERVER = os.environ.get("BWS_SERVER_URL", "")  # empty = try US, then EU
TTL = int(os.environ.get("VAULT_TTL", "3600"))
SERVERS = ("https://vault.bitwarden.com", "https://vault.bitwarden.eu")

# Names the bridge looks up; `check` reports which are present.
EXPECTED = [
    "SEEDBOX_ACCOUNT_PASSWORD",
    "TRACKER_HDSPACE_USERNAME", "TRACKER_HDSPACE_PASSWORD",
    "TRACKER_IPT_USERNAME", "TRACKER_IPT_PASSWORD", "TRACKER_IPT_COOKIE", "TRACKER_IPT_USERAGENT",
    "TRACKER_TORRENTHAVEN_USERNAME", "TRACKER_TORRENTHAVEN_PASSWORD", "TRACKER_TORRENTHAVEN_APIKEY",
    "TRACKER_CZTEAM_USERNAME", "TRACKER_CZTEAM_PASSWORD", "TRACKER_CZTEAM_PASSKEY",
    "TRACKER_RETROTOON_USERNAME", "TRACKER_RETROTOON_PASSWORD", "TRACKER_RETROTOON_APIKEY",
    "TRACKER_DOCSPEDIA_USERNAME", "TRACKER_DOCSPEDIA_PASSWORD", "TRACKER_DOCSPEDIA_COOKIE",
]


class VaultError(Exception):
    pass


_lock = threading.Lock()
_cache = {}  # key -> {"id", "value", "projectId"}
_loaded_at = 0.0
_server = SERVER or None


def _token():
    try:
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
    except OSError as e:
        raise VaultError(f"no access token at {TOKEN_FILE} ({e.strerror})") from None
    if not tok:
        raise VaultError(f"access token file {TOKEN_FILE} is empty")
    return tok


def _run(args, server):
    tok = _token()
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",  # bws keeps its state under $HOME; the container user's home isn't writable
        "BWS_ACCESS_TOKEN": tok,
        "BWS_SERVER_URL": server,
    }
    # global options before the subcommand, so a trailing "--" in args still ends option parsing
    r = subprocess.run(
        ["bws", "--output", "json", "--color", "no", *args],
        env=env, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout).replace(tok, "<token>").strip().splitlines()
        raise VaultError(f"bws {' '.join(args[:2])} failed: {err[-1] if err else r.returncode}")
    try:
        return json.loads(r.stdout or "null")
    except ValueError:  # some subcommands (e.g. delete) print a plain confirmation, not JSON
        return r.stdout.strip()


def _list():
    """All secrets the machine account can see, from the first server that accepts the token."""
    global _server
    candidates = [_server] if _server else list(SERVERS)
    last = None
    for server in candidates:
        try:
            out = _run(["secret", "list"], server)
            _server = server
            return out or []
        except VaultError as e:
            last = e
    raise last


def _load(force=False):
    global _cache, _loaded_at
    with _lock:
        if not force and _cache and time.time() - _loaded_at < TTL:
            return _cache
        _cache = {
            s["key"]: {"id": s["id"], "value": s.get("value", ""), "projectId": s.get("projectId")}
            for s in _list()
        }
        _loaded_at = time.time()
        return _cache


def get(name):
    """The secret's value. Raises VaultError if the vault is unreachable or the name is missing."""
    c = _load()
    if name not in c:
        c = _load(force=True)  # added in the vault since the last load?
    if name not in c:
        raise VaultError(f"secret {name} is not in the vault")
    return c[name]["value"]


def has(name):
    try:
        return name in _load()
    except VaultError:
        return False


def names():
    return sorted(_load())


def server():
    return _server


def put(name, value, note=""):
    """Create or update a secret in the project the others live in (needs 'Can read, write')."""
    c = _load(force=True)
    # Values can start with '-' (e.g. "-----BEGIN OPENSSH PRIVATE KEY-----"), which bws's argument
    # parser would take for an option: pass --value=... in one token, and positionals after "--".
    if name in c:
        _run(["secret", "edit", f"--value={value}", "--", c[name]["id"]], _server)
    else:
        projects = {s["projectId"] for s in c.values() if s.get("projectId")}
        if len(projects) != 1:
            projects = {p["id"] for p in _run(["project", "list"], _server) or []}
        if len(projects) != 1:
            raise VaultError(f"expected exactly one project to write {name} into, found {len(projects)}")
        _run(["secret", "create", f"--note={note}", "--", name, value, projects.pop()], _server)
    _load(force=True)


def _check():
    try:
        have = set(names())
    except VaultError as e:
        print(f"vault: NOT reachable: {e}")
        return 1
    print(f"vault: OK via {server()} -- {len(have)} secret(s) visible (values never printed)")
    for n in EXPECTED:
        print(f"  {'present' if n in have else 'missing'}  {n}")
    extra = sorted(have - set(EXPECTED))
    if extra:
        print("  other names: " + ", ".join(extra))
    return 0


if __name__ == "__main__":
    sys.exit(_check() if sys.argv[1:2] in ([], ["check"]) else 2)
