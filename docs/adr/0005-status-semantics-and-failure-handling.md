# ADR-0005: Status Semantics and Failure Handling

**Status:** Accepted
**Date:** 2026-08-07
**Accepted:** 2026-08-08
**Amended:** 2026-08-11 — added §2.9: raising is only safe before the resolved drop-in exists, which reorders install ahead of freeing port 53.
**Amended:** 2026-08-08 — §2.7 retried `snap.SnapError`, which does not cover its sibling exceptions. Corrected to `snap.Error`.
**Related:** [ADR-0001: Charm Scope and Specification](0001-charm-scope-and-specification.md), [ADR-0003: Reconciler and Functional Core](0003-reconciler-and-functional-core.md), [ADR-0004: FTL Configuration Mechanism](0004-ftl-configuration-mechanism.md)

---

## 1. Context

Juju statuses look like a severity scale and are not one. Each settable status
answers a *different question*, and getting the mapping wrong produces a charm
that either hides real problems or sends operators to investigate nothing.

This charm additionally has a hazard that makes the `raise`-versus-`Blocked`
choice a correctness question rather than a UX preference.

### 1.1 The hazard: our `remove` handler owns host DNS

Verified from `snap/hooks/remove`: the snap **cannot** delete the
`systemd-resolved` drop-in or restart the service. Confinement blocks it. The
charm's `remove` handler is the only thing that restores the host's resolver.

Now combine that with how Juju treats error state:

- **`juju remove` does not work** on a unit in error without `--force`.
- **`--force` skips cleanup.**

So a unit stuck in error that gets force-removed leaves the host with
`DNSStubListener=no`, no Pi-hole, and **no DNS at all**. That single failure mode
decides the whole strategy below.

### 1.2 Error state costs more than it looks

From the reference discussion (*"It's probably ok for a unit to go into error
state"*), the costs are concrete:

1. Removal requires `--force`, which skips cleanup (§1.1).
2. **Model migration is blocked, full stop**, and so are some Juju upgrades.
3. Error **detaches the charm from managing the workload** — it stops reacting to
   config and relation changes.
4. **The retry you were counting on is not guaranteed.** `automatically-retry-hooks`
   is *model* config; it defaults true but is *"not set that way everywhere"*, and
   OpenStack CI runs with it disabled and treats reaching Error as a rejection.
5. **Error preserves the original hook context.** Correcting a config option does
   not help: Juju retries the hook *with the old value*. The operator must run
   `juju resolve --no-retry` first. So raising on bad config produces a unit that
   retries forever against a value the operator already fixed.

The consensus is not "never raise" — a genuine bug in our own code *is* an error
and the traceback is the point. It is: **things needing normal human intervention
(bad config, missing relation) must not be errors. That is what Blocked is for.**

### 1.3 The push/pull status problem, and the race it creates in this charm

- **Pull statuses** can be queried at any time. `collect_unit_status` exists for
  these.
- **Push statuses** are only knowable by *attempting* a mutating operation.

This charm has a concrete instance of the resulting race. Suppose `_reconcile`
calls `set_ftl_key`, the read-back fails, and it raises `PiholeError`. Meanwhile
`_on_collect_status` independently runs `pihole snap-check` and
`pihole api dns/blocking` — and **both succeed**, because the daemon is perfectly
healthy and only one config key was silently dropped.

**The unit reports `ActiveStatus` while the config it was asked for was never
applied.** That is exactly the defect non-negotiable #6 exists to prevent, and
`collect_unit_status` cannot re-derive it.

---

## 2. Decisions

### 2.1 The status vocabulary, as questions

| Status | The question it answers | Settable |
|---|---|---|
| `ActiveStatus` | is the workload doing its job right now? *"If the unit is operational but some feature is in a degraded state, set active with an appropriate message."* | yes |
| `MaintenanceStatus` | is **this unit** busy, and will it clear on its own? | yes |
| `WaitingStatus` | am I blocked on **another application**? | yes |
| `BlockedStatus` | can a human do something, **and can we name it?** | yes |
| `ErrorStatus` | — read-only; `add_status` raises `InvalidStatusError` | no |
| `UnknownStatus` | — the state before the first `status-set` | no |

**"Waiting for gravity bootstrap" is `MaintenanceStatus`, not `WaitingStatus`.**
Gravity is this unit's own workload. `WaitingStatus` is reserved for waiting on a
*related* application — and this charm reaches Active with zero relations, so it
has almost no legitimate use for it.

### 2.2 `BlockedStatus` must name the remedy

The naive reading is "manual intervention needed → blocked". The sharper criterion
is that Blocked exists for a situation where **the charm can tell the human what
needs to be done.** If we cannot name the action, Blocked is the wrong status —
that is either our bug (raise) or transient (Maintenance).

```python
# Bad: states the problem, not the remedy.
ops.BlockedStatus("port 53 in use")

# Good.
ops.BlockedStatus(
    "port 53 held by systemd-resolved; run the free-port-53 action or "
    "stop the conflicting resolver"
)
```

### 2.3 The decision table

| Situation | Status / action | Why |
|---|---|---|
| snap installing, config applying | `Maintenance` | this unit is busy; clears itself |
| gravity bootstrap downloading | **`Maintenance`** | our own workload, clears itself. Blocked would send someone to investigate nothing |
| invalid config | `Blocked` | error would retry the **stale** config forever (§1.2.5) |
| port 53 held and we cannot free it | `Blocked` naming the action | operator can act — **and we must stay removable** (§1.1) |
| a required plug cannot be connected | `Blocked` | needs privileges the charm lacks |
| `snap set` read-back mismatch | `Blocked` via §2.4 | a retry reproduces the same silent drop |
| FTL crash-looping on `EADDRINUSE` | `Blocked` | the launcher no longer pre-checks the port, so this needs intervention |
| DNS answering, last gravity sync failed | **`Active` with a message** | the workload *is* offering its service, merely degraded |
| DNS answering but the HTTP API is down after we set `webserver.port` | `Blocked` | the admin UI and the config path are both gone; a human must look |
| daemon serving with `pwhash = ""` | `Blocked` | the config API is open to the network ([ADR-0007 §1.3](0007-admin-password-handling.md)). Should be unreachable by construction, but assert it |
| snap store transient failure | **retry ~3× in-hook, then raise** | genuinely transient — but see §2.9, which constrains *when* raising is safe |
| a bug in our own code | **let it raise** | it *is* an error; the traceback is the point |

**The bias is toward Blocked, and the reason is specific rather than stylistic:
keeping the unit removable is a correctness requirement.**

### 2.4 The push-status channel — an instance attribute, not `StoredState`

`ops/_main.py` runs `_emit_charm_event()` and then `_evaluate_status()` in the same
method, same process, same charm instance:

```python
self._emit_charm_event(self.dispatcher.event_name)
if not self.dispatcher.is_restricted_context():
    _charm._evaluate_status(self.charm)
```

So a plain instance attribute carries a push status from the reconciler to the
status handler:

```python
def __init__(self, framework: ops.Framework):
    super().__init__(framework)
    self._reconcile_failure: ops.StatusBase | None = None

def _reconcile(self, _: ops.EventBase) -> None:
    try:
        ...
    except PiholeError as e:
        # A push status: collect_unit_status cannot re-derive this, because the
        # daemon is healthy and only one key silently failed to apply.
        self._reconcile_failure = ops.BlockedStatus(str(e))

def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
    if self._reconcile_failure is not None:
        event.add_status(self._reconcile_failure)
    # ... then the pull statuses
```

This is state, but it lives for one hook execution and is gone. It does **not**
violate the no-`StoredState` rule in
[ADR-0003](0003-reconciler-and-functional-core.md) §2.8, which is about caching
*across* hooks.

**Build this in the first stage.** Retrofitting it means auditing every status
path again.

### 2.5 Prefer converting push to pull where it is cheap

Many apparent push statuses can be rewritten as pull statuses. For this charm,
*"does `pihole.toml` match intent?"* **is** pullable — read the TOML in the status
handler and diff it against config. Do that, and fall back to §2.4 only where
attempting the operation is the only way to know.

### 2.6 Readiness is a pull status, and not `snap services`

`ActiveStatus` requires the FTL HTTP API to answer (`GET /api/dns/blocking`). The
daemon reports `active` long before blocking works, because the launcher forks a
background child that waits up to 90s for FTL to answer DNS before downloading the
blocklist ([snap-constraints §10](../snap-constraints.md)).

**Ordering correction.** An earlier draft of this ADR gated readiness on the API
without noticing that **on a stock install the API never comes up at all**: the
packaged `webserver.port` requests TLS, certificate generation fails inside the
snap, and the SSL error aborts the whole webserver including plain HTTP
([snap-constraints §5.1](../snap-constraints.md)). A charm implemented as first
written would have sat in `MaintenanceStatus` forever, waiting for an endpoint that
could never appear.

So the API gate is only valid *after* the charm has corrected `webserver.port`.
That correction is an **install step, not a config step**, and it must precede the
first daemon start — see [ADR-0004 §2.4](0004-ftl-configuration-mechanism.md) and
the Stage 1 ordering in the [roadmap](../roadmap.md).

If the API is unreachable *after* the charm has set the port and the daemon is
active, that is not "still starting" — it is `BlockedStatus`, because something
the charm cannot fix has gone wrong.

`snap-check` provides the rest, with semantic exit codes: `0` OK, `1` config
error, `2` runtime/port error. Both are cheap and side-effect-free, which is what
`collect_unit_status` requires — **that handler must never mutate anything.**

### 2.7 Retry, but bounded and in-hook

The middle ground between raising and swallowing:

```python
@tenacity.retry(
    retry=tenacity.retry_if_exception_type(snap.Error),   # NOT snap.SnapError
    wait=tenacity.wait_fixed(2) + tenacity.wait_random(0, 5),
    stop=tenacity.stop_after_attempt(3),
    reraise=True,
)
def install(self, revision: int | None) -> None: ...
```

**Retry `snap.Error`, never `snap.SnapError`.** Verified against
`charmlibs-snap 1.0.1`: `SnapError`, `SnapAPIError` and `SnapNotFoundError` all
inherit **directly from `Error`** and none is a subclass of another. So retrying
`SnapError` alone leaves store and lookup failures un-retried — which are exactly
the flaky ones. Those then reach error state, and a unit in error needs `--force` to
remove, which skips the cleanup that restores host DNS (§1.1). `Error` is also the
only one of the four that still exists on `charmlibs` main; the `Snap*` names are
legacy aliases already removed there.

Applied to the snap store, which is genuinely flaky. Not applied to anything where
a retry reproduces the same result — a silently-dropped `snap set` key is not
transient.

### 2.9 Raising is only safe while the host resolver is intact

§2.3 permits a genuine failure to raise, and §1.1 says error state is what destroys
host DNS. Those two collide the moment the charm has written the
`systemd-resolved` drop-in: from then on, any uncaught exception produces a unit
that needs `--force` to remove, and `--force` skips the handler that puts the
resolver back.

**Two rules follow, and both are load-bearing.**

**First, the install ordering is `install → free 53`, not the reverse.** The
workload's actual constraint is that port 53 must be free *before the daemon
starts* ([snap-constraints §2.1, §11](../snap-constraints.md)), and the snap ships
`install-mode: disable`, so installing first satisfies it. Ordering it this way
buys two things:

- If the snap store fails after its retries, the drop-in was **never written**, so
  reaching error state leaves host DNS untouched. Raising becomes safe again,
  exactly as §2.3 assumes.
- The store fetch happens while the host still has a working resolver. After the
  drop-in is in place, `DNS=127.0.0.1` points at a port nothing is listening on
  yet, so name resolution depends on the link's DHCP servers via `nss-resolve` —
  a needlessly fragile moment to be downloading a snap.

**Second, every failure *after* the drop-in exists must be a caught, named error.**
Once the resolver has been displaced, an uncaught `snap.Error` or `OSError` is a
DNS-loss path, not a stack trace. The workload modules must convert those into
`PiholeError`/`ResolvedError` carrying a remedy, so `_reconcile` can turn them into
`BlockedStatus` and the unit stays removable. **Widening the charm's `except` to
include `snap.Error` is not the fix** — that would import `charmlibs` into
`charm.py` and break non-negotiable #2. The conversion belongs in the module that
owns the effect, which is also where the context for the remedy lives
([ADR-0003](0003-reconciler-and-functional-core.md) §2.6).

### 2.8 Precedence, and why one bad Blocked is expensive

`blocked > maintenance > waiting > active`, ties to the first added. **One spurious
Blocked masks every other status the handler adds** — another reason to reserve it
for genuine operator problems. Each code path in the handler should call
`add_status` at least once.

`collect_app_status` also exists and the framework only emits it on the leader, so
it needs no `is_leader()` guard. It adds little for a single-unit charm; it matters
if peers are ever added.

---

## 3. Consequences

### Positive

- The removal hazard is addressed by design rather than by hoping hooks never
  fail: biasing to Blocked keeps the unit removable, so the `remove` handler
  actually runs and DNS comes back.
- The push-status channel closes a real hole that would otherwise report
  `ActiveStatus` over unapplied config — the highest-severity silent failure this
  charm could have.
- Requiring Blocked messages to name a remedy makes them actionable at 3am instead
  of merely accurate.
- Bounded in-hook retry handles snap-store flakiness without the migration-blocking
  and cleanup-skipping costs of error state.

### Negative

- Almost never raising means genuine bugs can hide behind a Blocked status if a
  developer catches too broadly. Mitigation: `except Exception` is only acceptable
  where broad failure is what actually occurs, and ruff's `BLE001` is deliberately
  *not* enabled because it cannot tell the difference — so this stays a review
  judgement.
- The instance attribute in §2.4 is a mutable field on the charm, which is
  stylistically at odds with the frozen-data approach of ADR-0003. It is justified
  narrowly and must not grow into a general-purpose scratchpad.
- Reading `pihole.toml` in `collect_unit_status` (§2.5) makes the status handler do
  file IO on every hook. Cheap, but it must stay strictly read-only or it breaks
  the handler's contract.
- The status table is long enough that it will drift from the code unless the
  reviewer checks it. It is a design document, not an executable spec.
