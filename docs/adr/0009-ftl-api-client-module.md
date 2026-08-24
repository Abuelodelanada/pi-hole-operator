# ADR-0009: Split the FTL API client out of `Pihole`

**Status:** Accepted
**Date:** 2026-08-20
**Related:** [ADR-0002: Tech stack and repository architecture](0002-tech-stack-and-repo-architecture.md),
[ADR-0003: Reconciler and functional core](0003-reconciler-and-functional-core.md),
[ADR-0004: FTL configuration mechanism](0004-ftl-configuration-mechanism.md),
[ADR-0007: Admin password handling](0007-admin-password-handling.md),
[Snap constraints reference](../snap-constraints.md)

---

## 1. Context

`src/pihole.py` is 979 lines and `Pihole` has 31 methods. Measured against the
class body, **256 of its 558 method lines — 46% — are the HTTP conversation with
FTL's API**, spread across 13 methods:

| Public | Private |
|---|---|
| `api_ready`, `admin_password_state`, `api_facts`, `await_api` | `_api_request`, `_authenticate`, `_logout`, `_open_cli_session`, `_try_open_cli_session`, `_read_cli_pw`, `_classify_password`, `_classify_password_settled`, `_probe_blocking` |

Those 13 share almost nothing with the other 18. The rest of `Pihole` talks to
snapd through `charmlibs.snap` and reads `pihole.toml` from `$SNAP_DATA`; the API
methods manage bearer sessions against a local HTTP server, count FTL's 16 session
slots, and implement the bounded settle window that ADR-0007 §4.3 requires.

The coupling is only three injected collaborators: `run`, `snap_data` (for
`_read_cli_pw`), and the `sleep`/`monotonic` pair.

This is not a correctness defect. Rule 2 is satisfied today — `pihole.py` does not
import `ops`. The cost is comprehension and test surface: a change to session
handling forces a reader through a module whose first 700 lines are about snaps,
and the API methods cannot be exercised without constructing the snap-facing half.

---

## 2. Approaches

### A. Leave it

**Pros**
- No churn. `PiholeFacts`, `charm.py` and every test stay untouched.
- One workload facade is genuinely simpler to hold in mind than two.

**Cons**
- The file keeps growing: Stage 2 adds FTL config keys, Stage 3 adds diagnostics,
  Stage 7 adds DHCP. All three land in the snap-facing half, so 979 lines is the
  floor, not the ceiling.
- The API half stays untestable in isolation.

### B. Extract `src/ftl_api.py`, and have callers use it directly

`pihole_state.PiholeFacts` gains `FtlApi` as a second collaborator; `charm.py`
constructs both.

**Pros**
- No delegation layer. Each caller names what it actually needs.

**Cons**
- Touches `PiholeFacts`, `fetch`, `charm.py`, `conftest.py` and three test files.
- The charm now assembles two workload objects, so ordering and lifetime become
  its problem. Rule 8 says pass the narrowest collaborator, but the charm is
  exactly where we do *not* want more assembly.

### C. Extract `src/ftl_api.py`, and keep four delegating methods on `Pihole`

The nine private methods move. `api_ready`, `admin_password_state`, `api_facts`
and `await_api` stay on `Pihole` as one-line delegations to a composed `FtlApi`.

**Pros**
- `PiholeFacts`, `fetch` and `charm.py` are unchanged, so the blast radius is two
  files plus new tests.
- `FtlApi` is constructible on its own, so its tests stop needing a snap.
- `Pihole` remains the single facade the charm knows, which is what makes the
  charm's own wiring trivial.

**Cons**
- Four methods exist only to forward. That is real indirection, and a reader
  chasing `api_facts` now takes two hops.
- Two modules to keep free of `ops` instead of one.

---

## 3. Recommendation

**C.** The deciding factor is the blast radius against the charm: B is the purer
decomposition but moves assembly into `charm.py`, and ADR-0003 §2.7 already
established that the charm is the one place we cannot inject into cleanly. Four
forwarding methods are a cheaper price than making the charm own two workload
lifetimes.

**A is rejected and recorded.** Not because 979 lines is a threshold, but because
every remaining stage adds to the snap-facing half specifically, so the ratio
gets worse on its own.

---

## 4. Design

New module `src/ftl_api.py`. Imports neither `ops` nor `charmlibs`, so rule 2
applies to it exactly as to `pihole.py` and `resolved.py`.

```python
@final
class FtlApi:
    """Talk to FTL's HTTP API. Owns sessions, nothing else."""

    def __init__(
        self,
        run: Runner = _subprocess_run,
        snap_data: Path = SNAP_DATA,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None: ...

    def ready(self) -> bool: ...
    def password_state(self, password: str) -> AdminPasswordState: ...
    def facts(self, password: str) -> ApiFacts: ...
    def await_ready(self, timeout: float) -> None: ...
```

`Pihole` composes it and forwards:

```python
self._api = api or FtlApi(run=run, snap_data=snap_data, sleep=sleep, monotonic=monotonic)

def api_facts(self, password: str) -> ApiFacts:
    """Establish both API facts from a single session."""
    return self._api.facts(password)
```

The `api` parameter is injected with a default so `FtlApi` can be faked in
`Pihole`'s tests without a snap, and vice versa.

**Moves to `ftl_api.py`:** the nine private methods above, plus `ApiSession`,
`ApiUnavailableError`, `SessionOutcome`, `BlockingProbe`, `classify_auth_status`,
`is_transient`, `classify_blocking`, and the `PASSWORD_SETTLE_*` constants.

**Stays in `pihole.py`:** everything snapd, `_read_toml`, `_ftl_config_value`,
`_in_container`, `install_remedy`, `Runner`, `SnapLike`, `_subprocess_run`.

**Unchanged:** `PiholeFacts`, `fetch`, `compute` and `charm.py` are not edited.

**Shared names live in `pihole_state.py`.** `AdminPasswordState` and `ApiFacts`
were already there. `SNAP_NAME`, `SNAP_DATA`, `PIHOLE_TOML`, `CLI_PW` and
`PWHASH_KEY` move there too, because both modules need them and the pure core is
the only place that imports neither, so it is the one reachable without a cycle.
This is a correction to the first draft of this ADR, which said they stayed in
`pihole.py` — that would have forced either a cycle or five duplicated constants.

**`FtlApi` takes no `run`.** The first draft copied `run: Runner` from `Pihole`'s
constructor. `FtlApi` only speaks HTTP and never shells out, so the parameter is
dead and is omitted.

**Timeouts cross the seam as `ApiTimeoutError`.** `PiholeError` stays in
`pihole.py`, so `ftl_api.py` cannot raise it without a cycle. `FtlApi.await_ready`
raises its own `ApiTimeoutError`, and `Pihole.await_api` converts it, preserving
the original message and remedy. Callers see no change.

**Tests:** `tests/unit/test_ftl_api.py` is new and covers the nine moved methods
directly. `test_pihole.py` keeps its cases for the four delegations and loses the
session-level cases that move.

---

## 5. Consequences

### Positive

- `pihole.py` drops from 978 to **566 lines** and `Pihole` to 22 methods, of which
  the API surface is 4 forwarding lines. `ftl_api.py` is 515 lines. The first draft
  predicted ~730 for `pihole.py`; the actual reduction is larger, because the move
  took the module-level session types and classifiers with it.
- `FtlApi` is testable without snapd, which is the half that has the settle
  window, the session budget and the retry semantics — the parts most likely to
  need a regression test.
- Stage 2, 3 and 7 additions land in a module that is no longer sharing a file
  with an HTTP client.

### Negative

- Four methods that do nothing but forward. If a fifth API method is ever needed
  by the charm, the temptation is to skip the facade, and then we have B by
  accident and no ADR saying so.
- A third module to keep `ops`-free, and a third place `charm-reviewer` must check
  for the layering rule.
- One more `Protocol`-shaped seam (`api` injected into `Pihole`), which is the
  kind of indirection ADR-0003 §2.7 warns against when there is no second
  implementation. Justified here only because the tests are that implementation.
- Churn against untracked code. `src/` is staged but not committed, so this lands
  on top of work with no release history to bisect against.
