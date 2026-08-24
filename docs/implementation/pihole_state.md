# `src/pihole_state.py` — the functional core

**Module:** [`src/pihole_state.py`](../../src/pihole_state.py)
**Decided by:** [ADR-0003: Reconciler and functional core](../adr/0003-reconciler-and-functional-core.md),
[ADR-0009 §4](../adr/0009-ftl-api-client-module.md) (shared names)
**Pattern:** [The reconciler with a functional core](../pattern.md)

---

## Purpose

This is the only module in the charm with **zero IO imports**. It holds the
charm's entire decision logic and the vocabularies that logic is written in.

It imports neither `ops` nor any workload module, which has two consequences:

1. Every function in it is testable with plain `pytest` and no mocks. See
   [`tests/unit/test_pihole_state.py`](../../tests/unit/test_pihole_state.py).
2. It is the only place a name shared by two workload modules can live without a
   cycle. `pihole.py` and `ftl_api.py` both import it, and neither imports the
   other for anything but the `FtlApi` class itself. This is why the snap path
   constants live here rather than in `pihole.py` (ADR-0009 §4).

Reading `compute`, `_bootstrap` and `_converge` — 56 lines — tells you everything
the charm does.

---

## Design

Five sections, each behind a `# --` divider in the file.

### Observed facts

`ServiceStatus` (what snapd reports), `ApiFacts` (what one API session answers),
and `AdminPasswordState` — a four-variant union mapping the four measured oracle
outcomes from ADR-0007 §4.3:

| Observation | Variant |
|---|---|
| `pwhash` empty | `PasswordUnset` |
| `POST /api/auth` → 200 | `PasswordAccepted` |
| → 401 | `PasswordRejected` |
| → 429, or API unreachable | `PasswordUnverified` |

`PasswordUnverified` is a fourth case rather than a `bool` because "we could not
check" is not "it is wrong". Three separate `match` statements group these four
differently, because they answer three different questions — which message to
show an operator, whether to retry, and whether to reapply the password.

### The state

```python
type PiholeState = SnapAbsent | SnapPresent
```

`SnapAbsent` carries no fields. `SnapPresent` carries the eight facts that only
exist once the snap is installed. Nothing can construct "not installed, but the
webserver port is 80".

### The intent

`PiholeIntent`, one field for Stage 1: `admin_password`, declared
`field(repr=False)` so the password cannot reach a log line through a `repr()`.

### The outcomes

`PiholeOutcome`, seven variants: `ReleasePort53`, `InstallSnap`,
`SetWebserverPort`, `SetAdminPassword`, `StartFtl`, `AwaitApi`, `Noop`. Each is a
value, not an action; `charm.py`'s `_apply` is the only thing that turns one into
an effect.

### The effect boundary and the two functions

`PiholeFacts` is a `Protocol` of six reads. Two implementations exist: `Pihole`
in production, and `FactsStub` in the tests.

`fetch` is the charm's **only** impure read path, and it short-circuits: if
`installed_revision()` is `None` it returns `SnapAbsent()` without reading
anything else, because none of the other facts are knowable.

`compute` dispatches on the state union:

- `SnapAbsent` → `_bootstrap`, which returns a **fixed six-outcome tuple**. The
  order is the correctness condition and is stated once, literally, in that
  function.
- `SnapPresent` → `_converge`, which appends conditionally in the same order,
  minus whatever is already true.

---

## Edge cases

| Case | Behaviour | Why |
|---|---|---|
| `webserver_port()` returns `None` (file missing, unparseable TOML) | Recorded as `""` | `""` never equals `WEBSERVER_PORT`, so an unreadable port is treated as wrong and corrected. Mistaking it for correct would skip the fix and leave the webserver dead. |
| `version` is `None` on an installed snap | `SnapPresent.version: str \| None` | The snap may declare no version. `charm.py` matches `version=str() as version` so it only reports a real one. |
| A port correction is needed | `AwaitApi()` is appended too, even if `api_ready` was `True` | Changing the port restarts FTL (snap-constraints §4), so the step carries its own readiness gate instead of leaving an unguarded bounce for the next status check. |
| `PasswordUnverified` | **Not** reapplied | A hash is already set; rewriting it while the daemon is down is churn, and the salt means the write cannot be verified anyway. |
| `PasswordUnset` | Always reapplied | An empty `pwhash` means FTL accepts *any* password, so the config API is open to the network. |
| Fully converged machine | `(Noop(),)`, never `()` | An empty sequence and "nothing to do" are different claims. `Noop` makes the second one explicit and gives `_apply` something to log. |
| A new variant added to any union | `tox -e static` fails | Every `match` ends in `case _ as unreachable: assert_never(unreachable)`. Without it, `_apply` returns `None` and the charm reports success having skipped the new outcome. |

---

## Testing strategy

[`tests/unit/test_pihole_state.py`](../../tests/unit/test_pihole_state.py) — 15
test functions, 22 collected cases, **zero mocks**: no `monkeypatch`, no
`unittest.mock`, no snap. `FactsStub` implements `PiholeFacts` with plain
attributes and counts its reads.

That the file needs no mocks is not a convenience, it is the acceptance test for
the whole design. If a test here ever needs one, the decide/act split has broken
upstream.

Four groups:

| Group | Representative test |
|---|---|
| The bootstrap sequence and its order | `test_the_bootstrap_order_is_the_correctness_condition` |
| Drift: one wrong fact yields exactly one outcome | `test_one_drifted_fact_yields_exactly_one_outcome` (parametrized) |
| Password policy | `test_an_unverifiable_password_is_left_alone`, `test_an_empty_pwhash_is_always_reapplied` |
| `fetch` discipline | `test_fetch_reports_an_uninstalled_machine_without_reading_further`, `test_fetch_reads_every_fact_exactly_once` |

Two tests exist specifically to defend properties that no linter checks:

- `test_fetch_reads_every_fact_exactly_once` — the "fetch once" rule. A second
  read appearing anywhere would fail it.
- `test_the_password_never_appears_in_a_repr` — the `field(repr=False)` on
  `PiholeIntent.admin_password`. Removing it is a one-character change that would
  otherwise leak a credential into `juju debug-log`.
