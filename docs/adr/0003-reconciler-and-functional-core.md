# ADR-0003: Reconciler and Functional Core

**Status:** Accepted
**Date:** 2026-08-07
**Accepted:** 2026-08-08
**Related:** [ADR-0002: Tech Stack and Repository Architecture](0002-tech-stack-and-repo-architecture.md), [ADR-0004: FTL Configuration Mechanism](0004-ftl-configuration-mechanism.md), [ADR-0005: Status Semantics and Failure Handling](0005-status-semantics-and-failure-handling.md)

---

## 1. Context

ADR-0002 fixes the module layout. This ADR fixes the *control flow*: how events
reach logic, and how logic is split between deciding and acting.

### 1.1 The defect this is guarding against

The canonical charm bug is a method that **decides and acts at the same time**.
The archetype, from Canonical's own workshop material, is a
`_generate_prometheus_config()` that pushes a file *and* returns
`should_reload`. You cannot test the decision without performing the effect. Every
mock-heavy charm test suite is a symptom of this shape.

`AGENTS.md` non-negotiable #7 states the rule; this ADR states the mechanism.

### 1.2 Why this charm in particular earns the machinery

Reifying decisions as data costs code. It is worth it only when the decision is
genuinely complex. Here it is:

- **Two apply mechanisms** depending on whether a key is reachable via `snap set`
  ([ADR-0004](0004-ftl-configuration-mechanism.md)).
- **A workload that lies about success** — `snap set` returns 0 on keys it drops,
  and two `pihole` subcommands print usage and exit 0
  ([snap-constraints §4.2, §7.1](../snap-constraints.md)).
- **Asynchronous readiness** — the daemon is `active` long before blocking works.
- **Host state the snap cannot touch** — the resolved drop-in and the gravity
  timer.
- **Hard ordering requirements** — port 53 free before start; DHCP pool before
  `dhcp.active`.

"Under exactly which conditions do we restart FTL?" is the kind of question where
bugs hide. It deserves to be a type, not a comment.

---

## 2. Decision

### 2.1 One reconciler

Every observed event routes to a single `_reconcile`. Separate handlers exist
**only** for events that cannot be deferred, which is the objective test from the
official guidance: *"if an event cannot be deferred, it needs a dedicated
handler."*

That set is exactly: **actions**, `stop`, `remove`, `secret_rotate`,
`secret_expired`, and the `collect_*_status` lifecycle events.

Everything deferrable goes through `_reconcile` — including `config_changed`,
`upgrade_charm`, `secret_changed`, `leader_elected`, `update_status`, and every
relation event. The one permitted exception is `upgrade_charm` **if** it ever
needs migration logic distinct from convergence.

For this charm the separate handlers are: `collect_unit_status`, the four actions,
and `remove`.

### 2.2 Fetch once, decide purely, apply dumbly

```
    ┌─────────┐       ┌───────────┐      ┌─────────┐
    │  Fetch  ├──────►│  Compute  ├─────►│  Apply  │
    └─────────┘       └───────────┘      └─────────┘
         ▲                  ▲                 ▲
         │           Functional core          │
         └───────── Imperative shell ─────────┘
```

```python
def _reconcile(self, _: ops.EventBase) -> None:
    """Converge the machine toward intent.

    Every step must be safe to run twice and safe to never run.
    """
    config = self.load_config(PiholeConfig, errors="blocked")
    self.unit.set_ports(*self._ports(config))
    state = fetch(self._pihole)                    # read the world once
    for outcome in compute(state, config):         # decide purely
        self._apply(outcome)                       # act dumbly
```

`fetch` is the **only** impure read path. If a second function starts reading the
machine, the boundary has broken and the tests will start needing mocks again.

### 2.3 Model the input as a union, not as optional fields

```python
@final
@dataclass(frozen=True)
class SnapAbsent:
    """The snap is not installed on this machine."""


@final
@dataclass(frozen=True)
class SnapPresent:
    """Facts read from the machine. Every field is observed, never assumed."""

    revision: int
    ftl_enabled: bool
    ftl_active: bool
    ftl_config: Mapping[str, str]        # flattened from pihole.toml
    connected_plugs: FrozenSet[str]
    gravity_db_bytes: int
    blocking_state: BlockingState        # from `pihole api dns/blocking`
    port_53_holder: str | None           # None means free, or held by us
    resolved_stub_disabled: bool


type PiholeState = SnapAbsent | SnapPresent
```

A union beats a dataclass with half-`None` fields whose invariants live in
comments and force `cast()` at every use site. **Make illegal states
unrepresentable.**

Container types in every signature and frozen field are `Mapping`, `Sequence`,
`FrozenSet` — never bare `dict`, `list`, `set`. This is not decoration: `set`
iteration order is a real source of relation-databag churn, which is exactly what
`tox -e flaplint` exists to detect.

### 2.4 Reify the decision as an outcome ADT

```python
type PiholeOutcome = (
    ReleasePort53
    | InstallSnap        # revision: int | None
    | ConnectPlugs       # plugs: FrozenSet[str]
    | ApplyFtlConfig     # via_snap_set: Mapping | via_ftl_config: Mapping
    | WriteGravityTimer  # on_calendar: str
    | StartFtl
    | RestartFtl         # reason: str
    | Noop
)
```

Two things to notice:

1. **`ApplyFtlConfig` carries the reachable/unreachable split as data.** The
   decision "which mechanism applies this key" becomes pure and testable; only
   the mechanism itself stays impure. This is the highest-value application of
   the pattern in the whole charm.
2. **`RestartFtl` carries a reason.** Restarts drop DNS for every client on the
   network. Making the cause a required field means it always reaches the log.

### 2.5 `compute` returns an ordered sequence, and is exhaustive

```python
def compute(state: PiholeState, intent: PiholeConfig) -> Sequence[PiholeOutcome]:
    """Decide what to do. No IO. No exceptions raised for control flow."""
    match state:
        case SnapAbsent():
            return (ReleasePort53(), InstallSnap(intent.snap_revision), StartFtl())
        case SnapPresent():
            return _compute_present(state, intent)
        case _ as unreachable:
            assert_never(unreachable)
```

**A sequence, not a set**, because the workload has hard ordering requirements.
Ordering lives here, in data — never in the event graph.

`assert_never` combined with a PEP 695 `type X = A | B` union is the load-bearing
mechanism, not a flourish: **pyright fails the build** when a variant is added and
a `match` branch is forgotten. That is why `tox -e static` matters more in this
charm than in a typical one, and why ADR-0002 pins Python 3.14.

`_apply` is the mirror image — a `match` over the outcome union with
`assert_never`, deliberately stupid, the only place effects happen.

### 2.6 Errors are data that carry context

```python
@final
@dataclass(frozen=True)
class SnapSetError(Exception):
    key: str
    expected: str
    actual: str
```

Subclassing `Exception` keeps `logger.error(..., exc_info=err)` and
`ExceptionGroup` working. Carrying the context *inside* the error lets the status
handler build an informative `BlockedStatus` without going back to the workload to
ask again.

### 2.7 Injection goes below the charm, because above it is impossible

Verified against the `ops` source, not assumed. `ops/_main.py` does:

```python
self.charm = self._charm_class(self.framework)
```

Only the framework is passed. `ops.testing.Context.__init__` takes
`charm_type: type[CharmType]` — a type, not a factory — and reuses the same
machinery. There is no `charm_factory`, `charm_kwargs`, or `charm_args` hook
anywhere in `ops` or `ops.testing`.

**So constructor injection into the charm is not available.** At that boundary the
seam is a module attribute replaced with `monkeypatch`, and there is no
alternative. Do not invent a factory indirection to pretend otherwise.

Inject *below* the charm, where the untestable code lives:

```python
class Pihole:
    """Own every effect on the machine. Knows nothing about ops."""

    def __init__(
        self,
        cache_factory: Callable[[], Mapping[str, SnapLike]] = snap.SnapCache,
        run: Runner = subprocess.run,
        snap_data: Path = SNAP_DATA,
    ) -> None: ...
```

This buys something `monkeypatch` does not: **a fake that lies the way the snap
lies** — `run` returns exit 0 and the value never appears in `pihole.toml` — so
the read-back verification of non-negotiable #6 can be tested directly instead of
by patching `subprocess` and asserting on call order.

Charm libraries are **instantiated, never subclassed**. `COSAgentProvider(self,
...)` takes the whole charm; the ops ecosystem injects the god object. That is a
wart to live with, not a pattern to copy — **our own functions take the narrowest
thing they need**, never `charm: PiholeCharm`.

### 2.8 Two things we will not use

- **No `defer()`.** Not deprecated, but the official guidance is explicit that
  deferring while waiting for other configuration is an antipattern — it builds a
  queue of handlers that all redo the same expensive work. Set a status and
  return; the next event reconciles. The docs are blunt: *"if you're starting to
  use `defer` in various places, consider whether it's time to rewrite the charm
  using the reconciler pattern."* We already did.
- **No `ops.StoredState`.** Caching a value that already exists in Juju config, on
  disk, and in the running process *"doubles the number of possible states from 8
  to 16 without increasing the number of correct states."* Non-negotiable #6
  requires reading real state every reconcile anyway, so the cache only adds wrong
  states. (The single-hook status attribute in
  [ADR-0005](0005-status-semantics-and-failure-handling.md) is not an exception to
  this — it does not persist across hooks.)

---

## 3. What we deliberately do not adopt from `fp-edge-canonical`

The reference for this style is `canonical/fp-edge-canonical`. Be precise about
what it is: **workshop material** by an external presenter, forked by Canonical in
March 2026, 13 commits all from the author, issues disabled. Its own README says
*"the code cannot be used in any way as an actual charmed operator. The only
purpose of this repo is for educational purposes."* **No Canonical production
charm uses it.** Its central irony: the repo whose thesis is *"this makes tests
easy"* contains 49 tests, all of the `Result` type itself, and **zero tests of any
charm logic**.

So we take the patterns — all of which are plain 3.12 stdlib — and reject the
infrastructure:

| Rejected | Why |
|---|---|
| A hand-rolled `Result[E, A]` | ~325 lines copied from a talk, with three `# pyright: ignore` for broken covariance. Its own slides call the result *"Extreme Nestiness"* and point at `Result.do` as the fix — **which is commented out because it was never implemented.** pydantic v2 already accumulates validation errors, which covers our actual multi-error case. |
| A monadic reconciler pipeline | `flat_map` short-circuits: the first error aborts everything after it. But a failure reconciling `systemd-resolved` must not prevent reporting the snap's status. Keep the fetch/compute/apply *shape*; do not chain it monadically. `collect_unit_status` with multiple partial statuses is the right mechanism. |
| `from x import *` | Every functional module in that repo does it, and its config ignores `F403`/`F405` to compensate. Contradicts non-negotiable #3. |
| `StoredState` as the status channel | That repo writes status tuples into a shared mutable dict nothing reads — the exact "spooky action at a distance" its own slides denounce. |
| A single `observe(config_changed)` | That repo has no reconciler and no `collect_unit_status`. Non-negotiable #1 is stricter and better. |
| `requires-python = ">=3.13"` | Everything they use is 3.12 or earlier. The pin is arbitrary and would break `ubuntu@24.04`. |

---

## 4. Consequences

### Positive

- `compute` is tested by construction and `==`. Frozen dataclasses give `__eq__`
  free, so there are **no mocks at all** in the highest-risk logic. That is where
  coverage should concentrate, and it will do so naturally.
- pyright mechanically prevents a forgotten `match` branch when a new outcome is
  added — a class of bug that is otherwise found in production.
- Ordering constraints become assertions on a returned tuple rather than
  assertions on mock call order.
- "Safe to run twice" gets a literal test: converged state must yield `(Noop(),)`.
- The `Pihole` fake can reproduce the snap's lying behaviour, so non-negotiable #6
  is testable rather than aspirational.

### Negative

- More types, more `match` statements, more names to invent than an imperative
  charm would need. This is a real cost and it is only justified by §1.2 — **do
  not wrap a two-line function in an ADT to look principled.**
- `fetch` reads more of the world than any single reconcile strictly needs, so
  some hooks do redundant work. Acceptable: correctness over hook latency, and
  non-negotiable #6 forces most of those reads anyway.
- Contributors familiar with conventional charms will find the indirection
  unfamiliar and may put logic in the wrong module. The objective boundary test in
  ADR-0002 §2.7 is the mitigation.
- The charm-level seam remains `monkeypatch` on a module attribute, which is less
  elegant than the injection used below it. That asymmetry is imposed by `ops` and
  cannot be designed away.
