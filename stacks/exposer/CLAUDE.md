# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The **public edge** for every self-hosted stack, running on the Jetson Orin Nano Super (aarch64,
`10.0.0.179`; the router forwards 443 here). Since 2026-10-20 the application stacks run on the **HP**
(`10.0.0.64`), so Caddy proxies Jellyfin/Immich/Nextcloud to `10.0.0.64:8096/2283/8081` over the LAN,
the fail2ban jellyfin + nextcloud jails read **Caddy's** access logs (their app logs are on the HP),
the old LAN pass-through on `:8096` was removed on 2026-10-20 (clients use the public name or `10.0.0.64:8096`), and the Jetson's own
DIUN + autoheal live in this stack. **Deployed by the homelab pipeline** to `/srv/homelab/exposer` on
the Jetson (Jetson runner; secrets from Bitwarden `homelab-dev`, `EXPOSER__*`, via the `jetson-dev`
machine account). Image updates come as Renovate PRs. `~/exposer` is the retired hand-run copy. Off-the-shelf
containers, no custom code — two local builds: `crowdsec-bouncer` only wraps an official
release binary, and `caddy` is stock Caddy + two plugins, for the country filter and the WAF (see
*Geo filter* and *WAF*):

| service | role |
|---|---|
| `caddy` | TLS termination (Let's Encrypt) + reverse proxy + **US/India-only geo filter** + **WAF** hook; owns host `:80`/`:443` |
| `duckdns` | keeps `rosurya.duckdns.org` pointed at the dynamic Comcast WAN IP |
| `fail2ban` | tails the public-facing auth logs and bans brute-forcers via host iptables |
| `crowdsec` | reads Caddy's access logs for scans/probes/CVE attempts; pulls the community blocklist; **is the WAF** (AppSec, loopback `:7422`) |
| `crowdsec-bouncer` | drops the IPs crowdsec has decided on, via an ipset rule on host `INPUT` |

It was split out of `../media-server` so that the application stacks are independently
restartable. Before the split, `docker compose down` in `media-server/` took Immich,
Nextcloud and TLS down with it, and exposing a new service meant editing the media stack's
compose file.

**Only host port 443 is forwarded on the router.** Port 80 is blocked by Comcast, so ACME
issuance happens over TLS-ALPN-01 on `:443` — Caddy falls back to it automatically. This is
why there is no HTTP-01 challenge and no `:80` redirect worth relying on.

## Commands

```bash
docker compose up -d                          # bring up / apply changes
docker compose config -q                      # validate compose before applying
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile   # reload vhosts, no downtime
docker compose logs -f caddy
docker exec fail2ban fail2ban-client status   # jails + current bans
docker exec fail2ban fail2ban-client status immich
docker exec fail2ban fail2ban-client set immich unbanip 1.2.3.4
docker exec crowdsec cscli decisions list     # local bans (add -a to include the community blocklist)
docker exec crowdsec cscli alerts list        # what triggered them
docker exec crowdsec cscli decisions delete --ip 1.2.3.4
docker exec crowdsec cscli metrics show acquisition   # is it actually reading the Caddy logs?
docker exec caddy geoip blocked 50            # last 50 requests the geo filter or the WAF refused
docker exec caddy geoip 1.2.3.4               # country of an IP + allowed or not
docker logs crowdsec 2>&1 | grep 'WAF block'  # every WAF block: rule name + client IP
docker exec crowdsec cscli metrics show appsec   # WAF: requests checked / blocked, hits per rule
```

Validate a Caddyfile edit *without* touching the running server:

```bash
docker run --rm -e GEO_ALLOW_COUNTRIES="US IN" -e CROWDSEC_CADDY_BOUNCER_KEY=x -v ./caddy:/etc/caddy:ro \
  caddy-geo-local caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

(Stock `caddy:*` images can't validate it any more: they don't know `maxmind_geolocation`, `crowdsec`
or `appsec`.)

## Geo filter: public vhosts answer only the US and India

**Rule (since 2026-10-20): every public vhost (`jellyfin`, `immich`, `nextcloud`, `ha`) serves only
clients that geolocate to `GEO_ALLOW_COUNTRIES` — `US IN` — plus LAN (10/8, 172.16/12,
192.168/16), loopback and Tailscale (100.64/10), which always pass.** Everyone else gets a plain
`403 Not available from your location (your IP: x.x.x.x). [geo filter]` with the response
header `X-Blocked-By: geo`. The `*.home.rosurya.duckdns.org` names are stricter still
(`home_only`: LAN + Tailscale only) and don't use it; the plain-HTTP short names are LAN-only.

How it works:
- `(geo_filter)` in `caddy/Caddyfile`, imported first in each public site file. **A new public
  vhost must `import geo_filter`** — CI fails a site file that imports `security_headers` more
  often than `geo_filter`.
- The list lives in ONE place: `GEO_ALLOW_COUNTRIES` in `docker-compose.yaml` (caddy service).
  The Caddyfile reads it as `{$GEO_ALLOW_COUNTRIES}`, and so does the `geoip` helper. An empty or
  malformed list would make the plugin **let everyone in silently**, so the container refuses to
  start instead (`geoip-entrypoint`), and the deploy rolls back.
- Plugin: `porech/caddy-maxmind-geolocation`, built in by `caddy/image/Dockerfile` (xcaddy).
  It's pinned to a **commit**, because its `v1.0.0` tag was moved after release and Go's checksum
  database rejects it. Never bypass that check.
- Database: **DB-IP "IP to Country Lite"** (free, CC BY 4.0, no account, monthly). A copy is
  baked into the image so Caddy always starts, even offline. The live copy is
  `caddy/geoip/country.mmdb` (gitignored). The container's refresher downloads each new month's
  file, checks that it maps 8.8.8.8 → US and 49.36.0.1 → IN, swaps it in, then runs
  `caddy reload --force`. The plugin keeps the old file open until a reload.
- **Fails closed**: if the database can't be read, every non-LAN/Tailscale client is refused.
  A geo 403 for *everyone*, US included, means `docker exec caddy geoip info` / `geoip verify`.
- Certificate renewal is unaffected: TLS-ALPN-01 completes in the TLS handshake, before any
  HTTP handler runs, so Let's Encrypt's validators abroad still get through.
- **CrowdSec ignores geo 403s** (third rule in `crowdsec/parsers/edge-apps-whitelist.yaml`,
  keyed on that header). Otherwise a phone app retrying 10× in 10 s would trip `http-probing` and
  turn the readable 403 into a silent firewall ban. fail2ban never sees them either: refused
  requests never reach a login.
- Nextcloud's `.well-known` `redir`s run before `respond` (Caddy's directive order), so a refused
  client can still get those 301s. They only point back at the same host, which then 403s.
- The test run on 2026-10-20, over every Caddy log back to late July: every successful login came
  from the US or the LAN except one — Safari on an iPhone with **iCloud Private Relay**, whose
  relay address (`172.225.236.249`) DB-IP places in Greenland. Everything else refused was scanners
  (`/wp-config.php~`, `/actuator/heapdump`, …).

### Abroad and something doesn't load — debug it in this order

1. **Is it the geo filter?** Open the site in a browser. The geo filter's 403 page says
   `[geo filter]` and shows your IP. No answer at all (a timeout) is **not** the geo filter. It's a
   firewall ban: `docker exec crowdsec cscli decisions list --ip <ip>`,
   `docker exec fail2ban fail2ban-client banned`. An app (Jellyfin/Immich/HA) usually just says
   "can't connect", so check with a browser or step 2.
2. **What does the box see?** SSH to the Jetson over Tailscale (`ssh rosurya@100.122.87.20`), then:
   ```bash
   docker exec caddy geoip blocked 50     # recent refusals: time, IP, country, host+path
   docker exec caddy geoip <your ip>      # e.g. "203.0.113.7  GB BLOCKED (allowed: US IN)"
   docker exec caddy geoip info           # database month, allowed list, self-test
   docker logs caddy 2>&1 | grep geoip    # refresher activity / failures
   ```
3. **Get in without changing anything** — Tailscale is already set up (subnet route `10.0.0.0/24`):
   turn it on and use the LAN addresses, which skip the filter: Jellyfin `http://10.0.0.64:8096`,
   Immich `http://10.0.0.64:2283`, Home Assistant `http://10.0.0.179:8123`. The public names still
   resolve to the public IP even over Tailscale, so they stay blocked. Split DNS covers only
   `home.rosurya.duckdns.org`, and the Jetson is not a Tailscale exit node (making it one would let
   every public name work abroad: `sudo tailscale set --advertise-exit-node` + approve in the
   admin console — not done).
4. **Wrong country for a good reason?** iCloud Private Relay (Safari, and Mail's privacy protection)
   and VPN apps exit from relay IPs that can geolocate anywhere. Turn them off for the site.
   Roaming mobile data usually exits in the SIM's home country, so it normally passes.
5. **Allow another country**: add its ISO code to `GEO_ALLOW_COUNTRIES` in `docker-compose.yaml`
   (e.g. `US IN GB`), then open a PR and merge. The deploy recreates `caddy` (a few seconds of
   downtime). Emergency, from SSH: edit `/srv/homelab/exposer/docker-compose.yaml` the same way,
   then `cd /srv/homelab/exposer && docker compose up -d caddy`. The next pipeline deploy reverts
   it unless the change is also in git.
6. **Database looks wrong or stale**: `docker exec caddy geoip refresh` (downloads, verifies,
   reloads). If the downloads keep failing, the baked copy still works; it's just older.
7. **A 403 that says `[waf]`** instead of `[geo filter]` is the WAF, not the country — see *WAF* below.

## WAF: every public request is checked against known exploits

**Since 2026-10-20 every request to a public vhost passes the geo filter first, then the WAF:**
CrowdSec's AppSec component checks it (method, URL, headers, first 1 MiB of the body) against its
blocking rules and answers allow/block in ~4 ms. A block is a plain
`403 Request blocked by the web application firewall (your IP: x.x.x.x). [waf]` with the response
header `X-Blocked-By: waf` (in the access log, so `geoip blocked` lists it).

What it blocks — `crowdsecurity/appsec-default` (`crowdsec/acquis.d/appsec.yaml`):
- **Virtual patches** (`crowdsecurity/vpatch-*`, ~190): one rule per actively exploited CVE
  (Grafana path traversal, Fortinet, Jira, PHP-CGI, …), plus `.env` / `.git` / `.svn` grabs and
  Symfony/Laravel debug probes. They are narrow — each matches one exploit's exact request — which
  is why they almost never hit real traffic.
- A few generic rules (`crowdsecurity/generic-*`: WordPress uploads, Freemarker SSTI).
- Report-only (never block): `crowdsecurity/experimental-*` (e.g. "no User-Agent").
- Honest scope (2026-10-20): **no rule targets Jellyfin, Immich, Nextcloud or Home Assistant yet.**
  The value today is killing exploit scanners on their first request (instead of after the fact,
  from the logs) and banning them: two *different* rules hit by one IP within 60 s =
  `crowdsecurity/appsec-vpatch` ban, enforced by the firewall bouncer. If one of our apps gets a
  mass-exploited CVE, the hub usually ships a vpatch rule within days — `hub-refresh` picks it up.

How it's wired:
- `caddy/Caddyfile`: global `crowdsec { … }` block (LAPI `127.0.0.1:8089`, AppSec
  `127.0.0.1:7422`) + `(waf)` snippet, imported after `geo_filter` in each public site file
  (**CI fails a public site file without `import waf`**). `order appsec before reverse_proxy` puts
  it after `respond`, so geo-refused requests never reach CrowdSec.
- Plugin: `hslatman/caddy-crowdsec-bouncer` (its `appsec` package only), pinned in
  `caddy/image/Dockerfile`. Only the AppSec handler is used; bans are still enforced by the
  firewall bouncer, not by Caddy.
- **Bouncer key**: Caddy has its own (`CROWDSEC_CADDY_BOUNCER_KEY` = the firewall key + `-caddy`,
  derived in compose; crowdsec registers it as bouncer `caddy` via `BOUNCER_KEY_caddy`). Each bouncer
  needs its own key — LAPI tracks decision-stream progress per key. Registration only happens
  if the bouncer doesn't exist yet: after rotating `CROWDSEC_BOUNCER_KEY`, also
  `docker exec crowdsec cscli bouncers delete caddy firewall` and recreate crowdsec.
- **Fail-open**: CrowdSec down, or no answer in 500 ms → the request passes unchecked (Caddy logs
  `appsec component unavailable`). The sites never go down because of the WAF. Because that also
  hides a broken WAF, `deploy/smoke.sh` requires a `/.env` probe to come back `X-Blocked-By: waf`.
- **Bodies**: only the first 1 MiB is sent to CrowdSec (`appsec_max_body_bytes`); the rest streams
  straight through. Without the cap the plugin would buffer whole uploads in RAM.
- **Rules stay fresh**: the image upgrades hub items only at container start, so compose wraps its
  entrypoint in `crowdsec/hub-refresh`: daily `cscli hub update` + `upgrade`, and a SIGHUP reload
  only if something changed (`docker logs crowdsec | grep hub-refresh`). A reload pauses the WAF for
  ~5 s (fail-open). `post-deploy.sh` also reloads CrowdSec, so edits to `crowdsec/` apply on deploy.
- LAN/Tailscale requests are checked too (so a false positive shows up at home first), but CrowdSec's
  default whitelist means a private IP is never banned.
- Each block also becomes a CrowdSec alert (shared with the community like the log-based ones).

Tested 2026-10-20 before going live (throwaway CrowdSec + Caddy on spare ports):
- Replayed **all 23,167 distinct requests** in the Caddy logs (Jul–Oct). 659 would have been blocked,
  every one a scanner probe; **0 real app requests** (Immich answers 200 to any path, so probes
  there "succeeded" in the logs — that's the SPA fallback, not a hit).
- Jellyfin playback through it: 93 MB/s vs 94 direct, seek latency unchanged; 300 parallel Immich
  requests 0.24 s vs 0.15 s; 400 MB uploads (HTTP/1.1, HTTP/2, chunked, multipart) arrive
  byte-identical with Caddy at ~30 MiB RAM; HA, Immich and Jellyfin WebSockets, Nextcloud WebDAV
  verbs all pass.
- CrowdSec stopped / hung / reloading → sites keep working (hung = +0.5 s per request).

### A request is blocked by the WAF (403 `[waf]`) — what to do

1. **Which rule?** `docker logs crowdsec 2>&1 | grep 'WAF block'` → `WAF block: <rule> from <ip>`,
   and `docker exec caddy geoip blocked 50` for the URL (lines marked `waf`).
2. **Banned too?** A timeout instead of the 403 page = firewall ban:
   `docker exec crowdsec cscli decisions list --ip <ip>`; lift it with
   `docker exec crowdsec cscli decisions delete --ip <ip>`.
3. **Switch the rule off** in `crowdsec/appsec-configs/waf-exceptions.yaml` (examples inside: the
   whole rule, or only for one site / path) and merge — the deploy reloads CrowdSec. Emergency:
   edit `/srv/homelab/exposer/crowdsec/appsec-configs/waf-exceptions.yaml` on the Jetson, then
   `docker exec crowdsec sh -c 'kill -HUP "$(pidof crowdsec)"'` (and put it in git after).
4. **Switch the whole WAF off** (emergency): comment out the `appsec` line in `(waf)` in
   `/srv/homelab/exposer/caddy/Caddyfile`, then
   `docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile`. The next
   pipeline deploy turns it back on unless the change is in git (and smoke.sh would fail — so a
   permanent "off" also means removing the `waf_blocks` smoke check).

## Exposing a new service

This is the whole point of the repo — it must not require editing shared files.

1. Publish the service on **loopback + LAN only** in its own stack, never `0.0.0.0`
   (e.g. `"127.0.0.1:2283:2283"` and `"10.0.0.179:2283:2283"`). It stays off the public
   internet even if the router forward changes; Caddy is the only public door.

   Check the port is actually free first — `ss -tln`. Several containers on this box run
   `network_mode: host` (jellyfin, bazarr, zigbee2mqtt, homeassistant, and this stack's
   caddy/fail2ban) and grab host ports without declaring them in any `ports:` block, so
   compose cannot warn you about the clash. Nextcloud sits on **8081** precisely because
   zigbee2mqtt's frontend defaults to 8080 and claims it first.
2. Add `caddy/sites.d/<name>.caddy`:

   ```caddy
   <name>.rosurya.duckdns.org {
       import geo_filter             # REQUIRED on a public vhost (CI checks); see "Geo filter"
       import waf                    # REQUIRED on a public vhost (CI checks); see "WAF"
       import security_headers
       import access_log <name>      # only if something needs the access log
       reverse_proxy 127.0.0.1:<port>
   }
   ```
3. `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`. Caddy issues the
   cert on first request. No DuckDNS change is needed — the subdomain is covered.
4. Optionally add a `fail2ban/jail.d/<name>.conf` + `filter.d/<name>.conf` pair.

`caddy/Caddyfile` holds only global options and the `(geo_filter)` / `(waf)` / `(security_headers)` / `(access_log)`
snippets, then `import sites.d/*.caddy`. Don't add vhosts to it directly.

## Architecture: why host networking

`caddy` and `fail2ban` both run `network_mode: host`.

- **Caddy** must reach backends that live in *other* compose projects. Jellyfin is host-net
  itself (it needs LAN/DLNA discovery) and therefore can never join a user-defined bridge
  network — so `127.0.0.1:<port>` is the one addressing model that works uniformly for
  Jellyfin, Immich and Nextcloud. It also keeps the application stacks completely unaware of
  this one: **no backend compose file references Caddy**.
- **fail2ban** needs `NET_ADMIN`/`NET_RAW` in the host net namespace so its `iptables-allports`
  bans land on the host's INPUT chain, where inbound `:443` actually arrives.

Consequence: **only one process can own `:443`.** Never start a second reverse proxy before
stopping this one, or Caddy silently fails to bind and every vhost goes dark.

## Current backends

| vhost | backend | stack |
|---|---|---|
| `jellyfin.rosurya.duckdns.org` | `127.0.0.1:8096` | `../media-server` |
| `nextcloud.rosurya.duckdns.org` | `127.0.0.1:8081` | `../next-cloud` |
| `immich.rosurya.duckdns.org` | `127.0.0.1:2283` | `../immich` |

## fail2ban: the log-source coupling

The jails read logs from three different places, and this is the only remaining tie to the
application stacks (all mounts read-only):

- **immich** → `caddy/logs/immich.log`, local to this repo. Immich has no clean auth log, so
  the filter parses **Caddy's JSON access log** for `POST /api/auth/login` → `401`. Caddy is
  the internet-facing edge, so `client_ip` there is the real client IP.
  **Therefore: changing the immich access-log format or path breaks the jail.**
- **jellyfin** → `/home/rosurya/media-server/jellyfin/config/log` (absolute host path).
  The real client IP only appears once Jellyfin's "Known proxies" includes `127.0.0.1`;
  otherwise every line reads `127.0.0.1` and bans would hit the proxy itself.
- **nextcloud** → `/home/rosurya/next-cloud/data` (absolute host path). `remoteAddr` is the
  real client IP because that stack sets `TRUSTED_PROXIES=127.0.0.1`.

All jails set `ignoreip = 127.0.0.1/8 ::1 10.0.0.0/24` so Caddy and LAN clients are never
banned. Test a filter against real log data without waiting for an attack:

```bash
docker exec fail2ban fail2ban-regex /var/log/caddy/immich.log /data/filter.d/immich.conf
```

## CrowdSec

Runs **alongside** fail2ban, not instead of it. fail2ban bans on failed *logins* read from
each app's own log; CrowdSec reads `caddy/logs/*.log` for scanning behaviour (sensitive-file
probes, CVE attempts, 404 sweeps) and pre-emptively drops the ~15k IPs on the community
blocklist. It registers with CrowdSec's central API on first start and shares attack signals
(offending IP + scenario) in exchange for that blocklist.

- **Two containers.** `crowdsec` only decides; `crowdsec-bouncer` enforces, with a
  `CROWDSEC_CHAIN` jump at the top of host `INPUT` (IPv4 and IPv6) matching ipsets. Stopping
  the bouncer removes the chain and sets — everything is unblocked, nothing is left behind.
- **LAPI is on `127.0.0.1:8089`**, not CrowdSec's default 8080 (zigbee2mqtt owns it). The two
  containers share `CROWDSEC_BOUNCER_KEY` from `.env`.
- **iptables-legacy.** The bouncer image forces the legacy backend to match the host and
  Docker. (fail2ban's image uses the nf_tables backend; both hook netfilter, so both work.)
- **A vhost with no `import access_log` is invisible to CrowdSec's log scenarios** (the WAF still
  checks it). Every public vhost has one now.
- **The WAF is CrowdSec too** (AppSec on loopback `:7422`) — see *WAF* above.
- **Whitelists matter.** App clients produce 404 bursts that `http-probing` reads as a scan.
  Jellyfin and Nextcloud use hub whitelists (`PARSERS=` in compose); Immich and Home Assistant
  use `crowdsec/parsers/edge-apps-whitelist.yaml`. Private/LAN IPs are whitelisted by default.
- **The bouncer has no official image**, so `crowdsec/bouncer/Dockerfile` wraps the pinned,
  checksum-verified release binary. Upgrade by bumping `VERSION` + `SHA256` there and the image
  tag in compose, then `docker compose build crowdsec-bouncer`. The update detector does not
  watch it.

Before trusting a new scenario or whitelist, replay real logs and see who *would* have been
banned — `-no-api` means nothing is recorded or enforced:

```bash
docker exec crowdsec sh -c '(cat /var/log/caddy/*.log; zcat /var/log/caddy/*.gz) | sort > /tmp/all.log &&
  crowdsec -c /etc/crowdsec/config.yaml -dsn file:///tmp/all.log -type caddy -no-api 2>&1 | grep performed; rm /tmp/all.log'
```

(The replay occasionally dies with `concurrent map read and map write` on v1.8.1; rerun it.)

## State that must survive

- `caddy/data/` — Let's Encrypt certificates **and the ACME account key**. Bind-mounted (not
  a named volume) so it moves with the repo. Losing it forces re-issuance of every cert
  against Let's Encrypt's rate limits, and port 80 being blocked means TLS-ALPN-01 is the
  only path back. Back this up; it is gitignored.
- `fail2ban/db/fail2ban.sqlite3` — ban history. Root-owned; `sudo` to copy it.
- `caddy/logs/` — access logs, roll at 10 MiB × 5. Written by Caddy (as root) and read by
  fail2ban and crowdsec.
- `crowdsec/config/` and `crowdsec/data/` — generated config, central-API credentials and the
  decisions database. Root-owned and gitignored. Disposable: deleting them just re-registers
  the instance and the blocklist refills within a couple of hours.

## Cross-stack ops that live elsewhere

`autoheal` and `diun` remain in `../media-server`, deliberately. Both are label/socket driven
and already operate across **all** compose projects: autoheal restarts any container labelled
`autoheal=true` (including this stack's `caddy`, plus `immich_server` and the Nextcloud `app`),
and diun watches every image on the box. Keep the `autoheal=true` label on `caddy` — it works
across projects.
