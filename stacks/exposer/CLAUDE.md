# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The **public edge** for every self-hosted stack, running on the Jetson Orin Nano Super (aarch64,
`10.0.0.179`; the router forwards 443 here). Since 2026-10-20 the application stacks run on the **HP**
(`10.0.0.64`), so Caddy proxies Jellyfin/Immich/Nextcloud to `10.0.0.64:8096/2283/8081` over the LAN,
the fail2ban jellyfin + nextcloud jails read **Caddy's** access logs (their app logs are on the HP),
`caddy/sites.d/jellyfin-lan.caddy` passes old LAN clients on `:8096` through, and the Jetson's own
DIUN + autoheal live in this stack. **Deployed by the homelab pipeline** to `/srv/homelab/exposer` on
the Jetson (Jetson runner; secrets from Bitwarden `homelab-dev`, `EXPOSER__*`, via the `jetson-dev`
machine account). Image updates come as Renovate PRs. `~/exposer` is the retired hand-run copy. Off-the-shelf
containers, no custom code — the one local build (`crowdsec-bouncer`) only wraps an official
release binary:

| service | role |
|---|---|
| `caddy` | TLS termination (Let's Encrypt) + reverse proxy; owns host `:80`/`:443` |
| `duckdns` | keeps `rosurya.duckdns.org` pointed at the dynamic Comcast WAN IP |
| `fail2ban` | tails the public-facing auth logs and bans brute-forcers via host iptables |
| `crowdsec` | reads Caddy's access logs for scans/probes/CVE attempts; pulls the community blocklist |
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
```

Validate a Caddyfile edit *without* touching the running server:

```bash
docker run --rm -v ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro -v ./caddy/sites.d:/etc/caddy/sites.d:ro \
  caddy:latest caddy validate --config /etc/caddy/Caddyfile
```

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
       import security_headers
       import access_log <name>      # only if something needs the access log
       reverse_proxy 127.0.0.1:<port>
   }
   ```
3. `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`. Caddy issues the
   cert on first request. No DuckDNS change is needed — the subdomain is covered.
4. Optionally add a `fail2ban/jail.d/<name>.conf` + `filter.d/<name>.conf` pair.

`caddy/Caddyfile` holds only global options and the `(security_headers)` / `(access_log)`
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
- **A vhost with no `import access_log` is invisible to CrowdSec** — currently `nextcloud`.
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
