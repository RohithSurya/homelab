# logging — central logs (Loki + Grafana + Alloy)

All machines' logs in one place, kept 90 days. Runs on the **HP**; the other machines run the
`log-agent` stack (`stacks/log-agent`). Read logs in **Grafana**: `https://logs.home.rosurya.duckdns.org`
(short `http://logs.home`; LAN + Tailscale only, via the Jetson's Caddy; direct `http://10.0.0.64:3020`)
(user `admin`, password = Bitwarden `LOGGING__GRAFANA_ADMIN_PASSWORD`) → Explore → Loki.

```
Jetson  ─ alloy (log-agent) ─┐  containers, journal, Caddy access logs
pc5090  ─ alloy (log-agent) ─┤  containers, journal                     basic auth (user "agent")
                             ├──> loki-gateway :3100 (push only) ──┐
router  ─ syslog tcp :1514 ──> alloy (HP) ─────────────────────────┼──> loki ──> grafana :3020
HP containers + journal ─────> alloy (HP) ─────────────────────────┘
```

## Labels (what to filter on)
| label | values |
|---|---|
| `host` | `hp`, `jetson`, `pc5090`, `router` |
| `source` | `docker`, `journal`, `syslog` (router), `caddy` (Jetson access logs) |
| `stack`, `service`, `container` | Docker Compose project / service / container name (`source="docker"`) |
| `unit`, `ident`, `level` | systemd unit, syslog identifier, priority (`journal`, `syslog`) |
| `filename` | Caddy log file = vhost (`source="caddy"`) |

Examples: `{host="hp", container="media-bridge"} |= "[seedr]"`,
`{host="router", ident="dropbear"}`, `{source="caddy"} | json | status >= 500`,
`{host="jetson", ident="sshd"} |= "Failed"`.

## Pieces
- **loki** — single binary, filesystem storage in the `logging_loki_data` volume (NVMe). Retention
  `retention_period: 2160h` (compactor deletes). Not published on the host.
- **loki-gateway** (nginx) — host `:3100`. `POST /loki/api/v1/push` needs basic auth `agent` /
  `LOKI_PUSH_PASSWORD`; `/ready` is open; **everything else is 403**, so nobody on the LAN can query
  Loki around Grafana's login. The nginx entrypoint renders `gateway/templates/*.template` with the
  password (`{PLAIN}` htpasswd, in the container only). It re-resolves `loki` every 10 s, so a
  recreated Loki container is found without restarting the gateway.
- **grafana** — host `:3020`, own login, no sign-up, no anonymous access. Datasource provisioned
  from `grafana/provisioning` (uid `loki`). Its DB (`logging_grafana_data`) is what `backup.sh` saves.
- **alloy** — the HP's agent (`alloy/common.alloy` + `journal-run.alloy`: the HP's journald is
  **volatile**, `/run/log/journal`) and the router's syslog receiver (`syslog.alloy`, host
  `:1514` tcp+udp). Debug UI on `127.0.0.1:12345` (`ssh -L 12345:127.0.0.1:12345 hp`).

## Alerts (`grafana/provisioning/alerting/`, folder "Homelab")
Provisioned from files -- edit them here; the UI shows them read-only. All go to the **ops ntfy
topic** (`NTFY_TOPIC` = Bitwarden `MEDIA__NTFY_TOPIC`) through ntfy's built-in `grafana` template;
grouped by alert + host, sent after 1 min, repeated at most every 12 h (`ntfy.yaml`).

| alert | fires when | priority |
|---|---|---|
| No logs from hp / jetson / pc5090 | no line for 30 min (5 min `for`) | high |
| No logs from router | no syslog line for 60 min | high |
| Python traceback | `Traceback (most recent call last)` in `media-bridge` or `hermes` | default |
| Failed logins | > 5 failed SSH/dropbear logins in 10 min on one host | high |

A Loki/query failure raises Grafana's own `DatasourceError` alert on the same route.
Silence one: the alert's "Silence" link (in the ntfy message) or Alerting → Silences.

## Rules
- Keep Grafana LAN + Tailscale only: the logs carry private-tracker names, release names, IPs and
  Hermes's personal data. Never add it to a public Caddy vhost.
- `alloy/common.alloy` is shared with `stacks/log-agent/alloy/common.alloy`; edit both
  (`ci-checks.sh` fails when they differ). `alloy validate` runs on both in CI.
- An agent seeing a container for the first time reads its whole Docker log; `stage.drop` throws
  away lines older than 7 days because Loki refuses them (`reject_old_samples_max_age`).
- Anything on the LAN can send syslog to `:1514` and it will be labelled `host="router"`. Fine for a
  home LAN; don't forward the port.
- Adding a machine: run `stacks/log-agent` there with its own `HOST_LABEL` and the push password.
