# Backlog

Open research questions, feature ideas, and deferred work. When an item here
matures into a concrete design choice, it graduates to an ADR.

Blocking spikes are **not** here — they live in the ADR that needs them and are
listed in [roadmap.md](roadmap.md#open-spikes).

## Research

- **Does the snap work on Ubuntu 22.04?** The snap is `base: core26`, now verified
  on 24.04 and 26.04. Whether snapd on 22.04 can fetch `core26` is unknown. Only
  relevant if a 22.04 platform is ever requested. (ADR-0002)
- **snapd's bootstrap mount of the `snapd` snap fails in a Juju-created 26.04 LXD
  container.** A kernel squashfs mount is attempted and fails for lack of loop
  devices, with no fallback to `snapfuse` — even though `/usr/bin/snapfuse` is
  present and snapd uses it happily for every *later* mount. A plain
  `lxc launch ubuntu:26.04` is unaffected because it ships the `snapd` snap
  pre-seeded. Forces integration tests and manual deployments onto LXD VMs. **Worth
  reporting to snapd**; check whether it is already tracked first. Open question:
  why the fallback applies to later mounts but not the bootstrap one.
  (ADR-0002 §2.2.2)
- **Is `cos_agent` migrating to PyPI?** The library is Charmhub-hosted and
  Charmhub library hosting is being retired, but no PyPI replacement exists and no
  migration has been announced. Re-check the interface library index periodically
  rather than assuming. If Charmhub freezes updates first, we inherit whatever
  version we last fetched. (ADR-0008)
- **Config drift caused by the admin UI.** An operator editing a value in the web
  UI writes `pihole.toml` directly; the charm overwrites it on the next reconcile.
  Correct convergence, but it will surprise people. **Now cheap to surface** as an
  `ActiveStatus` message, since `GET /api/config` returns the whole tree in one
  request. (ADR-0004 §7)
- **CHAOS TXT API discovery.** The `pihole api` wrapper locates the API with
  `dig +short -p <dns.port> chaos txt local.api.ftl @127.0.0.1` rather than assuming
  a port. More robust than ours; adopt if the charm ever stops owning
  `webserver.port`. (ADR-0004 §5.1)
- **`webserver.acl` as defence in depth.** Restricting the API to known addresses
  would harden a deployment beyond merely requiring a password — but a restrictive
  ACL also blocks the admin UI the operator wants to reach. Needs a design that
  separates "who may read the UI" from "who may write config", possibly driven by a
  Juju space rather than a config option. (ADR-0007 §4.1)
- **Kebab-case aliases as an upstream contribution.** The snap's configure hook
  could expose kebab-case aliases mapping to camelCase FTL keys, eliminating the
  66-key gap. **No longer on our critical path**, since `PATCH /api/config` covers
  every key (ADR-0004), but still a worthwhile upstream improvement. (ADR-0004)
- **Balloon-hash comparison for password idempotency.** Whether comparing intent
  against the stored `pwhash` is worth writing security-sensitive crypto code in a
  charm, versus applying `setpassword` unconditionally. (ADR-0007)
- **Is there a canonical DNS-as-a-service interface?** Check
  `charm-relation-interfaces` before inventing one. Today there is none, so
  exposing Pi-hole as a resolver to other charms has no standard shape. (ADR-0006)

## Features

Ordered by estimated value — user impact against implementation risk.

1. **Blocklist / adlist management** — the most visible Pi-hole feature and the
   most likely source of user disappointment in the first release. Adlists live in
   the `adlist` table of `gravity.db`, not in config; the launcher seeds Steven
   Black's list with `INSERT OR IGNORE`. Managing them needs the v6 HTTP API
   (`/api/lists`) or `pihole-FTL sqlite3` plus `pihole -g`. **Neither is idempotent
   or transactional**, which is why it is deferred rather than scheduled: we will
   not add a permanent config option whose reconcile step is unproven. Needs a
   proven idempotent design first. (ADR-0006)
2. **Metrics via an exporter** — Pi-hole v6 exposes no Prometheus endpoint and the
   snap ships nothing. Three options with named costs in ADR-0008 §2.2. Default is
   to ship none until a requirement exists. (ADR-0008)
3. **`tls-certificates` for the admin UI** — `tls_certificates_interface.v4` is the
   recommended library and FTL reads `webserver.tls.cert`. **Currently blocked by a
   snap defect:** FTL cannot emit a certificate in this snap at all
   (snap-constraints §5.1), which is why the charm disables TLS outright
   (ADR-0006 §2.10). An externally issued certificate may sidestep it — unproven.
   Whoever picks this up must revisit `webserver.port` at the same time. The snap's
   own docs recommend a reverse proxy instead, which may be the better answer.
   (ADR-0001, ADR-0006 §2.10)
4. **Upstream resolver relation** — would let Pi-hole learn a recursive resolver's
   address instead of the operator copying it by hand. **Blocked on two things that
   do not exist** (checked 2026-08-08): there is no `unbound` charm on Charmhub, and
   `charm-relation-interfaces` has no resolver-discovery interface — `dns_record` is
   for *creating* DNS records, a different concern. Trigger: a recursive-resolver
   charm exists **and** an interface is standardised. Decide relation-versus-config
   precedence explicitly at that point. (ADR-0006 §2.4)
5. **Prometheus alert rules** — follows metrics; inert without them. (ADR-0008)
6. **A logs-driven Grafana dashboard** — feasible without metrics, but most
   Pi-hole dashboards people expect are metric-based, so the value is limited.
   (ADR-0008)
7. **Peer relation** — needed only if units must agree on something: a
   leader-elected password, or a coordinated gravity refresh. Note `leader_elected`
   is already in the reconciler's event list, so the wiring cost is small.
8. **TOTP / 2FA** (`webserver.api.totp_secret`) — unreachable via `snap set` and
   **write-only**, so it can never be reconciled. Would have to be an action.
   (ADR-0007)
9. **Password rotation on a schedule** via `secret_rotate` — non-deferrable, so it
   needs its own handler. (ADR-0007)
10. **`app_pwhash`** for non-2FA-aware API clients. No demand yet. (ADR-0007)

## Deferred from ADRs

- **Operator-supplied admin password.** The charm always generates one (ADR-0007 §3);
  there is deliberately no config option. If a real requirement appears — integrating
  an external password manager, or restoring a known credential after a rebuild — the
  vehicle is a `type: secret` config option. Note Juju does not validate the secret
  when the config is set, so a missing `juju grant-secret` surfaces later as
  `SecretNotFoundError`; the `BlockedStatus` must name that command. A pydantic model
  holding an `ops.Secret` field needs `arbitrary_types_allowed`. (ADR-0007 §5)

Items explicitly scoped out. Add when there is demand.

- **High availability.** Two Pi-holes are two independent resolvers with
  independently drifting blocklists, **not a cluster**. Real HA needs
  `keepalived`/anycast and is a different charm. Should be designed as its own
  document, not bolted on. (ADR-0001 §2.3)
- **Bundling or managing Unbound.** The snap project has a standing architectural
  decision against it, treating a recursive resolver as an external companion.
  Aligning with that means a separate charm plus a relation, never a bundled
  binary. (ADR-0001)
- **Migrating an existing non-snap `/etc/pihole`.** The snap's own documentation
  says the v5→v6 path *"has not been fully tested for this snap workflow."* Do not
  automate on top of a migration its authors do not trust. (ADR-0001)
- **`lxd-profile.yaml`.** Charms carrying one are subject to a Charmhub allow-list,
  and `--force` exists partly to bypass that check. Add only if a stage proves a
  kernel module or privileged container is genuinely required. (ADR-0001)
- **A 24.04 platform.** The charm is single-base `ubuntu@26.04` (ADR-0002 §2.1).
  Multi-base is supported but doubles revisions per release and would force the code
  to stay correct on 3.12 and 3.14 permanently. Add only if real users cannot move —
  which, with no installed base, nobody currently is. (ADR-0002)
- **`web-port` config option.** FTL's default `"80o,443os,..."` already fails soft
  (the `o` suffix means optional), so there is nothing to fix yet. (ADR-0006)
- **Timeout tuning options** (`install-timeout`, `readiness-timeout`). A permanent
  public API for a transient problem. Constants in `pihole.py` instead. (ADR-0006)

## Upstream issues to file

Drafted in the repository root; delete once filed.

- **`snap-issue-webserver-tls.md`** — a stock install has no web UI and no HTTP API,
  because TLS certificate generation fails and takes the whole webserver down with
  it. `snap-check` reports exit 0 regardless. (snap-constraints §5.1)
- **`snap-issue-unauthenticated-api.md`** — following the documented Quickstart
  leaves an unauthenticated, network-reachable config API that permits a full DNS
  hijack. (snap-constraints §5.2, ADR-0007 §1.3)

## Housekeeping

- **`charmcraft analyse` reports `language: unknown` and `framework: unknown`** for
  this charm, and will for any `plugin: uv` charm. Two charmcraft 4.3.1 defects, both
  verified by reading `charmcraft/dispatch.py` and `charmcraft/linters.py`: the
  `entrypoint` linter does not expand `${dispatch_path}` from the generated dispatch,
  and the `framework` linter looks for a flat `venv/ops` directory that the `uv`
  plugin never creates. `charmcraft pack` is unaffected. **File upstream.** Do not
  vendor a hand-written `dispatch` as a workaround without reading the `uv` plugin
  first — it removes `venv/bin/python*`, so the template's symlink and
  `LD_LIBRARY_PATH` setup are load-bearing. Details in the `machine-charm-scaffold`
  skill. (Stage 0)
- **`assumes: juju >= 3.6` should probably be `>= 3.6.17`**, which is the true floor
  for the 26.04 base (ADR-0002 §2.2.1). **NOT VERIFIED** whether `assumes` accepts a
  patch-level version. (Stage 0)
- **No `LICENSE` file.** The README deliberately claims no license because the tree
  carries none. Decide and add one. (Stage 0)

- **`charm-reviewer` audit at every stage boundary.** A green
  `tox -e lint,static,unit` is **not** evidence of compliance with non-negotiables
  1, 2, 4, 6, 7, or 8. Do not treat a passing gate as a review.
- **CI on 3.14 only** from Stage 0 — the sole interpreter in the 26.04 archive.
  Testing 3.12 would exercise a configuration that never exists in production and
  would silently forbid 3.13+ syntax. (ADR-0002 §2.2.4)
- **Watch `opentelemetry-collector` for a 26.04 revision.** It is a Stage 5
  precondition, not a base blocker.
  [PR #369](https://github.com/canonical/opentelemetry-collector-operator/pull/369)
  is open and unmerged. Scripted check in ADR-0002 §2.2.3. (ADR-0008)
- **`docs/implementation/`** — one document per module as it lands, following the
  house format: header metadata (module, ADR link), Purpose, Design, Edge Cases
  table, Testing Strategy. Documents code that exists, not code we intend to write.
- **README support-matrix section** — must state plainly: the snap is unofficial
  and unproven, revision pinning trades reproducibility against security updates,
  `juju expose` does nothing on LXD but matters on MAAS/EC2, NTP is off by default
  which **diverges from the workload's own default**, and (if ADR-0004 approach A
  is chosen) `snap get` is not a reliable source of truth for a third of the FTL
  keys.
- **Integration assertion that the log paths exist.** They are hardcoded against
  `$SNAP_COMMON`; a snap layout change would break log collection silently.
  (ADR-0008)
- **Re-check the `dns.dnssec` workaround whenever the snap bumps FTL.** It is a
  hard-coded exception for a specific FTL version. (ADR-0004)
- **Security review of the removal path.** The charm's `remove` handler is the only
  thing that restores host DNS. Worth deliberately testing the ugly cases: hook
  failure mid-removal, `--force` removal, and machine reboot with the drop-in in
  place. (ADR-0001 §1.3, ADR-0005 §1.1)
