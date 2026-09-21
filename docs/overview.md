# The shape of this charm

A two-minute map: the pattern this charm applies, and what each file in `src/`
is for. For the pattern taught from scratch with a smaller example, read
[`pattern.md`](pattern.md); for the core module in detail,
[`implementation/pihole_state.md`](implementation/pihole_state.md).

---

## The pattern: fetch → compute → apply

A Juju hook starts with no memory, runs once, and exits. So instead of asking
*"what just happened?"*, the charm asks **"what is true now, and what should be
true?"** — and closes the gap. Three steps, one job each:

```python
config = self.load_config(PiholeConfig, errors="blocked")   # what the operator asked for
match _intent_from(self._ensure_password(), config):        # + what the charm owns
    case PiholeIntent() as intent:
        state = fetch(self._pihole, intent.admin_password)  # 1. read the machine, once
        for outcome in compute(state, intent):             # 2. decide, touching nothing
            self._apply(outcome)                           # 3. act, deciding nothing
```

Three pieces of vocabulary:

| | What it is | Where it comes from |
|---|---|---|
| **intent** | what *should* be true | Juju: config, secrets, and charm policy |
| **state** | what *is* true | the machine, read **exactly once** |
| **outcomes** | the plan, as values | a union of frozen dataclasses — `compute` returns, never acts |

What it buys: `compute` is a pure function, so **everything the charm decides is
tested without a single mock**. And every step answers both questions with
*nothing*: what breaks if this runs twice? what breaks if it never runs?

## The files in `src/`

```
charm.py          ← the imperative shell: ops, and nothing else
pihole_config.py  ← the operator's vocabulary (pydantic)
pihole_state.py   ← the PURE CORE: intent, state, outcomes, fetch, compute
pihole.py         ← the workload: snapd, commands, pihole.toml
ftl_api.py        ← the workload: FTL's HTTP API client
resolved.py       ← the environment: systemd-resolved and port 53
```

The charm provides one optional relation, cos-agent (ADR-0008): relate it to
opentelemetry-collector and the subordinate takes the snap's logs — through
its read-only `logs` content slot — and host metrics.

**[`charm.py`](../src/charm.py)** — Observes events and nothing more. Every
deferrable event routes to one `_reconcile`; only events that **cannot be
deferred** get a handler of their own (`collect_unit_status`, `remove`, and the
actions). It holds `_apply`, which is deliberately stupid: one `match`, one
effect per branch, and `assert_never` so that a new outcome fails
`tox -e static` instead of being skipped in silence. **The only module that
imports `ops`.**

**[`pihole_state.py`](../src/pihole_state.py)** — The functional core. It imports
nothing that touches the machine: no `ops`, no `subprocess`, no `charmlibs`. It
holds `PiholeIntent`, the `PiholeState` and `PiholeOutcome` unions, `fetch` and
`compute`. It reaches the workload only through the `PiholeFacts` protocol, and
**that inverted import is the heart of the pattern**: it is what makes reading
`compute` tell you everything the charm does, and testing it need no mocks.

**[`pihole.py`](../src/pihole.py)** — Every effect on the machine, and every
read-back. Never imports `ops`. Its rule: **an exit code is not evidence** — the
snap returns 0 on keys it silently drops, so each mutation re-reads the state it
claimed to produce.

**[`ftl_api.py`](../src/ftl_api.py)** — FTL's HTTP client: sessions,
authentication, readiness. Split out of `pihole.py` because they are two
mechanisms that lie in two different ways ([ADR-0009](adr/0009-ftl-api-client-module.md)).

**[`resolved.py`](../src/resolved.py)** — The special case: it manages the
**host**, not the workload. Pi-hole needs port 53 and on Ubuntu
`systemd-resolved` holds it. It is the only module whose most important function
(`restore()`) lives outside the reconciler — giving the port back is not a step
toward a working Pi-hole, it is what you do when there will not be one.

**[`pihole_config.py`](../src/pihole_config.py)** — The pydantic model of the
config options. Imports pydantic and stdlib, nothing else.

## The boundary that makes it work

**`charm.py` is the only module that knows about `ops`; `pihole_state.py` is the
only one that decides; the rest only read and write.**

That is not a style preference. `ops.testing` offers no way to fake snapd,
systemd or `subprocess`, so the seam has to exist in our own code — and it is
the whole reason the decision logic can be tested with plain `pytest`. Two
objective signals that it has broken: a test of `charm.py` that patches
`subprocess` or `charmlibs`, and a test of `compute()` that needs a mock.

Why each rule exists lives in [`adr/`](adr/); what the workload actually does,
verified, lives in [`snap-constraints.md`](snap-constraints.md).
