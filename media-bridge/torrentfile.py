"""
.torrent / magnet helpers shared by the bridge and its backends (no network, no state).
"""

import base64
import hashlib
import re
import urllib.parse


def _bdecode(data, i):
    """Minimal bencode decoder -> (value, next_index). Bytes stay bytes."""
    c = data[i : i + 1]
    if c == b"i":
        e = data.index(b"e", i)
        return int(data[i + 1 : e]), e + 1
    if c == b"l":
        i += 1
        lst = []
        while data[i : i + 1] != b"e":
            v, i = _bdecode(data, i)
            lst.append(v)
        return lst, i + 1
    if c == b"d":
        i += 1
        d = {}
        while data[i : i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            d[k] = v
        return d, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        ln = int(data[i:colon])
        start = colon + 1
        return data[start : start + ln], start + ln
    raise ValueError(f"bad bencode byte {c!r} at offset {i}")


def read_torrent(path):
    """(raw info-dict bytes, decoded info dict, tracker URLs) of a .torrent file."""
    data = path.read_bytes()
    if data[:1] != b"d":
        raise ValueError("not a bencoded dict")
    i = 1
    info_raw = info = None
    trackers = []
    while data[i : i + 1] != b"e":
        k, i = _bdecode(data, i)
        if k == b"info":
            start = i
            info, i = _bdecode(data, i)
            info_raw = data[start:i]
        else:
            v, i = _bdecode(data, i)
            if k == b"announce" and isinstance(v, bytes):
                trackers.append(v)
            elif k == b"announce-list" and isinstance(v, list):
                trackers += [t for tier in v for t in tier if isinstance(t, bytes)]
    if info_raw is None:
        raise ValueError("no info dict")
    return info_raw, info, trackers


def torrent_is_private(path):
    # BEP 27: info['private'] == 1 marks a private-tracker torrent.
    return read_torrent(path)[1].get(b"private") == 1


def magnet_from_torrent(path):
    """Build a magnet URI from a .torrent file. The file itself is what gets uploaded; the
    magnet is kept for its btih, which duplicate/409 matching keys on. btih = SHA1 of the
    *raw* bencoded info-dict bytes (re-encoding could reorder keys and change the hash),
    name from info['name'], trackers from announce/announce-list."""
    info_raw, info, trackers = read_torrent(path)
    name = info.get(b"name")
    btih = hashlib.sha1(info_raw).hexdigest()
    dn = (name or path.stem.encode()).decode("utf-8", "replace")
    q = urllib.parse.quote_plus
    parts = [f"xt=urn:btih:{btih}", f"dn={q(dn)}"]  # xt urn must stay literal
    parts += [f"tr={q(t.decode('utf-8', 'replace'))}" for t in trackers]
    return "magnet:?" + "&".join(parts)


def magnet_btih(magnet):
    """Infohash from a magnet's xt=urn:btih: (hex, lowercased). Base32 form is converted."""
    m = re.search(r"xt=urn:btih:([0-9a-zA-Z]+)", magnet or "")
    if not m:
        return ""
    h = m.group(1)
    if len(h) == 32:
        try:
            h = base64.b32decode(h.upper()).hex()
        except Exception:
            return ""
    return h.lower()


def torrent_meta(path):
    """(btih, name, total bytes, tracker hosts) of a .torrent file."""
    info_raw, info, trackers = read_torrent(path)
    if b"length" in info:
        size = info[b"length"]
    else:
        size = sum(f.get(b"length", 0) for f in info.get(b"files", []))
    name = (info.get(b"name") or b"").decode("utf-8", "replace")
    hosts = [_host(t.decode("utf-8", "replace")) for t in trackers]
    return hashlib.sha1(info_raw).hexdigest(), name, size, [h for h in hosts if h]


def magnet_meta(magnet):
    """(btih, name, bytes or None, tracker hosts) of a magnet URI."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    xl = (q.get("xl") or [""])[0]
    size = int(xl) if xl.isdigit() else None
    hosts = [_host(t) for t in q.get("tr", [])]
    return magnet_btih(magnet), (q.get("dn") or [""])[0], size, [h for h in hosts if h]


def _host(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
