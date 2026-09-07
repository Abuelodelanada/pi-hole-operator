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

Reading `compute`, `_bootstrap`, `_converge` and their two helpers
(`_ntp_step`, `_drifted_config`) — 118 lines — tells you everything the charm
does.

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

`SnapAbsent` carries no fields. `SnapPresent` carries the thirteen facts that
only exist once the snap is installed. Nothing can construct "not installed, but
its refresh is held".

### The intent

`PiholeIntent`, six fields: `admin_password` (declared `field(repr=False)` so
the password cannot reach a log line through a `repr()`) plus the five Stage 2
config values. `None` in an optional field means *not managed*: the charm never
writes that key, and drift on it is not computed.

What `charm.py` actually holds is `DeclaredIntent = NoIntentYet | PiholeIntent`.
`NoIntentYet` is the follower case — no password minted yet, so there is nothing
to converge toward — and being a named variant rather than `None` is what keeps
`assert_never` on the two `match` sites that consume it.

### The outcomes

`PiholeOutcome`, nine variants: `ReleasePort53`, `InstallSnap`,
`HoldSnapRefresh`, `SetNtpServer`, `SetAdminPassword`, `StartFtl`, `AwaitApi`,
`SetFtlConfig`, `Noop`. Each is a value, not an action; `charm.py`'s `_apply` is
the only thing that turns one into an effect.

### The effect boundary and the two functions

`PiholeFacts` is a `Protocol` of eleven reads. Two implementations exist:
`Pihole` in production, and `FactsStub` in the tests.

`fetch` is the charm's **only** impure read path, and it short-circuits: if
`installed_revision()` is `None` it returns `SnapAbsent()` without reading
anything else, because none of the other facts are knowable.

Its second parameter is `admin_password: str` — a **measurement input**, not the
intent. A salted `pwhash` cannot be compared, so the only way to observe "does
FTL accept this password" is to offer a candidate (ADR-0007 §4.3). Taking the
candidate rather than the whole intent keeps anything *desired* out of the
function that only observes, and makes reaching for another intent field inside
`fetch` impossible rather than merely discouraged.

`compute` dispatches on the state union:

- `SnapAbsent` → `_bootstrap`, which returns a **fixed eight-outcome tuple**. The
  order is the correctness condition and is stated once, literally, in that
  function.
- `SnapPresent` → `_converge`, which appends conditionally in the same order,
  minus whatever is already true. Two helpers keep it flat: `_ntp_step` (the
  tri-state NTP comparison — unknown drifts to a correction) and
  `_drifted_config` (the FTL config diff).

---

## Edge cases

| Case | Behaviour | Why |
|---|---|---|
| `ntp_server_active()` returns `None` (unreadable TOML) | Treated as *open* | Unknown drifts toward the correction: it is idempotent and its own read-back adjudicates. Treating it as closed would leave 123/udp bound on a machine the charm could have fixed. |
| `version` is `None` on an installed snap | `SnapPresent.version: str \| None` | The snap may declare no version. `charm.py` matches `version=str() as version` so it only reports a real one. |
| The installed revision is not `SNAP_REVISION` | `InstallSnap()` again — a re-pin | The hold stops snapd's timer, but a manual `snap refresh` can still move the snap. The drift check is the second line of defence (ADR-0010). |
| An NTP correction is needed | `AwaitApi()` appended too, for the same reason | The configure hook restarts FTL whenever a *changed* value lands, so a plan that closes 123/udp cannot trust a readiness fact read before it. |
| `PasswordUnverified` | **Not** reapplied | A hash is already set; rewriting it while the daemon is down is churn, and the salt means the write cannot be verified anyway. |
| `PasswordUnset` | Always reapplied | An empty `pwhash` means FTL accepts *any* password, so the config API is open to the network. |
| Fully converged machine | `(Noop(),)`, never `()` | An empty sequence and "nothing to do" are different claims. `Noop` makes the second one explicit and gives `_apply` something to log. |
| A new variant added to any union | `tox -e static` fails | Every `match` ends in `case _ as unreachable: assert_never(unreachable)`. Without it, `_apply` returns `None` and the charm reports success having skipped the new outcome. |

---

## Testing strategy

[`tests/unit/test_pihole_state.py`](../../tests/unit/test_pihole_state.py) — 31
test functions, 43 collected cases, **zero mocks**: no `monkeypatch`, no
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
