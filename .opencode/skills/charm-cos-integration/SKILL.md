---
name: charm-cos-integration
description: >-
  Use when wiring observability — the cos-agent relation, COSAgentProvider,
  Prometheus scrape jobs, alert rules, Grafana dashboards, or log forwarding from
  this machine charm to the Canonical Observability Stack. Load before adding
  cos-agent to charmcraft.yaml or creating alert rule files.
metadata:
  verified: "2026-08-06"
---

# COS integration for a machine charm

## The pattern

Machine charms do **not** provide `prometheus_scrape`, `loki_push_api`,
`grafana_dashboard`, and `tracing` individually — that is the Kubernetes/COS
in-stack pattern. A machine charm provides **one** relation, `cos-agent`, to a
subordinate charm that does the forwarding
([instrument machine charms](https://documentation.ubuntu.com/observability/latest/how-to/integrate/instrument-machine-charms/)).

```
pihole (principal)  --cos-agent-->  opentelemetry-collector (subordinate)
                                              |
                                              +--> Prometheus / Loki / Grafana
```

## The subordinate

**`opentelemetry-collector`** — a machine subordinate. From its metadata:

> OpenTelemetry Collector is a Juju subordinate charm that deploys and manages the
> OpenTelemetry Collector on machines (LXD, MAAS, etc.)

> Integration with the cos-agent interface for custom scrape jobs, log file paths,
> alert rules, and dashboards provided by principal charms.

It declares the consumer side, which we do not write:

```yaml
requires:
  cos-agent:
    interface: cos_agent
    scope: container
```

## The naming trap: the library is `grafana_agent`, the subordinate is not

**Do not go looking for `charms.opentelemetry_collector.v0.cos_agent`. It does not
exist.** The `cos_agent` interface library is still published under the
`grafana_agent` name, is still Charmhub-hosted, and has **no PyPI replacement** —
`charmlibs-interfaces-cos-agent`, `charmlibs-cos-agent`, and `cos-agent` all 404.
In the official interface library index it carries **no badge**: neither
recommended nor deprecated. It is the only correct option today.

The name is a historical artefact. Grafana Agent itself reached EOL in November
2025, which is why the subordinate is `opentelemetry-collector`; the library kept
its name because the interface did not change.

**NOT VERIFIED**: whether Canonical plans to rename or migrate this library.
Re-check the interface library index before assuming a replacement exists.

```yaml
# charmcraft.yaml
charm-libs:
  - lib: grafana_agent.cos_agent
    version: "0"
```

```
charmcraft fetch-libs
```

This is the **one** legitimate `lib/charms/...` directory in this repo. It is
vendored third-party code: never edit it in place, update it only via
`charmcraft fetch-libs`.

## Provider side

```python
from charms.grafana_agent.v0.cos_agent import COSAgentProvider

self._cos_agent = COSAgentProvider(
    self,
    relation_name="cos-agent",
    metrics_endpoints=[{"path": "/metrics", "port": 9617}],
    log_slots=["pihole-by-rajannpatel:logs"],
    refresh_events=[self.on.config_changed],
)
```

Instantiate it in `__init__` alongside the other integration objects, before the
reconcile observers. It manages its own relation events.

**Use the library's default directory names and do not pass the path arguments.**
Verified against `cos_agent.py`:

```python
metrics_rules_dir: str = "./src/prometheus_alert_rules"    # line 34
logs_rules_dir:    str = "./src/loki_alert_rules"          # line 35
dashboard_dirs = dashboard_dirs or ["./src/grafana_dashboards"]   # line 661
```

So the layout is:

```
src/
  grafana_dashboards/       # *.json
  prometheus_alert_rules/   # *.rules
  loki_alert_rules/         # *.rules
```

Passing custom paths works but buys nothing and breaks the convention every other
machine charm follows. `recurse_rules_dirs` defaults to `False`, so do not nest
subdirectories under the rules directories expecting them to be picked up.

### Pi-hole specifics

**Metrics.** Pi-hole v6 has no Prometheus endpoint. The FTL HTTP API returns JSON
(`/api/stats/summary`, `/api/dns/blocking`), not the text exposition format.
Options, in order of preference:

1. Deploy a sidecar exporter (a community `pihole-exporter` translates the API to
   Prometheus) and point `metrics_endpoints` at it.
2. Have the charm write its own tiny exporter — more code to own, but no external
   dependency.
3. Ship no metrics initially and only forward logs and dashboards.

Do not point `metrics_endpoints` at the FTL API directly; Prometheus cannot parse
it. Pick one of the above and say which in the PR.

**Logs.** `log_slots` requires the snap to expose a `content` slot for its log
directory. **NOT VERIFIED** whether `pihole-by-rajannpatel` declares one — check
`snap/snapcraft.yaml` in the `snap-pi-hole` reference before relying on it. If it
does not, the logs live at
`/var/snap/pihole-by-rajannpatel/common/var/log/pihole/{FTL,pihole,webserver}.log`
and must be forwarded by path instead.

## Alert rules

`src/prometheus_alert_rules/*.rules` — one concern per file, in the standard
Prometheus format. The library injects Juju topology labels
(`juju_model`, `juju_application`, `juju_unit`); do not add them yourself.

```yaml
groups:
  - name: pihole_dns
    rules:
      - alert: PiholeDNSDown
        expr: up == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: Pi-hole DNS is not answering on {{ $labels.juju_unit }}.
          description: >-
            FTL has not responded for 5 minutes. Every device using this resolver
            has lost DNS. Check `juju ssh` and `pihole snap-check`.
```

Alerts worth writing for this workload, given the verified failure modes:

- FTL crash-looping — the launcher no longer pre-checks port 53, so an occupied
  port produces an indefinite restart loop rather than a clean failure.
- Gravity sync failing — the weekly timer is the only thing refreshing blocklists.
- Blocking disabled — `pihole api dns/blocking` returning `disabled` while the
  charm believes it is enabled means config drift.
- Query rate collapse to zero — usually means clients silently failed over to a
  different resolver.

Write the `description` for a human at 3am. State the user-visible impact and the
first diagnostic step.

## Dashboards

`src/grafana_dashboards/*.json`. Exported Grafana JSON. The library rewrites the
datasource and injects topology variables, so leave the datasource as a template
variable rather than hardcoding a UID.

## Do not

- Add `prometheus_scrape`, `loki_push_api`, `grafana_dashboard`, or `tracing`
  endpoints. On a machine charm those are the subordinate's job.
- Add `catalogue`, `probes`, or `datasource_exchange`. Those are for charms that
  are part of COS. Pi-hole is a workload observed *by* COS.
- Look for a `cos_agent` library under an `opentelemetry_collector` name. There
  isn't one; see the naming trap above.
