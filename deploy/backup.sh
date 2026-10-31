#!/usr/bin/env bash
# backup.sh <stack> <deploy dir> [sha|nightly]: what a rollback (or a bad day) needs, taken from
# the running stack before every deploy and nightly (.github/workflows/nightly.yml). Keeps the
# newest $BACKUP_KEEP per stack. Jellyfin's artwork (data/metadata, ~2.7 GB) is re-downloadable
# and left out.
# Files are read through a throwaway root container: some state is owned by root/www-data.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
stack=$1; dir=$2; sha=${3:-manual}
dest="$BACKUP_BASE/$stack/$(date +%Y%m%d-%H%M%S)-${sha:0:7}"
mkdir -p "$dest"
envval() { grep -m1 "^$1=" "$dir/.env" | cut -d= -f2-; }
tarc() {  # tarc <out.tgz> <dir relative to $dir>...
  # The apps keep running while this reads their state. SQLite's *-shm files are a cache that
  # SQLite deletes and recreates as connections close and open (rebuilt on open, never needed
  # for a restore), and busybox tar fails on any file that vanishes mid-read: a vanished
  # jellyfin.db-shm failed the 2026-10-20 media-server deploy. So skip them, and retry a read
  # that still races a live write.
  local out=$1 try; shift
  for try in 1 2 3; do
    if docker run --rm -v "$dir":/s:ro alpine tar -C /s -czf - --numeric-owner \
         --exclude='*.db-shm' --exclude='*.sqlite-shm' --exclude='*.sqlite3-shm' "$@" > "$dest/$out"; then
      return 0
    fi
    log "backup: reading $out failed (try $try of 3; a file changed while it was read)"
    sleep 5
  done
  die "backup: could not read $out after 3 tries"
}

case $stack in
  immich)
    docker exec immich_postgres pg_dumpall --clean --if-exists --username="$(envval DB_USERNAME)" \
      | gzip > "$dest/immich-db.sql.gz" ;;
  nextcloud)
    docker exec next-cloud-db-1 sh -c 'exec mariadb-dump --single-transaction --routines --triggers --all-databases -uroot -p"$MARIADB_ROOT_PASSWORD"' \
      | gzip > "$dest/nextcloud-db.sql.gz"
    docker run --rm -v next-cloud_nextcloud_app:/v:ro alpine tar -C /v -czf - --numeric-owner config \
      > "$dest/nextcloud-config.tgz" ;;
  media-server)
    tarc media-server-config.tgz \
      --exclude=jellyfin/config/cache --exclude=jellyfin/config/data/metadata \
      --exclude=jellyfin/config/data/transcodes --exclude='*/logs' --exclude='*/log' \
      radarr sonarr prowlarr bazarr jellyfin media-bridge/config diun ;;
  exposer)
    tarc exposer-state.tgz caddy/data fail2ban crowdsec/config ;;
  logging)
    # Grafana's DB (users, dashboards, alert state). The logs themselves are not backed up.
    docker run --rm -v logging_grafana_data:/v:ro alpine tar -C /v -czf - --numeric-owner . \
      > "$dest/grafana-data.tgz" ;;
  langfuse)
    # users, projects, API keys, prompts. The traces (ClickHouse) are not backed up.
    docker exec langfuse-postgres pg_dumpall --clean --if-exists -U postgres | gzip > "$dest/langfuse-db.sql.gz" ;;
  log-agent)
    echo "log-agent holds only read positions; config lives in git" > "$dest/README" ;;
  dashboard)
    echo "dashboard config lives in git (stacks/dashboard/config); nothing to back up" > "$dest/README" ;;
  *) die "no backup recipe for $stack" ;;
esac

for f in "$dest"/*; do [ -s "$f" ] || die "backup file $f is empty"; done
chmod -R go-rwx "$dest"
log "backup: $dest ($(du -sh "$dest" | cut -f1))"
ls -1dt "$BACKUP_BASE/$stack"/*/ 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -rf
