# langfuse — LLM call tracing (self-hosted Langfuse v4)

LangSmith-style traces of Hermes's LLM calls: each Hermes turn, the model calls inside it,
prompts, replies, tokens, latency, cost. Runs on the **HP**. UI: `https://langfuse.home.rosurya.duckdns.org`
(short `http://traces.home`; LAN + Tailscale only; login `rosurya@home.lan` / Bitwarden
`LANGFUSE__INIT_USER_PASSWORD`). `NEXTAUTH_URL` is that name, so the login does NOT work over
`http://10.0.0.64:3040` (secure cookies); the API does. Organisation **Home**,
project **Hermes** (id `hermes`); its API keys are `LANGFUSE__INIT_PROJECT_PUBLIC_KEY` /
`..._SECRET_KEY`.

| service | role |
|---|---|
| `langfuse-web` | UI + public API + OTLP endpoint `/api/public/otel/v1/traces` (host `:3040`) |
| `langfuse-worker` | turns queued ingestion events into ClickHouse rows |
| `otel-relay` (Alloy) | host `:4318`: OTLP relay that fixes Claude Code CLI spans (`otel-relay/relay.alloy`) |
| `clickhouse` | the traces (`events_full`, `events_core`, ...), capped at 2 GB RAM |
| `postgres` | users, projects, API keys, prompts, settings — the only thing backed up |
| `redis` | queues |
| `minio` | S3 store for raw ingestion events + media (bucket `langfuse`, not published) |
| `retention` | daily ClickHouse partition drop (`retention/retention.sh`) |

## Sending traces
**Wired today:** Hermes (`~/hermes` on the Jetson, environment `production`) -- both the plugin
and its Claude CLI spans (see `~/hermes/docker-compose.yaml`). Hermes's image pins langfuse SDK
4.15.6, the newest that fits its OpenTelemetry 1.39.1.

- **Langfuse SDK / Hermes's `observability/langfuse` plugin**: base URL `http://10.0.0.64:3040`,
  public + secret key.
- **Any OTLP exporter**: `http://10.0.0.64:3040/api/public/otel/v1/traces`, header
  `Authorization: Basic base64(pk:sk)`, `http/protobuf` or JSON.
- **Claude Code CLI**: send to the relay instead, `http://10.0.0.64:4318/v1/traces`, same Basic
  auth. The CLI puts tokens in `input_tokens` / `output_tokens` / `cache_*_tokens`, which Langfuse
  doesn't read (0 tokens, $0); the relay renames them to `gen_ai.usage.*` — `input_tokens`
  becomes uncached + cache read + cache creation, because Langfuse subtracts the cache counts
  again — and drops the `user.*` attributes (the Anthropic account's e-mail and ids).

## v4 facts that bite
- v4 is **"events only"**: `/api/public/traces` and `/api/public/observations` are gone; read with
  `GET /api/public/v2/observations?fields=core,basic,usage,model,metadata,io`. Health:
  `/api/public/health`.
- Next.js binds to `$HOSTNAME`; the compose file sets `HOSTNAME=0.0.0.0` so `127.0.0.1` answers
  (healthcheck).
- **Retention is an Enterprise feature** — self-hosted Langfuse keeps everything forever. Ours:
  `retention` drops each month-partition (every `default` table partitioned `toYYYYMM(...)`) once
  the whole month is older than `RETENTION_DAYS` (90, `env/prod.env`) → data lives 90–121 days.
  MinIO expires every object after 90 days (`minio/lifecycle.json`, applied on every start).
  Check: `docker logs langfuse-retention`.
- ClickHouse's own `system.*_log` tables get a 3–7 day TTL and its logs go to stdout only
  (`clickhouse/limits.xml`). `opentelemetry_span_log` can't take a `ttl` (it has an engine set).
- `LANGFUSE_INIT_*` only act on first start. Changing a key or the password in Bitwarden later does
  **not** change Langfuse — change it in the UI (or start over with empty volumes).
- Sign-up is off (`AUTH_DISABLE_SIGNUP`). Telemetry to Langfuse is off.

## Rules
- LAN + Tailscale only — traces hold whole conversations (personal data, home details). Never put
  it behind a public Caddy vhost.
- The ClickHouse traces are not backed up (re-creatable only by re-running things; acceptable).
  Postgres is (`backup.sh`).
- Postgres stays on **17** (Renovate `allowedVersions: ^17\.`). `postgres:18+` refuses this volume: the data is
  17-format and mounted at `/var/lib/postgresql/data`, while 18 wants one mount at `/var/lib/postgresql` and
  exits with "there appears to be PostgreSQL data in: /var/lib/postgresql/data (unused mount/volume)".
  PR #64 (18.6) made every deploy roll langfuse back 2026-10-31..11-11. Moving to 18 = stop, back up,
  `pg_upgrade` (or dump/restore) into the new layout, change the mount — a planned migration.
