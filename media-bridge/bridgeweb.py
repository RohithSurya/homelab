"""
Status page for the blackhole bridge, served by the youtube component's HTTP server on :8003
(LAN-only, like the other host-published feeds -- never exposed through Caddy; it shows private
tracker names). Same process as the bridge, so it reads the pull worker's live jobs and each
backend's last poll snapshot directly: rendering a page never calls Seedr or the seedbox.

  /bridge                      where every transfer is now and until when; waiting drops;
                               seeding holds; last 7 days; dry-run rule decisions
  /bridge/status.json          the same data, for Hermes/scripts
  /bridge/transfer/<tid>       one transfer's full event timeline

Every name shown comes from scraped sites -> html.escape everything; the response carries a
strict Content-Security-Policy (no scripts at all).
"""

import html
import json
import time
import urllib.parse

import backends
import bridge
import bridgelog as ev
import puller
from backends import follow

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"
TZ = time.strftime("%Z")
E = html.escape


def _abs(ts):
    return time.strftime("%a %m-%d %H:%M", time.localtime(ts)) + f" {TZ}"


def _rel(ts):
    d = ts - time.time()
    s = ev._dur(abs(d))
    return f"in {s}" if d >= 0 else f"{s} ago"


def _when(ts):
    return {"at": ts, "at_text": _abs(ts), "rel": _rel(ts)} if ts else None


def _seed_margin_h():
    try:
        from backends import qbit
        return qbit.SEED_MARGIN_HOURS
    except Exception:
        return 3


def _snapshot(rec):
    """The backend's last poll data for this transfer (qBittorrent torrent dict, Seedr entry)."""
    be = backends._instances.get(rec.get("backend", "seedr"))
    if be is None:
        return {}
    h = (rec.get("torrent_hash") or "").lower()
    if hasattr(be, "torrents") and isinstance(be.torrents, dict) and h in be.torrents:
        return be.torrents[h]
    by_id = getattr(be, "torrents_by_id", {})
    return by_id.get(rec.get("user_torrent_id")) or {}


def _transfer(rec):
    now = time.time()
    snap = _snapshot(rec)
    rel = rec.get("release_name") or rec.get("title")
    out = {
        "tid": rec.get("tid"), "release": rel, "category": rec.get("category"),
        "backend": rec.get("backend", "seedr"), "private": bool(rec.get("private")),
        "tracker_rule": (rec.get("seed_rule") or {}).get("key"), "added": _when(rec.get("added")),
        "phase": None, "detail": None, "progress": rec.get("last_progress"), "eta": None, "eta_reason": None,
        "deadline": None, "speed_mb_s": None, "flags": [k for k in ("slow_flagged", "failfast_flagged") if rec.get(k)],
    }
    if rec.get("held"):
        out["phase"] = "seeding_hold"
        rule = rec.get("seed_rule") or {}
        if rec.get("hold_until"):
            out["eta"], out["detail"] = _when(rec["hold_until"]), "Seedr hold (fixed)"
        elif rec.get("no_deliver"):
            out["eta"], out["detail"] = _when(rec.get("hold_max_until")), f"undelivered partial ({rec.get('partial_reason')}), cap"
        elif snap:
            need = (rule.get("hours", 336) + _seed_margin_h()) * 3600
            left = max(0, need - snap.get("seeding_time", 0))
            out["eta"] = _when(now + left)
            out["detail"] = (f"{rule.get('key', '?')}: seeded {snap.get('seeding_time', 0) / 3600:.1f}h of "
                             f"{rule.get('hours', '?')}h, ratio {snap.get('ratio', 0):.2f}"
                             + (f" (or ratio {rule['ratio']})" if rule.get("ratio") else "")
                             + (f", {snap.get('state')}" if snap.get("state") not in ("uploading", "stalledUP") else ""))
        else:
            out["eta_reason"] = "backend hasn't reported it this loop"
    elif rec.get("superseded"):
        out["phase"] = "cleaning_up"
        out["detail"] = next((k for k in ("stalled", "missing_result", "unfetchable", "slow", "dead") if rec.get(k)),
                             f"superseded by {rec.get('new_title')}")
    elif rec.get("unavailable_since"):
        out["phase"] = "unavailable"
        out["detail"] = f"not visible on its backend since {_abs(rec['unavailable_since'])}"
    elif rec.get("pulling"):
        out["phase"] = "pulling"
        job = puller.get(rec.get("tid") or "")
        size = rec.get("size")
        stage = bridge.INCOMING / rec["category"] / rel
        got = puller._staged_bytes(stage) if stage.exists() else 0
        out["progress"] = round(100 * got / size, 1) if size else None
        if job and job.status == "running" and job.started:
            spd = max(0, got - job.before) / max(now - job.started, 1)  # this run only, not a resume's head start
            out["speed_mb_s"] = round(spd / 1e6, 1)
            if size and spd > 0:
                out["eta"] = _when(now + (size - got) / spd)
        out["detail"] = f"{got / 2**30:.2f} GiB" + (f" of {size / 2**30:.2f} GiB" if size else "") + (
            f" -- {job.status}" if job else " -- waiting for the pull worker")
    elif rec.get("fetch_failing_since"):
        out["phase"], out["deadline"] = "fetch_retrying", _when(rec["fetch_failing_since"] + bridge.FETCH_TIMEOUT)
    elif rec.get("result_missing_since"):
        out["phase"], out["deadline"] = "result_missing", _when(rec["result_missing_since"] + bridge.RESULT_TIMEOUT)
    else:
        out["phase"] = "downloading" if rec.get("last_progress") is not None else "queued"
        spd = snap.get("dlspeed") or snap.get("download_rate")
        if spd:
            out["speed_mb_s"] = round(float(spd) / 1e6, 2)
        qeta = snap.get("eta")
        samples = rec.get("rate") or []
        if qeta and qeta < 8640000:
            out["eta"] = _when(now + qeta)
        elif len(samples) >= 2 and samples[-1][1] > samples[0][1] and rec.get("last_progress") is not None:
            rate = (samples[-1][1] - samples[0][1]) / max((samples[-1][0] - samples[0][0]) / 3600, 1e-6)
            out["eta"] = _when(now + (100 - rec["last_progress"]) / rate * 3600)
        else:
            out["eta_reason"] = "no rate yet"
        stall_at = rec.get("last_progress_at", rec.get("added", now)) + bridge.STALL_TIMEOUT
        if now - rec.get("last_progress_at", now) > 600:
            out["deadline"] = _when(stall_at)
        seeds = snap.get("num_complete", snap.get("seeders"))
        if seeds is not None:
            out["detail"] = f"swarm seeds {seeds}, connected {snap.get('num_seeds', snap.get('connected_to', '?'))}"
        stage = bridge.INCOMING / rec["category"] / rel
        if (stage / follow.STATE).exists():  # follow mode: finished pieces already copied home
            got = puller._staged_bytes(stage)
            out["followed_bytes"] = got
            home = f"home {got / 2**30:.2f} GiB" + (f" of {rec['size'] / 2**30:.2f}" if rec.get("size") else "")
            out["detail"] = f"{out['detail']}; {home}" if out.get("detail") else home
    return out


def status():
    now = time.time()
    active, holds = [], []
    if bridge.STATE_DIR.exists():
        for sp, rec in bridge.load_records():
            t = _transfer(rec)
            (holds if t["phase"] == "seeding_hold" else active).append(t)
    waiting = []
    for cat in bridge.CATEGORIES:
        for f in sorted((bridge.DATA / "blackhole" / cat).glob("*")):
            if f.suffix in (".magnet", ".torrent", ".txt"):
                waiting.append({"release": f.stem, "category": cat,
                                "reason": bridge.DROP_STATE.get(f.name, "not picked up yet"),
                                "since": _when(f.stat().st_mtime)})
    be_rows = []
    for name, be in list(backends._instances.items()):
        row = {"name": name, "up": bridge.BACKEND_UP.get(name, True)}
        q = getattr(be, "_quota", None)
        if q and q[0] is not None:
            row["quota_used_gib"], row["quota_gib"] = round(q[0] / 2**30, 1), round(q[1] / 2**30)
        if name in bridge.BREAKER_UNTIL and bridge.BREAKER_UNTIL[name] > now:
            row["breaker_until"] = _when(bridge.BREAKER_UNTIL[name])
        be_rows.append(row)
    recent, dry = [], []
    try:
        db = ev._db()
        for ts, tid, type_, release, detail in db.execute(
            "SELECT ts, tid, type, release, detail FROM events WHERE ts > ? AND type IN "
            "('delivered','failed','hold_ended','gave_up','dropped','partial_kept') ORDER BY ts DESC LIMIT 60",
            (now - 7 * 86400,),
        ):
            recent.append({"when": _when(ts), "tid": tid, "type": type_, "release": release,
                           "detail": json.loads(detail) if detail else {}})
        for ts, tid, type_, release, detail in db.execute(
            "SELECT ts, tid, type, release, detail FROM events WHERE ts > ? AND type LIKE 'would_%' ORDER BY ts DESC LIMIT 40",
            (now - 14 * 86400,),
        ):
            dry.append({"when": _when(ts), "tid": tid, "type": type_, "release": release,
                        "detail": json.loads(detail) if detail else {}})
    except Exception:
        pass
    return {"generated_at": _when(now), "backends": be_rows, "active": active, "waiting": waiting,
            "holds": holds, "recent": recent, "dry_run": dry,
            "rules": {"slow": bridge.SLOW_RULE, "slow_dry_run": bridge.SLOW_DRY_RUN,
                      "failfast": bridge.FAILFAST, "failfast_dry_run": bridge.FAILFAST_DRY_RUN,
                      "give_up_after": bridge.GIVE_UP_AFTER, "give_up_dry_run": bridge.GIVE_UP_DRY_RUN}}


# --- HTML --------------------------------------------------------------------------------------------

STYLE = """
body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;background:#fafafa;color:#1a1a1a}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 6px;color:#333}
table{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 2px #0001}
td,th{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
th{font-weight:600;font-size:12px;color:#555;background:#f3f3f3}
.muted{color:#777;font-size:12px}.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:12px;background:#eee}
.bad{background:#fde2e2}.ok{background:#e0f3e3}.warn{background:#fff1cc}.bar{height:6px;background:#eee;border-radius:3px}
.bar>i{display:block;height:6px;border-radius:3px;background:#4a8}
a{color:#0b61a4;text-decoration:none}
@media (prefers-color-scheme:dark){body{background:#151515;color:#e6e6e6}table{background:#1e1e1e}
th{background:#262626;color:#bbb}td,th{border-color:#2c2c2c}.pill{background:#333}.bad{background:#5a2323}
.ok{background:#1f4a2a}.warn{background:#5a4a14}.bar{background:#333}a{color:#6cb6ff}.muted{color:#999}}
"""


def _t(w):
    return f'{E(w["rel"])}<div class="muted">{E(w["at_text"])}</div>' if w else "&mdash;"


def _bar(p):
    if p is None:
        return ""
    return f'<div class="bar"><i style="width:{max(0, min(100, p)):.0f}%"></i></div>'


def page():
    s = status()
    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta http-equiv=refresh content=30><title>Download bridge</title><style>{STYLE}</style>",
           f"<h1>Download bridge</h1><div class=muted>updated {E(s['generated_at']['at_text'])} &middot; "
           "refreshes every 30s &middot; <a href=/bridge/status.json>status.json</a></div>"]
    pills = []
    for b in s["backends"]:
        q = f" &middot; {b['quota_used_gib']}/{b['quota_gib']} GiB" if "quota_gib" in b else ""
        br = f" &middot; auto-fail paused {E(b['breaker_until']['rel'])}" if b.get("breaker_until") else ""
        pills.append(f"<span class='pill {'ok' if b['up'] else 'bad'}'>{E(b['name'])}: {'up' if b['up'] else 'DOWN'}{q}{br}</span>")
    out.append("<p>" + " ".join(pills) + "</p>")

    out.append(f"<h2>In progress ({len(s['active'])})</h2>")
    if s["active"]:
        out.append("<table><tr><th>Release</th><th>Where</th><th>Progress</th><th>Until</th><th>Deadline</th></tr>")
        for t in s["active"]:
            prog = "" if t["progress"] is None else f"{t['progress']}%"
            flags = "".join(f" <span class='pill warn'>{E(f.split('_')[0])}</span>" for f in t["flags"])
            sp = f" &middot; {t['speed_mb_s']} MB/s" if t.get("speed_mb_s") else ""
            out.append(
                f"<tr><td><a href='/bridge/transfer/{E(t['tid'] or '')}'>{E(t['release'])}</a>"
                f"<div class=muted>{E(t['category'])} &middot; {E(t['backend'])}"
                f"{' &middot; private: ' + E(t['tracker_rule'] or '') if t['private'] else ''}{flags}</div></td>"
                f"<td>{E(t['phase'])}<div class=muted>{E(t['detail'] or '')}{sp}</div></td>"
                f"<td>{prog}{_bar(t['progress'])}</td>"
                f"<td>{_t(t['eta']) if t['eta'] else E(t['eta_reason'] or '&mdash;')}</td><td>{_t(t['deadline'])}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class=muted>nothing downloading or pulling</p>")

    if s["waiting"]:
        out.append(f"<h2>Waiting in the blackhole ({len(s['waiting'])})</h2><table><tr><th>Release</th><th>Why</th><th>Since</th></tr>")
        for w in s["waiting"]:
            out.append(f"<tr><td>{E(w['release'])}<div class=muted>{E(w['category'])}</div></td>"
                       f"<td>{E(w['reason'])}</td><td>{_t(w['since'])}</td></tr>")
        out.append("</table>")

    out.append(f"<h2>Seeding holds ({len(s['holds'])})</h2>")
    if s["holds"]:
        out.append("<table><tr><th>Release</th><th>Rule / progress</th><th>Ends</th></tr>")
        for t in s["holds"]:
            out.append(f"<tr><td><a href='/bridge/transfer/{E(t['tid'] or '')}'>{E(t['release'])}</a>"
                       f"<div class=muted>{E(t['backend'])}</div></td><td>{E(t['detail'] or '')}</td><td>{_t(t['eta'])}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class=muted>none</p>")

    out.append("<h2>Last 7 days</h2>")
    if s["recent"]:
        out.append("<table><tr><th>When</th><th>What</th><th>Release</th><th>Detail</th></tr>")
        for r in s["recent"]:
            d = r["detail"]
            if r["type"] == "delivered":
                txt = f"{d.get('bytes', 0) / 1e9:.2f} GB in {ev._dur(d.get('secs', 0))} ({d.get('mb_s')} MB/s)"
            else:
                txt = ", ".join(f"{k}={v}" for k, v in d.items() if k not in ("keys",))
            cls = "ok" if r["type"] in ("delivered", "hold_ended") else "bad" if r["type"] in ("failed", "gave_up", "dropped") else "warn"
            out.append(f"<tr><td>{_t(r['when'])}</td><td><span class='pill {cls}'>{E(r['type'])}</span></td>"
                       f"<td><a href='/bridge/transfer/{E(r['tid'] or '')}'>{E(r['release'] or '')}</a></td><td class=muted>{E(txt)}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class=muted>nothing yet</p>")

    rules = s["rules"]
    out.append("<h2>Rule decisions (dry-run)</h2><p class=muted>"
               f"slow rule: {E(rules['slow'])}{' (dry-run)' if rules['slow_dry_run'] else ' LIVE'} &middot; "
               f"fail-fast: {E(rules['failfast'])}{' (dry-run)' if rules['failfast_dry_run'] else ' LIVE'} &middot; "
               f"give up after {rules['give_up_after']}{' (dry-run)' if rules['give_up_dry_run'] else ' LIVE'}</p>")
    if s["dry_run"]:
        out.append("<table><tr><th>When</th><th>Would</th><th>Release</th><th>Why</th></tr>")
        for r in s["dry_run"]:
            out.append(f"<tr><td>{_t(r['when'])}</td><td>{E(r['type'].replace('would_', ''))}</td>"
                       f"<td><a href='/bridge/transfer/{E(r['tid'] or '')}'>{E(r['release'] or '')}</a></td>"
                       f"<td class=muted>{E(str(r['detail'].get('reason', r['detail'])))}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class=muted>no decisions yet</p>")
    return "".join(out)


def timeline_page(tid):
    rows = ev._db().execute(
        "SELECT ts, type, phase, release, backend, detail FROM events WHERE tid=? ORDER BY ts", (tid,)).fetchall()
    title = rows[0][3] if rows else tid
    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{E(title or '')}</title><style>{STYLE}</style><p><a href=/bridge>&larr; bridge</a></p>"
           f"<h1>{E(title or '')}</h1>"]
    if not rows:
        out.append("<p class=muted>no events</p>")
    else:
        out.append("<table><tr><th>When</th><th>Event</th><th>Phase</th><th>Detail</th></tr>")
        for ts, type_, phase, release, backend, detail in rows:
            d = json.loads(detail) if detail else {}
            txt = ", ".join(f"{k}={v}" for k, v in d.items())
            out.append(f"<tr><td>{E(_abs(ts))}<div class=muted>{E(_rel(ts))}</div></td><td>{E(type_)}</td>"
                       f"<td>{E(phase or '')}</td><td class=muted>{E(txt)}</td></tr>")
        out.append("</table>")
    return "".join(out)


def handle(path):
    """(status, content-type, body) for a /bridge... GET."""
    p = path.split("?")[0].rstrip("/")
    if p == "/bridge":
        return 200, "text/html; charset=utf-8", page()
    if p == "/bridge/status.json":
        return 200, "application/json", json.dumps(status(), default=str, indent=1)
    if p.startswith("/bridge/transfer/"):
        # parameterised query + escaped output: any id is safe to look up
        tid = urllib.parse.unquote(p[len("/bridge/transfer/"):])
        return 200, "text/html; charset=utf-8", timeline_page(tid)
    return 404, "text/plain", "not found"
