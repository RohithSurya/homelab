# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The **public edge** for every self-hosted stack on this Jetson Nano Super (aarch64). Three
off-the-shelf containers, no custom code:

| service | role |
|---|---|
| `caddy` | TLS termination (Let's Encrypt) + reverse proxy; owns host `:80`/`:443` |
| `duckdns` | keeps `rosurya.duckdns.org` pointed at the dynamic Comcast WAN IP |
| `fail2ban` | tails the public-facing auth logs and bans brute-forcers via host iptables |

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
| `nextcloud.rosurya.duckdns.org` | `127.0.0.1:8080` | `../next-cloud` |
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

## State that must survive

- `caddy/data/` — Let's Encrypt certificates **and the ACME account key**. Bind-mounted (not
  a named volume) so it moves with the repo. Losing it forces re-issuance of every cert
  against Let's Encrypt's rate limits, and port 80 being blocked means TLS-ALPN-01 is the
  only path back. Back this up; it is gitignored.
- `fail2ban/db/fail2ban.sqlite3` — ban history. Root-owned; `sudo` to copy it.
- `caddy/logs/` — access logs, roll at 10 MiB × 5. Written by Caddy (as root) and read by
  fail2ban.

## Cross-stack ops that live elsewhere

`autoheal` and `diun` remain in `../media-server`, deliberately. Both are label/socket driven
and already operate across **all** compose projects: autoheal restarts any container labelled
`autoheal=true` (including this stack's `caddy`, plus `immich_server` and the Nextcloud `app`),
and diun watches every image on the box. Keep the `autoheal=true` label on `caddy` — it works
across projects.
