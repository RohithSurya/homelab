#!/bin/sh
# Self-hosted Langfuse without an Enterprise licence never deletes traces. Once a day, drop every
# monthly partition (tables partitioned by toYYYYMM(...): events_full, events_core, traces,
# observations, scores...) whose whole month is older than RETENTION_DAYS. So data lives
# RETENTION_DAYS to RETENTION_DAYS + 31 days. Runs in the ClickHouse image (clickhouse-client).
set -u
ch() { clickhouse-client --host clickhouse --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" "$@"; }
tab=$(printf '\t')
while :; do
  if cutoff=$(ch -q "SELECT toYYYYMM(now() - INTERVAL ${RETENTION_DAYS:?} DAY)"); then
    n=0
    ch -q "SELECT DISTINCT p.database, p.table, p.partition_id
           FROM system.parts AS p
           JOIN system.tables AS t ON p.database = t.database AND p.table = t.name
           WHERE p.active AND p.database = 'default' AND t.partition_key LIKE 'toYYYYMM(%'
             AND match(p.partition_id, '^[0-9]{6}\$') AND toUInt32(p.partition_id) < $cutoff
           FORMAT TSV" > /tmp/drop.tsv || echo "retention: listing partitions failed"
    while IFS="$tab" read -r db tb pid; do
      [ -n "$pid" ] || continue
      if ch -q "ALTER TABLE \`$db\`.\`$tb\` DROP PARTITION ID '$pid'"; then
        echo "retention: dropped $db.$tb month $pid"; n=$((n + 1))
      else
        echo "retention: FAILED to drop $db.$tb month $pid"
      fi
    done < /tmp/drop.tsv
    echo "retention: done, cutoff month $cutoff, $n partition(s) dropped"
  else
    echo "retention: ClickHouse not answering; next try in 24h"
  fi
  sleep 86400
done
