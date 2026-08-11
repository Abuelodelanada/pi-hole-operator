---
name: machine-charm-workload
description: >-
  Use when writing or reviewing src/charm.py, src/pihole.py, or any code that
  installs snaps, manages systemd units, or writes files on the machine. Covers
  charmlibs-snap, charmlibs-systemd, charmlibs-apt APIs, the reconciler skeleton,
  status collection, and the two-layer design that makes the charm testable.
  Load before importing charmlibs or writing an event handler.
metadata:
  verified: "2026-08-06"
---

# Machine charm workload management

## `charms.operator_libs_linux` is deprecated

Every module is officially deprecated in favour of PyPI packages
([general libraries reference](https://documentation.ubuntu.com/charmlibs/reference/general-libs/)):

| Old (deprecated) | New |
|---|---|
| `charms.operator_libs_linux.v0.apt` | `charmlibs-apt` → `from charmlibs import apt` |
| `charms.operator_libs_linux.v2.snap` | `charmlibs-snap` → `from charmlibs import snap` |
| `charms.operator_libs_linux.v1.systemd` | `charmlibs-systemd` → `from charmlibs import systemd` |
| `charms.operator_libs_linux.v0.passwd` | `charmlibs-passwd` → `from charmlibs import passwd` |
| `charms.operator_libs_linux.v0.sysctl` | `charmlibs-sysctl` → `from charmlibs import sysctl` |
| `charms.operator_libs_linux.v0.dnf` | none — modern charms run on Ubuntu |
| `charms.operator_libs_linux.v0.grub` | none — unmaintained |

There is **no umbrella `charmlibs` package** to install. PyPI's `charmlibs` is a
namespace placeholder whose own summary says *"This package should not be
installed."* Declare each one explicitly in `pyproject.toml`.

They install as a namespace package: `charmlibs/snap/__init__.py`,
`charmlibs/systemd/__init__.py`. Source is available as the `charmlibs`
reference — read it rather than guessing at signatures.

## The two-layer design

This is the single most important structural decision, and it comes straight
from the official guidance
([run workloads with a machine charm](https://canonical.com/juju/docs/ops/latest/howto/run-workloads-with-a-charm-machines/)):

> Keep charming concerns (event handlers, status, config parsing) in
> `src/charm.py`, and put workload-specific logic in a separate module such as
> `src/myworkload.py`. Lets you unit test the charm by mocking the module (no
> `subprocess` patching in state-transition tests).

**`src/pihole.py` rules:**

- Never imports `ops`.
- Takes plain arguments, returns plain values or raises module-specific
  exceptions.
- Owns every call to `snap`, `systemd`, `subprocess`, and every filesystem write.
- Knows nothing about relations, config options, or Juju statuses.

**`src/charm.py` rules:**

- Never imports `charmlibs.*`, `subprocess`, or writes files.
- Translates charm config into arguments for `pihole.py`.
- Translates `pihole.py` return values into Juju statuses.

If you find yourself wanting to patch `subprocess` in a test of `charm.py`, the
split is wrong.

## `charmlibs.snap`

```python
from charmlibs import snap

cache = snap.SnapCache()
pihole = cache["pihole-by-rajannpatel"]
pihole.ensure(snap.SnapState.Latest, channel="stable")
pihole.start(enable=True)
```

`SnapCache()` is the mock point in tests of the workload module.

For this snap specifically, `ensure()` is not enough — see the `pihole-snap`
skill. The charm additionally needs `snap connect` for seven plugs, `snap set`
for reachable config keys, a `pihole-FTL --config` fallback for the 66
unreachable ones, and an explicit `start(enable=True)` because the snap ships
`install-mode: disable`.

Where `charmlibs.snap` has no API for something (`snap connect`, `snap set` with
dotted keys, `snap run --shell`), shell out from `pihole.py` with
`subprocess.run(..., check=True, capture_output=True, text=True)` — and then
**verify by reading real state**, because this snap returns 0 on operations it
silently drops.

## `charmlibs.systemd`

```python
from charmlibs import systemd

systemd.daemon_reload()
systemd.service_restart("systemd-resolved")
systemd.service_running("snap.pihole-by-rajannpatel.pihole-ftl.service")
```

Needed for the `systemd-resolved` orchestration and for the `gravity-sync.timer`
drop-in, neither of which the snap can do under strict confinement.

## Reconciler skeleton

```python
import logging

import ops

from pihole import Pihole, PiholeError
from pihole_config import PiholeConfig

logger = logging.getLogger(__name__)

PORTS = (
    ops.Port("tcp", 53),   # DNS
    ops.Port("udp", 53),   # DNS — the one everyone forgets
    ops.Port("tcp", 80),   # admin UI + API
    ops.Port("tcp", 443),  # admin UI over TLS
    ops.Port("udp", 123),  # NTP server, on by default
)


class PiholeCharm(ops.CharmBase):
    """Charm the Pi-hole snap on a machine."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.pihole = Pihole()

        # Status collection is registered first and only reports.
        framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # Events with genuinely distinct semantics. These cannot be deferred,
        # which per the official guidance is exactly the test for "needs its
        # own handler".
        framework.observe(self.on.remove, self._on_remove)

        # Actions.
        framework.observe(self.on.update_gravity_action, self._on_update_gravity)
        framework.observe(self.on.snap_check_action, self._on_snap_check)

        # Everything else converges through one reconciler.
        for event in (
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,
            self.on.secret_changed,
            self.on.cos_agent_relation_joined,
            self.on.cos_agent_relation_changed,
            self.on.cos_agent_relation_broken,
        ):
            framework.observe(event, self._reconcile)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the machine toward the desired state.

        Every step must be safe to run twice and safe to never run.
        """
        config = self.load_config(PiholeConfig, errors="blocked")
        self.unit.set_ports(*PORTS)
        self.pihole.free_port_53()
        self.pihole.install(revision=config.snap_revision)
        self.pihole.connect_plugs(dhcp=config.dhcp_enabled)
        self.pihole.apply_config(config, bind_address=self._bind_address)
        self.pihole.ensure_running()
        if version := self.pihole.workload_version():
            self.unit.set_workload_version(version)

    @property
    def _bind_address(self) -> str | None:
        """The address FTL should bind to and advertise to clients."""
        binding = self.model.get_binding("dns")
        return str(binding.network.bind_address) if binding else None

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        """Report status. Must not mutate anything."""
        if not self.pihole.installed:
            event.add_status(ops.MaintenanceStatus("installing pihole snap"))
            return
        diagnosis = self.pihole.diagnose()  # wraps `pihole snap-check`
        if diagnosis.port_conflict:
            event.add_status(ops.BlockedStatus(diagnosis.message))
            return
        if not self.pihole.blocking_ready():  # `pihole api dns/blocking`
            event.add_status(ops.MaintenanceStatus("waiting for gravity bootstrap"))
            return
        event.add_status(ops.ActiveStatus())

    def _on_remove(self, _: ops.RemoveEvent) -> None:
        """Undo host changes the snap cannot undo itself."""
        self.pihole.restore_port_53()


if __name__ == "__main__":  # pragma: nocover
    ops.main(PiholeCharm)
```

`ops.main(PiholeCharm)` is the current entrypoint. `from ops.main import main` is
deprecated since 2.16.0. `def __init__(self, *args)` still works but loses typing
and no current example uses it.

## Reconciler rules

1. **One `_reconcile`.** The only legitimate separate handlers are
   `collect_unit_status`, actions, `remove`, and `upgrade_charm` when it needs
   migration logic distinct from convergence. The official test for "needs its own
   handler" is crisp: *"if an event cannot be deferred, it needs a dedicated
   handler."* Non-deferrable events are actions, `stop`, `remove`,
   `secret_rotate`, `secret_expired`, and the `collect_*_status` lifecycle events.
2. **Idempotent steps.** `snap connect` on a connected plug is a no-op.
   `snap set` with an unchanged value does not restart. `set_ports` diffs against
   `opened_ports()` and closes the difference. Lean on that.
3. **Order matters where the workload demands it.** Port 53 must be free before
   the daemon starts. DHCP pool keys must be set before `dhcp.active`. Encode
   those orderings inside `pihole.py`, not in the event graph.
4. **`collect_unit_status` never mutates.** It reads cached or cheap state. If
   you need to run a command to determine status, make it cheap and
   side-effect-free (`pihole snap-check`, `pihole api dns/blocking`).
5. **Never report `ActiveStatus` from `snap services` alone.** For this snap the
   daemon is `active` before blocking works.
6. **Do not use `defer()`.** It is not deprecated, but the official guidance is
   explicit that deferring while waiting for other parts of the configuration is
   an antipattern — it builds a queue of handlers that all redo the same expensive
   work. Set a status and return; the next event will reconcile. From the docs:
   *"if you're starting to use `defer` in various places, consider whether it's
   time to rewrite the charm using the reconciler pattern."* We already did.
7. **Do not use `ops.StoredState`.** It is not deprecated, but the official
   guidance is to avoid it: caching a value that already exists in Juju config,
   on disk, and in the running process *"doubles the number of possible states
   from 8 to 16 without increasing the number of correct states."* Read the real
   state of the snap on every reconcile — which non-negotiable #6 requires anyway.

## Status semantics — get this right, it is counterintuitive

Each settable status answers a different question. From the ops docstrings:

| Status | ops says | The question it answers | Settable |
|---|---|---|---|
| `ActiveStatus` | *"correctly offering all the services it has been asked to offer"* — and *"if the unit is operational but some feature is in a degraded state, set active with an appropriate message"* | is the workload doing its job right now? | yes |
| `MaintenanceStatus` | *"performing an operation such as `apt install`, or waiting for something under its control"* | is **this unit** busy, and will it clear on its own? | yes |
| `WaitingStatus` | *"waiting on a charm it's integrated with"* | am I blocked on **another application**? | yes |
| `BlockedStatus` | *"an administrator has to manually intervene to unblock the charm to let it proceed"* | can a human do something about it? | yes |
| `ErrorStatus` | *"the unit-agent has encountered an error"* — **read-only**, `add_status` raises `InvalidStatusError` | — | no |
| `UnknownStatus` | the state before the first `status-set` — **read-only** | — | no |

**"Waiting for gravity bootstrap" is `MaintenanceStatus`, not `WaitingStatus`.**
Gravity is this unit's own workload. `WaitingStatus` is reserved for waiting on a
*related* application. This charm has almost no legitimate use for
`WaitingStatus`, because it reaches Active with zero relations.

### `BlockedStatus` is the one that directs a human

The test is a question, not a severity: **can an administrator do something about
it?** If yes, Blocked. If it clears on its own, Maintenance. If it depends on
another app, Waiting. If the service works but something is degraded, Active with
a message.

For this charm:

| Condition | Status | Why |
|---|---|---|
| port 53 held and the charm cannot free it | **Blocked** | the operator must free it; Maintenance would hang silently forever |
| invalid config (bad upstream address, DHCP pool without a router) | **Blocked** | only a human can fix the config |
| a required plug cannot be connected | **Blocked** | needs `snap connect` privileges the charm lacks |
| snap installing, config applying | **Maintenance** | this unit is busy |
| gravity bootstrap downloading | **Maintenance** | clears on its own; Blocked would send someone to investigate nothing |
| DNS answering, blocking on, last gravity sync failed | **Active with a message** | the workload *is* offering its service, just degraded |
| FTL crash-looping on `EADDRINUSE` | **Blocked** | the launcher no longer pre-checks the port, so this needs intervention |

Because a Blocked message exists to direct a human, it must name the action:

```python
# Bad: states the problem, not the remedy.
ops.BlockedStatus("port 53 in use")

# Good.
ops.BlockedStatus(
    "port 53 held by systemd-resolved; free the port or run the "
    "disable-stub-listener action"
)
```

### `raise` versus `BlockedStatus` — and the real cost of error state

This is contested territory. The reference discussion is
[*"It's probably ok for a unit to go into error state"*](https://discourse.charmhub.io/t/its-probably-ok-for-a-unit-to-go-into-error-state/13022),
which is worth reading in full because the ops docstrings do not capture the
trade-off. The consensus that emerges:

**Where the thread agrees with a plain "let it raise":**

- Ben Hoyt: *"I agree we should probably error more than we do."* An operation that
  should structurally succeed (a hook precondition) failing is a genuine error.
- The charm going into error is honest signalling: John Meinel frames it as
  *"something is fundamentally off, such that I cannot progress the model of the
  world"* — the charm is saying it can no longer do its job.

**But the costs are concrete and severe, not cosmetic:**

1. **`juju remove` does not work** on a unit in error without `--force`, and
   `--force` skips cleanup. Meinel: *"the charm has abdicated responsibility by
   going into Error. Which means we have no idea whether things will cleanup
   correctly."*
   **This is decisive for this charm.** Our `remove` handler is what deletes the
   `systemd-resolved` drop-in. A unit stuck in error that gets force-removed leaves
   the host with `DNSStubListener=no` and a dead Pi-hole — **the machine loses DNS
   entirely**. That failure mode alone justifies preferring Blocked wherever the
   choice is genuine.
2. **Model migration is blocked, full stop**, and so are some Juju upgrades. Paul
   Goins: *"if there are 'expected' cases where something is going to go into an
   error state, Juju migrations and at least some Juju upgrades are blocked, full
   stop, until those issues are resolved."*
3. **Error detaches the charm from managing the workload.** It stops reacting to
   config and relation changes entirely.

**And the retry you were counting on is not guaranteed.** `automatically-retry-hooks`
is *model* config. It defaults to `true`, but Tom Haddon notes it is *"not set that
way everywhere"*, and Meinel confirms OpenStack CI runs with it **disabled** and
treats reaching Error as a rejection. So "Juju will retry" is not a property you
can design on.

**The trap that decides the config case.** Error preserves the *original* hook
context. Leon: *"correcting a config option or relation data would not help
resolving the charm, because juju will continue to retry the hook with the old
context."* The operator then has to run `juju resolve --no-retry <unit>` and only
*after* making the correction. So raising on bad config produces a unit that retries
forever against the value the operator already fixed. Meinel is blunt: *"Things that
need normal human intervention (bad config, missing relation) should certainly not
be errors, and that is what Blocked is for."*

**The middle ground is retry inside the hook, not raise.** Both Hoyt and Meinel land
here: *"If the API is flakey, do say 3 retries of that operation in a simple loop
(or use a retry decorator)"*, and *"having a small number of retries before waking a
human would be good. If you really couldn't reach pebble after 10 retries you really
should be failing hard."*

For this charm that means the snap store, which is genuinely flaky:

```python
@tenacity.retry(
    # snap.Error, NOT snap.SnapError. Verified against charmlibs-snap 1.0.1:
    # SnapError, SnapAPIError and SnapNotFoundError all inherit directly from
    # Error and none subclasses another, so retrying SnapError alone misses the
    # store and lookup failures that are the flaky ones. The Snap* names are
    # also legacy aliases, already removed on charmlibs main.
    retry=tenacity.retry_if_exception_type(snap.Error),
    wait=tenacity.wait_fixed(2) + tenacity.wait_random(0, 5),
    stop=tenacity.stop_after_attempt(3),
    reraise=True,
)
def install(self, revision: int | None) -> None: ...
```

### The decision table for this charm

| Situation | Do this | Why |
|---|---|---|
| invalid config | `BlockedStatus` | retry cannot help, and error would retry the *stale* config forever |
| port 53 held, cannot free it | `BlockedStatus` | the operator can act, and we must stay removable so `remove` can restore resolved |
| plug cannot be connected | `BlockedStatus` | needs privileges the charm lacks |
| snap store transient failure | retry ~3× in-hook, then raise | genuinely transient; if it persists something is wrong |
| `snap set` read-back mismatch | `BlockedStatus` via the reconciler | see the push-status problem below — a retry will produce the same silent drop |
| gravity downloading | `MaintenanceStatus` | clears itself |
| a bug in our own code | let it raise | it *is* an error; the traceback is the point |

The bias for this charm is toward Blocked over error, and the reason is specific
rather than stylistic: **the `remove` handler owns host DNS state.** Keeping the
unit removable is a correctness requirement, not a UX preference.

### The sharper Blocked criterion

Dylan Stephano-Shachter, in the same thread, on why people get this wrong:

> People see "manual intervention" and think *the charm can't recover
> automatically, so a human needs to debug the issue, thus blocked state.* My
> understanding is that it is not actually for the situation above, but for a
> situation where **the charm can tell the human what needs to be done.**

So the test is not just "can a human act?" but "**can we name the action?**" If the
charm cannot say what to do, Blocked is the wrong status — that is an error or a
`MaintenanceStatus`, depending on whether it is our bug or transient.

Worth knowing that the taxonomy has a genuinely ambiguous corner, acknowledged by
Tony Meyer: a missing relation — is that Blocked (a human must run `integrate`) or
Waiting (Juju is setting up an integration already requested)? There is no clean
answer. It is moot for this charm, which reaches Active with zero relations.

### The push/pull status problem — a real hole to close

Leon's framing, which the thread converges on:

- **Pull statuses** can be queried at any time. `collect_unit_status` was designed
  for exactly these.
- **Push statuses** are only knowable by *attempting* a mutating operation. They
  cannot be re-derived in the status handler.

Tony Meyer names the resulting race precisely: a check *looks* pullable, *"but that
introduces a race where your main handler failed and your collect status handler
succeeded."*

**This charm has that race.** Consider: `_reconcile` calls `set_ftl_key`, the
read-back verification fails, and it raises `PiholeError`. Meanwhile
`_on_collect_status` independently runs `pihole snap-check` and `pihole api
dns/blocking` — both of which may well succeed, because the daemon is fine and only
one config key silently failed to apply. **The unit reports `ActiveStatus` while the
config it was asked for was never applied.** That is precisely the "model departure"
the thread warns about, and it is the exact defect non-negotiable #6 exists to
prevent.

**The fix, and it needs no `StoredState`.** `ops/_main.py` runs
`_emit_charm_event()` and then `_evaluate_status()` in the same method, in the same
process, on the same charm instance:

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
    ...

def _reconcile(self, _: ops.EventBase) -> None:
    try:
        ...
        self.pihole.apply_config(config, bind_address=self._bind_address)
    except PiholeError as e:
        # A push status: collect_unit_status cannot re-derive this, because the
        # daemon is healthy and only one key silently failed to apply.
        self._reconcile_failure = ops.BlockedStatus(str(e))

def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
    if self._reconcile_failure is not None:
        event.add_status(self._reconcile_failure)
    # ... then the pull statuses
```

This is state, but it lives for one hook execution and is gone. It does not violate
the "no `StoredState`" rule, which is about caching across hooks.

**Prefer converting push to pull where you can.** Andrew Scribner's challenge in the
thread is fair: many apparent push statuses can be written as pull statuses. For
this charm, "does `pihole.toml` match the intent?" *is* pullable — read the TOML in
the status handler and diff it against the config. Do that where it is cheap, and
fall back to the instance attribute only where attempting the operation is the only
way to know.


### Precedence

When several statuses are added (`StatusBase._get_highest_priority`):

```
blocked  >  maintenance  >  waiting  >  active
```

Ties go to the first one added. **One spurious Blocked masks every other status the
handler adds**, which is another reason to reserve it for genuine operator
problems.

`add_status` can be called many times, and the docs say *"each code path in a
collect-status handler should call `add_status` at least once"*.

`collect_app_status` also exists and the framework only emits it on the leader, so
you do **not** need an `is_leader()` guard in that handler. For a single-unit
Pi-hole it adds little; it matters if peers are added later.

## Ports

`self.unit.set_ports(*ports)` is declarative and idempotent — it diffs against
`opened_ports()` and closes what is no longer wanted. That makes it a correct
reconcile step, unlike `open_port`/`close_port` which manage ports individually.

**`ops.TCPPort` and `ops.UDPPort` do not exist in `ops`.** They only exist in
`ops.testing` (from `ops-scenario`). In production code the type is `ops.Port`:

```python
ops.Port("udp", 53)
```

**A bare `int` means TCP.** `set_ports(53)` opens 53/tcp only — for DNS you need
both entries explicitly. This is the single easiest way to ship a broken DNS
charm.

Do not mix `Unit.set_ports` with `ops.hookcmds.open_port`. The latter supports
port ranges and `--endpoints`, but `opened_ports()` silently discards ranges with a
warning, so `set_ports` would try to reopen them on every hook.

### The trap: `open-port` does nothing until `juju expose`

From the Juju hook-command docs: *"On public clouds the port will only be open
while the application is exposed. `open-port` will not have any effect if the
application is not exposed."*

So `set_ports` is **declarative documentation**, not what makes DNS reachable.

And worse for our test setup: **the LXD provider does not implement Juju's
`Firewaller` interface at all.** There is no `OpenPorts`/`ClosePorts`/
`IngressRules` in `internal/provider/lxd/`. On LXD, `juju expose` records the flag
and `juju status` displays it, but nothing changes at the network level — port 53
is reachable with or without it.

Consequences:

- Do not write an integration test asserting that `expose` opens or closes
  anything. On LXD it verifies nothing.
- Do not assume port 53 is closed on LXD without `expose`. It is not.
- On MAAS/EC2/OpenStack it does matter, and a deployment there will behave
  differently from every test we run. Say so in the README.

## Network binding — what a DNS charm actually needs

`set_ports` does not tell you which address to bind to or advertise. That comes
from the binding:

```python
binding = self.model.get_binding("dns")   # or an endpoint name, or a Binding
address = binding.network.bind_address     # bind FTL here
advertise = binding.network.ingress_address  # tell clients this
```

For Pi-hole this is the answer to "how do we serve the network rather than
localhost" on the *Juju* side, independent of the FTL `dns.listeningMode` problem
described in `pihole-snap`. Both have to be solved; they are different layers.

In tests, `testing.Network(...)` with `testing.BindAddress`/`testing.Address`
populates it. Consider declaring `extra-bindings:` so an operator can place DNS on
a specific space instead of adding a "listen interface" config option — that is
non-negotiable #4 applied.

## Other machine-only APIs worth knowing

- **`self.unit.set_workload_version(str)`** — shows the Pi-hole version (not the
  charm's) in `juju status`. Call it in the reconciler once the snap is installed.
  Raises `TypeError` if not given a `str`.
- **`self.unit.reboot(now=False)`** — machine charms only. Reboots after the hook
  completes successfully; `now=True` cuts immediately and Juju re-runs the hook
  afterwards. Raises `ModelError` from an action handler. Relevant only if host
  networking changes ever require it.
- **`self.app.planned_units()`** — how many units Juju *intends* to have.
  The way to distinguish a deliberate scale-down from a failed unit in `remove`.
- **`ops.hookcmds`** — a typed escape hatch for hook tools the model does not
  expose (port ranges, `--endpoints`, `goal_state()`). Public and documented, but
  see the warning about mixing it with `set_ports`.
- **Logging needs no setup.** `ops.main` wires `logging` to `juju-log`, so
  `logging.getLogger(__name__)` already reaches `juju debug-log`.

## Typed config and params — prefer the native pydantic support

`ops` 2.23+ has first-class pydantic integration. Since this repo mandates pydantic
anyway, use it instead of hand-parsing `self.config`:

```python
config = self.load_config(PiholeConfig, errors="blocked")
```

`errors="blocked"` sets `BlockedStatus` with a useful message and exits 0, so Juju
does not retry a hook that can only fail again. Dashes in option names map to
underscores, and `pydantic.Field(alias=...)` is respected. A `type: secret` option
arrives as an `ops.Secret` object rather than a URI string.

The action equivalent:

```python
params = event.load_params(UpdateGravityParams, errors="fail")
```

## Secrets

`Model.get_secret` is **keyword-only**:

```python
secret = self.model.get_secret(id=uri)      # or label=...
```

`get_secret("secret:abc")` positionally is a `TypeError`. The ops docstring for
`Relation.load` gets this wrong — do not copy from it.

| Method | Use |
|---|---|
| `get_content()` | cached on the object |
| `get_content(refresh=True)` | **only** in a `secret_changed` handler — this is what tells Juju to start tracking the new revision |
| `peek_content()` | latest revision without changing tracking or caching |
| `set_content(...)` | creates a new revision; a no-op since Juju 3.6 if the content is identical |

`set_content` is another instance of non-negotiable #6: if the charm lacks
permission or the secret is gone, **the method succeeds** and the unit errors at
the end of the hook.

## Events that do not exist or should not be observed

- `pre_series_upgrade` / `post_series_upgrade` — **removed in Juju 4.0.** Do not
  observe them.
- `leader_settings_changed` — deprecated since ops 2.4.0.
- `collect_metrics` — removed in Juju 3.6.11.
- Any `*_pebble_ready` — Kubernetes only; they only exist if `charmcraft.yaml`
  declares `containers:`.

## Verification pattern

Because the snap lies about success, every apply gets a read-back:

```python
def set_ftl_key(self, key: str, value: str) -> None:
    """Set an FTL config key and verify it landed.

    Raises:
        PiholeError: if the value did not take effect.
    """
    if _is_snapd_safe_key(key):
        subprocess.run(
            ["snap", "set", SNAP_NAME, f"ftl.{key}={value}"],
            check=True, capture_output=True, text=True,
        )
    else:
        # snapd rejects camelCase/underscore option names; bypass it.
        self._ftl_config(key, value)
        self.restart()

    actual = self._read_toml_key(key)
    if actual != value:
        raise PiholeError(f"{key}: set to {value!r} but reads back as {actual!r}")
```

`_is_snapd_safe_key` implements snapd's own regex,
`^(?:[a-z0-9]+-?)*[a-z](?:-?[a-z0-9])*$`, applied per dotted segment.
