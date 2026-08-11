# ADR-0008: COS Integration

**Status:** Proposed
**Date:** 2026-08-07
**Related:** [ADR-0002: Tech Stack and Repository Architecture](0002-tech-stack-and-repo-architecture.md), [ADR-0006: Configuration Surface](0006-configuration-surface.md)

---

## 1. Context

The charm should be observable by the Canonical Observability Stack. For a machine
charm the mechanism is not the in-stack pattern: a machine charm provides **one**
relation, `cos-agent`, to a subordinate that does the forwarding.

```
pihole (principal)  --cos-agent-->  opentelemetry-collector (subordinate)
                                              │
                                              └──> Prometheus / Loki / Grafana
```

Two facts about this workload constrain what we can actually deliver, and both were
verified rather than assumed.

### 1.1 Pi-hole exposes no metrics

Pi-hole v6 has **no Prometheus endpoint**. The FTL HTTP API returns JSON
(`/api/stats/summary`, `/api/dns/blocking`), which Prometheus cannot parse.

And the snap adds nothing: across 25 wiki pages there is **no mention of
Prometheus, an exporter, `/metrics`, Grafana, or any observability integration**.
The snap's entire day-2 story is `snap logs`, `dmesg | grep DENIED`,
`pihole snap-check`/`snap-debug`, and the web dashboard.

So metrics are work we own, not something to wire up.

### 1.2 There is no content slot for logs — verified

`COSAgentProvider` accepts `log_slots=[...]`, which requires the snap to expose a
`content` slot for its log directory.

**Verified by grep: `snapcraft.yaml` contains no `slots:` key whatsoever.**

So `log_slots` is not merely unset — it is **impossible** for this snap. Logs must
be forwarded by path:

```
/var/snap/pihole-by-rajannpatel/common/var/log/pihole/{FTL,pihole,webserver,gravity-init}.log
```

### 1.3 The naming trap

Grafana Agent reached EOL in November 2025, so the subordinate is
**`opentelemetry-collector`**. But the interface is still `cos_agent` and its
library is still published as `charms.grafana_agent.v0.cos_agent`.

**`charms.opentelemetry_collector.v0.cos_agent` does not exist.** Nor does any
PyPI replacement — `charmlibs-interfaces-cos-agent`, `charmlibs-cos-agent`, and
`cos-agent` all 404. In the official interface library index it carries **no
badge**: neither recommended nor deprecated. It is the only correct option today.

**NOT VERIFIED:** whether Canonical plans to migrate it. Re-check the index before
assuming a replacement exists.

---

## 2. Decisions

### 2.1 Logs and dashboards first; metrics deferred

Split the work, because logs need no exporter and metrics do:

- **Now:** the `cos-agent` relation, log forwarding by path, and **Loki** alert
  rules.
- **Deferred:** Prometheus metrics, and therefore Prometheus alert rules.

This ordering is not arbitrary. Prometheus alert rules without a metrics source
are inert files. Loki rules over forwarded logs work immediately, and the failure
modes we most need to detect are **visible in logs**:

| Alert | Signal in the logs |
|---|---|
| FTL crash-looping | repeated `EADDRINUSE` in `FTL.log` — the launcher no longer pre-checks port 53, and `restart-condition: on-failure` makes this an indefinite loop |
| Gravity sync failing | `gravity-init.log`; the weekly timer is the only thing refreshing blocklists |
| Confinement denials | AppArmor `DENIED` bursts, which indicate a plug that should be connected |

Write every `description` for a human at 3am: state the user-visible impact and the
first diagnostic step. For a DNS sinkhole the impact line is usually *"every device
using this resolver has lost DNS."*

### 2.2 The metrics decision, made explicitly later

Three options, none free:

| Option | Cost |
|---|---|
| A community `pihole-exporter` | **Another unproven third-party dependency**, layered on an already-unproven snap ([ADR-0001](0001-charm-scope-and-specification.md) §1.2). |
| A charm-owned exporter translating `/api/stats/summary` | No external dependency, but we own a network service forever, including its security surface. |
| No metrics | Loki alerts only. Honest, and possibly sufficient for a home/small-network DNS appliance. |

**Default to the third** until someone states a metrics requirement. Choose between
the first two only then, in a PR that names the trade-off.

**Never point `metrics_endpoints` at the FTL API directly.** Prometheus cannot
parse JSON; it would silently produce no metrics, which is worse than shipping
none.

### 2.3 Endpoint declaration

```yaml
provides:
  cos-agent:
    interface: cos_agent
    limit: 1
    optional: true
```

- `limit: 1` is decided **now** because `limit` is enforced by Juju and is not
  safely reversible — adding it later breaks `juju refresh` for anyone with two
  relations (see [ADR-0006](0006-configuration-surface.md) §2.5).
- `optional: true` is documentation only. The real guarantee — that the charm
  reaches `ActiveStatus` with zero relations — lives in `_reconcile` and
  `collect_unit_status`.

### 2.4 The one legitimate vendored charm library

```yaml
charm-libs:
  - lib: grafana_agent.cos_agent
    version: "0"
```

Fetched with `charmcraft fetch-libs` into `lib/charms/grafana_agent/`. This is the
**only** legitimate `lib/charms/...` directory in the repo: third-party, vendored,
**never edited in place, never linted**, updated only via `fetch-libs`.

`version` must be a **string** (`"0"`, not `0`).

Two consequences of ADR-0002's choices:

- **`charm-libs:` is only for Charmhub-hosted libraries.** Everything else lives
  in `pyproject.toml`.
- **The `uv` plugin does not install a Charmhub library's transitive `PYDEPS`.**
  Add them to `pyproject.toml` by hand.

Charmhub library hosting is being retired (deprecation warnings now → new uploads
disabled in the 26.10 cycle → updates disabled). We consume one because no PyPI
replacement exists; we **create none**.

### 2.5 Provider wiring

```python
self._cos_agent = COSAgentProvider(
    self,
    relation_name="cos-agent",
    refresh_events=[self.on.config_changed],
)
```

Instantiated in `__init__` alongside the other integration objects, **composed
never subclassed**. It manages its own relation events.

Note it takes the whole charm — the ops ecosystem injects the god object here. That
is a wart to live with, not a pattern to copy: our own functions take the narrowest
collaborator they need ([ADR-0003](0003-reconciler-and-functional-core.md) §2.7).

### 2.6 Use the library's default directories

Verified against `cos_agent.py`:

```python
metrics_rules_dir: str = "./src/prometheus_alert_rules"    # line 34
logs_rules_dir:    str = "./src/loki_alert_rules"          # line 35
dashboard_dirs = dashboard_dirs or ["./src/grafana_dashboards"]   # line 661
```

So the layout is fixed and **we pass no path arguments** — custom paths work but
buy nothing and break the convention every other machine charm follows.

`recurse_rules_dirs` defaults to `False`, so do not nest subdirectories expecting
them to be picked up.

The library injects Juju topology labels (`juju_model`, `juju_application`,
`juju_unit`) into rules, and rewrites the datasource in dashboards — so leave the
datasource as a template variable rather than hardcoding a UID, and **do not add
topology labels by hand**.

### 2.7 Endpoints we must not add

- **`prometheus_scrape`, `loki_push_api`, `grafana_dashboard`, `tracing`** — on a
  machine charm these are the subordinate's job. Adding them is the Kubernetes
  pattern applied in the wrong place.
- **`catalogue`, `probes`, `datasource_exchange`** — these are for charms that are
  *part of* COS. Pi-hole is a workload observed *by* COS.

### 2.8 Integration testing

```
juju integrate pihole:cos-agent otelcol:cos-agent
```

**Always name both endpoints.** Every application implicitly provides `juju-info`,
and an unqualified `integrate` may resolve to that instead of `cos-agent`.

---

## 3. Future Work (Out of Scope)

- **Metrics**, per §2.2, with the exporter choice deferred to the PR that needs it.
- **Prometheus alert rules**, which follow metrics.
- **A Grafana dashboard driven by logs** rather than metrics — feasible but of
  limited value; most Pi-hole dashboards people expect are metric-based.
- **Integration-testing this relation**, which is blocked until
  `opentelemetry-collector` publishes a 26.04 revision. The charm targets
  `ubuntu@26.04` ([ADR-0002](0002-tech-stack-and-repo-architecture.md) §2.2), and
  Juju enforces base compatibility between a principal and its subordinates.
  [PR #369](https://github.com/canonical/opentelemetry-collector-operator/pull/369)
  adds the bases but is open and unmerged. **This is a Stage 5 precondition
  only** — the provider side can be implemented and unit-tested now. Re-check with
  the scripted query in ADR-0002 §2.2.3.

---

## 4. Consequences

### Positive

- Shipping logs and Loki rules first means observability that **works on day one**
  rather than a set of inert Prometheus rule files.
- The alerts we can write cover the failure modes that actually bite: the port-53
  crash loop, gravity failure, and confinement denials.
- Verifying the absence of `slots:` avoids shipping a `log_slots` configuration
  that would have silently forwarded nothing.
- Deciding `limit: 1` now avoids an unfixable `juju refresh` break later.
- Recording the `grafana_agent` naming trap saves the next contributor from
  hunting a library that does not exist.

### Negative

- **No metrics in the first release**, which is what most operators will expect
  first from an observability integration. Dashboards will be thin.
- We vendor a Charmhub library from a deprecated distribution mechanism, with no
  migration path announced. When Charmhub freezes updates, we inherit whatever
  version we last fetched.
- The library name (`grafana_agent`) does not match the subordinate
  (`opentelemetry-collector`), which is permanently confusing and cannot be fixed
  from our side.
- Forwarding logs by path rather than by content slot means the paths are
  hardcoded against `$SNAP_COMMON`; a snap layout change breaks log collection
  silently. Worth an integration assertion that the files exist.
- **This relation cannot be integration-tested yet.** `opentelemetry-collector`
  publishes no 26.04 revision, so Stage 5's integration test waits on a third
  party. We deliberately did *not* hold the whole charm back on 24.04 for it
  (ADR-0002 §2.2), which means this one stage carries the delay instead of the
  entire project — but the delay is real and outside our control.
