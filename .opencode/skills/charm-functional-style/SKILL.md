---
name: charm-functional-style
description: >-
  Use when designing or writing src/pihole_state.py, src/pihole.py, or the
  reconciler — the functional core / imperative shell split, outcome ADTs,
  frozen dataclasses, exhaustive match with assert_never, and errors as data.
  Load before writing a function that both decides something and performs an
  effect.
metadata:
  verified: "2026-08-07"
  source: "canonical/fp-edge-canonical (workshop material, see caveats)"
---

# Functional style for charms

## Where this comes from, and how much to trust it

`canonical/fp-edge-canonical` is the reference. Be precise about what it is:

- It is **workshop material** by an external presenter (`ncreep` / Daniel
  Beskin), forked by Canonical in March 2026. 13 commits, all from the author.
  0 stars, 0 human PRs since the fork, issues disabled.
- Its own `code/README.md` says: *"After all the edits the code cannot be used in
  any way as an actual charmed operator. The only purpose of this repo is for
  educational purposes."*
- **No Canonical production charm uses this.** It is a talk, not an
  architectural direction.
- Its central contradiction: the repo whose thesis is *"this makes tests easy"*
  contains **49 tests, all of the `Result` type itself, and zero tests of any
  charm logic**. The testability argument is demonstrated only in slides, on a
  function (`rules_filename`) that does not exist in the repo.
- The refactor is partial and leaks: `compute.py` does `from charm import
  PROMETHEUS_DIR`; `fetch_impl` calls `charm._generate_command()`, which does
  lightkube IO and mutates `_stored.status` in four branches; the entire
  `retention_size` logic is byte-for-byte identical to the unrefactored baseline.

So: **take the patterns, not the infrastructure.** The patterns below are all
Python 3.12 stdlib — frozen dataclasses, `match`, `assert_never`, PEP 695 type
unions. They need no FP library and they reinforce this repo's non-negotiables.
What follows the patterns is a list of things from that repo we deliberately do
**not** adopt.

The repo is available as the `fp-edge` reference if you want to read the source.

## The thesis worth keeping

From the slides:

```
    ┌─────────┐       ┌───────────┐      ┌─────────┐
    │  Fetch  ├──────►│  Compute  ├─────►│  Apply  │
    └─────────┘       └───────────┘      └─────────┘
         ▲                  ▲                 ▲
         │           Functional core          │
         └───────── Imperative shell ─────────┘

    Make the core maximally smart.
    Make the shell maximally dumb.
```

The concrete defect it attacks: a method that **decides and acts at the same
time**. In the baseline, `_generate_prometheus_config()` pushes the file *and*
returns `should_reload`. You cannot test the decision without performing the
effect. Every mock-heavy charm test is a symptom of this.

This maps directly onto this repo's non-negotiable #2 (charm logic vs workload
logic) and #6 (never trust an exit code) — it just draws the boundary in **data**
rather than only in modules.

## Pattern 1 — Model the input as a union, not as optional fields

Do not start the reconciler with an early return. Make "the snap is not
installed" a *case of the input type*.

```python
from dataclasses import dataclass
from typing import final

@final
@dataclass(frozen=True)
class SnapAbsent:
    """The snap is not installed on this machine."""


@final
@dataclass(frozen=True)
class SnapPresent:
    """The snap is installed. Every field is a fact read from the machine."""

    revision: int
    ftl_running: bool
    ftl_config: Mapping[str, str]
    gravity_db_bytes: int
    connected_plugs: FrozenSet[str]
    resolved_stub_disabled: bool


type PiholeState = SnapAbsent | SnapPresent
```

Note the container types: `Mapping`, `FrozenSet`, `Sequence` — never `dict`,
`set`, `list` in a signature or a frozen field. That is not decoration: `set`
iteration order is a real source of relation-databag churn, which is exactly what
`flaplint` exists to catch (see the `python-style` skill).

Why a union beats `Optional` fields: the antipattern is a dataclass where half
the fields are `None` and the invariants live in comments, forcing `cast()` at
every use site. **Make illegal states unrepresentable.**

## Pattern 2 — Reify the decision as an outcome ADT

This is the highest-value pattern for this charm. Instead of booleans threaded
through control flow, name every thing the charm can decide to do:

```python
@final
@dataclass(frozen=True)
class InstallSnap:
    revision: int | None


@final
@dataclass(frozen=True)
class ApplyFtlConfig:
    """Keys reachable via snap set, and keys that need the --config fallback."""

    via_snap_set: Mapping[str, str]
    via_ftl_config: Mapping[str, str]


@final
@dataclass(frozen=True)
class RestartFtl:
    reason: str


@final
@dataclass(frozen=True)
class ReleasePort53:
    pass


@final
@dataclass(frozen=True)
class Noop:
    pass


type PiholeOutcome = InstallSnap | ApplyFtlConfig | RestartFtl | ReleasePort53 | Noop
```

The outcome type *answers questions*: "under what conditions does nothing
happen?", "when exactly do we restart FTL?". Those are the questions where bugs
hide, and they become a type instead of a comment.

## Pattern 3 — Compute is pure and exhaustive

```python
from typing import assert_never

def compute(state: PiholeState, intent: PiholeConfig) -> Sequence[PiholeOutcome]:
    """Decide what to do. No IO. No exceptions raised for control flow."""
    match state:
        case SnapAbsent():
            return (InstallSnap(intent.snap_revision),)
        case SnapPresent():
            return _compute_present(state, intent)
        case _ as unreachable:
            assert_never(unreachable)
```

`assert_never` with a PEP 695 `type X = A | B` union is the load-bearing
mechanism, not a stylistic flourish: **pyright fails the build** if you add a case
to the union and forget a `match` branch. That is a real guarantee, and it is why
`tox -e static` matters more here than in a typical charm.

`compute` takes plain data and returns plain data. Testing it needs no
`ops.testing`, no mocks, no `monkeypatch` — construct the dataclass, compare the
result with `==` (frozen dataclasses give you `__eq__` for free).

## Pattern 4 — Fetch once, at one boundary

All reads of the world happen in one function that returns the frozen snapshot.
Nothing downstream touches Juju, the snap, or the filesystem.

```python
def fetch(charm: ops.CharmBase) -> PiholeState:
    """Read every fact the decision depends on. The only impure read path."""
```

If a second function starts reading the machine, the boundary has broken and the
tests will start needing mocks again.

## Pattern 5 — Effects as a Protocol of atomic, verifying operations

Declare the effects as an interface, one method per atomic operation. Crucially
for this charm, **each one verifies real state and returns a value the caller
cannot accidentally ignore**:

```python
class PiholeActions(Protocol):
    def install(self, revision: int | None) -> None: ...
    def connect_plug(self, plug: str) -> None: ...
    def set_ftl_key(self, key: str, value: str) -> None: ...
    def restart_ftl(self) -> None: ...
```

The fp-edge repo's `reload_config` is literally this charm's problem:

```python
def reload_config(self) -> Result[ReloadError, None]:
    reloaded = self._charm._prometheus_client.reload_configuration()
    return Ok(None) if reloaded is True else Err(ReloadError())
```

Translated: `set_ftl_key` runs `snap set`, **reads the value back from
`pihole.toml`**, and raises `PiholeError` if it did not land. Non-negotiable #6
expressed as a signature.

## Pattern 6 — Errors are data that carry context

```python
@final
@dataclass(frozen=True)
class SnapSetError(Exception):
    key: str
    expected: str
    actual: str
```

Subclassing `Exception` keeps `logger.error(..., exc_info=err)` and
`ExceptionGroup` working. Carrying the context inside the error means the status
handler can build an informative `BlockedStatus` without going back to the
workload for more information.

## Pattern 7 — Composition over inheritance, and where injection is possible

`ops` and the charm libraries are object-oriented. That is not a conflict with the
patterns above, but it does constrain *where* dependency injection works. Verified
against the `ops` source, not assumed:

**Constructor injection into the charm is not available.** `ops/_main.py`:

```python
self.charm = self._charm_class(self.framework)
```

Only the framework is passed. `ops.testing.Context.__init__` takes
`charm_type: type[CharmType]` — a type, not a factory — and reuses the same
machinery via `_ops_main_mock.py`. There is no `charm_factory`, `charm_kwargs`, or
`charm_args` hook anywhere in `ops` or `ops.testing`. So at the charm boundary the
seam is a module attribute replaced with `monkeypatch`, and there is no
alternative. Do not invent a factory indirection to pretend otherwise.

**Inject below the charm, where the untestable code lives.** `Pihole` is a plain
class and can take its collaborators:

```python
class Runner(Protocol):
    def __call__(
        self, args: Sequence[str], *, check: bool, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]: ...


class Pihole:
    """Own every effect on the machine. Knows nothing about ops."""

    def __init__(
        self,
        cache_factory: Callable[[], Mapping[str, SnapLike]] = snap.SnapCache,
        run: Runner = subprocess.run,
        snap_data: Path = SNAP_DATA,
    ) -> None:
        self._cache_factory = cache_factory
        self._run = run
        self._snap_data = snap_data
```

This buys something `monkeypatch` does not: a **fake that lies the way the snap
lies** — `snap set` exits 0 and the value never appears in `pihole.toml` — so the
read-back verification of non-negotiable #6 can be tested directly rather than by
patching `subprocess` and asserting on call order.

**Compose the charm libraries, never subclass them.** `COSAgentProvider(self,
...)` is instantiated in `__init__`. Note that it takes the whole charm: the ops
ecosystem injects the god object. That is a wart to live with, not a pattern to
copy — **our own functions take the narrowest thing they need**, never
`charm: PiholeCharm`.

**Where injection is not worth it.** This is a process that runs once per hook and
exits. A constructor with eight collaborators and a factory to assemble them is
ceremony with no payoff. A `Protocol` earns its place for one of two reasons:
**(a)** a second implementation exists — a real one and a fake, as with
`PiholeFacts`, implemented by `Pihole` and by `FactsStub` in the tests; or
**(b)** it inverts an import the layering forbids — that same `PiholeFacts` is
what lets `pihole_state.py` describe the reads it needs without `import pihole`
pulling `charmlibs.snap` into the pure core. Wanting testability alone is not a
reason: for that, replacing a module attribute is enough. The fp-edge slides say
as much about their own `ConfigActions`: *"You don't have to use abstract
interfaces like this. But phrasing your 'atomic' actions helps with design."*

Rule of thumb: **inject the effect boundary, not every collaborator.**

## Pattern 8 — A boolean must not gate whether a function has effects

Pattern 2 removes booleans that carry a *decision*. This one removes the boolean
that carries *permission to mutate*. The shape looks harmless:

```python
# Don't. The name cannot answer "does this mutate?"
def _intent(self, *, generate: bool) -> PiholeIntent | None:
    password = self._read_password()
    if password is None and generate and self.unit.is_leader():
        password = secrets.token_urlsafe(24)
        self._store_password(password)   # a write, reachable only via the flag
    ...
```

Two callers pass different values: the reconciler wants the write, the status
handler must not have it. So whether `collect_unit_status` stays side-effect-free
depends on an argument at a call site, not on anything the type checker can see.
One wrong `True` in a future refactor makes a status handler mutate state, and no
gate catches it.

Split it so each name is the answer:

```python
def _current_intent(self) -> PiholeIntent | None:
    """The declared desired state as it stands now, reading only."""
    return self._intent_from(self._read_password())


def _converged_intent(self) -> PiholeIntent | None:
    """The declared desired state to converge toward, minting if needed."""
    return self._intent_from(self._obtain_password())
```

Now `_on_collect_status` calls `_current_intent()` and there is no argument that
could make it write. The reachability of the effect moved from a runtime value
into the call graph, where reading the code answers the question.

Not every boolean parameter is this defect. `check: bool` passed straight through
to `subprocess.run` is fine — it configures an effect that happens either way. The
test is whether flipping the flag changes *whether* the function mutates anything.

## What we deliberately do not adopt

**A hand-rolled `Result[E, A]`.** The fp-edge repo implements one in `result.py`
(~145 lines) plus `result_combinators.py` (~180 lines of mechanical boilerplate
duplicated for arities 2–5, because Python has no variadic generics). It has
`# pyright: ignore[reportGeneralTypeIssues]` three times for broken covariance.
Its own slides call the resulting code *"Extreme Nestiness"* and point at
`Result.do` as the fix — **which is commented out because they never implemented
it**. Copying 325 lines from a talk with no integration tests is debt that buys
nothing here. If a concrete multi-error validation case appears, use `returns`
from PyPI, or note that **pydantic v2 already accumulates validation errors**,
which covers the "invalid config" case without any `Result` at all.

**A monadic pipeline as the reconciler.** `fetch().flat_map(compute)
.flat_map(apply).on_error(handler)` is elegant, but `flat_map` short-circuits: the
first error aborts everything after it. This reconciler must be safe to run twice
*and* safe to never run, and a failure reconciling `systemd-resolved` must not
prevent reporting the snap's status. `collect_unit_status` with multiple partial
statuses is the right mechanism. Keep the fetch/compute/apply *shape*; do not
chain it monadically.

**`from x import *`.** Every functional module in that repo does it, and its
`pyproject.toml` ignores `F403`/`F405` to compensate. It contradicts
non-negotiable #3.

**`StoredState` as the status channel.** That repo writes status tuples into a
shared mutable dict that nothing reads — the exact "spooky action at a distance"
its own slides denounce. We have `collect_unit_status`.

**A single `observe(config_changed)`.** That repo has no reconciler and no
`collect_unit_status`. Non-negotiable #1 is stricter and better.

**`requires-python = ">=3.13"`.** Everything they use (PEP 695 `type X`,
`def map[B]`, `assert_never`, `ExceptionGroup`) is Python 3.12 or earlier. The pin
is arbitrary and would break `ubuntu@24.04`.

## The honest trade-off

Reifying decisions as ADTs costs code: more types, more `match` statements, more
names to invent. It pays off when the decision is genuinely complex — and for
this charm it is: reachable vs unreachable config keys, a snap that lies about
success, asynchronous readiness, host state the snap cannot touch. That is the
case where "which conditions cause a restart?" deserves to be a type.

It does not pay off for trivially linear code. Do not wrap a two-line function in
an ADT to look principled. The slides are right about this much:

> Not a magic bullet. You don't have to go "all in".
