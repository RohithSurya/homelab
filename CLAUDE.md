# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted media automation stack defined by a single `docker-compose.yaml`, running on a
**Jetson Nano Super (aarch64)**. Pipeline: **search** (Prowlarr) → **automate** (Sonarr TV / Radarr
movies) → **cloud download** (Seedr) → **stream** (Jellyfin). The custom Python code is one service,
`media-bridge/` (five responsibilities in one container: blackhole bridge (Seedr/seedbox backends) + jellyfin-reaper + movierulz
scraper/Torznab feed + tamilmv scraper/Torznab feed + youtube grabber); everything else is an off-the-shelf image. Service config/state directories
(`prowlarr/`, `radarr/`, `sonarr/`, `bazarr/`, `jellyfin/`, `data/`, `media-bridge/config/`) are
runtime data and are gitignored — only the compose file and the custom service code are version-controlled.

**Public exposure is NOT in this repo.** Caddy (TLS + reverse proxy), DuckDNS and fail2ban live in
their own stack at `~/exposer`, so that this stack, `~/immich` and `~/next-cloud` are independently
restartable. Jellyfin needs nothing here to be reachable at `jellyfin.rosurya.duckdns.org`: exposer's
Caddy proxies to `127.0.0.1:8096` (Jellyfin is `network_mode: host`) and exposer's fail2ban tails
`./jellyfin/config/log` by absolute path. To change how Jellyfin is exposed, edit
`~/exposer/caddy/sites.d/jellyfin.caddy` — see `~/exposer/CLAUDE.md`.

## Commands

```bash
docker compose up -d                              # bring up / apply compose changes
docker compose up -d --build media-bridge         # rebuild the custom service after editing its .py
docker compose logs -f media-bridge               # follow its logs ([bridge]/[seedr]/[reaper]/[movierulz]/[tamilmv]/[youtube] tags)
docker restart prowlarr flaresolverr sonarr radarr  # REQUIRED after any gluetun restart (see below)
```

There is no build/lint tooling — `media-bridge` is a small Python package run directly
(`CMD ["python", "-u", "main.py"]`, which starts its components as 11 supervised threads; 2 are pull workers). To
exercise a change, rebuild the service and watch its logs. The bridge has offline tests (stdlib
`unittest`, fakes only — **never run a BitTorrent client on the Jetson, not even for tests**; the ISP
blocks the user for it):
`docker run --rm --user 1000:1000 -v "$PWD/media-bridge:/app" -w /app --entrypoint python media-server-media-bridge -m unittest discover -s tests`
Secrets come from `.env` (gitignored); see `.env.example` for the required keys.

## Architecture: the VPN namespace

`gluetun` (Proton VPN, WireGuard, kill switch) is the network anchor. The **indexer-facing** apps —
`prowlarr` (9696), `flaresolverr` (8191), `sonarr` (8989), `radarr` (7878) — run with
`network_mode: "service:gluetun"`, so they share gluetun's network namespace and exit IP, and their
ports are published *on the gluetun service*, not their own. They also `depends_on` gluetun being
`service_healthy`.

Consequences that drive most edits here:
- **Adding a VPN-routed container**: give it `network_mode: "service:gluetun"`,
  `depends_on: gluetun (service_healthy)`, and publish its ports on the gluetun service block.
- **Restart gotcha**: restarting gluetun recreates its netns and silently breaks every dependent
  container. After any gluetun restart you MUST `docker restart prowlarr flaresolverr sonarr radarr`
  (or `docker compose down && up -d`). `media-bridge` and `jellyfin` are unaffected — and the
  `media-bridge` host-published feeds (movierulz Torznab `8002`, youtube grabber `8003`, tamilmv Torznab `8005`) keep serving,
  since they are published on the host, not on gluetun.
- **Recreate ≠ restart**: the dependents join gluetun by container ID (`container:<id>`), so after
  gluetun is *recreated* (compose change, image bump) `docker restart` can't fix them — recreate them
  too: `docker compose up -d --force-recreate --no-deps gluetun`, wait for healthy, then
  `docker compose up -d --force-recreate --no-deps prowlarr flaresolverr sonarr radarr`.
- **gluetun's built-in HTTP proxy** (`HTTPPROXY=on`, `:8888`, since 2026-09-30) lets off-VPN containers
  make individual requests from the VPN exit IP via `http://gluetun:8888` (tamilmv crawler:
  `TAMILMV_HTTP_PROXY`). Not published on the host — reachable only from docker networks.
- **Cross-service URLs inside the netns**: containers sharing gluetun's namespace talk to the *arr
  apps via the hostname **`gluetun`** (e.g. `http://gluetun:7878`), NOT `radarr`/`sonarr` (which don't
  resolve). `media-bridge` (on the default bridge net) reaches the *arr apps AND FlareSolverr the same
  way — `http://gluetun:7878/8989/8191`, since those ports are published on gluetun — and reaches
  host-net Jellyfin via `host.docker.internal` (`extra_hosts: host-gateway`).
- **FlareSolverr must share gluetun's exit IP** (it's in the same netns) because `cf_clearance`
  cookies are IP-bound. CF-protected indexers are wired with a `flaresolverr` tag in Prowlarr. The
  movierulz crawler also bypasses CF through this same FlareSolverr (it never fetches the site directly).

What runs **outside** the VPN: `media-bridge` (seedr does the torrenting in its cloud, so the Jetson
never joins a swarm; keeping it off-VPN also keeps Seedr CDN downloads direct/fast), `bazarr` and
`jellyfin` (both `network_mode: host` for LAN/DLNA/Tailscale reach; they hit the *arr apps on
`127.0.0.1` because gluetun publishes those ports on the host).

**`home-egress` (tinyproxy, host `:8006`) — the one per-indexer VPN exception.** Some private trackers
bind the login cookie to the IP it was issued to: **KrazyZone** (no opt-out; its login form has only
"Remember Me") answers the VPN exit IP with an empty 200 + `Refresh: account-login.php` +
`Set-Cookie: pass=deleted`, which Prowlarr reports as "Redirected to …account-login.php". Since the
browser login happens from the home IPv4, those indexers go through this proxy: Prowlarr indexer proxy
`home-egress` (HTTP `10.0.0.179:8006`) + tag `home-egress` on the indexer — Prowlarr only proxies
indexers that carry the tag. Everything else stays on the VPN. Guards: `home-egress/filter` is a
destination allowlist (`FilterDefaultDeny`; `prowlarr.servarr.com` is on it because Prowlarr won't save or
health-check a proxy that can't reach it), and `Allow 172.16.0.0/12` admits only docker-bridge sources
(gluetun's netns arrives as `172.22.0.1`), so LAN hosts get refused. Config is bind-mounted: edit
the filter and `docker restart home-egress`. If Comcast changes the home IPv4, the cookie dies — re-login in
the browser and paste the new cookie.

## The `/data` single-root convention

`./data` is mounted into sonarr, radarr, media-bridge, and bazarr at the same path so imports are
hardlink-friendly and paths map 1:1 across services. Layout:
- `data/blackhole/{sonarr,radarr}/` — Sonarr/Radarr (configured with a **Torrent Blackhole** download
  client, since Seedr has no native *arr connector) drop `<release>.magnet` files here.
- `data/downloads/{sonarr,radarr}/` — media-bridge's blackhole bridge writes finished downloads here; the
  *arr watch-folder scan imports them.
- `data/media/{tv,movies}` — the Jellyfin library, mounted read-only into jellyfin.
- `data/blackhole/.bridge-state/` — media-bridge seedr per-transfer state JSON (one file per transfer).

## ntfy: two topics, deliberately separate

Push alerts go to ntfy.sh on **two** topics, and they must stay split:
- **`NTFY_TOPIC`** — media-bridge's operational alerts (seedr stalls, dead scraper domains, youtube
  job results). Event-driven, can repeat, sometimes urgent.
- **`NTFY_TOPIC_DIUN`** — DIUN image-update notices only. One batch a day at 08:00.

Both live in `.env`. They were one topic until 2026-09-11, which **silently hid DIUN entirely**: the
phone app collapses a topic's messages into one notification group, so media-bridge's recurring
alerts (a dead scraper seed re-alerting every crawl pass, ~24/day at priority 4) buried DIUN's
4-8 once-daily messages at priority 3. DIUN was never broken — `docker logs diun` showed "Image update
found" on every run and no send errors; the messages were arriving and being drowned. If DIUN ever
seems silent again, check the topic *before* suspecting the notifier: `docker exec diun diun notif test`
publishes a test message, and `curl -s "https://ntfy.sh/<topic>/json?poll=1&since=12h"` shows what the
topic actually received (ntfy.sh caches 12h, so a message older than that is simply gone, not lost).
A new ntfy topic also needs subscribing to in the phone app — nothing on the box can do that for you.

## Jellyfin: official image on a LinuxServer directory layout

Jellyfin ran `lscr.io/linuxserver/jellyfin` until 2026-08-27, when it moved to the **official**
`jellyfin/jellyfin:preview` (**12.0.0**). The two images lay `/config` out differently, and 10.12+
added a marker-file sanity check that **hard-aborts** on a mismatch:

```
Expected to find only .jellyfin-data but found marker for /config/.jellyfin-config
```

The existing data is in the LinuxServer layout (config XMLs at the root of `/config`, program data in
`/config/data`, cache in `/config/cache`), so the compose block pins the official image to it with four
`JELLYFIN_CONFIG_DIR`/`DATA_DIR`/`CACHE_DIR`/`LOG_DIR` vars. **Do not drop them** — the alternative is
relocating `data/`, `metadata/`, `root/`, `plugins/` and every config XML, all of which are also
referenced by absolute path from the DB. `PUID`/`PGID` are LinuxServer-only and ignored by the official
image; `user: "1000:1000"` replaces them so Jellyfin keeps writing files as the *arr user (and
exposer's fail2ban keeps being able to read `./jellyfin/config/log`).

**Jellyfin 12 disabled legacy API authorization.** The `X-Emby-Token` header and the `?api_key=` query
param both return **401**; the only accepted form is:

```
Authorization: MediaBrowser Token="<JELLYFIN_API_KEY>"
```

This broke media-bridge's reaper and youtube components on upgrade — both now send that header. Any new
Jellyfin API caller must use it too.

Rollback, if 12.0.0 ever needs undoing: its DB migrations are **one-way**, so restore from
`~/backups/jellyfin-config-pre-12.0.0-*.tar.gz` (config XMLs + `data/data` DBs, taken pre-upgrade) and
put the image back to `lscr.io/linuxserver/jellyfin:latest` with the four env vars removed.

## media-bridge (`media-bridge/`)

One container running five components as supervised daemon threads (`main.py` → each in a try/except
wrapper that logs + ntfy-alerts + restarts, so one crash can't take the others down). It is **off the
VPN** on the default bridge net; it reaches the *arr apps and FlareSolverr via `http://gluetun:<port>`
and host-net Jellyfin via `host.docker.internal`. Shared helpers live in `common.py` (`clog`, `norm`,
`ntfy`). Logs are tagged `[bridge]` / `[seedr]` / `[reaper]` / `[movierulz]` / `[tamilmv]` / `[youtube]`. Rebuilding/restarting this container to
ship a movierulz change also restarts the bridge loop mid-download — safe, since the bridge resumes from its
`.bridge-state` JSON.

**Port bindings: all interfaces, never a literal LAN IP** (changed 2026-08-30, when the Jetson gained a
wired link and became dual-homed — Ethernet `enP8p1s0` and Wi-Fi `wlP1p1s0`, both on `10.0.0.0/24`;
Ethernet holds the long-standing `10.0.0.179` reservation so existing clients and the router's 443 forward
moved to the wire untouched; Wi-Fi is statically pinned to `10.0.0.180` (NetworkManager profile `SRP`,
`ipv4.method=manual`), with a matching router DHCP reservation on `f8:3d:c6:57:20:87` holding that address
out of the pool. Nothing in this stack depends on the Wi-Fi address any more, which is the point — do not
re-pin it anywhere).
The feeds (`8002`, `8003`, `8005`) publish as plain `"800x:800x"`. Binding a literal address here has two
failure modes, both hit in practice: it (1) **dies with its NIC** — and since all the binds live in one
container, a Wi-Fi blip stopped Docker from starting `media-bridge` *at all*, taking the bridge and
`[reaper]` down with it; and (2) **races the DHCP lease at boot**, because `docker.service` orders on
`network-online.target`, which one interface can satisfy before another's lease exists — the same race
that took Immich down for 10h on 2026-08-09. The retired `never 0.0.0.0` comment justified itself as
protection against a changed router forward, which it never was: a forward targets a LAN IP and the
sockets were bound to exactly that LAN IP. **What actually keeps these off the internet is that the
router forwards only 443** (see `~/exposer`) — so never forward `8002`, `8003` or `8005`.

### blackhole bridge (`media-bridge/bridge.py` + `media-bridge/backends/`)

Polling loop (`process_new_magnets` → `check_completions`, default every 30s) that is the *arr apps'
download client: they keep the **Torrent Blackhole** client and never see which service downloads.
Split since 2026-09-25 into a backend-agnostic engine and pluggable **download backends**:
- `bridge.py` — everything the *arr apps depend on: blackhole scan, `.torrent` parse (`torrentfile.py`),
  routing, state files, delivery under the release name, supersede, stall / missing-result /
  unfetchable deadlines, `arr_mark_failed`, and the private-tracker hold. Log tag `[bridge]`.
- `backends/base.py` — the adapter contract (`add` / `locate` / `begin_loop` / `status` / `fetch` /
  `cancel` / `discard` / `start_hold` / `hold_over` / `end_hold` / `reap`, plus `Full` / `Exists` /
  `Rejected`). `backends/__init__.py` registers backends by name and imports them lazily.
- `backends/seedr.py` — the Seedr REST/FTPS adapter (all the Seedr quirks below live here). Log tag `[seedr]`.
- **`BRIDGE_ROUTE`** (compose) picks the backend per drop, first match wins: `<private|public|*>:<backend>`,
  default `*:seedr`. State files carry `"backend"`; files without it (pre-2026-09-25) mean `seedr`.
  A backend named by a route or a live state file must stay registered until its transfers finish.
- `backends/qbit.py` — the **seedbox** backend (Pulsed Media PMSS box `3-229mighty`, qBittorrent 4.5.2). See
  *seedbox backend* below. Log tag `[seedbox]`.
- **`SEED_RULES`** (compose): per-private-tracker seeding rule, key = substring of the grab's indexer name
  (from *arr history, retried a few polls since the *arr writes the drop first), `<h>h` or `<ratio>|<h>h`;
  `private-default` (336h) covers anything unmatched. A drop whose rule exceeds its backend's cap
  (`max_seed_hours`, Seedr 48) **waits in the blackhole** — never sent to a backend that would H&R it. A
  magnet whose indexer matches a rule is private too (magnets carry no private flag). `iptorrents=48h` only
  while the account is VIP (until 2026-10-25) → `1.0|336h` after.
- **Staged delivery**: fetches land in `downloads/.incoming/<cat>/<release>` and are moved into the watch
  folder with ONE `rename` only when complete (`deliver`: identical existing dir → keep; different → swap
  whole, never mix). The *arr blackhole scan imports any watch-folder item idle ~30s, so fetching straight
  into it let slow/retrying fetches be imported half-done.
- **Never judge what you can't see**: `status()` may return `UNAVAILABLE` and `hold_over()` may return
  `None` (torrent not in the listing, client restarting, `error`/`missingFiles`). Those never fail,
  tombstone, reap, or release a hold — the bridge waits (deadlines frozen) and ntfys after
  `UNAVAILABLE_ALERT` (2h). **A missing seedbox torrent is never a finished hold.**
- **Clock freeze** (`freeze_clocks`, marker `.bridge-state/.seen-<backend>`): after any gap in observing a
  backend (> `CLOCK_GAP`), deadlines that were already running are pushed back by the gap, so an outage
  can't make every transfer look stalled. **Circuit breaker** (`may_fail`): `BREAKER_FAILS` (3) automatic
  failures on one backend within 15 min pauses auto-failing there for 1h (+1 ntfy).
- **Private partials** (`keep_private_partial`): a private torrent that downloaded > 50 MiB is never
  cancelled on stall/supersede — it's failed in the *arr (re-grab) but kept seeding as an undelivered
  hold (`no_deliver`), capped at `PARTIAL_HOLD` (7d). Trackers count partial snatches as H&R.

- **Pull workers** (`puller.py`, threads `bridge-puller-N`): on `DONE` the bridge calls `be.prepare_fetch`
  (API calls stay on the bridge thread) and queues a job; the worker runs `be.pull` with connections of
  its own and writes only inside its `.incoming` stage dir; the bridge delivers + releases/holds when it
  sees the job done. So a long pull no longer freezes the loop. **`PULL_WORKERS` (2) jobs at once** (since
  2026-10-17: a 9 GB movie and its follow steps sat behind a 35 GB remux) — they share the box's uplink, so
  total speed is unchanged, only who waits. A job that would leave < `PULL_MIN_FREE_GB` on the Jetson,
  counting what the other running pulls still have to write, waits (`waiting_space`, never counted as a fetch failure). Supersede
  cancels a running pull (the worker removes its own stage); `cleanup_local_partial` skips busy stages.
  Seedr uses a per-thread requests session. `puller.run_pending()` runs jobs synchronously (tests).
  **Scene RARs**: after a pull the worker extracts every RAR set in the stage (`extract_archives`: `.rar`+`.rNN`
  or `.partNN.rar`, not inside `Sample/`) with Debian's non-free `unrar` (baked into the image), into a temp
  dir moved in only on success, then deletes the volumes — Radarr/Sonarr never unpack archives, so a RAR'd
  private release (IPTorrents SKYFiRE etc.) otherwise imports nothing but its sample.
- **Event log** (`bridgelog.py`, `config/bridge.db`, WAL): append-only `events` (tid, type, phase,
  release, detail) + `attempts` per movie/episode. Every transfer carries `rec["tid"]` (legacy states get
  one on sight). Waiting drops are logged only when their reason changes (`drop_wait`). All writes are
  swallowed on error — the log can never break a transfer. Look things up with
  `docker exec media-bridge python -m bridgelog` (active + waiting), `... bridgelog <name>` (timeline),
  `... bridgelog --attempts`.
- **Grab info** (`grab_info`): the *arr's "grabbed" history rows for the drop give the indexer, the
  history ids (all episodes of a pack) and attempt keys (`radarr:movie:<id>` / `sonarr:episode:<id>`).
  `arr_mark_failed` marks by those ids (the name search sees only 100 rows and one episode of a pack).
- **Slow rule** (`check_slow`, `SLOW_*`, public-only, **dry-run by default**): after 2h, <5% or a
  projected ETA (the bridge's own ~1h rate — qBittorrent's `eta` is 8640000 whenever speed is 0) above
  24h for 1h → fail + re-grab. **Give-up** (`record_failure`, `GIVE_UP_*`, **dry-run by default**): the
  10th failed attempt for a movie/episode unmonitors it in the *arr BEFORE marking failed (marking failed
  always re-searches), and later grabs of it are dropped. No ntfy for any of these (user decision).
- **Fail-fast** (`check_failfast`, `FAILFAST*`, public-only, **dry-run by default**): a public download
  with nothing downloaded after 10 min whose backend reports no metadata or 0 swarm seeds
  (`Backend.swarm()`: qBittorrent's state/num_complete; Seedr's transfer entry has `seeders`, `leechers`,
  `connected_to`, `download_rate`, and `size` once metadata is known) is failed + re-grabbed instead of waiting for
  the 45-min stall. movierulz/tamilmv report a fake seeder count (`FAKE_SEEDERS`), so dead picks are common.
- **Extra public trackers** (`trackers.py`, `PUBLIC_EXTRA_TRACKERS`): ngosang/trackerslist
  `trackers_best.txt` (~20), refreshed daily, cached in `config/trackers_best.txt` (a failed fetch keeps the
  cache). Appended to public magnets (`&tr=`) before any backend; public `.torrent`s on the seedbox get them
  via `/torrents/addTrackers`. **Never for a private torrent** — public by BOTH checks only (no private flag,
  indexer not a `SEED_RULES` tracker): announcing a private infohash to public trackers gets accounts banned.
- **Import fix** (`check_imports`, `IMPORT_FIX`, default on): every delivery with grab-time ids (`arr_keys`) goes on
  a watch-list (`.bridge-state/.import-watch.json`, 24h). Every 2 min the *arr queue is read; a watched item that is
  `importBlocked` for **"title mismatch" / "Unknown Series/Movie"** (e.g. `Rewired with Joe Dispenza` vs Sonarr's
  `Rewired`) gets a `ManualImport` command, mapped **only** to the recorded movie / episodes (Radarr: largest
  video → `movieId`; Sonarr: `SxxEyy` in each filename → episode ids from the key set, anything unmappable is
  skipped). Up to 3 tries, 10 min apart. Other block reasons (TBA title, not an upgrade) are only logged
  (`import_blocked` event), never forced. The manualimport GET uses `folder=` only.

- **Sonarr refresh nudges** (2026-10-02; Heart Beat S03E37-E40 took 28.5 h from airing to watchable with a
  2-minute download, because Sonarr waited twice for its 12-hourly `Refresh Series`): (1) **grab side** —
  `nudge_sonarr` (`movierulz.py`, run after every movierulz and tamilmv crawl pass): a series release indexed
  in the last `SONARR_NUDGE_DAYS` (3) whose `SxxEa-Eb` covers episodes Sonarr doesn't list for a **library**
  show → `RefreshSeries` for that show, at most every `SONARR_NUDGE_INTERVAL` (3600 s, in memory; 0 = off).
  A refresh that adds already-aired episodes makes Sonarr search them itself. (2) **import side** —
  `check_imports`: a delivery `importBlocked` for "TBA title" gets a `RefreshSeries` every
  `TBA_REFRESH_INTERVAL` (3600 s, first one an hour after delivery; event `tba_refresh`) — still never
  force-imported, so files get real names. If a site's episode numbering never matches TVDB, the nudge
  repeats hourly until the release is 3 days old, then stops.

- **Status page** (`bridgeweb.py`) on the youtube server: `http://10.0.0.179:8003/bridge` (HTML, 30s refresh),
  `/bridge/status.json`, `/bridge/transfer/<tid>` (timeline). Same process as the bridge, so it reads the
  pull worker's jobs and each backend's last poll snapshot — rendering never calls Seedr or the seedbox.
  Shows where each transfer is + until when (qBit eta, else the bridge's own rate; pull ETA from staged
  bytes; hold end from seeding_time vs rule), waiting drops with their reason, holds, last 7 days, and the
  dry-run rule decisions. Names come from scraped sites → everything `html.escape`d + a script-free CSP.
  LAN-only like `:8003` itself; never put it behind Caddy (it shows private-tracker names).

### seedbox backend (`backends/qbit.py`) + vault (`vault.py`)

**Status (2026-09-27):** live for **private** torrents (`BRIDGE_ROUTE=private:seedbox,public:seedr`). Public stays on
Seedr: the box's **shared uplink**, not our client, caps pulls home — 32-40 MB/s evenings, **4 MB/s** at a busy hour
(2026-09-26 04:20 EDT; a cached re-read was just as slow, so network, not disk; no PMSS throttle on the account),
while the home line does ~127 MB/s from Helsinki. Research + numbers: plan file
`~/.claude/plans/since-seedr-has-limitation-smooth-globe.md`.

- **Secrets come from Bitwarden Secrets Manager** (cloud, US), never `.env`: `vault.py` shells out to the
  pinned, checksum-verified `bws` CLI baked into the image; the only local secret is the machine-account
  token `media-bridge/config/bws_token` (mode 600, gitignored). Values live in memory only. Names:
  `SEEDBOX_ACCOUNT_PASSWORD` (admin use only — media-bridge never needs it), `SEEDBOX_SSH_KEY_SFTP`,
  `SEEDBOX_SSH_KEY_QBIT`, `SEEDBOX_QBIT_PASSWORD`, `SEEDBOX_SSH_HOSTKEY`, `TRACKER_*`. `python -m vault check`
  lists names, never values. `bws` parses values starting with `-` as options → `put` uses `--value=`/`--`.
- **Two restricted SSH keys** on the box (`~/.ssh/authorized_keys`): SFTP key =
  `restrict,command="/usr/lib/openssh/sftp-server -R"` (read-only SFTP: pulls, `~/.quota`, port file);
  qBit key = `restrict,port-forwarding,permitopen="127.0.0.1:*",command="/bin/false"` (tunnel only).
  Host key pinned in `media-bridge/config/seedbox_known_hosts` (+ vault copy); unknown/changed = refuse.
- **qBittorrent** listens on the box's `127.0.0.1:<~/.qbittorrentPort>` (37301 today; PMSS may move it —
  re-read on connection errors), reached through a paramiko direct-tcpip tunnel + a local forwarder.
- **Pulls** (paramiko, one SSH connection per stream): big files (≥ 64 MiB) one at a time in `PULL_SEGMENTS` (2)
  ranges, resumable via `<file>.part.map` (saved only after an fsync, atomically); small files
  `PULL_PARALLEL_FILES` (4) at a time. Pull connections are tuned (`_sftp_transport`/`_sftp_client`/`_open_remote`):
  16 MiB window (`SEEDBOX_SFTP_WINDOW_MB`; every SSH stack's 2 MiB default caps a stream at ~12 MB/s at 110 ms),
  255 KiB requests (`SEEDBOX_SFTP_REQUEST`, **never above 261120** = OpenSSH's max read: a clamped reply makes
  paramiko drop prefetch), AES-GCM preferred. Measured 2026-09-26: tuned = lftp+HPN-SSH speed (both hit the box
  cap), 16 vs 34 CPU-s/GB; rclone/aria2 no better (aria2 needs the account password); **lftp `get1
  --target-position` corrupts earlier-written ranges** — never use it to fill one file in several passes.
- **Follow mode** (`backends/follow.py`, `FOLLOW=off|private|all`, **off by default**): while a seedbox torrent
  downloads, `maybe_follow` (bridge thread, every `FOLLOW_INTERVAL` 60 s, once `FOLLOW_MIN_MB` 128 are ready) queues
  a `kind="follow"` puller job that copies qBittorrent's **finished pieces** (`/torrents/pieceStates`) as byte ranges,
  SHA-1-checks each against the torrent itself (`/torrents/export` — exact layout incl. BEP 47 pad files, which
  `/torrents/files` hides), and writes **only verified pieces** into sparse `<file>.part` in the stage dir, at most
  `FOLLOW_STEP_MB` (2048) per step so other pulls never wait long. State: `.follow.json` (bitmap, saved after
  fsync) + `.follow.hashes`. At DONE the final pull (it waits for a running step) fetches only the missing
  pieces (`FOLLOW_PASSES` tries: libtorrent reports pieces before they reach disk — a hash failure, re-read later),
  renames, delivers as usual; **any failure there discards the follow state and pulls normally**. No export (old
  qBittorrent) or a v2-only torrent → normal pull. Measured with the prototype: home 8–29 s after the box
  finished vs ~2 min for 4 GB; status page shows "home X GiB" for followed downloads.
  Its WebUI password is its own random one; **a password change in the Pulsed panel resets it to the
  account password** (PMSS `changePw.php`) → the bridge stops at the FIRST refused login (qBittorrent bans
  127.0.0.1 after repeated failures, which also locks the user's WebUI) and ntfys.
- **PMSS watchdog flags** in the box home: `~/.qbittorrentEnable` (keeps qBittorrent up), `~/.rtorrentDisable`.
  The watchdog rewrites only perf/CSRF keys in qBittorrent.conf while it's stopped — not share limits,
  queueing, or the password. Rootless Docker (~200 MB of the 1 GB cgroup) is root-config; only PM support
  can disable it.
- **Space**: `~/.quota` (PMSS `quota -s` snapshot, 230G soft) minus bytes our downloads still need minus
  this loop's adds; qBittorrent's `free_space_on_disk` is the whole shared disk — never use it. Magnets
  without `xl=` need `SEEDBOX_UNKNOWN_SIZE_GB` (40) headroom. Full → the drop waits (no retry cap).
- Adds always set `paused=false`/`stopped=false`, `ratioLimit=-1`, `seedingTimeLimit=-1`, category
  `bridge-<cat>`, tag `media-bridge`; qBit 4.x `Ok.` and 5.2 JSON/409 both handled. Holds end on
  qBittorrent's own `seeding_time` ≥ rule + `SEED_MARGIN_HOURS` (3) or ratio; a stopped/queued held
  torrent is resumed. The reaper never deletes a hash a live record still owns.
- **Server reboot 2026-09-25**: every port dead from home + "connects but never answers" via VPN looked
  like an IP ban; it was a reboot. PMSS runs no fail2ban (ADR 0010). Check `uptime` before suspecting a ban.

Seedr backend: HTTP Basic auth; password is base64 in `SEEDR_PASSWORD_B64` because a literal `$` breaks
Docker/shell interpolation. Per magnet: add to Seedr → poll → download finished files into the watch
folder → delete from Seedr to free space.

Non-obvious invariants — preserve these when editing (engine rules in `bridge.py`, Seedr rules in
`backends/seedr.py`):
- **Completion is signalled by the torrent leaving `root["torrents"]`**, NOT by the destination folder
  existing. Seedr surfaces the folder early and fills it as files finish; fetching on folder-existence
  grabs a half-finished download.
- Results land two ways: multi-file torrents as a top-level **folder**, single-file torrents as a bare
  **root file** — both are handled, matched by normalized name prefix.
- **Delivered under the *arr's release name, not Seedr's**: the local dir is
  `downloads/<cat>/<release_name>` (the `.magnet` stem = the title the *arr grabbed). The *arr maps a
  watch-folder item to its series/movie by parsing the folder name, and Seedr's name is the torrent's raw
  internal one (`www.1TamilMV.promo - Heart Beat (2026) S03 EP (01-04) …`), which can map to nothing or
  to the wrong show. `cleanup_local_partial` matches either name, for dirs written before this.
- **Override/supersede (Radarr only)**: a new release whose `movie_key` (normalized "title+year")
  matches an in-progress *different* release of the same movie triggers `purge_superseded`, which
  deletes the old Seedr transfer immediately and writes a **tombstone** state file; `check_completions`
  reaps the old release's late-appearing folder/partial each loop until `TOMBSTONE_GRACE` expires.
- **`name_matches` (`common.py`) is the single result/supersede matcher** — used by `check_completions` (both the
  folder and the bare-root-file branch) and by the supersede reaper. It requires the full shorter
  normalized name to be a **substring** of the longer (`len>=18`), never a fixed-length prefix:
  - *substring, not prefix*, because Seedr names the result after the torrent's **internal** name, which
    on scraped sites carries a leading site tag the magnet's `dn=` (our stored title) lacks — Seedr's
    `www.8MovieRulz.com - <release> ESub.mkv` vs our `<release>`. A prefix/`[:20]` compare never fires
    there, so the transfer looped forever on "finished but result not visible yet" and the movie was
    never delivered (every movierulz grab hit this).
  - *full shorter name, not a truncated prefix*, because a truncated prefix wrongly matches two
    *different* releases of the same movie and defeats the supersede safety guard.
- **Stall handling**: if Seedr `progress` doesn't advance for `STALL_TIMEOUT` (default 2700s), the
  transfer is failed in Radarr/Sonarr via `arr_mark_failed` (history `eventType=1` match by normalized
  `sourceTitle` == stored `release_name`, then `POST /history/failed/{id}` → blocklist + auto re-grab)
  and an ntfy notification is sent. `arr_mark_failed` uses plain `requests`, NOT the module-level
  `SESSION` (which carries Seedr Basic-auth that must not leak to the *arr).
- **Missing-result handling**: a transfer that left `root["torrents"]` but whose folder/file never
  appears (Seedr dropped/deduped it — hit 2026-09-22 when a single-episode torrent finished alongside a
  pack holding the same file) used to log `finished but result not visible yet` every poll forever.
  After `RESULT_TIMEOUT` (default 1800s) `handle_missing_result` fails it via `arr_mark_failed`, ntfys,
  and tombstones it (so a late-appearing result is still reaped).
- **FTP fallback download (`seedr_ftp.py`)**: Seedr's HTTPS CDN (`rd*.seedr.cc/ff_get_premium/…`)
  sometimes 404s persistently on a finished transfer's files while the bytes are intact in storage
  (2026-09-23: a whole Heart Beat pack 404'd on `rd12` for 30+ min and would have been discarded by
  `FETCH_TIMEOUT`). `download_file` tries HTTPS, and on any failure pulls the same file over **explicit
  FTPS** (`SEEDR_FTP_HOST=ftps.seedr.cc`, account creds unless `SEEDR_FTP_PASSWORD_B64` is set), which
  reads storage directly — same ~38 MB/s. FTP also resumes at a byte offset (`REST`), so a leftover
  `.part` goes straight to FTP instead of being truncated by a fresh HTTPS GET; the result is checked
  against Seedr's `size`. FTP paths mirror the folder tree (`/<top folder path>/<sub>/<file>`; bare
  root files `/<name>`). FTP is **download-only here** — add/progress/completion/delete stay on REST
  (Seedr documents no FTP watch folder). Empty `SEEDR_FTP_HOST` disables it. Two Seedr quirks the
  code depends on: (1) a **dropped FTP transfer is reported as `550 Issue during transfer`** — the same
  code as "no such file" — so a 5xx is treated as permanent only if no bytes moved on that attempt, and
  connections are `close()`d, never `quit()` (QUIT reads that pending 550 and masks the real error);
  (2) a **subfolder's REST `path` is its full path from the root** (`Top/Sub/Deeper`), not its own name —
  `fetch_folder_recursive` uses the last component for both the local dir and the FTP path (joining it
  whole used to nest the torrent's tree inside itself locally).
- **413 on add** = Seedr full or release too big. If a same-movie zombie is hogging space, free it and
  retry; if lots of space free yet still 413 (or after 5 tries), the release can't fit the account and
  the magnet is dropped. Seedr account is only ~34 GB — Quality Definition max sizes are capped at
  130 MB/min in both Sonarr and Radarr to keep grabs within budget.
- **409 on add** = Seedr ALREADY holds this torrent — normally because the magnet was added by hand on
  seedr.cc, which is a reasonable thing to do. `adopt_existing` takes it over instead of retrying:
  a 409 leaves no state file behind, and `check_completions` only ever reads state files, so the
  finished movie would sit in Seedr undelivered while the magnet 409'd on every 30s poll forever.
  The live transfer is matched **by infohash** (exact — the name matcher is not needed here); if it
  already finished it is no longer in `root["torrents"]`, so it falls back to `name_matches` over
  folders and bare root files, the same two result shapes `check_completions` handles.
- **`.torrent` drops are uploaded as files** (`POST /rest/transfer/file`, field `file` — same response
  as a magnet add), not converted to magnets: the file keeps the info dict's `private` flag and the
  passkeyed announce list. `magnet_from_torrent` survives only to supply the btih for 409 matching.
  Prowlarr's per-indexer **Prefer Magnet URL is off** on the private trackers so they arrive as `.torrent`.
- **Private-tracker hold (`SEED_HOLD`, 49h) — hit-and-run protection.** Seedr's Pro plan seeds private
  torrents **2:1 or 48h, whichever first**, as client **Deluge 2.1.1**, and only while the files are in
  the account (deleting early is assumed to stop seeding — Seedr doesn't document it). A private
  torrent still leaves `root["torrents"]` at 100% like a public one; seeding is invisible in the API.
  So a torrent with `info.private == 1` is marked `private` in its state file, and `release_or_hold`
  keeps its Seedr folder/file **plus the state file** for `SEED_HOLD` after delivery, then deletes it.
  Held results are excluded from everything that deletes or matches by name: `purge_superseded`, the
  tombstone reaper, and the result lookup for other transfers. An unfetchable private result is failed
  in the *arr but held, never tombstoned (the tombstone reaper deletes every loop). Before this, the
  bridge deleted each private result right after download — a hit-and-run on every grab.
- **Which private trackers Seedr can serve** (per-tracker H&R rules vs one 48h window, 2026-09-25):
  HD-Space 48h ✅ (enabled, **freeleech-only** — its overall ratio must stay ≥1.0 at all times);
  IPTorrents 1:1-or-336h but **VIP-exempt** for now ✅ (re-check when VIP ends); RetroToon 72h,
  CZTeam 1:1-or-72h, TorrentHaven 120h (even freeleech) ❌ — **disabled in Prowlarr**; DocsPedia
  unknown, disabled. Longer rules would need re-seed cycles (delete + re-add after 48h for another
  window, within the tracker's 10-day window), which each re-download the torrent — not built.

### reaper component (`media-bridge/reaper.py`)

Polling loop (default every 6h) that reclaims disk: deletes a movie once **every** Jellyfin user has
Played it AND `GRACE_DAYS` (default 15) have elapsed since the most recent watch. Matches Jellyfin
items to Radarr movies by TMDB id, and deletes **through Radarr's API** (`deleteFiles=true`) so the
file, folder, Bazarr `.srt` sidecars, and DB entry go together — never `rm` on `/data/media`.

Safety guards (keep intact): `DRY_RUN` defaults **true** (flip to false in compose only after verifying
the dry-run list); any Jellyfin/user API failure or zero users **aborts the whole run** (never assume
"watched"); `MAX_DELETES_PER_RUN` caps blast radius; only movies with `hasFile` are considered.

**The quorum is pinned via `JELLYFIN_USERS=rosurya`** — do not drop it back to the default (all enabled
users). Jellyfin also has a passwordless, login-screen-visible `guest` account; since the quorum is the
*intersection* of every user's played movies, an account that watches little or nothing would make that
set permanently near-empty and **silently stop all reaping**. Any new Jellyfin account must be added to
`JELLYFIN_USERS` deliberately, not by default.

### movierulz component (`media-bridge/movierulz.py`)

A crawler thread + a stdlib Torznab HTTP server (`:8002`, host-published) that makes 5movierulz a
generic Torznab indexer for Prowlarr — MediaFusion's tamil-site spiders were compiled into its
image and couldn't be taught a new site, so we scrape this one ourselves.

- **Crawler** (`run_crawler` → `crawl_once`, every `CRAWL_INTERVAL`, default hourly): paginates the
  **master `/movies` listing** (`MOVIERULZ_MOVIES_PATH`) newest-first. One master source deliberately
  replaces the old per-language category walk, because it covers **every language and type including
  `*-dubbed`** — nothing is left out. `MOVIERULZ_BASE` is required and lives in `.env`, out of source;
  unset, the crawler disables itself but the Torznab feed still serves.
  **The domain rotates, and a dead one looks like a FlareSolverr fault, not a DNS one:** every page
  fetch fails with `500 Server Error ... /v1` from FlareSolverr, which reads as Cloudflare trouble.
  **A rotation that still redirects is silent too** (`.solutions` → `.institute`, noticed 2026-09-30
  after **10.7 days** of "scanned 1 page(s), 0 posts fetched" with no error): FlareSolverr follows the
  301 and returns the new domain's listing, whose post links didn't match a `POST_RE` built from
  BASE's host. `POST_RE` now matches **any `…movierulz.<tld>` host**, so the crawl keeps working through
  the redirect and `crawl_once` logs `domain moved: post links point at <host> … update MOVIERULZ_BASE`
  (meta `moved_to`). **Stale-feed ntfy** (`check_stale`, after every pass): when the newest post
  (first-seen) is older than **`MOVIERULZ_STALE_ALERT_DAYS` (4)**, a high-priority push to the ops topic
  names BASE, where it moved, and the last pass's result; it repeats at most daily
  (`MOVIERULZ_STALE_ALERT_REPEAT`) and resets when posts flow again.
  Check `docker compose logs flaresolverr` for the real cause — `ERR_NAME_NOT_RESOLVED` means the
  domain is simply gone (`www.5movierulz.watch` went NXDOMAIN 2026-08-24 and the crawler indexed
  nothing for 11 days while FlareSolverr itself was healthy, still serving tamilmv fine). Fix is a
  one-line `MOVIERULZ_BASE` update in `.env`. After changing it, mind the **backfill floor**: an
  incremental pass stops at the first fully-known page (floor `MIN_PAGES`), so if a catch-up walk is
  interrupted partway the still-unseen posts sit *below* that floor and no later pass ever reaches
  them. Either let the first post-fix crawl run to completion undisturbed, or reset
  `meta.backfill_done=0` to force a full resumable re-walk.
  Fetches every page **through FlareSolverr** (`FLARESOLVERR_URL`, CF-protected site), never directly.
  Post discovery keys on the trailing numeric id, covering both URL schemes (`…-<id>.html` and the legacy
  `…/movie-watch-online-free-<id>.html`). Metadata is parsed from the magnet **`dn=` name** (authoritative,
  uniform across URL forms), size from `xl=` with a name-regex fallback. Stored in
  `media-bridge/config/movierulz.db` (sqlite), keyed by btih (`INSERT OR IGNORE` → stable `pubDate`).
- **Resumable backfill (`meta.backfill_done`):** until the catalogue has been fully walked once, *every*
  pass paginates to the real last page, so the long first backfill **survives restarts** (already-seen
  posts are skipped cheaply, making re-scans of early pages fast). On reaching the end it sets
  `backfill_done=1` and switches to incremental: stop once a whole page is already-known — known before
  this pass **or seen on an earlier page of it** (every listing page repeats a sidebar of the NEWEST posts;
  judged against "known before" alone, any new post made every page look new and each pass walked all
  ~400 pages through FlareSolverr until 2026-09-30) — floored by
  `MIN_PAGES` (default 3, so a curated first page can't hide new posts sitting deeper) and capped by
  `MAX_PAGES` (1000, a safety net — the real stop is the last/empty page).
- **Empty-post retry (`post_needs_fetch` + `posts.magnets`):** the site frequently publishes a movie's
  post page **hours before** its magnet links exist, so a first scrape can find 0 magnets. Each post
  stores the **raw magnet count** seen on the page (`mark_post`); a post that had magnets is done
  forever (even if every magnet was a skipped episode/Part), but a **zero-magnet** post is re-fetched
  until `MOVIERULZ_EMPTY_RETRY_DAYS` (default 3) after **first-seen** (`scraped_at` is first-seen via
  `INSERT OR IGNORE`, NOT last-attempt — don't switch it back to `OR REPLACE` or the window never
  closes). Keying retry on magnet count, not release count, is deliberate: skip-only pages (single
  episodes/Parts) DO have magnets, so they must not be retried. Without this, a movie whose magnets
  landed after we scraped it was indexed **never**.
- **Scope = movies + episode-range packs only.** `S0X EP (a-b)` → `kind=series` with the title
  normalized to a Sonarr **multi-episode** release `… S0XEa-Eb …` — NOT a bare `S0X` season pack: sites
  post running seasons in chunks (Heart Beat S03 is 32 episodes, posted 4 at a time), and a bare `S0X`
  told Sonarr each chunk was the whole season. Single episodes (`S04 EP09`) and "Part" forms
  (`S1P2`, `S1P-1-2`) are **skipped** — the season/part regexes also guard against mis-filing them as
  movies.
- **Series year pinned to Sonarr's (`series_title`, applied when serving, not stored):** release names
  carry the *season's* year (`Heart Beat (2026) S03…` for a 2024 show), and Sonarr maps a
  titled-with-year release only to a series of that year → `[Permanent] Unknown Series`. Stripping the
  year is **not** a fix: a bare name can hit a global scene alias (`Heart Beat` → TVDB 74684, the 1992 UK
  *Heartbeat*). So for shows in the Sonarr library the served title gets the show's own year
  (rewritten or inserted), from a `SONARR_URL` series map refreshed every `ALIAS_REFRESH`. Shows not in
  Sonarr are served unchanged. Test a title with `GET /api/v3/parse?title=…` on Sonarr — it shows the
  mapped series (or none).
- **Torznab server** (`serve_torznab`): `t=caps` advertises movie + tv search; `t=search/movie/tvsearch`
  validate `MOVIERULZ_APIKEY` (Torznab error 100 on mismatch) and substring-match stored titles. Every
  `<item>` MUST carry a `<pubDate>` (Prowlarr drops items without it — the same bug the custom
  `mediafusion-api` binary patches) and emits a configurable `FAKE_SEEDERS` (movierulz exposes no
  seeders) so Radarr/Sonarr minimum-seeders doesn't filter everything.
- **Prowlarr wiring**: add as a generic Torznab indexer, `apiPath=/api`, apiKey `= MOVIERULZ_APIKEY`,
  baseUrl **`http://10.0.0.179:8002/torznab`** — the reserved Ethernet IP, NOT `media-bridge:8002`.
  Prowlarr lives in gluetun's netns, and gluetun resolves DNS itself (`DNS_ADDRESS=9.9.9.9`), not via
  Docker's embedded resolver, so **container names do not resolve** there (the `172.17.0.1` bridge
  gateway is unreachable too); the LAN IP works because `FIREWALL_OUTBOUND_SUBNETS` allows 10.0.0.0/24.
  The failure is silent: Radarr's `t=caps` call to Prowlarr 500s (`Name does not resolve`), Radarr logs
  `Unable to connect to movierulz (Prowlarr)` and just **drops the indexer from every search** (its
  "N active indexers" count shrinks) — until 2026-09-13 this hid movierulz + tamilmv from all *arr searches.
- **Query resolution (`search_terms` + `unorm`) — alias-aware, and never "match everything":**
  Radarr searches a movie's **alternate titles**, which for Indian films are **native-script**
  (`கருப்பு`) — and it strips the vowel signs first (`கருப்பு` → `கர பப`). Two consequences, both
  handled here and shared with tamilmv (which imports `search_terms`/`match_rank`/`page`):
  - `norm()` keeps only `[a-z0-9]`, so a native-script query collapsed to `""` → zero tokens →
    `all([])` is **True** → the query matched the **whole catalogue** and each *arr search got 100
    unrelated releases (they showed up as ~300 `Unknown Movie` rejections per search). A query that
    resolves to nothing matchable now returns **zero** items, not everything.
  - `unorm()` (NFKD + drop combining marks + drop non-alphanumerics, Unicode-aware) makes Radarr's
    mark-stripped query compare equal to the stored alt title, and `search_terms` expands the query
    to **every latin title Radarr knows for that movie** (map pulled from `RADARR_URL`/`RADARR_API_KEY`
    every `ALIAS_REFRESH`, default 900s, best-effort — stale map on failure, never a blocked search).
    That is what makes a **dub-titled release findable**: the Telugu cut of *Karuppu* is released as
    *Veerabhadrudu*, a name no Radarr query ever contains. A trailing year in the query is carried
    onto the alias terms. Radarr then maps the `Veerabhadrudu …` release back to Karuppu on its own,
    since `VeeraBhadrudu` is in its alternate titles — no title rewriting needed on our side.
  - **Short aliases and paging** (2026-10-16, Athadu): Radarr lists *He* as an alternate title of
    *Athadu*, and the token `he` substring-matched nearly every title, so the 100-item page was all
    unrelated releases and Athadu itself never made it in. Query words of ≤ 2 chars must now be a
    whole title word (`title_matches`); the query's own title is ranked ahead of alias-only matches
    (`match_rank`); and both feeds honour Torznab `offset`/`limit` (`page_args`). Before that, a
    full page made Prowlarr page on (offsets 0–900, the same 100 items each time, ~30 s a scan) until
    it timed the indexer out (`tamilmv server is currently unavailable … Http request timed out`).
    Collapsed titles are cached (`_title_key`), so only the first search after a restart is slow.
- **Radarr-side settings these feeds depend on** (runtime config in the gitignored `radarr/`, so it is
  not version-controlled — re-apply after a Radarr rebuild):
  - `Settings → Indexers → Whitelisted Hardcoded Subs = esub,esubs,msub,msubs`. Every release on these
    sites is tagged `ESub` (**soft** English subs, not burned in), and Radarr's hardcoded-subs
    specification rejected **100% of movierulz + tamilmv releases** without this — the single biggest
    reason grabs from these two indexers silently never happened. `allowHardcodedSubs` stays `false`,
    so genuine `HC`/`HARDSUB` releases are still rejected.
  - Quality profile **`Language: Any`** (was `Original`). With `Original`, every dub pack
    (`[Hin + Mal + Kan]`, and the Telugu `Veerabhadrudu` cut of a Tamil film) is rejected with
    "Original Language (Tamil) is wanted, but found …". Trade-off: `Any` applies no language
    preference at all — if you later want to *prefer* the original while still allowing dubs, do it
    with a language custom format score, not by putting this filter back.

### tamilmv component (`media-bridge/tamilmv.py`)

A crawler thread + a stdlib Torznab HTTP server (`:8005`, host-published) that scrapes **tamilmv**
(Tamil/Telugu/Hindi/Malayalam/Kannada/English movies + episode-range packs) into a Torznab feed for
Prowlarr. **This replaced the retired MediaFusion 4-container stack** (postgres + redis
+ Rust api/worker), whose compiled tamil spiders were the only part worth keeping — and whose ext.to
scraper (Cloudflare-Turnstile-walled, no config off-switch) 403-thrashed FlareSolverr continuously.

- **Same pattern as movierulz**, and reuses its stateless helpers by import (`fetch`, `parse_magnet`,
  `classify`, `normalize_title`, `title_matches`, `torznab_category`, `error_xml`, `MAGNET_RE`). Own
  sqlite `media-bridge/config/tamilmv.db` (`posts` keyed by `(site, post_id)` — a leftover of the
  two-site days, see below; `releases` keyed by btih). Own Torznab server on `:8005` (server title
  `tamilmv`, apikey `TAMILMV_APIKEY`, every `<item>` carries `<pubDate>` + `FAKE_SEEDERS`).
- **Domain rotation (the key bit): seeds + per-language forum maps come from MediaFusion's UPSTREAM
  GitHub `scraper_config.json`** (`TAMILMV_CONFIG_URL`), refreshed daily and cached to
  `config/tamilmv_config.json`. The **live domain** is found by following the seed's 301 redirect
  (`resolve_base`, plain HTTP — the redirect resolves at Cloudflare's edge, no FlareSolverr). Recovery
  ladder when a base dies: re-resolve → force a GitHub config refresh for a newer `homepage` → still
  dead → ntfy (then set `TAMILMV_SEED_OVERRIDE` as an emergency manual pin).
  **The dead-site ntfy is rate-limited** to one push per `TAMILMV_DEAD_ALERT_INTERVAL` (default 86400s)
  per site, tracked in `dead_alert:<site>` in tamilmv.db's `meta` table. Ungated, it fired on *every*
  crawl pass — ~24/day forever, which drowned the shared ntfy topic (see "ntfy: two topics" above). A
  successful resolve resets the timer to 0, so a site that recovers and dies again alerts immediately
  rather than waiting out the previous outage's window. The per-pass `log()` line is NOT throttled —
  `docker compose logs media-bridge` still shows the true state hourly.
- **tamilblasters was removed 2026-10-19** (dead since 2026-08-23: every domain answered Cloudflare 522).
  `SITES` in `tamilmv.py` lists the sites that are crawled **and served**; old `tamil_blasters` rows
  stay in the DBs and archive (keyed by `site`) but never reach the feed or a topic page, and
  `reparse` skips them. Re-adding a site = an entry in `SITES` (its config key + seed override env var).
- **Archive everything, extract from the archive, serve only what is flagged** (rebuilt 2026-09-30, after
  Seethamma Vakitlo Sirimalle Chettu — a 2023 topic on page ~10 of Telugu forum 25 — was never indexed
  while Radarr burned 16 dead public grabs). Measured on 1tamilmv then: ~33k topics, 68 forums; the old
  crawler read 5 pages per forum (3,010 topics), and the default **last-post** sort moves old topics
  around, so neither "stop at a known page" nor any resume point was sound.
  - **Every topic read is archived** (`archive_view`: title, canonical, breadcrumb + every post on the
    topic's first page, gzipped ≈ 6 KB) in **`config/tamilmv_archive.db`** (separate from the feed DB;
    ~200 MB for the whole site). `extract()` works on that HTML, so a parser fix is
    **`docker exec media-bridge python -m tamilmv reparse`** — no re-crawl. `PARSER_V` bump = older-parsed
    topics are re-extracted locally (`topic_action` → `reparse`) as the walk reaches them. `WALK_EPOCH` bump
    = the backfill walks every forum from page 1 again (only needed when the archive lacks something, e.g.
    v2 on 2026-09-30: the ~5k topics read before the archive existed).
  - **Walking** — three layers, all listings sorted **`&sortby=start_date&sortdirection=desc`**
    (`listing_url`) so new topics only ever appear on top:
    1. **head walk** (`crawl_site` → `crawl_forum`, hourly, `tamilmv-crawler` thread): release forums,
       pages 1..`TAMILMV_MAX_PAGES`, incremental; FlareSolverr fallback on a challenge;
    2. **backfill** (`run_backfill`, its own **`tamilmv-backfill`** thread): **all 68 forums** (from the
       sitemap, `discover_forums` → `forums` table) to their **`data-pages` count**, `TAMILMV_BACKFILL_CHUNK`
       pages per step, continuously; order = config forums → `TAMILMV_EXTRA_FORUMS` → every other forum.
       Progress in meta `walk:<site>:<forum id>` / `walkdone:…`; a resume re-reads one page. **Never stop
       on "empty page"**: IPS answers a page past the end with page 1. **Plain HTTP only**: a CF
       challenge / 429 / 5xx / proxy error raises `PlainUnavailable` → pause `TAMILMV_BACKFILL_PAUSE`
       (1800 s) — it never falls back to FlareSolverr (a multi-day crawl would starve movierulz); a 404
       (deleted topic) doesn't pause. Measured 1.7 s/topic → the whole site ≈ 16–18 h. Logs a progress
       line every 10 min (`backfill tamilmv: N pages, M topics …, K of 68 forum(s) left`);
    3. **sitemap** (`sitemap_sweep`, daily): refreshes the forum list; unseen topics within
       `TAMILMV_SITEMAP_DAYS` are read. Verified against 962 listing-walked topics: none missing;
    4. **audit** (`audit`, once per walk epoch, after every forum is `walkdone`): every topic in the
       FULL sitemap (no window) that isn't archived, is gone, or has a pending attachment gets one more
       try; what still fails is logged `audit_missing`.
  - **Parallel backfill without gaps**: the topics of ONE listing page are fetched by
    `TAMILMV_BACKFILL_WORKERS` (3) threads (`run_topics`; each thread its own requests session; a
    worker inherits the caller's plain-only mode and log role); the walk position moves **only after
    every topic of that page is processed**. A transient failure anywhere on the page (network /
    challenge / 429 / 5xx → `PlainUnavailable`) stops the not-yet-started topics, leaves the position,
    and the page is re-read after the pause (topics already done are no-ops then). A topic that simply
    isn't there (404/410, or a 200 page without `<article>` — moved/hidden stubs redirect home) is
    marked **`posts.gone`** and does NOT block the page. A `.torrent` attachment that fails to download
    sets **`posts.pending`** → the topic is re-extracted from the archive on its next visit, which
    retries only that download. Measured 2026-09-30 (20 pages, 3 workers): 0.65 s/topic, 499 of 499
    listed topics archived or gone, 0 missing → the whole site ≈ 6–7 h.
  - **Uniqueness**: a topic is **(site, topic id)** everywhere — never its URL (the domain rotates and
    the slug changes as series grow; `posts.url` just keeps the latest). `posts` and the archive's
    `pages` are keyed by it (the archive keeps the latest copy: `INSERT OR REPLACE`); `releases` by
    **infohash** (one row per torrent; the first topic keeps it → stable pubDate) with every topic it
    was posted in in **`release_topics`**; `weblinks` by (site, topic, URL) + a stable `lid` hash (a
    topic's links are replaced as a set on each extract); `attachments` by (site, attachment id).
    Listing pages are never stored — only the walk position.
  - **Error log** (`log_error` → **`config/tamilmv_errors.jsonl`**, one JSON object per line, rotates
    to `.1` at 50 MB; never raises): `ts, site, post_id, url, forum, thread (backfill/head/sitemap/
    active/audit/manual), stage (listing/fetch/attachment/extract/audit), type, status, detail,
    outcome (retry/gone/pending/recorded)`. Types: transient `network`/`challenge`/`rate_limited`/
    `server_error` (retry), `not_found`/`no_article`/`http_other` (gone), `attachment_failed` (pending),
    and one-off findings written once per topic (`logged` table): `not_torrent`, `private_torrent`,
    `magnet_unparsed`, `magnet_skipped` (scope rules: single episode / Part), `empty` (served topic with
    no torrent and no link — a likely parser gap), `extract_error` (parser crash; topic archived, fix
    + `reparse`); `listing_failed`, `audit_missing`.
  - **Commands** (`docker exec media-bridge python -m tamilmv …`): `errors [--type T] [--since 24h|7d]
    [--unresolved]` (counts per type, latest examples, resolved vs unresolved against the DB);
    `refetch <topic id | URL> …` (fetch now, re-archive, re-extract, print torrents/links — searchable
    immediately; clears `gone`); `recrawl [--forum ID …] [--refetch]` (reset the walk + audit for all or
    some forums; `--refetch` = every topic archived before now is downloaded again as the walk reaches
    it); `reparse [site]` (re-extract the whole archive, no network); `links <words>`.
  - **Serving** (`is_served`): topic override `posts.serve` > forum override `forums.serve` > automatic =
    a **release forum** (upstream config map + `TAMILMV_EXTRA_FORUMS`: `38`/`60`/`13` DVD9-DVD5, never
    mapped upstream, and `92-post-share-here` with language `auto` = read from the title). Everything is
    extracted from every forum; unserved forums (songs, news…) just never reach the feed. To serve/hide
    by hand: `UPDATE forums SET serve=1|0 WHERE forum_id='18'` or `UPDATE posts SET serve=… WHERE post_id=…`
    in `config/tamilmv.db` (NULL = back to automatic) — takes effect on the next query, no restart.
- **What a topic yields** (`extract`): (a) `magnet:` links; (b) **mangled `http://magnet?xt=…`** links
  (2023-era posts — Seethamma's only magnet) rewritten to `magnet:?`; (c) **`.torrent` attachments**
  (`attachment.php?id=…`, downloadable as a guest) → parsed with `torrentfile.parse_torrent` into a
  magnet with `xl=` + trackers; skipped when a magnet on the page has the same `dn` (same torrent),
  cached in `attachments` so a re-read never downloads twice, and a **private torrent is never turned
  into a magnet**. In forum 25, 246 of 600 topics were attachment-only — invisible before. (d) **web
  links** (shortener / file-host links: gplinks, tnlink, nowshort, gdlink, gdflix, cyberloom…) from the
  **opening post only** (`first_post`; replies post links to other movies, share buttons sit after it)
  into `weblinks`. tamilmv lists each file as `.torrent` + MAGNET + **"DIRECT LINK"** button: a generic
  label takes the name/size of the torrent just above it and records its **btih** ("same file as
  torrent X"). Every link is ad/captcha-gated (cyberloom → countdown page, nowshort → reCAPTCHA; the
  final URL carries a short-lived token), so links are **never downloaded** and saving the gate page is
  useless — the link itself stays valid.
- **`[DL-LINK]` items + topic pages (how links reach Radarr/Sonarr)**: a search (never the empty-query
  RSS) also returns up to `TAMILMV_LINKS_MAX` (50) links as **`[DL-LINK] <file name>`** items — Radarr and
  Sonarr parse `DL-LINK` as the release group (title/year/quality/language intact; `[LINK]`/`[DDL]` could
  collide with real groups). They are **never grabbed and never penalize**: **0 seeders** (the tamilmv
  indexer's minimum is 1 → always rejected "Not enough seeders", but listed in Interactive Search), no
  `magneturl`, and the download URL `/nograb/<lid>.torrent` answers **404** = "release no longer
  available" (not an indexer failure, no blocklist). Every item's `<comments>` (= the **info link** Prowlarr
  passes to Radarr/Sonarr — click the release title) is **`TAMILMV_PUBLIC_URL/t/<site>/<topic id>`**
  (`#l<lid>` highlights the link): the direct links, the live tamilmv link (current domain from meta
  `base:<site>`), the topic's torrents, and the **saved copy** of the posts. `TAMILMV_PUBLIC_URL` is the
  **Tailscale address** `http://100.122.87.20:8005` (Radarr/Sonarr are used the same way at home and
  away; the Jetson does not advertise `10.0.0.0/24`, so a LAN-IP link would break away from home). The
  page is scraped content: `sanitize` strips scripts/iframes/handlers/`javascript:` and a strict CSP
  (`default-src 'none'`, no images — they'd tell image hosts who is looking) makes script impossible.
- **DVD Remux rewrite** (`served_title`): Radarr reads "DVD9"/"DVD5" as a raw disc image (quality DVD-R)
  and rejects it ("Raw Bluray/DVD release") — every tamilmv DVD9 release, though they are single `.mkv`
  remuxes. A DVD9/DVD5 torrent whose magnet `dn` ends in a video extension is served as **"DVD Remux"**
  (Radarr: DVD quality, accepted); an ISO / VIDEO_TS torrent keeps "DVD9" and stays rejected (unimportable).
- **Plain HTTP, FlareSolverr fallback**: pages, sitemap and attachments answer plain requests (no CF
  challenge, 2026-09-30), ~10× cheaper than FlareSolverr. Plain requests go only through
  **`TAMILMV_HTTP_PROXY`** (gluetun's built-in proxy → VPN exit IP); unset = plain HTTP off (FlareSolverr
  only; no attachments, sitemap or backfill) — media-bridge is off the VPN and must not scrape these sites
  from the home IP.
- **Re-read rules** (`topic_action`): new topic / slug changed / never archived → fetch; the empty-post
  retry window is anchored on the topic's **post date** (`posts.posted_at`), not first-seen, or the
  backfill's thousands of old link-only topics would each be re-read every pass for days.
- **Slug-change refetch (`post_needs_fetch(site, pid, url)` + `_slug`)**: unlike movierulz (one post per
  episode batch), tamilmv posts a running season as **one topic** and appends each new batch to it,
  retitling it as it grows (`S03 EP (01-04)` → `S03 EP (01-32)`) — and the listing URL's slug follows. A
  post with magnets used to be "done forever", so a series was read once at its first batch and never
  again (Heart Beat S03 sat at episodes 1-4 in the feed for 8 weeks while the topic held 1-32). A topic
  whose listing slug differs from the stored `posts.url` slug is now re-read; the slug is compared
  host-agnostically so a domain rotation doesn't re-read everything, and `mark_post` always refreshes `url`.
- **…but the listing can't be relied on to show the topic (`refresh_active_series`)**: `crawl_forum`
  stops at the first page of already-known topics, and a season topic started weeks ago sits on page 2+
  even while it grows — Heart Beat S03 went `ep-01-32 → ep-01-36` on page 2 of the web-series forum and
  a full pass fetched 0 topics (2026-09-23). So every pass also fetches, **directly by id**, each series
  topic that gained a release in the last `TAMILMV_ACTIVE_DAYS` (21), each at most every
  `TAMILMV_ACTIVE_REREAD` s (10800; 21600 until 2026-10-02). The stored slug still resolves on the current domain, and
  `process_topic` adopts the page's `<link rel=canonical>` so `posts.url` learns the new name. ~19 topics;
  adds ~2-3 min of FlareSolverr per pass. Log line: `active series: N re-read, M new`.
- **New episodes can still stall at Sonarr, correctly**: Sonarr only grabs episodes its TVDB list has.
  tamilmv often posts a batch before TVDB lists it (Heart Beat E33-36, TVDB stopped at E32), so the
  release is indexed and rejected until TVDB catches up; then Sonarr's 12h refresh + a search grabs it.
  Check `https://skyhook.sonarr.tv/v1/tvdb/shows/en/<tvdbId>` before debugging the indexer.
  Once grabbed, it can stall **again at import**: TVDB lists new episodes as `TBA` for days, and Sonarr's
  `Episode Title Required = Always` blocks importing a TBA episode for 48h after airing (queue shows
  `importBlocked` / "Episode has a TBA title and recently aired" — E33-36 on 2026-09-24). It clears by
  itself; to force it, manual-import the download folder (`GET /api/v3/manualimport?folder=…` — NOT
  `?downloadId=…&seriesId=…`, which returns the series' already-imported library files). Files get
  `TBA` names; Series → Rename Files fixes them once TVDB has titles.
- **Prowlarr wiring**: generic Torznab indexer, `apiPath=/api`, apiKey `= TAMILMV_APIKEY`, baseUrl
  **`http://10.0.0.179:8005/torznab`** (LAN IP, not `media-bridge:8005` — container names don't resolve in
  gluetun's netns; see movierulz's Prowlarr wiring). Querying it hits the local sqlite only (no CF), so keeping RSS
  enabled on this indexer is cheap — the CF cost is entirely in the background crawler.
- **Search behaviour is movierulz's**: `query_releases` calls the shared `search_terms`/`match_rank`,
  so the alias expansion and the "never match everything" guard described above apply here too.

### youtube component (`media-bridge/youtube.py`)

A web form (`serve`, host `:8003`) + a worker thread (`run_worker`) that pulls a **full-movie YouTube
upload into a Radarr movie's folder** — the no-torrent fallback for old / regional / **dubbed** films
that no indexer carries (e.g. Vaaranam Aayiram's Telugu dub "Surya S/o Krishnan"). **Manual URL only**
(no auto-search → never grabs a trailer/wrong cut).

- **Disposable-container download (why, and the gotcha):** the download runs in a throwaway
  `jauderho/yt-dlp` container (multi-arch, bundles yt-dlp + **ffmpeg** for the DASH video+audio merge)
  spawned via the **host Docker socket** and **removed after each job** — so yt-dlp/ffmpeg never bloat the
  media-bridge image. This is **docker-out-of-docker**: media-bridge mounts `/var/run/docker.sock` and
  `group_add: "994"` (host `docker` gid) so uid 1000 can use it; the SDK is `pip install docker`.
  Because a sibling container's bind-mount source is resolved by the **host** daemon (not media-bridge's
  namespace), the worker translates the Radarr movie path `/data/…` → **host** path via `DATA_HOST_PATH`
  (`/home/rosurya/media-server/data`) before mounting it as `/out`. Runs `--user 1000:1000` so files land
  owned by the *arr user.
- **Highest quality:** `YTDLP_SORT=res,vcodec:h264,acodec:aac,br` — highest resolution first, then prefer
  h264 (Jetson Direct Play) + aac, then bitrate. Merged to `.mkv`.
- **Register:** on success → `POST /api/v3/command {RescanMovie}` (Radarr tracks the existing file
  regardless of quality profile) + a best-effort Jellyfin `/Library/Refresh`; ntfy on done/fail.
- **Keeping yt-dlp fresh — `_ensure_image()`, and the trap it fixes:** `YTDLP_IMAGE` is a *moving* tag
  (`:latest`), but docker only fetches an image that is **absent**, so the original pull-once-if-missing
  worker froze `:latest` at its first-pull date **forever**. That is a live failure mode, not a
  hypothetical: YouTube gates each player client behind a **GVS PO token** as it gets to it, and on
  2026-08-30 an 8-week-old image (yt-dlp 2026.07.04) failed every download with `HTTP Error 403` after
  exactly ~10 MB — `android_vr` was the last un-gated client and had just been gated too (`ios`/`mweb`
  warn "requires a GVS PO Token", `web_safari` yields no formats, `tv` says "page needs to be reloaded").
  yt-dlp **2026.08.19** fixed it; nothing in this repo was wrong. So the worker now re-pulls a moving tag
  every `YTDLP_PULL_INTERVAL` (default 21600s, tracked in a `meta` table in `youtube.db`) at startup and
  before each job, logging the resolved yt-dlp version. **A pull failure is deliberately non-fatal when a
  local copy exists** (registry blip / offline box must not block a working download); a pinned version or
  `@sha256` ref is never re-pulled (`_mutable_tag`). **DIUN does not cover this image** — it watches
  *running* containers and this one exists only while a job runs.
- **Self-heal on stale-yt-dlp failures:** if yt-dlp exits non-zero and the tail matches
  `HTTP Error 403|Requested format is not available|PO Token` (`STALE_RE`), the worker force-re-pulls the
  image and retries the job **once** (`--continue` resumes the partial). Error text stored/notified is
  `_tail_lines()` — progress lines (`[download] 12.3% of …`) are stripped, since they otherwise fill the
  whole tail and bury the one `ERROR:` line the UI and ntfy show.
- **Queue/resume:** jobs live in `config/youtube.db` (sqlite); one download at a time. A restart mid-job
  resets `downloading → queued` and yt-dlp `--continue` resumes the partial. `[youtube]` log tag.
- **Usage:** open `http://10.0.0.179:8003/` (the reserved Ethernet IP; it also answers on the Wi-Fi address), pick a Radarr movie from the dropdown (add it in Radarr
  first if missing), paste the YouTube URL, Queue. The form also lists recent job status, with a **retry**
  button on failed rows (`POST /retry` → `requeue()`, which clears the error and sets `queued`; guarded to
  `status='failed'` so a finished movie is never re-downloaded). `:8003` is an **unauthenticated LAN
  control endpoint** (consistent with the other host-published feeds).
- **If YouTube ever gates `android_vr` for good** (a newer yt-dlp stops being enough): the answer is a
  `bgutil-ytdlp-pot-provider` sidecar minting GVS PO tokens, which needs the plugin baked into a custom
  yt-dlp image. Not needed as of 2026-08-30.
