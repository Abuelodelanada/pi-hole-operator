# ADR-0008: COS Integration

**Status:** Accepted
**Date:** 2026-08-07
**Accepted:** 2026-09-18 — implemented and verified live: the relation, the
databag, the slot connection, the filelog receiver with our topology, and the
files readable inside the subordinate's namespace. Every Stage 5 acceptance
box is ticked; the one hop this harness cannot prove — logs observed in a
deployed Loki — is reworded out of the box and into BACKLOG with its trigger.
**Amended:** 2026-09-18 — §1.2's remedy rewritten again: the upstream slot
landed (PR #18) and is in the pinned revisions (ADR-0010's 2026-09-18 bump), so
`log_slots` is now passed and connected live. §2.5's wiring gained `log_slots`
and `upgrade_charm` as a refresh event. §2.1's deferral reason updated: the
label shapes are now observed, and the selector decision is topology, not
`filename` — the filename label is the subordinate's mount path and embeds its
snap revision, so a filename selector breaks on every subordinate refresh.
**Related:** [ADR-0002: Tech Stack and Repository Architecture](0002-tech-stack-and-repo-architecture.md), [ADR-0006: Configuration Surface](0006-configuration-surface.md), [ADR-0009: Split the FTL API client out of `Pihole`](0009-ftl-api-client-module.md), [ADR-0010: The Snap Revision Is Charm Policy](0010-snap-revision-is-charm-policy.md)

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

### 1.2 The logs content slot — upstream, since PR #18

`COSAgentProvider` accepts `log_slots=[...]`, which requires the snap to expose a
`content` slot for its log directory. cos_agent v0 (LIBPATCH 27) has **no
path-based mechanism at all** — verified in the library and the subordinate's
source: the provider publishes `log_slots`; the consumer `snap connect`s its
`logs` plug to each named slot, reads the mount from snapd's fstab, and tails
the mounted path. No slot → no connect → no fstab entry → no log receiver, and
there is no journald receiver either.

**History:** verified by grep on 2026-08-07 that the snapcraft had no `slots:`
key at all, which made `log_slots` impossible; this section then wrongly
concluded "forward by path" (corrected 2026-09-07 — no such mechanism exists);
the operator filed
[PR #18](https://github.com/rajannpatel/snap-pi-hole/pull/18) on 2026-09-07
asking for a read-only `logs` slot, and it merged and published in the pinned
revisions (1417/1415, [ADR-0010](0010-snap-revision-is-charm-policy.md)'s
2026-09-18 bump). The slot exposes `$SNAP_COMMON/var/log/pihole/`; the files
observed in it on a live unit are `FTL.log`, `pihole.log`, `webserver.log`,
`gravity-init.log`, and `gravity-first-run.log`. Connected live on the
operator's model: `snap connections` shows
`opentelemetry-collector:logs ↔ pihole-by-rajannpatel:logs`.

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

- **Now:** the `cos-agent` relation. Host metrics arrive with it via the
  subordinate's own `node-exporter` (§2.2).
- **When the rules are authored** (the slot has landed; forwarding works): the
  selector is **topology labels, never `filename`** — observed live, the
  `filename` label is the subordinate's mount path
  (`/snap/opentelemetry-collector/<rev>/shared-logs/pihole/FTL.log`), which
  embeds the subordinate's snap revision and breaks on every one of its
  refreshes. Also observed: the subordinate sets `juju_charm` to *its own* charm
  name in our receiver's topology, so rules key on `juju_application`/
  `juju_unit` only. Until the rules are written and validated against a real
  Loki, this stays deferred — authoring against a guessed label shape is the
  failure this charm refuses to ship.
- **Deferred:** Prometheus metrics, and therefore Prometheus alert rules.

The failure modes we most need to detect are visible in logs, so when forwarding
lands these are the signals worth rules:

| Alert | Signal in the logs |
|---|---|
| FTL crash-looping | repeated `EADDRINUSE` in `FTL.log` — the launcher no longer pre-checks port 53, and `restart-condition: on-failure` makes this an indefinite loop |
| Gravity sync failing | `gravity-init.log`; the weekly timer is the only thing refreshing blocklists |

**Not alertable from snap logs:** AppArmor `DENIED` bursts. Denials land in the
host's `kern.log`/journal, not in `$SNAP_COMMON`, so a rule over the forwarded
snap logs can never see them. Dropped from the first cut; revisit if the host's
logs ever get forwarded.

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

**The requirement was stated 2026-09-07, and the choice was researched** — five
community exporters, none shippable: `eko/pihole-exporter` (the recognised name)
merged v6 support but is broken against v6's session model — it re-authenticates
up to 7× per scrape and wedges after one timeout, with the fixes unmerged since
July 2025; the fork that fixes it is days old with no adoption; the only exporter
that handles the 16-session cap correctly is Docker-only; none ship a snap; and no
Pi-hole charm exists on Charmhub. **Decision: defer Pi-hole-specific metrics.**
Host metrics arrive anyway — the subordinate installs and scrapes the
`node-exporter` snap itself (verified in its source: receiver
`prometheus/node-exporter`, job `juju_<topology>_node-exporter`), so relating
cos-agent yields host-level metrics with no exporter on our side. When
Pi-hole-specific metrics are wanted, a **charm-owned exporter** is the leading
candidate — the session-lifecycle machinery is already verified in
[ADR-0009](0009-ftl-api-client-module.md) — but it carries a real cost the
community options share: FTL requires a session for reads once a password is set
(snap-constraints §7.2.6), so any exporter holds the admin password at rest.

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
    log_slots=[f"{pihole_state.SNAP_NAME}:logs"],
    refresh_events=[self.on.config_changed, self.on.upgrade_charm],
)
```

`log_slots` names the snap's read-only `logs` content slot (§1.2). `upgrade_charm`
is a refresh event because an upgrade can change what we publish — this one did:
`log_slots` went from impossible to advertised, and without the event the
subordinate keeps reading the pre-upgrade databag. No `metrics_endpoints`
(deferred, §2.2).

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
- **Observing logs in a deployed Loki** — the only hop this harness cannot
  prove, because no machine Loki charm exists (COS's Loki runs on Kubernetes).
  The machine-side chain is verified (databag, connection, files — asserted by
  the suite; receiver and in-namespace readability — verified live). The
  base-compatibility caveat that used to live here is recorded in
  [ADR-0002](0002-tech-stack-and-repo-architecture.md)'s header `Amended:` lines:
  deploy the subordinate with `--channel=0.130/stable --revision=<per-arch>`.

---

## 4. Consequences

### Positive

- Log forwarding works on day one via the upstream slot; the Loki rules follow
  once their label shapes are validated against a real Loki, so the first
  release ships plumbing that is verified rather than rules that might not fire.
- The alerts worth writing cover the failure modes that actually bite: the
  port-53 crash loop and gravity failure. Confinement denials are out — they
  land in the host's `kern.log`, not the snap's logs (§2.1).
- Verifying the absence of `slots:` before PR #18 avoided shipping a `log_slots`
  configuration that would have silently forwarded nothing; verifying the slot's
  presence is now part of ADR-0010's bump ritual, because its failure mode is
  silent.
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
- Forwarding rides on the subordinate's correctness: its `juju_charm` topology
  label is wrong (its own name instead of ours — an upstream bug), and the
  `filename` label embeds its snap revision. Our rules must therefore key on
  `juju_application`/`juju_unit` only, and the integration suite asserts what
  it can (databag, connection, files) rather than trusting it — the receiver
  config was verified by hand on the operator's model.
- **Observing logs in a deployed Loki** — the only hop this harness cannot
  prove, because no machine Loki charm exists (COS's Loki runs on Kubernetes).
  Everything on our side of that hop is integration-tested: the databag, the
  slot connection, and the files; the receiver config and in-namespace
  readability were verified live on the operator's model (2026-09-18).
