# Caddy + the country filter (see "Geo filter" in ../../CLAUDE.md).
#
# Stock Caddy can't match on country, so this adds one plugin
# (porech/caddy-maxmind-geolocation) with xcaddy, and bakes in a free DB-IP country
# database so Caddy can ALWAYS start, even offline: the plugin refuses to load if its
# db file is missing, and that would take every vhost down. A refresher inside the
# container swaps in each month's database and reloads Caddy (geoip-entrypoint).
#
# To upgrade Caddy: bump both FROM tags together (Renovate does this).
FROM caddy:2.11.7-builder AS build
# Pinned to a commit: the v1.0.0 tag was moved after release, so Go's checksum database
# rejects it ("verifying module: checksum mismatch"). Never bypass that check (GONOSUMDB etc.).
ARG GEO_PLUGIN=github.com/porech/caddy-maxmind-geolocation@9066f91c9696fcf554a0230ea768301259708ff1
RUN xcaddy build --with "${GEO_PLUGIN}"

FROM caddy:2.11.7
# libmaxminddb = mmdblookup (used to check a database before it goes live, and by `geoip`)
RUN apk add --no-cache libmaxminddb jq
COPY --from=build /usr/bin/caddy /usr/bin/caddy
COPY geoip geoip-entrypoint /usr/local/bin/

# The baked fallback database: this month's, or last month's early in the month.
# DB-IP "IP to Country Lite", CC BY 4.0 (https://db-ip.com), no account needed.
RUN set -eu; mkdir -p /usr/share/geoip; \
    for m in "$(date -u +%Y-%m)" "$(date -u -d "@$(( $(date -u +%s) - 32*86400 ))" +%Y-%m)"; do \
      if wget -q -O /tmp/db.gz "https://download.db-ip.com/free/dbip-country-lite-$m.mmdb.gz"; then \
        gunzip -c /tmp/db.gz > /usr/share/geoip/country.mmdb && echo "$m" > /usr/share/geoip/month && break; \
      fi; \
    done; \
    rm -f /tmp/db.gz; \
    GEOIP_DB=/usr/share/geoip/country.mmdb geoip verify

ENTRYPOINT ["/usr/local/bin/geoip-entrypoint"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
