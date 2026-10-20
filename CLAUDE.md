# CLAUDE.md — homelab monorepo

Every self-hosted stack, its deploy tooling and the pipeline that ships it. Each stack has its own
`CLAUDE.md` with the details (`stacks/media-server/CLAUDE.md` is the big one).

## Machines
| Machine | Role | Runs |
|---|---|---|
| HP Pro Mini 400 G9 — `10.0.0.64`, Tailscale `100.110.179.59`, x86-64, Intel Quick Sync | **prod** | media-server, immich, nextcloud (cut over 2026-10-20) |
| Jetson Orin Nano Super — `10.0.0.179`, arm64 | **dev** + public edge | exposer (Caddy/DuckDNS/fail2ban/CrowdSec); router forwards 443 here |
| Router: GL.iNet Flint 2 (OpenWrt) `10.0.0.1` | | static leases, 443 → Jetson, AdGuard DNS |

HP disks: internal NVMe = code + app state (`/srv/homelab/<stack>`); USB 5 TB HDD (serial
WD-WX52D812X87F) = `/mnt/media` (2 TB, media-server `data/`), `/mnt/photos` (1.5 TB, Immich +
Nextcloud files), `/mnt/backups` (rest). Docker refuses to start unless `/mnt/media` and `/mnt/photos`
are mounted. The Jetson's old movies are NFS (v3 — the Tegra kernel has no NFSv4 server) at
`data/media/movies{,-private}`.

## Layout
```
stacks/<stack>/docker-compose.y(a)ml  shared definition; host-specific values are ${VARS} whose
                                      defaults are the old Jetson values
stacks/<stack>/compose.prod.yaml      prod overrides (GPU, paths)        env/prod.env  non-secret prod values
stacks/<stack>/env.tpl                the .env layout; values are {{vault:STACK__KEY}} placeholders
deploy/                               deploy.sh rollback.sh backup.sh smoke.sh plan.sh ci-checks.sh envvault.py
deploy/targets                        which stack deploys on which host, and whether it is enabled yet
infra/runner/                         the self-hosted GitHub Actions runner image + install.sh
.github/workflows/                    ci.yml (every push/PR) and deploy.yml (merge to main)
```

## Secrets
Bitwarden Secrets Manager. Projects: `homelab-prod`, `homelab-dev` (same names, dev values), `Hermes`.
Secret names are `<STACK>__<KEY>` (MEDIA, IMMICH, NEXTCLOUD, EXPOSER) plus media-bridge's own
`SEEDBOX_*` / `TRACKER_*`. Deploys render `.env` (mode 600) from `env.tpl` with
`deploy/envvault.py render`; nothing secret is ever committed or sent to GitHub. Each machine's
machine-account token lives in `~/.config/homelab/bws_token` (mode 600).
Never print secret values: list names only (`bws secret list` in TSV form dumps values).

## Pipeline
- `ci.yml` (Jetson runner): shellcheck, actionlint, compose config for every stack × env,
  media-bridge unit tests, `caddy validate`. Same script by hand: `deploy/ci-checks.sh`.
- `deploy.yml` on push to `main`: ci → `plan.sh` (changed + enabled stacks) → per stack on its host:
  backup → render `.env` → build → `up --wait` → gluetun rule → `smoke.sh` → record SHA. On failure
  `rollback.sh` redeploys the last good SHA (code/config/images only — databases are never restored
  automatically; the pre-deploy dump is in `$BACKUP_BASE/<stack>/`). ntfy reports the outcome.
- Only the owner's merges deploy (`github.actor` check). Manual run: Actions → deploy → Run workflow
  (`dry_run` defaults to true and also works for stacks not enabled yet).
- Runners are containers (`gh-runner`, host network, host Docker socket) — `infra/runner/install.sh`.
- `nightly.yml` (04:17 New York, HP): `backup.sh` for every enabled stack (keep 30 per stack in
  `/mnt/backups`), then `mirror.sh` copies `/mnt/photos/{immich-library,nextcloud-data}` and
  `/mnt/backups` to the Jetson (`~/backups/hp-mirror`) — the only second copy of the photos. It refuses
  to run if a USB partition isn't mounted or a source is empty. ntfy only on failure.
- Renovate (`.github/renovate.json`, GitHub App) opens image/action update PRs for the HP stacks; CI
  tests them and the owner's merge deploys them. The Immich version is in `stacks/immich/env/prod.env`.
  exposer too. Only the Jetson's non-repo stacks (bedroom-agent) still use `~/updater` (wud/approver).

## Status (2026-10-20)
- **Live on the HP via the pipeline:** nextcloud, immich, media-server (`deploy/targets` enabled).
- **exposer** is deployed by the pipeline on the Jetson (`/srv/homelab/exposer`; token
  `~/.config/homelab/bws_token` = machine account `jetson-dev`, reads `homelab-dev` only).
- The Jetson's old copies were deleted 2026-10-20 (it went from 98 % to 69 % full). It still holds the old
  movies (NFS-shared to the HP) and `~/backups/hp-mirror` (below). `~/media-server` is a dead checkout --
  **never `git pull` there**.
- Follow-ups: Hermes `homeops` still restarts media containers on the Jetson's Docker; the updater
  (wud/approver) doesn't see the HP; KrazyZone needs a fresh login cookie (home IPv4 changed with
  the Flint 2).

## Names at home
- Dashboard: `https://home.rosurya.duckdns.org`; admin apps: `https://<app>.home.rosurya.duckdns.org`
  (Caddy `sites.d/home.caddy`, LAN + Tailscale only, real certificates).
- Short names to type: `<app>.home` or `<app>.lan` (e.g. `jellyfin.home`, `radarr.lan`, `home.lan`) --
  plain-HTTP redirects to the real names (`sites.d/short-names.caddy`). DNS for all of these is AdGuard
  on the router (rewrites: `home.rosurya.duckdns.org`, `*.home.rosurya.duckdns.org`, `*.home` and a list
  of `<app>.lan` names -> 10.0.0.179). A new app needs a line in both Caddy files (and a `.lan` rewrite).

## Rules
- Never run a BitTorrent client on any home machine, not even for tests (the ISP blocks the user).
- A stack is moved to the HP by its own cutover PR, which sets `enabled=yes` in `deploy/targets`.
- Port 443 is the only thing forwarded from the internet; keep every other port LAN/Tailscale-only.
