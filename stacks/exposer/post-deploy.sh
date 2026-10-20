#!/usr/bin/env bash
# Run by deploy/deploy.sh after `up`: Caddy and fail2ban read their config only at start or reload,
# and their config files are bind-mounted, so a deploy that only changes those files recreates
# nothing. Reload both so every change takes effect (both are no-ops when nothing changed).
set -euo pipefail
docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | grep -iv 'not formatted' || true
docker exec fail2ban fail2ban-client reload >/dev/null
echo "caddy + fail2ban reloaded"
