# Implementation roadmap

**Status:** Accepted — each stage's acceptance block records its own state
**Last updated:** 2026-09-07
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
| [`overview.md`](overview.md) | A two-minute map: the pattern, and what each file in `src/` is for. |
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
  `resolved.restore()`; `set_ports` for 53/tcp+udp and 80/tcp. (443 was added
  later, once the snap began self-signing its certificate — ADR-0006 §2.8.)
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
- Pure: an NTP server that does not match intent yields exactly
  `SetNtpServer(active=…)` plus its own readiness gate, because the configure hook
  restarts FTL on a changed value.
- Regression: `set_ports` opens exactly what the charm serves. (Stage 1 asserted
  443 was *absent*; that inverted when the snap started serving it — ADR-0006 §2.8.)
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
  for `dns.upstreams` and friends. The **mapping** is serialised from a tuple
  sorted by key, so an unchanged config never produces a spurious diff; the
  **values** of an array keep the operator's order, because resolver order is a
  preference and not noise.
- An HTTP client using stdlib `urllib.request`: `POST /api/auth` with the **admin
  password** — a `cli_pw` session is answered 403 for config (snap-constraints
  §7.2.8) — then a single `PATCH /api/config` carrying the whole desired mapping,
  then `DELETE /api/auth`. It lives in `ftl_api.py` (ADR-0009).
- **Re-read `cli_pw` on every use.** It rotates on every FTL restart (verified).
- `apply_ftl_config()` with mandatory read-back against `pihole.toml` via stdlib
  `tomllib`, raising `PiholeError(key, expected, actual)` — because **an unknown key
  returns `200` and is silently ignored**.
- Map FTL's `400` `hint` into the `BlockedStatus` message verbatim; it is already
  written for a human.
- `dns.dnssec` needs no special case any more: the API applies it correctly.
- `$SNAP_DATA` resolved through `current`; never a hardcoded revision.
- ~~`extra-bindings: dns`~~ — **deferred 2026-09-05.** It was declared and never
  consumed, which is public surface that promises something the charm does not
  do. It lands with the work that needs it: `dns.interface`, the FTL key that
  makes `dns-listening-mode`'s `SINGLE` and `BIND` mean anything. See
  [BACKLOG.md](BACKLOG.md).
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
- `compute` emits `SetFtlConfig` **only** when a value actually changed. (There is
  no `RestartFtl`: the PATCH applies live — ADR-0004 §6 and §8.)
- Collections sorted **at construction**, never in the assertion.

**Acceptance**

- [x] `dns.listeningMode=ALL` is observable in `pihole.toml` and FTL was **not**
      restarted (PID unchanged). — `test_a_camelcase_key_lands_without_restarting_ftl`,
      green on LXD 2026-09-05. The PID comes from systemd's `MainPID`, so it
      changes if and only if the service restarted.
- [x] `juju config pihole upstream-dns=...` is observable in `pihole.toml` **and**
      in a `dig` result. — `test_upstream_dns_reaches_both_the_toml_and_resolution`.
      The TOML is read with `tomllib` on the unit, not grepped: FTL writes arrays
      across lines, and an anchored `grep` returns `upstreams = [` and nothing
      else.
- [x] Setting the same config twice does not restart FTL (check the PID). —
      `test_a_converged_machine_applies_nothing_and_restarts_nothing`. The PID
      alone cannot earn this box: a PATCH never restarts FTL, so the PID is stable
      whether the charm re-applied or not. The test counts the charm's own
      `Applied FTL config` log lines across extra reconciles driven by a 10s
      `update-status-hook-interval`, which observes `(Noop(),)` directly. It also
      asserts the `applying Noop().` count *rose*, so a hook interval that never
      took effect fails the test instead of passing it.
- [x] Beyond the stage's own list, ADR-0010 gained its first end-to-end evidence:
      `test_the_installed_revision_is_the_one_pinned_for_this_architecture` reads
      `dpkg --print-architecture` and compares the installed revision to
      `SNAP_REVISIONS[arch]` **by equality** — accepting either number would pass
      an amd64 unit running the arm64 build — and
      `test_the_snap_is_held_against_auto_refresh` proves the hold.
- [x] `charm-reviewer` clean (2026-09-07, fourth pass over the tree that closes
      this stage). Two earlier passes found the acceptance evidence itself to be
      the weak part — a revision test that accepted any pinned number and a
      converged-machine test that could not fail — which is why three of the boxes
      above describe what their test *cannot* be fooled by. Four items are carried
      as `docs/BACKLOG.md` **Accepted debt**, each with a trigger; the API-origin
      port and Stage 1's historical webserver-port note are deferred on the record.

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
  unconditionally today; `network-control`/`firewall-control` join with
  DHCP (Stage 7). **Read `snap connections` back** — the snap's own docs
  warn that store auto-connection and `--dangerous` installs produce
  different states.
- `snap_check()` mapping exit codes into status: `0` OK, `1` config error
  (plug disconnected or unauthenticated web API — the plug trigger is
  unreachable for this charm), `2` runtime/port error → `Blocked` **naming
  the remedy**. Called from `collect_unit_status`, which must not mutate.
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

- [x] An integration test pins the `snap-check` exit codes. —
      `test_snap_check_exit_0_on_a_converged_unit` and
      `test_snap_check_exit_2_on_a_port_conflict`. The nuance the verification
      surfaced: exit 1's **plug trigger** is effectively unreachable
      (`network-bind` auto-connects on install), but its **password trigger**
      — a network-reachable web API with no password — is a real drift state
      the charm models (`PasswordUnset`) and reports with its own Blocked
      message; snap-check's version is a second line of defence, not the
      primary. The suite pins 0 and 2 and documents this instead of faking 1.
      Exit 2 is pinned in its real shape: snap-check **skips port checks
      while FTL is active** ("Port conflict checks skipped"), so the test
      stops the daemon and holds the port — the crash-loop scenario the code
      exists for.
- [x] `FTL.log` free of capability warnings, `dmesg` free of the denials
      plugs cause. — `test_ftl_log_is_free_of_capability_warnings` and
      `test_no_capability_denials_for_pihole_snap`. The box's original "dmesg
      free of AppArmor DENIED" premise was too broad: strict confinement emits
      noise no plug removes — curl probing `/etc/ldap/ldap.conf` during gravity
      downloads, and snap-check itself denied `dmesg` by its own sandbox — both
      observed on a converged, fully-plugged unit. The assertion is scoped to
      what connecting the plugs actually clears: `operation="capable"`
      denials and `/proc/*/comm` opens.
- [x] The gravity timer drop-in is written, loaded, and removed cleanly.
- [x] `charm-reviewer` clean (2026-09-21, fifth pass over the slice that
      closes this stage). Five passes, each finding what the previous one's
      fixes left: a tautological read-back on the remedy path, a status
      handler that ran the diagnostic before knowing the workload existed, a
      "restart" that was a no-op, an invented `systemctl show` format, a
      vacuous plug assertion, a diagnostic banner that suppressed the
      charm's own message, facts that broke the never-raise contract — all
      fixed in this commit, and the fifth pass pre-cleared the closure once
      its one blocker (a docstring asserting the negation of the
      facts-totality contract) landed. Deferred with triggers: the
      version-report memo and `RefreshSnap` (the operator is thinking);
      Loki rules + end-to-end and Pi-hole-specific metrics (ADR-0008). —
      `test_gravity_schedule_drop_in_lands_and_reads_back` and
      `test_gravity_schedule_unset_removes_drop_in`. The load signal is
      `systemctl show -p DropInPaths --value` containing the drop-in path —
      not `TimersCalendar`, whose real output is a brace-delineated,
      normalised, time-varying format that can never match the operator's raw
      expression. The tests wait on `settled` (workload active AND agent idle),
      because `all_active` alone can pass before config-changed finishes.

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

> **Precondition — cleared, with a caveat.** `opentelemetry-collector` publishes
> `ubuntu@26.04` in track `0.130`, but the channel's recommended pointer for amd64
> serves the 22.04 build, so a bare `juju deploy` fails base compatibility. Deploy
> with `--channel=0.130/stable --revision=<per-arch>` — the full record is in the
> [ADR-0002](adr/0002-tech-stack-and-repo-architecture.md)'s header `Amended:` lines.
> Never `--force-base`.

**Deliverables**

- `provides: cos-agent` with `limit: 1`, `optional: true`.
- `charm-libs: [{lib: grafana_agent.cos_agent, version: "0"}]` +
  `charmcraft fetch-libs`. Vendored, never edited, never linted. Transitive
  `PYDEPS` added to `pyproject.toml` by hand.
- `COSAgentProvider(self, ...)` in `__init__`, with
  `log_slots=["pihole-by-rajannpatel:logs"]` — the snap's read-only `logs`
  content slot (upstream PR #18, in the pinned revisions). `upgrade_charm` is a
  refresh event: an upgrade that changes what we publish must republish.
- Default rule directories, **no path arguments**:
  `src/loki_alert_rules/`, `src/grafana_dashboards/`,
  `src/prometheus_alert_rules/` (empty for now).
- Loki alerts: **deferred** — the selector rules and the observed label shapes
  that decide them live in [ADR-0008 §2.1](adr/0008-cos-integration.md); this
  line deliberately does not restate them. When they land: FTL crash-loop and
  gravity-failure signals authored against real lines, descriptions written for
  a human at 3am.

**Acceptance**

- [x] `juju integrate pihole:cos-agent otelcol:cos-agent` — **both endpoints
      named**. — `test_cos_agent_integrates_and_publishes`. The subordinate needs a
      revision pin (`--channel=0.130/stable --revision=491` amd64, 486 arm64): the
      channel's amd64 pointer serves the 22.04 build, so a bare deploy fails base
      compatibility (ADR-0008 §3). The assertion is the `config` databag read from
      **otelcol/0's** view — `juju show-unit pihole/0` does not display the unit's
      own databag, a fact that cost a live model to learn.
- [x] The machine-side log chain, verified end to end. — Reworded from "log
      lines appear in Loki": no machine Loki charm exists (COS's Loki runs on
      Kubernetes), so that hop is unprovable in this harness and lives in BACKLOG
      with its trigger. What this suite proves: the databag publishes
      `log_slots` (`test_cos_agent_databag_publication`, unit), and on LXD
      `test_log_paths_exist` asserts the files on disk **and** the slot
      connected (`snap connections`:
      `opentelemetry-collector:logs ↔ pihole-by-rajannpatel:logs`). Verified
      live on the operator's model, beyond the suite (2026-09-18): the
      subordinate's filelog receiver carries our topology labels, and the
      files are readable inside its snap namespace.
- [x] `charm-reviewer` clean (2026-09-19, fourth pass over the slice that
      closes this stage). Four passes, each finding what the previous one's
      fixes left: a vacuous `snap services` assertion and doc inversions; a
      stale ADR-0008 §1.2 and a teardown race in the slot assertion; a false
      "open box" claim; a duplicated clause — all fixed in this commit, and
      the fourth pass cleared the closure. Deferred with triggers: the Loki
      rules and end-to-end observation (BACKLOG), Pi-hole-specific metrics
      (ADR-0008 §2.2).
- [x] The charm still reaches `ActiveStatus` with the relation **removed**. —
      `test_charm_stays_active_with_relation_removed`; the subordinate's own
      `blocked` without COS backends is expected and is not the charm's status.
- [x] An assertion that the log paths exist. — `test_log_paths_exist`: the log
      files under `$SNAP_COMMON/var/log/pihole/` on a live unit (the observed
      set is recorded in ADR-0008 §1.2). The "hardcoded" premise is gone — the
      rules were deferred (ADR-0008 §2.1), so the paths live in the ADR and in
      the upstream slot request, not in the charm.

---

## Stage 6 — Metrics

**Pi-hole-specific metrics: deferred by decision (2026-09-07).** The research
and the decision live in [ADR-0008 §2.2](adr/0008-cos-integration.md); this
section deliberately does not restate them. Host-level metrics arrive anyway:
the subordinate installs and scrapes `node-exporter` itself, so relating
`cos-agent` already yields them. When Pi-hole-specific metrics
are wanted, a charm-owned exporter is the leading candidate.

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
