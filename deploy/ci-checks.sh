#!/usr/bin/env bash
# ci-checks.sh: everything a change must pass before it may deploy. Runs on the Jetson runner
# (and by hand: deploy/ci-checks.sh). Needs only Docker -- no secrets, no running stacks.
set -euo pipefail
cd "$(dirname "$0")/.."
fails=0
step() { printf '\n== %s\n' "$1"; }
run() { if "$@"; then echo "   ok"; else echo "   FAILED"; fails=$((fails + 1)); fi; }

step "shell scripts (shellcheck)"
run docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:v0.10.0 -x -S warning \
  deploy/*.sh infra/runner/*.sh infra/hosts/*/*.sh stacks/*/post-deploy.sh \
  stacks/exposer/caddy/image/geoip stacks/exposer/caddy/image/geoip-entrypoint \
  stacks/exposer/crowdsec/hub-refresh

step "GitHub workflows (actionlint)"
run docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7 -no-color -oneline

step "python syntax (deploy tooling)"
run python3 -m py_compile deploy/envvault.py

step "compose files, every stack x env"
for d in stacks/*/; do
  s=$(basename "$d")
  for env in default prod dev; do
    [ "$env" = default ] || [ -f "$d/compose.$env.yaml" ] || [ -f "$d/env/$env.env" ] || continue
    tmp=$(mktemp -d); cp -a "$d/." "$tmp/"
    # placeholders stand in for the secrets: the shape is checked, not the values
    sed -E -e 's/^([A-Z_]*(LOCATION|PATH|DIR))=\{\{vault:[A-Za-z0-9_]+\}\}/\1=.\/ci-placeholder/' \
           -e 's/\{\{vault:[A-Za-z0-9_]+\}\}/ci-placeholder/' "$d/env.tpl" > "$tmp/.env"
    a=(--project-directory "$tmp" -f "$(ls "$tmp"/docker-compose.y*ml)" --env-file "$tmp/.env")
    if [ "$env" != default ]; then
      [ -f "$tmp/compose.$env.yaml" ] && a+=(-f "$tmp/compose.$env.yaml")
      [ -f "$tmp/env/$env.env" ] && a+=(--env-file "$tmp/env/$env.env")
    fi
    printf '   %-14s %-8s' "$s" "$env"
    if docker compose "${a[@]}" config -q 2>"$tmp/err"; then echo ok; else echo "FAILED: $(tail -1 "$tmp/err")"; fails=$((fails + 1)); fi
    rm -rf "$tmp"
  done
done

step "media-bridge unit tests"
run docker build -q -t homelab-media-bridge:ci stacks/media-server/media-bridge >/dev/null
run docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/stacks/media-server/media-bridge:/app" -w /app \
  --entrypoint python homelab-media-bridge:ci -m unittest discover -s tests

step "Caddy config (exposer; the local image with the geo filter + WAF plugins)"
run docker build -q -t caddy-geo-local:ci stacks/exposer/caddy/image >/dev/null
geo=$(sed -n 's/^ *- GEO_ALLOW_COUNTRIES=//p' stacks/exposer/docker-compose.yaml)
run docker run --rm -e GEO_ALLOW_COUNTRIES="$geo" -e CROWDSEC_CADDY_BOUNCER_KEY=ci-placeholder -v "$PWD/stacks/exposer/caddy:/etc/caddy:ro" caddy-geo-local:ci \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

step "every public vhost imports geo_filter and waf (exposer)"
geo_imports() {  # a site file with security_headers (= public) must import geo_filter and waf as often
  local f s ok=0
  for f in stacks/exposer/caddy/sites.d/*.caddy; do
    for s in geo_filter waf; do
      if [ "$(grep -c 'import security_headers' "$f")" != "$(grep -c "import $s\$" "$f")" ]; then
        echo "   $f: a public site block without 'import $s'"; ok=1
      fi
    done
  done
  return "$ok"
}
run geo_imports

step "result"
[ "$fails" = 0 ] && echo "all checks passed" || { echo "$fails check(s) failed"; exit 1; }
