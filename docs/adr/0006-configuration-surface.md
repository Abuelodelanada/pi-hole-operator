# ADR-0006: Configuration Surface — Config Options, Relations, and Bindings

**Status:** Accepted
**Date:** 2026-08-07
**Accepted:** 2026-08-08
**Amended:** 2026-08-08 — `web-password` moved from accepted to rejected, per [ADR-0007 §3](0007-admin-password-handling.md).
**Related:** [ADR-0001: Charm Scope and Specification](0001-charm-scope-and-specification.md), [ADR-0004: FTL Configuration Mechanism](0004-ftl-configuration-mechanism.md), [ADR-0007: Admin Password Handling](0007-admin-password-handling.md)

---

## 1. Context

A config option is a **permanent public API**. Removing or renaming one breaks
every existing deployment — the same class of irreversibility as `limit` on a
relation endpoint. So `AGENTS.md` non-negotiable #4 requires checking three
alternatives, in order, before adding one:

1. Does **another charm** own this data? → a relation.
2. Is it **network placement**? → `extra-bindings` / a Juju space.
3. Is it **deployment shape** the operator sets outside the charm? → constraints,
   placement, their own tooling.

Config options are the residue, not the default. This ADR records the audit for
every option, including the rejections, so the rejections do not get silently
re-added.

---

## 2. Decisions

### 2.1 Accepted config options

| Option | Type | Justification against §1 |
|---|---|---|
| `snap-channel` | string (`stable`\|`edge`) | Only two values exist ([snap-constraints §1](../snap-constraints.md)). Not deployment shape — it selects workload behaviour, and only the charm can act on it. |
| `snap-revision` | string | The **only** reproducibility lever, because the snap publishes no tracks. Empty means track the channel. Document the cost: a pinned revision stops receiving security updates. |
| `upstream-dns` | string (CSV in, JSON array out) | Core workload intent, and operator intent rather than data another charm owns. See §2.4. |
| `dns-listening-mode` | string enum | An enum, not a raw FTL string, so the charm owns the vocabulary. FTL's values are `LOCAL \| SINGLE \| BIND \| ALL \| NONE` (default `LOCAL`) — **not** `LISTEN_LOCAL`/`LISTEN_ALL` as an earlier draft stated; corrected from the annotated `pihole.toml`. |
| `blocking-enabled` | boolean | Reachable, cheap, genuinely operational. |
| `ntp-server-enabled` | boolean, **default `false`** | See §2.3. |
| `dnssec-enabled` | boolean | Must route through the ADR-0004 fallback — `snap set` silently drops it. |
| `gravity-schedule` | string (systemd `OnCalendar`) | The snap **physically cannot** do this: the configure hook rejects all `timer.*` keys and the schedule is static in snap metadata. The charm writes a host systemd drop-in. Legitimately charm-owned. |

`pydantic` parses all of these via `self.load_config(PiholeConfig,
errors="blocked")`. Dashes map to underscores automatically, `Field(alias=...)` is
honoured, and `errors="blocked"` sets `BlockedStatus` and exits 0 so Juju does not
retry a hook that can only fail again — consistent with
[ADR-0005](0005-status-semantics-and-failure-handling.md) §1.2.5.

### 2.2 Rejected, with the alternative that replaces each

| Rejected | Instead | Rule applied |
|---|---|---|
| `listen-interface` / `bind-address` | **`extra-bindings: dns`** + `self.model.get_binding("dns")`, using `network.bind_address` to bind and `network.ingress_address` to advertise. | #2 — network placement. Juju spaces own this. This is non-negotiable #4 in its purest form. |
| `web-port` as an *operator* option | Nothing — but the charm **must manage `webserver.port` itself**. See §2.10. | The reasoning in the original draft ("FTL's default already fails soft") was factually wrong. |
| `install-timeout`, `readiness-timeout` | Constants in `pihole.py`. | A permanent public API for a transient problem is a bad trade. |
| `blocklists` / `adlists` | **Deferred** — see [BACKLOG.md](../BACKLOG.md). | Adlists live in the `adlist` table of `gravity.db`, not in config. Not declarative, not transactional, not idempotent. **Do not add a config option whose implementation has not been proven.** |
| `upstream-dns` as a *relation* | Kept as a config option. See §2.4 — no resolver charm and no interface exist, and upstreams are operator intent. | #1 checked, does not apply. |
| `web-password` | **Nothing.** The charm generates the admin password, owns it in a Juju secret, and exposes `get-admin-password` / `rotate-admin-password`. See [ADR-0007 §3](0007-admin-password-handling.md). | One source of truth beats two mechanisms plus a precedence rule. |

### 2.3 NTP off by default — a deliberate divergence from the snap

The snap ships `ntp.ipv4.active` and `ntp.ipv6.active` defaulting to `true`, so a
stock install serves NTP on **123/udp**. That is unexpected attack surface for
something an operator deployed as a DNS appliance.

Both keys are reachable via `snap set`, so the charm *can* decide. It defaults to
`false`, and `set_ports` only opens 123/udp when the option is enabled.

**This is a divergence from upstream defaults and must be documented in the
README.** Silently changing a workload default is worse than the exposure it
prevents.

### 2.4 `upstream-dns` is a config option, and stays one

An earlier draft of this ADR claimed this option was "designed to become
relation-provided", reasoning that a future `unbound` charm would be the natural
owner of which upstream resolvers Pi-hole should use. **Both premises were checked
on 2026-08-08 and both are false:**

- **There is no `unbound` charm on Charmhub.** (`bind`, `coredns` and `designate`
  exist, but they are authoritative or managed-DNS charms, not a drop-in recursive
  resolver a Pi-hole would forward to.)
- **There is no interface for it.** The only DNS-related entry in
  `charm-relation-interfaces` is `dns_record`, which solves a different problem:
  *"the requirer is a charm that wishes to create a set of DNS records, and the
  provider is the charm managing those."* That is record registration, not resolver
  discovery.

So a relation here would have neither a counterparty nor a standardised interface,
and designing for one now would mean **inventing an interface** — which
non-negotiable #4 and the relations guidance both forbid without checking first.

**The positive case for config is strong on its own.** Upstream resolvers are
operator intent, not data another charm owns: nothing in a Juju model owns
`1.1.1.1` or `9.9.9.9`. Pi-hole's own admin UI presents them as a hand-entered
list, which is exactly the shape of a config option. This is the residue case
non-negotiable #4 describes, not a failure to look for a relation.

**If a resolver charm and an interface both appear**, revisit it then — and decide
precedence explicitly at that point rather than assuming it now. "Relation wins,
config is an override" and "config wins, relation is a default" are both
defensible; silently applying both would be the one genuinely wrong answer.
Tracked in [BACKLOG.md](../BACKLOG.md) with that trigger.

### 2.5 Relation endpoints

```yaml
provides:
  cos-agent:
    interface: cos_agent
    limit: 1
    optional: true
```

That is the only endpoint in the initial charm. Two properties are easy to
conflate and must not be:

- **`optional` is enforced by nothing.** The reference says so verbatim: *"Not
  enforced by Juju, but used by other tools."* It is documentation. The guarantee
  that the charm comes up clean with zero relations lives in `_reconcile` and
  `collect_unit_status`. **A correct `charmcraft.yaml` is not evidence.**
- **`limit` *is* enforced by Juju**, on all three roles, and is **not safely
  reversible**: `preUpgradeRelationLimitCheck` means publishing a revision that
  adds `limit: 1` to an endpoint where a user already has two relations **breaks
  their `juju refresh`**. Decide it once, now.

Note that every application implicitly provides a `juju-info` endpoint that cannot
be declared or removed, and explicit matching endpoints take precedence over it.
So `juju integrate pihole opentelemetry-collector` may resolve to something other
than `cos-agent`. **Always name both endpoints explicitly** in tests and docs.

### 2.6 Reconciling relations tolerates absence

`self.model.relations["not-declared"]` raises `KeyError`; a declared endpoint with
zero integrations returns `[]`, which is what "optional by default" relies on.
During `relation-broken` the breaking relation is **already excluded** from the
list.

So: no `if relation is None: return` guards at the top of `_reconcile` — iterate
over possibly-empty collections instead. A reconciler that bails early on a
missing relation stops converging everything downstream of it.

Anything writing an app databag must be guarded by `self.unit.is_leader()`, and
`leader_elected` must be in the reconciler's event list or a newly elected leader
never publishes.

### 2.7 Actions, and the one Juju-version trap

Actions are the escape hatch for imperative operations that do not belong in
`_reconcile`, and they are the one place a per-event handler is correct — they
cannot be deferred, which is the official test.

| Action | Purpose |
|---|---|
| `snap-check` | Run the snap's own diagnostic; return stdout and exit code verbatim. |
| `update-gravity` (`force: boolean`) | Refresh blocklists now instead of waiting for the weekly timer. |
| `get-admin-password` | Retrieve the admin UI password (from the secret, never from snapd state). |
| `free-port-53` | The remedy named in the port-53 `BlockedStatus` message. |

**Always set `additionalProperties` explicitly.** The default differs between
Juju 3 (`true`) and Juju 4 (`false`), so omitting it means the behaviour changes
under you.

### 2.8 Ports are computed from config

```python
ops.Port("tcp", 53), ops.Port("udp", 53), ops.Port("tcp", 80)
```

plus 123/udp only when NTP is enabled, and 67/udp + 546/udp only when DHCP is.

**Not 443.** The charm disables TLS (§2.10), so advertising 443 would document a
listener that does not exist.

Three traps:

- **`ops.TCPPort` / `ops.UDPPort` do not exist in `ops`** — only in `ops.testing`.
  Production code uses `ops.Port`.
- **A bare `int` means TCP.** `set_ports(53)` opens 53/tcp only. Omitting 53/udp
  is the single easiest way to ship a broken DNS charm.
- **This is documentation, not enforcement.** `open-port` has no effect unless the
  application is exposed, and **the LXD provider implements no firewaller at all**
  — there is no `OpenPorts`/`ClosePorts`/`IngressRules` in
  `internal/provider/lxd/`. On LXD, port 53 is reachable with or without
  `juju expose`. Never write a test asserting otherwise, and say so in the README
  because MAAS/EC2 behave differently from every test we run.

`set_ports` is declarative and diffs against `opened_ports()`, which is what makes
it a correct reconcile step. Do not mix it with `ops.hookcmds.open_port`.

### 2.9 DHCP is accepted in principle, gated in practice

`dhcp-enabled`, `dhcp-range-start`, `dhcp-range-end`, `dhcp-router`,
`dhcp-netmask` — with cross-field pydantic validation so enabling DHCP without a
complete pool is `Blocked` rather than a crash loop.

**Gated on two verifications** ([snap-constraints §4.4, §9](../snap-constraints.md)):

1. End-to-end operation under strict confinement was never proven. The observed
   failure was `EADDRINUSE` from LXD's `lxdbr0` dnsmasq — a port conflict, not an
   AppArmor denial — so confinement does not *appear* to be the blocker, but that
   must be shown on a host with 67 free.
2. **The wiki contradicts itself on the key names**: `ftl.dhcp.start/end/router`
   versus `ftl.dhcp.ipv4.range.start/end/router`. Both cannot be right.

Ordering is mandatory and lives in `compute`'s output sequence: pool → router →
`dhcp.active` **last**. Setting `active` first fails with *"DHCP start address is
not valid"*, and `restart-condition: on-failure` turns that into a crash loop
rather than a degraded service.

### 2.10 `webserver.port` is charm-managed, and not an operator option

The packaged default requests TLS, FTL cannot generate its certificate inside this
snap, and the SSL failure aborts the **entire** webserver — including the
plain-HTTP listeners. A stock install has no admin UI and no HTTP API
([snap-constraints §5.1](../snap-constraints.md)).

The charm therefore sets

```
ftl.webserver.port = "80o,[::]:80o"
```

**before the daemon first starts**, unconditionally. Properties:

- It is a **reachable** `snap set` key, so it works before the API exists — which
  is precisely why it is the one key in the bootstrap phase of
  [ADR-0004 §4](0004-ftl-configuration-mechanism.md).
- Applying it before first start avoids the failure entirely: verified, port 80
  binds and the API answers on the first boot.
- It is **not** exposed as a config option. This is a workaround for a workload
  defect, not a deployment choice, and an operator setting it to something that
  re-enables TLS would silently lose the admin UI and the charm's own config path.

When TLS support becomes real — either the snap fixes certificate generation or we
supply a certificate through `tls-certificates` — this value has to be revisited
together with that work. Tracked in [BACKLOG.md](../BACKLOG.md).

---

## 3. Consequences

### Positive

- Nine options, each with a written justification, and five rejections with named
  alternatives. Future "can we just add an option for X" requests have a rubric to
  be measured against.
- Using `extra-bindings` instead of a `listen-interface` option means DNS
  placement is a Juju space concern, which is where operators already manage it.
- The `upstream-dns` decision now rests on a checked fact — no resolver charm, no
  interface — rather than on speculation about a charm that does not exist.
- Deciding `limit: 1` on `cos-agent` now avoids breaking `juju refresh` later.
- Gating DHCP behind verification keeps an unproven feature from shipping as if it
  were supported.

### Negative

- Nine options is a large permanent API surface for a first release. Each one is
  a compatibility commitment we cannot walk back.
- `ntp-server-enabled=false` diverges from the workload's own default, so an
  operator comparing the charm against a manual install will see different
  behaviour. Documented, but still a surprise.
- `gravity-schedule` takes a raw systemd `OnCalendar` string, which leaks an
  implementation detail into the public API. The alternative — inventing a
  charm-specific schedule vocabulary — is worse, but this is not free.
- `dns-listening-mode` as an enum means the charm must track FTL's vocabulary and
  translate. If FTL adds a mode, the charm needs a release to expose it.
- Rejecting `blocklists` means the charm's most visible Pi-hole feature — which
  lists are blocked — is not manageable through the charm at all in the first
  release. This is the most likely source of user disappointment, and it is a
  deliberate trade against shipping an unproven non-idempotent reconcile step.
