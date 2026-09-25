# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted media automation stack defined by a single `docker-compose.yaml`, running on a
**Jetson Nano Super (aarch64)**. Pipeline: **search** (Prowlarr) → **automate** (Sonarr TV / Radarr
movies) → **cloud download** (Seedr) → **stream** (Jellyfin). The custom Python code is one service,
`media-bridge/` (six responsibilities in one container: seedr bridge + jellyfin-reaper + movierulz
scraper/Torznab feed + tamilmv/tamilblasters scraper/Torznab feed + daddylive live-TV resolver +
youtube grabber); everything else is an off-the-shelf image. Service config/state directories
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
docker compose logs -f media-bridge               # follow its logs ([seedr]/[reaper]/[movierulz]/[tamilmv]/[daddylive]/[youtube] tags)
docker restart prowlarr flaresolverr sonarr radarr  # REQUIRED after any gluetun restart (see below)
```

There is no build/lint/test tooling — `media-bridge` is a small Python package run directly
(`CMD ["python", "-u", "main.py"]`, which starts its six components as 11 supervised threads). To
exercise a change, rebuild the service and watch its logs.
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
  `media-bridge` host-published feeds (movierulz Torznab `8002`, youtube grabber `8003`, daddylive live-TV `8004`, tamilmv Torznab `8005`) keep serving,
  since they are published on the host, not on gluetun.
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

## The `/data` single-root convention

`./data` is mounted into sonarr, radarr, media-bridge, and bazarr at the same path so imports are
hardlink-friendly and paths map 1:1 across services. Layout:
- `data/blackhole/{sonarr,radarr}/` — Sonarr/Radarr (configured with a **Torrent Blackhole** download
  client, since Seedr has no native *arr connector) drop `<release>.magnet` files here.
- `data/downloads/{sonarr,radarr}/` — media-bridge's seedr component writes finished downloads here; the
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
alerts (a dead tamilblasters seed re-alerting every crawl pass, ~24/day at priority 4) buried DIUN's
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

One container running six components as supervised daemon threads (`main.py` → each in a try/except
wrapper that logs + ntfy-alerts + restarts, so one crash can't take the others down). It is **off the
VPN** on the default bridge net; it reaches the *arr apps and FlareSolverr via `http://gluetun:<port>`
and host-net Jellyfin via `host.docker.internal`. Shared helpers live in `common.py` (`clog`, `norm`,
`ntfy`). Logs are tagged `[seedr]` / `[reaper]` / `[movierulz]` / `[tamilmv]` / `[daddylive]` / `[youtube]`. Rebuilding/restarting this container to
ship a movierulz change also restarts the seedr loop mid-download — safe, since seedr resumes from its
`.bridge-state` JSON.

**Port bindings: all interfaces, never a literal LAN IP** (changed 2026-08-30, when the Jetson gained a
wired link and became dual-homed — Ethernet `enP8p1s0` and Wi-Fi `wlP1p1s0`, both on `10.0.0.0/24`;
Ethernet holds the long-standing `10.0.0.179` reservation so existing clients and the router's 443 forward
moved to the wire untouched; Wi-Fi is statically pinned to `10.0.0.180` (NetworkManager profile `SRP`,
`ipv4.method=manual`), with a matching router DHCP reservation on `f8:3d:c6:57:20:87` holding that address
out of the pool. Nothing in this stack depends on the Wi-Fi address any more, which is the point — do not
re-pin it anywhere).
The four feeds (`8002`–`8005`) publish as plain `"800x:800x"`. Binding a literal address here has two
failure modes, both hit in practice: it (1) **dies with its NIC** — and since all four binds live in one
container, a Wi-Fi blip stopped Docker from starting `media-bridge` *at all*, taking `[seedr]` and
`[reaper]` down with it; and (2) **races the DHCP lease at boot**, because `docker.service` orders on
`network-online.target`, which one interface can satisfy before another's lease exists — the same race
that took Immich down for 10h on 2026-08-09. The retired `never 0.0.0.0` comment justified itself as
protection against a changed router forward, which it never was: a forward targets a LAN IP and the
sockets were bound to exactly that LAN IP. **What actually keeps these off the internet is that the
router forwards only 443** (see `~/exposer`) — so never forward `8002`–`8005`.

**Open-relay caveat on `/hls`** (pre-existing, unrelated to the binding change): `_proxy_hls` base64-decodes
the path and `SESSION.get`s it with no scheme or host allowlist, so anyone who can reach `:8004` can make
the Jetson fetch an arbitrary URL and read the bytes back — including loopback and the router's admin page.
LAN-only today. Fixing it means allowlisting upstream hosts in `_proxy_hls`, not a firewall rule (the LAN is
exactly where it is reachable from).

### seedr component (`media-bridge/seedr.py`)

Polling loop (`process_new_magnets` → `check_completions`, default every 30s) bridging the *arr
blackhole to Seedr's REST API (HTTP Basic auth; password is base64 in `SEEDR_PASSWORD_B64` because a
literal `$` breaks Docker/shell interpolation). Per magnet: add to Seedr → poll → download finished
files into the watch folder → delete from Seedr to free space.

Non-obvious invariants — preserve these when editing:
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
- **`_name_matches` is the single result/supersede matcher** — used by `check_completions` (both the
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
  already finished it is no longer in `root["torrents"]`, so it falls back to `_name_matches` over
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
generic Torznab indexer for Prowlarr — the tamilmv/tamilblasters spiders are compiled into the
MediaFusion image and can't be taught a new site, so we scrape this one ourselves.

- **Crawler** (`run_crawler` → `crawl_once`, every `CRAWL_INTERVAL`, default hourly): paginates the
  **master `/movies` listing** (`MOVIERULZ_MOVIES_PATH`) newest-first. One master source deliberately
  replaces the old per-language category walk, because it covers **every language and type including
  `*-dubbed`** — nothing is left out. `MOVIERULZ_BASE` is required and lives in `.env`, out of source;
  unset, the crawler disables itself but the Torznab feed still serves.
  **The domain rotates, and a dead one looks like a FlareSolverr fault, not a DNS one:** every page
  fetch fails with `500 Server Error ... /v1` from FlareSolverr, which reads as Cloudflare trouble.
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
  `backfill_done=1` and switches to incremental: stop once a whole page is already-known, floored by
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
  handled here and shared with tamilmv (which imports `search_terms`/`query_matches`):
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

A crawler thread + a stdlib Torznab HTTP server (`:8005`, host-published) that scrapes **tamilmv +
tamilblasters** (Tamil/Telugu/Hindi/Malayalam/Kannada/English movies + episode-range packs) into a
Torznab feed for Prowlarr. **This replaced the retired MediaFusion 4-container stack** (postgres + redis
+ Rust api/worker), whose compiled tamil spiders were the only part worth keeping — and whose ext.to
scraper (Cloudflare-Turnstile-walled, no config off-switch) 403-thrashed FlareSolverr continuously.

- **Same pattern as movierulz**, and reuses its stateless helpers by import (`fetch`, `parse_magnet`,
  `classify`, `normalize_title`, `title_matches`, `torznab_category`, `error_xml`, `MAGNET_RE`). Own
  sqlite `media-bridge/config/tamilmv.db` (`posts` keyed by `(site, post_id)` since topic ids collide
  across the two sites; `releases` keyed by btih). Own Torznab server on `:8005` (server title
  `tamilmv`, apikey `TAMILMV_APIKEY`, every `<item>` carries `<pubDate>` + `FAKE_SEEDERS`).
- **Domain rotation (the key bit): seeds + per-language forum maps come from MediaFusion's UPSTREAM
  GitHub `scraper_config.json`** (`TAMILMV_CONFIG_URL`), refreshed daily and cached to
  `config/tamilmv_config.json`. The **live domain** is found by following the seed's 301 redirect
  (`resolve_base`, plain HTTP — the redirect resolves at Cloudflare's edge, no FlareSolverr). Only the
  **content pages** are CF-walled and go through the same `FLARESOLVERR_URL` as movierulz. Recovery
  ladder when a base dies: re-resolve → force a GitHub config refresh for a newer `homepage` → still
  dead → ntfy (then set `TAMILMV_SEED_OVERRIDE`/`TAMILBLASTERS_SEED_OVERRIDE` as an emergency manual pin).
  **The dead-site ntfy is rate-limited** to one push per `TAMILMV_DEAD_ALERT_INTERVAL` (default 86400s)
  per site, tracked in `dead_alert:<site>` in tamilmv.db's `meta` table. Ungated, it fired on *every*
  crawl pass — ~24/day forever, which drowned the shared ntfy topic (see "ntfy: two topics" above). A
  successful resolve resets the timer to 0, so a site that recovers and dies again alerts immediately
  rather than waiting out the previous outage's window. The per-pass `log()` line is NOT throttled —
  `docker compose logs media-bridge` still shows the true state hourly.
- **Config shapes differ**: tamilmv uses `supported_search_forums` (33 forums), tamil_blasters uses
  `catalogs` (28) — `_parse_site_cfg` normalizes both to `[(forum_path, language)]`.
- **Crawl**: per site → `resolve_base` → for each forum `…/index.php?/forums/forum/<id>/page/<n>/` via
  FlareSolverr, extract full topic URLs (`TOPIC_RE`, slug required so nav links are ignored), fetch each
  topic, `MAGNET_RE` + `parse_magnet` (language from the forum map). Bounded per pass by
  `TAMILMV_MAX_PAGES` (per-forum cap) + incremental all-known stop; empty-post retry as in movierulz.
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
  `TAMILMV_ACTIVE_REREAD` s (21600). The stored slug still resolves on the current domain, and
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
- **Search behaviour is movierulz's**: `query_releases` calls the shared `search_terms`/`query_matches`,
  so the alias expansion and the "never match everything" guard described above apply here too.

### daddylive component (`media-bridge/daddylive.py`)

A refresher thread + a stdlib HTTP server (`:8004`, host-published) that turns DaddyLive (DLHD) into a
Jellyfin **Live TV** source (M3U tuner + XMLTV EPG). Built because nothing off-the-shelf works on this
box: MediaFusion's Rust build never ported its live scrapers, and Cabernet's DaddyLive plugin was taken
down. **Why a custom resolver and not a community M3U feed:** those publish bare `*.newkso.ru` URLs that
are **Cloudflare-walled** (403 even with the right headers, even via FlareSolverr). DaddyLive's *own*
player flow instead resolves to a different, **token-authed CDN (`phantemlis.top`, segments on Cloudflare
R2) that needs NO request headers and IS reachable server-side** — so we resolve each channel the way the
site's player does, on demand.

- **Resolution chain** (`resolve`, per channel id, plain server-side HTTP — not CF-walled):
  `{DLHD_BASE}/{folder}/stream-<id>.php` → iframe → `{embed_host}/premiumtv/daddy3.php?id=<id>` → page JS
  `source: window.atob('<base64>')` → decode → `https://…phantemlis…/premium<id>/index.m3u8?md5v1=…&md5v2=…&expires=…`.
  The embed host **rotates** (discovered dynamically per request); `DADDYLIVE_FOLDERS`
  (default `cast,stream,watch,plus,casting,player` — `cast`/Player 2 is tried first, see below) are tried
  in order so one down folder doesn't kill a channel. The `md5/expires` tokens are **self-authorizing (no Referer/UA) and minted fresh
  per fetch (~74min TTL)** → resolved on demand. The site's `watch.php?id=<id>` shows **6 players = the 6
  `DADDYLIVE_FOLDERS`** (Player 1=stream, 2=cast, 3=watch, 4=plus, 5=casting, 6=player); each folder's
  `stream-<id>.php` embeds a **different provider** — Player 1/3 → `daddy3`→phantemlis, **2 → a rotating
  Clappr embed** (`*.pages.dev/embed/<slug>` → e.g. `inproviszon.st`), 4 → `welovetocare.shop`,
  **5 → `cdnlivetv.tv`/`tezgoal.com` (oplayer)**, 6 → `popcdn.day`. `_extract_source` understands four
  provider formats: phantemlis' `atob(...)`; the cdnlivetv **oplayer** (`_extract_oplayer_source`:
  `source:{src:VAR}` where `VAR = atob(a)+atob(b)+…`, reassembled from base64 parts →
  `api.cdnlivetv.tv/secure/api/v1/<id>/playlist.m3u8?token=…`); **lovetier** (Player 6, reached via popcdn's
  iframe → `lovetier.bz/player/<slug>`), a plain `streamUrl:"…index.m3u8?token=…"` JSON literal
  (`STREAMURL_RE`, unescape `\/`) → `deviantart.lovetier.bz`; and the **Player 2 Clappr embed**
  (`CDNBASE_RE`), whose page has no literal `.m3u8` — it builds the URL as `CDN_BASE + slug + '.m3u8'`
  (`CDN_BASE` scraped from the page, `slug` = `_embed_slug()` off the `/embed/<slug>` iframe URL) →
  e.g. `inproviszon.st/fox-usa.m3u8`. That playlist **requires the embed-page Referer** (CF 403 without),
  but its segments are **PNG-disguised TS** (70-byte fake-PNG header + MPEG-TS, on `cdn-editing-temp.picsart.com`)
  that **403 if a Referer is sent** — so segment fetches drop Referer/Origin (`_drop_referer`, in both
  `_validate_source` and the `/hls` relay) and the PNG lead-in is trimmed to the first TS sync
  (`_strip_png_decoy`). embed host, slug, and CDN all **rotate** — everything is scraped per resolve.
  **welovetocare** (P4) is a JS SPA that reads `location.hash` (nothing server-side) and **tezgoal** (P5 alt)
  is unreachable server-side — both still unparsed. So e.g. FOX USA (54) plays via Player 2, Willow Cricket
  (346) via cdnlivetv, Willow XTRA (598) via lovetier.
  `_resolve_from_page` walks folders in order and returns the first source that passes **segment-level
  validation** — `_validate_source` descends master→variant→media playlist and requires a real segment to
  return bytes, so a player whose playlists load but whose `.ts` segments are dead (a common failure, e.g.
  phantemlis returning `500` on the child playlist — currently site-wide) is **rejected** (logged
  `rejecting unplayable source`) and the next player is tried. So a channel with only phantemlis + no
  alternate (some feeds) can 502 while phantemlis is down; one with a live Player 5 (e.g. Willow Cricket 346)
  resolves via cdnlivetv.
- **Favorites precheck** (`run_precheck`, `DADDYLIVE_FAVORITES` = comma-sep ids, every `DADDYLIVE_PRECHECK`s,
  default 240): re-resolves+validates favorite channels in the background and keeps a **warm cache**
  (`_warm`, TTL `max(PRECHECK*3, 300)`) that `resolve()` checks before the 60s `_resolve_cache`, so a
  favorite opens instantly from a known-live player. Logs only online/offline **transitions** per channel.
- **HLS rewriting proxy (required, NOT a 302 redirect):** DaddyLive serves HLS segments with **disguised
  extensions** (`.js`/`.zst`/`.jpg` on Cloudflare R2) that ffmpeg's HLS demuxer **rejects** (`not in
  allowed_segment_extensions`), and Jellyfin gives no way to pass `-allowed_extensions ALL`. So `/live/<id>`
  fetches the resolved playlist and **rewrites every sub-playlist/segment/key URI** to route back through
  `/hls/<b64-upstream-url>.<m3u8|ts|key>` (`rewrite_playlist`), relaying the bytes with clean
  `.ts`/`vnd.apple.mpegurl` types so ffmpeg accepts them. The M3U points at `/live/<id>.m3u8` (extension
  hint so Jellyfin picks the HLS demuxer). Verified: ffprobe sees h264+aac and a transcode runs clean.
- **Channel list + EPG come from the public nightah/daddylive feed** (`DADDYLIVE_M3U_URL`/`_EPG_URL`): its
  `premium<N>` == our channel id, so we keep its names/logos/tvg-id/group + EPG and just swap the dead
  newkso URL for our `/live/<id>`. Channels upserted into `media-bridge/config/daddylive.db` (sqlite). Feed
  uses `tvg-id="test"` as a placeholder for ~136 channels lacking EPG mapping — expected, not a bug.
- **HTTP server** (`serve`): `GET /daddylive.m3u` (channel table; URLs = `/live/<id>.m3u8`),
  `GET /epg.xml` (cached pass-through XMLTV), `GET /live/<id>[.m3u8]` (resolve → rewritten master playlist),
  `GET /hls/<b64>.<m3u8|ts|key>` (proxy/relay). Some ids 502/offline at any moment — expected.
- **Jellyfin wiring**: Live TV → M3U Tuner `http://127.0.0.1:8004/daddylive.m3u`, XMLTV guide
  `http://127.0.0.1:8004/epg.xml` (Jellyfin is host-net → reaches the host-published port; the feed
  survives gluetun restarts since it's published on the host, not gluetun). **The M3U and the rewritten
  `/hls` URLs now derive their base from the request's own `Host` header** (`Handler._request_base_url()`,
  threaded into `rewrite_playlist(..., public=)` → `_proxy_uri`), so a client that dials the Ethernet IP
  gets Ethernet URLs back and one that dials Wi-Fi gets Wi-Fi URLs — nothing is pinned to a NIC.
  `DADDYLIVE_PUBLIC_URL` is now only the **fallback** for callers with no request context (the precheck
  thread). It must still not be `127.0.0.1` (that would resolve to the TV itself). Before 2026-08-30 it
  was hardcoded to the Wi-Fi IP, which made every segment URL unreachable whenever that NIC was down.
- **Playback notes**: `resolve()` caches the master URL per channel for `DADDYLIVE_RESOLVE_TTL` (60s) so
  Jellyfin's repeated opens (probe + transcode + retry) don't each re-resolve — fixes "player error then
  plays in secs". Jellyfin's **Direct Stream (`-c copy`) remux is flaky on some live streams** (FFmpeg
  code 8) while transcode is reliable → on the Jetson cap the client's quality to ~720p to force
  (software) transcode. Offline channels legitimately 502.
- **Rotation maintenance**: only a change to the dlhd *domain* needs a one-line `DLHD_BASE` update in
  `.env`; embed/CDN host rotations are handled automatically. Watch for `atob(…)`/iframe-pattern changes.

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
