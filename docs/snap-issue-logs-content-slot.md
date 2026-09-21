# Add a read-only `logs` content slot for the log directory

> **Filed 2026-09-07, merged as
> [PR #18](https://github.com/rajannpatel/snap-pi-hole/pull/18), published in
> revisions 1417 (amd64) / 1415 (arm64).** This document is the record of the
> request; the text below is as filed.

## Context

The [opentelemetry-collector snap](https://github.com/canonical/opentelemetry-collector-snap)
declares a `logs` plug ([snapcraft.yaml](https://github.com/canonical/opentelemetry-collector-snap/blob/main/0.130/snap/snapcraft.yaml)):

```yaml
plugs:
  logs:
    interface: content
    target: $SNAP/shared-logs
```

When the collector is co-installed with a workload snap that exposes a matching
`content` slot, it connects to that slot and tails the exposed log files,
forwarding them to Loki. That is the standard log-forwarding path for snap
workloads under the Canonical Observability Stack.

This snap declares no slots today, so there is nothing for that plug to connect
to — its logs cannot leave the machine through the standard mechanism. We are
asking because we operate this snap under
[pi-hole-operator](https://github.com/Abuelodelanada/pi-hole-operator), a Juju
machine charm that deploys the collector as a subordinate and needs this
connection to work.

## Request

Declare a read-only content slot exposing the log directory:

```yaml
slots:
  logs:
    interface: content
    source:
      read: ["$SNAP_COMMON/var/log/pihole"]
```

## What this enables

```
snap connect opentelemetry-collector:logs pihole-by-rajannpatel:logs
```

After that connection, the log files under `$SNAP_COMMON/var/log/pihole/`
(`FTL.log`, `pihole.log`, `webserver.log`, `gravity-init.log`,
`gravity-first-run.log`) appear under the collector's `$SNAP/shared-logs` and
are tailed and forwarded.

## Why this shape

- **Read-only** (`read:`, no `write:`) — a log forwarder must never be able to
  modify or truncate the logs it reads.
- **Inert until connected** — a slot changes nothing for anyone who does not
  connect it: no behavior change, no permission change for existing installs.
- **The standard mechanism** — the `content` interface with a read-only source
  is how snaps expose files to another snap with the narrowest possible
  permission; it is the same pattern other snap workloads use for observability
  integration.
