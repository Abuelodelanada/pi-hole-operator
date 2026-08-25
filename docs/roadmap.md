# Implementation roadmap

**Status:** Proposed
**Last updated:** 2026-08-07
**Audience:** `charm-engineer`

Staged delivery plan for the charm specified in
[ADR-0001](adr/0001-charm-scope-and-specification.md). Each stage is
independently shippable and leaves the repository green and reviewable.

Design rationale is **not** repeated here — this document sequences work and
defines acceptance. For *why*, follow the ADR links.

---

## How to read this

| Doc | Answers |
|---|---|
| [`pattern.md`](pattern.md) | How the charm decides what to do, taught with a small example that is not Pi-hole. |
| [`adr/`](adr/) | Why the charm is shaped this way. Decisions, alternatives, consequences. |
| [`snap-constraints.md`](snap-constraints.md) | What the workload actually does. Verified facts, cited by the ADRs. |
| [`implementation/`](implementation/) | How a module that already exists works, and the edge cases it encodes. |
| **this file** | In what order we build it, and how we know a stage is done. |
| [`BACKLOG.md`](BACKLOG.md) | What we deliberately are not building yet, and the trigger to revisit. |

`docs/implementation/` gets one document per module as that module lands —
documenting code that exists, not code we intend to write. Present today:
[`pihole_state.md`](implementation/pihole_state.md).

---

## Stage sequencing rationale

Two ordering decisions are load-bearing and not obvious:

1. **The functional core lands in Stage 1, not later.** Retrofitting
   fetch/compute/apply onto an imperative reconciler is a rewrite, and the
   push-status channel ([ADR-0005](adr/0005-status-semantics-and-failure-handling.md)
   §2.4) is worse still — adding it late means auditing every status path again.
   Architecture is present from the first functional stage; only its *scope* grows.
2. **Stage 2 opens with a spike, not with code.** The mechanism for the 66
   unreachable FTL keys is unresolved
   ([ADR-0004](adr/0004-ftl-configuration-mechanism.md) §6). A guessed command
   that silently no-ops is the exact failure this charm is designed to prevent.

---

## Stage 0 — Scaffold and toolchain

**Goal:** a charm that packs, deploys, and reaches `ActiveStatus` doing nothing.
Proves the toolchain before any workload risk enters.

**Reference:** [ADR-0002](adr/0002-tech-stack-and-repo-architecture.md)

**Deliverables**

- `charmcraft.yaml`: `base: ubuntu@26.04`, `platforms: {amd64:, arm64:}`,
  `parts.charm.plugin: uv`, `build-snaps: [astral-uv]`. No Kubernetes keys.
  `summary` ≤ 78 chars. `links.documentation` points at *this charm's* docs.
- `pyproject.toml`: `requires-python = ">=3.14"`; deps per ADR-0002 §2.5; ruff
  with `line-length = 99` **and** `max-doc-length = 72`; `PLC0415` in `select`;
  pyright strict; coverage `fail_under = 90`.
- `uv.lock` committed. `tox.ini` with `flaplint` outside `env_list`.
- `.jujuignore` (including `/.opencode`), `icon.svg`, `README.md`,
  `CONTRIBUTING.md`.
- `src/charm.py`: full observer wiring, `_reconcile` as a no-op,
  `collect_unit_status`, and the `_reconcile_failure` attribute **already present**.
  Entry point `ops.main(PiholeCharm)`.
- `tests/unit/conftest.py` with the shared fixtures. **`testing.Model(type="lxd")`
  is mandatory** — `ops.testing` defaults to `kubernetes`, and a machine charm
  tested with the default is in the wrong environment.
- CI on **3.14 only** — the sole interpreter in the 26.04 archive.
- **Integration tests must use LXD VMs**, not containers: snapd cannot mount snaps
  in a 26.04 container (ADR-0002 §2.2.2). Put
  `constraints="virt-type=virtual-machine"` in the `conftest.py` fixture so it
  cannot be forgotten.

**Acceptance**

- [x] `tox -e lint,static,unit` green; `charmcraft pack` succeeds.
- [x] `charmcraft analyse` resolves `language: python` and `framework: operator`.
- [x] Deploys on LXD and reaches `active/idle` with **zero relations**.

---

## Stage 1 — Install, free port 53, start, restore on removal

**Goal:** a working Pi-hole answering DNS, with the host recoverable. Highest-risk
stage; everything after it is elaboration.

**Reference:** [ADR-0003](adr/0003-reconciler-and-functional-core.md),
[ADR-0005](adr/0005-status-semantics-and-failure-handling.md),
[ADR-0007](adr/0007-admin-password-handling.md),
[snap-constraints §2, §5.1, §5.2, §8, §10, §11](snap-constraints.md)

> **Two workload defects make this stage bigger than it looks.** A stock install
> has **no admin UI and no HTTP API** (snap-constraints §5.1), and if it did, that
> API would be **writable by anyone on the network** (§5.2). Both are opened by the
> charm's own act of starting the daemon, so both must be closed here — not in a
> later stage.

**Deliverables**

- `src/resolved.py` — write/remove `/etc/systemd/resolved.conf.d/pihole.conf`
  (`[Resolve]\nDNS=127.0.0.1\nDNSStubListener=no\n`), then
  `systemd.service_restart("systemd-resolved")`. Idempotent: identical content
  causes no restart.
- `src/pihole.py` — `install()` with bounded `tenacity` retry on `snap.Error`
  (**not** `snap.SnapError`, whose siblings it does not cover — ADR-0005 §2.7),
  `start(enable=True)`, `ftl_status()`, `blocking_state()`, `snap_check()`,
  `workload_version()`. Collaborators injected per ADR-0003 §2.7.
- `src/pihole_state.py` — `SnapAbsent | SnapPresent`, a minimal outcome union,
  `fetch()`, `compute()`.
- `charm.py` — `_reconcile` wired to real outcomes; `_on_remove` calling
  `resolved.restore()`; `set_ports` for 53/tcp+udp and 80/tcp. **Not 443** — the
  charm disables TLS (ADR-0006 §2.10).
- **`snap set ftl.webserver.port="80o,[::]:80o"` before the first start.** Without
  it the webserver never binds and the API never appears (snap-constraints §5.1).
- **The NTP server the snap opens by default on 123/udp is closed** —
  `ntp.ipv4.active` and `ntp.ipv6.active` set false, verified in `pihole.toml`.
  Third instance of the stage's own rule: a hole opened by the charm's act of
  starting the daemon is closed here, not later.
- **An admin password is generated and applied before the daemon serves** — stored
  in a charm-owned, app-level Juju secret, retrieved by label, written only on the
  leader (ADR-0007 §4.1). There must be no window with `pwhash = ""`. There is
  **no config option** for it.
- Readiness gated on the HTTP API (`GET /api/dns/blocking`), **never**
  `snap services` — and only *after* the port fix, or the gate can never pass.
- **Mandatory ordering** in `compute`'s output sequence:
  `install → free 53 → set webserver.port → close NTP → set password → start
  → gate on API`.
  Install precedes freeing port 53 so a store failure cannot leave the host without
  a resolver (ADR-0005 §2.9).

**Tests**

- Pure: `compute(SnapAbsent(), ...)` yields the ordered install sequence. No mocks.
- Pure: converged `SnapPresent` yields `(Noop(),)` — the literal "safe to run
  twice" proof.
- Transition: not-ready yields `Maintenance`, **not** `Active` — the "safe to never
  run" direction.
- Workload: `ensure` called **and** `start(enable=True)` called — regression test
  for `install-mode: disable`.
- Workload: resolved drop-in written on install, **deleted on remove**.
- Pure: the outcome sequence puts `webserver.port`, the NTP closure and the
  password **before** `StartFtl`. This is the whole stage's correctness condition
  and it is a pure assertion on a tuple — no mocks.
- Pure: an active NTP server yields exactly `DisableNtpServer` plus its own
  readiness gate, because the configure hook restarts FTL on a changed value.
- Regression: `set_ports` never opens 443 while TLS is disabled.
- `ctx.unit_status_history` passes through `Maintenance` rather than jumping to
  `Active`.
- pytest runs with `-W error`.

**Integration**

- Deploy → `active/idle`; `dig +short @127.0.0.1 example.com` returns an answer.
- **`juju remove-application` leaves the host with working DNS.**
- **Port 80 is bound and the HTTP API answers** on the first boot, with no manual
  intervention.
- **Nothing listens on 123/udp** after convergence (`ss -ulpn`).
- **An unauthenticated `PATCH /api/config` from another host is refused.** This is
  the §5.2 regression test and it must run from off-machine, not from localhost.
- Deploy with `constraints="virt-type=virtual-machine"`: snaps cannot be installed
  in a 26.04 LXD container at all (ADR-0002 §2.2.2).
- Verify real state with `juju exec --unit pihole/0 -- ...`. Use `--unit` (root,
  hook context), **not** `--machine` (runs as `ubuntu`, where `snap get` can fail
  on permissions).
- Beware the CLI rename: `juju run` in a 2.9 tutorial is `juju exec` today; `juju
  run` today is the old `juju run-action`. Both exist, so the mistake is silent.

**Acceptance**

- [x] All of the above, plus Stage 0's acceptance still holds. (CI on 3.14
      lands with `.github/`, deliberately deferred.)
- [x] `charm-reviewer` clean (2026-08-25, against the tree that closes this
      stage; the NTP fact is `bool | None` — a future consumer treating it as
      plain `bool` reopens the fail-open hole).

---

## Stage 2 — Configuration and the unreachable-key problem

**Goal:** declarative config that is verified to have landed.

**Reference:** [ADR-0004](adr/0004-ftl-configuration-mechanism.md),
[ADR-0006](adr/0006-configuration-surface.md)

> **The spike is done.** [ADR-0004](adr/0004-ftl-configuration-mechanism.md) is
> **Accepted**: steady-state configuration goes through `PATCH /api/config`, which
> handles all 166 keys including the camelCase ones, and does not restart FTL.
> `_is_snapd_safe_key` is **not** part of the design — do not write it.

**Deliverables**

- `src/pihole_config.py` — pydantic model via
  `self.load_config(PiholeConfig, errors="blocked")`. CSV in, **JSON array out**
  for `dns.upstreams` and friends. Deterministic ordering: serialise from a sorted
  tuple.
- An HTTP client in `pihole.py` using stdlib `urllib.request`: `POST /api/auth`
  with a freshly read `cli_pw`, then a single `PATCH /api/config` carrying the whole
  desired mapping, then `DELETE /api/auth`.
- **Re-read `cli_pw` on every use.** It rotates on every FTL restart (verified).
- `apply_ftl_config()` with mandatory read-back against `pihole.toml` via stdlib
  `tomllib`, raising `PiholeError(key, expected, actual)` — because **an unknown key
  returns `200` and is silently ignored**.
- Map FTL's `400` `hint` into the `BlockedStatus` message verbatim; it is already
  written for a human.
- `dns.dnssec` needs no special case any more: the API applies it correctly.
- `$SNAP_DATA` resolved through `current`; never a hardcoded revision.
- `extra-bindings: dns`; bind address from `self.model.get_binding("dns")`.
- `ntp-server-enabled` config option to re-enable the NTP server Stage 1
  disables; 123/udp opened only when enabled.

**Tests**

- **The lying-API test:** the fake returns `200` and the TOML read-back returns the
  old value → `PiholeError`. Highest-value test in the suite; it encodes the
  verified unknown-key behaviour.
- `dns.listeningMode` (camelCase) round-trips through the API path — the case
  `snap set` cannot express at all.
- A `400` response surfaces FTL's `hint` verbatim in the resulting `BlockedStatus`.
- `cli_pw` is re-read on every call, never cached.
- The charm never emits `pihole -a -p` or `pihole restartdns` — both are v5 syntax
  that print usage and **exit 0**.
- `compute` emits `RestartFtl` **only** when a value actually changed.
- Collections sorted **at construction**, never in the assertion.

**Acceptance**

- [ ] `dns.listeningMode=ALL` is observable in `pihole.toml` and FTL was **not**
      restarted (PID unchanged).
- [ ] `juju config pihole upstream-dns=...` is observable in `pihole.toml` **and**
      in a `dig` result.
- [ ] Setting the same config twice does not restart FTL (check the PID).

---

## Stage 3 — Plugs, diagnostics, and actions

**Goal:** the charm knows when it is unhealthy, and the operator has escape
hatches.

**Reference:** [ADR-0005](adr/0005-status-semantics-and-failure-handling.md),
[ADR-0006 §2.7](adr/0006-configuration-surface.md),
[snap-constraints §3, §7.3](snap-constraints.md)

**Deliverables**

- `connect_plugs()` — idempotent, safe every reconcile. `system-observe`,
  `hardware-observe`, `mount-observe`, `time-control`, `process-control`
  unconditionally; `network-control`/`firewall-control` gated on DHCP. **Read
  `snap connections` back** — the snap's own docs warn that store auto-connection
  and `--dangerous` installs produce different states.
- `snap_check()` mapping exit codes into status: `0` OK, `1` config error, `2`
  runtime/port error → `Blocked` **naming the remedy**. Called from
  `collect_unit_status`, which must not mutate.
- Actions, each with `additionalProperties` set **explicitly**: `snap-check`,
  `update-gravity` (`force`), `free-port-53`. (`get-admin-password` and
  `rotate-admin-password` ship in Stage 1 with the password itself.)
- `WriteGravityTimer` — the host drop-in at
  `/etc/systemd/system/snap.pihole-by-rajannpatel.gravity-sync.timer.d/override.conf`
  then `daemon_reload()`. `OnCalendar=` must be **cleared before being set**.

**Tests**

- Actions go through `ctx.run(ctx.on.action("update-gravity", params={...}),
  state)`. **`ctx.run_action` does not exist** — it was removed and raises
  `AttributeError`. Action names use **dashes**.
- `event.fail(...)` surfaces as `testing.ActionFailed`; assert on
  `exc_info.value.message`.
- Exit code 2 → `Blocked` whose message names a runnable action.

**Acceptance**

- [ ] An integration test pins the `snap-check` exit codes, since the wiki
      documents none.
- [ ] `FTL.log` is free of `CAP_SYS_TIME` / `CAP_SYS_NICE` / `/proc/<pid>/comm`
      warnings and `dmesg` free of AppArmor `DENIED` after a converged deploy.

---

## Stage 4 — (folded into Stage 1)

The password is a security control, not a convenience, so it ships in Stage 1:
generated on install, stored in a charm-owned secret, with `get-admin-password` and
`rotate-admin-password` actions. See
[ADR-0007](adr/0007-admin-password-handling.md).

Operator-supplied passwords are deliberately **not** offered; the rationale and the
trigger to revisit are in ADR-0007 §5 and [BACKLOG.md](BACKLOG.md).

---

## Stage 5 — COS: logs, dashboards, Loki alerts

**Goal:** observability that works on day one.

**Reference:** [ADR-0008](adr/0008-cos-integration.md)

> **Precondition — check before starting.** `opentelemetry-collector` must publish
> an `ubuntu@26.04` revision, because Juju enforces base compatibility between a
> principal and its subordinates. Verify with the scripted query in
> [ADR-0002 §2.2.3](adr/0002-tech-stack-and-repo-architecture.md). If it has not
> landed, the provider side can still be **implemented and unit-tested** — only the
> integration test blocks. Do not use `--force-base` to work around it.

**Deliverables**

- `provides: cos-agent` with `limit: 1`, `optional: true`.
- `charm-libs: [{lib: grafana_agent.cos_agent, version: "0"}]` +
  `charmcraft fetch-libs`. Vendored, never edited, never linted. Transitive
  `PYDEPS` added to `pyproject.toml` by hand.
- `COSAgentProvider(self, ...)` in `__init__`. **No `log_slots`** — the snap has no
  content slot. Forward by path from `$SNAP_COMMON/var/log/pihole/`.
- Default rule directories, **no path arguments**:
  `src/loki_alert_rules/`, `src/grafana_dashboards/`,
  `src/prometheus_alert_rules/` (empty for now).
- Loki alerts: FTL `EADDRINUSE` crash loop, gravity sync failure, AppArmor
  `DENIED` bursts. Descriptions written for a human at 3am.

**Acceptance**

- [ ] `juju integrate pihole:cos-agent otelcol:cos-agent` — **both endpoints
      named**.
- [ ] Log lines from Pi-hole appear in Loki with Juju topology labels.
- [ ] The charm still reaches `ActiveStatus` with the relation **removed**.
- [ ] An assertion that the log paths exist, since they are hardcoded.

---

## Stage 6 — Metrics

**Deliberately unscheduled.** See [ADR-0008 §2.2](adr/0008-cos-integration.md).
Default is to ship no metrics until a requirement exists; then choose between a
community exporter and a charm-owned one in a PR that names the trade-off.

**Never** point `metrics_endpoints` at the FTL API — Prometheus cannot parse JSON
and would silently collect nothing.

---

## Stage 7 — DHCP, conditional on verification

**Goal:** DHCP server mode, only if it demonstrably works.

**Reference:** [ADR-0006 §2.9](adr/0006-configuration-surface.md),
[snap-constraints §4.4, §9](snap-constraints.md)

### 7.a Verify first

1. Does DHCP work end-to-end under strict confinement, on a host with **port 67
   free**? The observed failure was `EADDRINUSE` from LXD's `lxdbr0` dnsmasq — a
   port conflict, not an AppArmor denial — but that was never proven.
2. **Which key naming scheme actually lands in `pihole.toml`?** The wiki documents
   two mutually exclusive sets. Set both in a scratch VM and read the TOML back.

### 7.b Implementation

- `dhcp-enabled`, `dhcp-range-start`, `dhcp-range-end`, `dhcp-router`,
  `dhcp-netmask`, with cross-field pydantic validation → `Blocked`, never a crash
  loop.
- **Mandatory ordering in `compute`'s sequence:** pool → router → `dhcp.active`
  **last**.
- `network-control` + `firewall-control` connected only when enabled.
- 67/udp and 546/udp opened only when enabled.
- README warning: two DHCP servers on one broadcast domain assign conflicting
  addresses.

**Acceptance**

- [ ] Ordering asserted on the **pure `compute` output** — no mocks, which is
      precisely why the ordering lives in data.
- [ ] Integration tests gated behind a pytest marker: on LXD port 67 is normally
      taken and an ungated test will crash-loop the daemon.

---

## Per-stage verification checklist

No stage merges without all of these:

- [ ] `tox -e lint,static,unit` green — **but note this is not evidence of
      compliance** with non-negotiables 1, 2, 4, 5, 6, 7, or 8. Those are audited by
      `charm-reviewer`. A passing gate is not a review.
- [ ] `tox -e flaplint` shows no new high-confidence findings (advisory).
- [ ] Every new reconcile step answers **"what breaks if this runs twice?"** and
      **"what breaks if this never runs?"** with *nothing*.
- [ ] Every workload mutation is followed by a read-back of real state.
- [ ] `charm-reviewer` run and clean.
- [ ] The charm still reaches `ActiveStatus` with **zero relations**.
- [ ] `juju remove-application` still leaves the host with working DNS.
- [ ] Any ADR the stage settles is moved from Proposed to Accepted, with the
      evidence recorded in it.

---

## Open spikes

Blocking work, owned by the ADR that needs it. **Record answers in the ADR, not
here.**

| # | Question | Blocks | ADR |
|---|---|---|---|
| ~~1~~ | ~~Which mechanism sets an unreachable FTL key?~~ | — | **Resolved 2026-08-07** → [ADR-0004](adr/0004-ftl-configuration-mechanism.md) is Accepted |
| ~~2~~ | ~~Is `setpassword` cheap enough to run unconditionally?~~ | — | **Resolved 2026-08-07** → [ADR-0007 §4.2](adr/0007-admin-password-handling.md): use the `/api/auth` oracle |
| 3 | Which DHCP key naming scheme lands in `pihole.toml`? | Stage 7 | [ADR-0006 §2.9](adr/0006-configuration-surface.md) |
| 4 | Does DHCP work end-to-end under strict confinement with port 67 free? | Stage 7 | [ADR-0006 §2.9](adr/0006-configuration-surface.md) |

Only the DHCP spikes remain, and both are Stage 7. **Nothing blocks Stages 0–5.**
Port 67 is free inside an LXD container, so spikes 3 and 4 are cheaper than
originally assumed.

Non-blocking, tracked in [BACKLOG.md](BACKLOG.md): the `core26` support matrix and
the `cos_agent` PyPI migration question.

---

## Practical notes for integration testing

- Environment: `sudo concierge prepare -p machine`. `jubilant` +
  `pytest-jubilant` on LXD.
- **Pack once by hand**, export `CHARM_PATH`, reuse. Never pack inside a test.
- **Use LXD VMs, not containers.** Two independent reasons: snapd cannot mount
  snaps in a 26.04 container at all (ADR-0002 §2.2.2), and the charm rewrites
  `/etc/systemd/resolved.conf.d/` and binds port 53, which conflicts with a
  container's own resolver anyway. `juju deploy ... --constraints
  virt-type=virtual-machine`.
- Gravity bootstrap is asynchronous and downloads a blocklist. Budget 900s and
  assert on `pihole api dns/blocking`, not on unit status alone.
- **Do not test `juju expose`.** The LXD provider implements no firewaller, so
  port 53 is reachable with or without it. Such a test passes for the wrong reason.
- `update-status` defaults to 5m; lower it in the test model fixture. But note the
  design signal: **if the charm only reaches `ActiveStatus` via `update-status`,
  some event that should trigger a reconcile is not observed.** Lowering the
  interval hides that bug rather than fixing it.
- Debugging a hanging reconcile: `juju show-status-log <unit>` (full transition
  history — invaluable for a flapping reconciler), `juju debug-log`,
  `juju debug-hooks`. Installing a snap holds the machine lock, which is a common
  reason hooks look stuck.
