# ADR-0001: Charm Scope and Specification

**Status:** Accepted
**Date:** 2026-08-07
**Accepted:** 2026-08-08
**Related:** [Snap constraints reference](../snap-constraints.md)

---

## 1. Context

We are building a Juju **machine charm** that deploys and operates Pi-hole v6 on
Ubuntu machines (LXD, MAAS, clouds) using the `pihole-by-rajannpatel` snap. The
repository is greenfield: only `AGENTS.md` and agent configuration exist.

This ADR fixes what the charm is for, what it will not do, and — most importantly
— the risk profile of its foundation, because that risk shapes every subsequent
decision.

### 1.1 Pi-hole is a good charm candidate

It is a long-running network service with real day-2 operations: upstream
resolvers change, blocklists need refreshing, the admin UI needs a password, DNS
must survive reboots and upgrades, and port 53 has to be wrestled away from
`systemd-resolved` on every Ubuntu host. That is convergence work, which is what
a charm is for.

### 1.2 The foundation is weak, and pretending otherwise would be a design error

| Fact | Consequence |
|---|---|
| The snap is **unofficial**: *"The Pi-hole project maintainers do not yet support snap-based installations."* | No upstream escalation path for workload bugs. Every workload interaction needs a read-back rather than trust. |
| Publisher `rajannpatel` is **unproven**, not verified. | Store presence is not an assurance. |
| Only `latest/stable` and `latest/edge` exist — **no tracks**. | `--channel` buys almost nothing. Reproducibility requires `--revision`, which forfeits security updates. The charm must expose this trade-off, not resolve it. |
| Several snap commands **return exit 0 having done nothing** (see [snap-constraints §4.2, §7.1](../snap-constraints.md)). | "Verify every success signal" becomes a structural requirement, not a nicety. |
| Non-amd64/arm64 builds are decoupled from the security gate. | Publish `amd64` and `arm64` only. |

None of this makes the charm not worth building. All of it makes *trust* the wrong
default, and that is why `AGENTS.md` non-negotiable #6 exists.

### 1.3 The defining hazard: this charm owns host DNS

Verified from `snap/hooks/remove`: the snap **cannot** restore
`systemd-resolved` on removal. Confinement blocks it; the hook only prints
instructions. So the charm's `remove` handler is the only thing that returns the
machine to a working resolver.

A Pi-hole that fails is an inconvenience. A Pi-hole that is *removed badly* is a
machine that cannot resolve any name at all. That asymmetry is the single most
important fact about this charm and it propagates into
[ADR-0005](0005-status-semantics-and-failure-handling.md).

---

## 2. Decision

Build a single-unit machine charm that converges a Pi-hole v6 installation toward
declared intent, with the following specification.

### 2.1 Functional scope

| Capability | Notes |
|---|---|
| Install and pin the snap | channel or explicit revision |
| Free port 53 from `systemd-resolved` | and **restore it on removal** |
| Start and enable the FTL daemon | required explicitly: the snap ships `install-mode: disable` |
| Declarative FTL configuration | upstream DNS, blocking, listening mode, DNSSEC, NTP posture — see [ADR-0004](0004-ftl-configuration-mechanism.md) and [ADR-0006](0006-configuration-surface.md) |
| Admin UI password from a Juju secret | see [ADR-0007](0007-admin-password-handling.md) |
| Connect the snap plugs that features require | idempotent, safe every reconcile |
| Gravity refresh schedule | via a host systemd drop-in the snap cannot write |
| Health reporting and diagnostics | `snap-check` exit codes, `pihole api dns/blocking` readiness |
| Operator escape hatches as actions | `snap-check`, `update-gravity`, `get-admin-password`, `free-port-53` |
| Observability via `cos-agent` | see [ADR-0008](0008-cos-integration.md) |
| DHCP server mode | **conditional** on verification — see [ADR-0006](0006-configuration-surface.md) |

### 2.2 Non-functional requirements

1. **Reaches `ActiveStatus` with zero relations.** Non-negotiable #5. The
   guarantee lives in `_reconcile` and `collect_unit_status`, not in
   `charmcraft.yaml` — `optional: true` is enforced by nothing.
2. **`juju remove-application` leaves the host with working DNS.** Treated as a
   correctness requirement with a mandatory integration test, not a nicety.
3. **Never reports `ActiveStatus` from `snap services` output.** The daemon is
   `active` long before blocking works ([snap-constraints §10](../snap-constraints.md)).
4. **Every workload mutation is followed by a read of real state.** An exit code
   is never the only evidence.
5. **Idempotent by construction.** Every reconcile step must be safe to run twice
   and safe to never run.

### 2.3 Deployment shape

Single unit. **Ubuntu 26.04**, `amd64` + `arm64`. No storage, no peers initially.
Base and platform rationale lives in
[ADR-0002 §2.1–2.2](0002-tech-stack-and-repo-architecture.md).

Multi-unit is **not** HA: two Pi-holes are two independent resolvers with
independently drifting blocklists, not a cluster. Real HA needs
`keepalived`/anycast and is a different charm.

---

## 3. Scope

### Included

- Single-unit convergence of the snap, its config, and the host state it needs.
- The `cos-agent` relation as the one integration point.
- Actions for the imperative operations that do not belong in a reconciler.

### Out of Scope

Explicitly, so nothing is built speculatively:

- **Anything Kubernetes.** No Pebble, no `lightkube`, no OCI image, no
  `containers:`, `devices:`, or `charm-user:` in `charmcraft.yaml`.
- **Bundling or managing Unbound.** The snap project has a standing architectural
  decision against it (*"Explanation: why Unbound is not bundled"*); a recursive
  resolver is a separate charm and, later, a relation.
- **HTTPS certificate lifecycle.** The snap's own docs place it out of scope and
  recommend a reverse proxy. Revisit as `tls-certificates` after
  [ADR-0008](0008-cos-integration.md) — tracked in [BACKLOG.md](../BACKLOG.md).
- **`lxd-profile.yaml`.** It subjects the charm to a Charmhub allow-list. Do not
  add it unless a stage proves a kernel module or privileged container is needed.
- **A Charmhub-hosted charm library under `lib/charms/pihole/...`.** Charmhub
  library hosting is being retired and we own no published interface.
- **Migrating an existing non-snap `/etc/pihole`.** The snap's own documentation
  says that path *"has not been fully tested."* Not something to automate on top
  of.
- **High availability.** See §2.3.
- **A 24.04 or 22.04 platform.** The charm is single-base `ubuntu@26.04`. Multi-base
  is possible but doubles revisions per release and would force the code to stay
  correct on two interpreters permanently. See
  [ADR-0002](0002-tech-stack-and-repo-architecture.md).

---

## 4. Consequences

### Positive

- A clear, small charm: one workload, one relation, no cluster semantics.
- The "verify everything" posture is justified by evidence rather than paranoia,
  which makes it defensible in review.
- Naming the removal hazard up front means the status strategy has an objective
  criterion to optimise for, instead of taste.
- Excluding HA, TLS, and Unbound keeps the first release achievable.

### Negative

- We depend on an unofficial, single-maintainer snap with no versioned track. If
  it is abandoned, the charm has no workload. There is no mitigation beyond
  documenting it and keeping the workload module thin enough to retarget.
- Read-back verification makes every apply step slower and more code than a naive
  charm would need.
- Publishing only `amd64`/`arm64` excludes architectures the snap technically
  supports. This is deliberate: we will not ship a charm on a build that is
  outside the snap's own security gate.
- Excluding revision-pinning as a *default* means operators tracking `stable`
  can be surprised by a workload change. Exposing `snap-revision` as an option
  shifts that choice to them rather than removing it.
