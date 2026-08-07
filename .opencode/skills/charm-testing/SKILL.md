---
name: charm-testing
description: >-
  Use when writing or reviewing tests — unit tests with ops.testing (Scenario),
  conftest fixtures, coverage gates, or integration tests with jubilant on LXD.
  Covers Model(type='lxd'), the two-layer mocking strategy, and jubilant APIs
  that only exist for machine models. Load before writing any test file.
metadata:
  verified: "2026-08-06"
---

# Testing a machine charm

Three layers, mirroring the source design. Do not mix them.

| Layer | Tool | What you mock |
|---|---|---|
| Pure decision (`compute`, config mapping) | plain `pytest` | **nothing** |
| State transition (`charm.py`) | `ops.testing` `Context` + `State` | `src.pihole` — the whole module |
| Workload (`pihole.py`) | plain `pytest` | `snap.SnapCache`, `subprocess.run`, filesystem |
| Integration | `jubilant` + `pytest-jubilant` on LXD | nothing |

## Layer 0 — pure functions need no test infrastructure

This is the payoff of the functional split (see `charm-functional-style`). A
function with the shape `compute(state: PiholeState, intent: PiholeConfig) ->
Sequence[PiholeOutcome]` has no `self`, no IO, and no exceptions used for control
flow. Testing it is construction and `==`, because frozen dataclasses give you
`__eq__`:

```python
def test_absent_snap_yields_install():
    # GIVEN a machine with no snap installed
    state = SnapAbsent()

    # WHEN the outcome is computed
    outcomes = compute(state, PiholeConfig(snap_revision=1348))

    # THEN the only action is to install it at the pinned revision
    assert outcomes == (InstallSnap(revision=1348),)


def test_unchanged_config_yields_noop():
    # GIVEN a running snap whose FTL config already matches intent
    state = SnapPresent(revision=1348, ftl_running=True,
                        ftl_config={"dns.upstreams": '["1.1.1.1"]'}, ...)

    # WHEN the outcome is computed with the same intent
    outcomes = compute(state, PiholeConfig(upstream_dns=("1.1.1.1",)))

    # THEN nothing happens — this is the "safe to run twice" proof
    assert outcomes == (Noop(),)
```

**Put as much logic as possible in this layer.** Every decision that lives here is
a decision tested without a mock, and mocks are where charm test suites rot.

**If a test at this layer needs `monkeypatch`, the function is not pure** — the
decide/act split is wrong. Fix the code, not the test.

Coverage tends to concentrate here naturally, which is the right shape: the
failure paths are decisions, and decisions are cheap to enumerate.

## The trap that will get you at layer 1

`ops.testing.Model.type` is `Literal['kubernetes', 'lxd']` and **defaults to
`'kubernetes'`**
([ops.testing reference](https://canonical.com/juju/docs/ops/latest/reference/ops-testing/)).
A machine charm tested with the default is running in the wrong environment and
will not surface machine-specific behaviour.

```python
state = testing.State(model=testing.Model(type="lxd"))
```

Put this in a `conftest.py` fixture so it cannot be forgotten.

There is **no machine equivalent of `Container` or `Exec`.** `Container` is
documented as *"A Kubernetes container where a charm's workload runs"*, and `Exec`
only exists inside `Container.execs`. `ops.testing` offers **no** mocking of
apt/snap/systemd/subprocess. That is not a gap to work around — it is why the
two-layer design exists.

Also machine-specific: `Storage.index` is always 1 on Kubernetes, but increments
per instance on machines.

## Layer 1 — state transition tests

```python
# tests/unit/conftest.py
from unittest.mock import MagicMock

import pytest
from ops import testing

import charm


@pytest.fixture
def mock_pihole(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the whole workload module with a mock."""
    mock = MagicMock()
    mock.installed = True
    mock.blocking_ready.return_value = True
    mock.diagnose.return_value = charm.Diagnosis(port_conflict=False, message="")
    monkeypatch.setattr(charm, "Pihole", lambda *a, **kw: mock)
    return mock


@pytest.fixture
def ctx() -> testing.Context[charm.PiholeCharm]:
    return testing.Context(charm.PiholeCharm)


@pytest.fixture
def base_state() -> testing.State:
    """A machine model, not the kubernetes default."""
    return testing.State(model=testing.Model(type="lxd"), leader=True)
```

```python
# tests/unit/test_charm.py
def test_config_changed_applies_upstreams(ctx, base_state, mock_pihole):
    # GIVEN a unit configured with two upstream resolvers
    state_in = dataclasses.replace(
        base_state, config={"upstream-dns": "9.9.9.9,149.112.112.112"}
    )

    # WHEN config-changed fires
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # THEN the workload module receives them and the unit goes active
    applied = mock_pihole.apply_config.call_args.args[0]
    assert applied.upstream_dns == ["9.9.9.9", "149.112.112.112"]
    assert state_out.unit_status == testing.ActiveStatus()
```

```python
def test_reconcile_is_idempotent(ctx, base_state, mock_pihole):
    # GIVEN a converged unit
    state = ctx.run(ctx.on.config_changed(), base_state)

    # WHEN the same event fires again
    state = ctx.run(ctx.on.config_changed(), state)

    # THEN nothing regresses
    assert state.unit_status == testing.ActiveStatus()
```

Test both directions of the non-negotiable: **what breaks if it runs twice**
(the test above) and **what breaks if it never runs** (assert the status is
`Waiting`/`Blocked`, not `Active`, when a step has not happened).

## Layer 2 — workload module tests

Patch what `pihole.py` actually calls. Official pattern:

```python
def test_install_uses_snap_cache(monkeypatch: pytest.MonkeyPatch):
    # GIVEN a fake snap cache
    fake_snap = MagicMock()
    monkeypatch.setattr(
        "pihole.snap.SnapCache",
        lambda: {"pihole-by-rajannpatel": fake_snap},
    )

    # WHEN the workload is installed
    pihole.Pihole().install(revision=None)

    # THEN the snap is ensured and explicitly started, because the snap ships
    # install-mode: disable
    fake_snap.ensure.assert_called_once()
    fake_snap.start.assert_called_once_with(enable=True)
```

```python
def test_set_ftl_key_raises_when_value_does_not_land(monkeypatch):
    # GIVEN a snap that accepts the set but drops the value (the dnssec bug)
    monkeypatch.setattr("pihole.subprocess.run", lambda *a, **kw: _ok())
    monkeypatch.setattr(pihole.Pihole, "_read_toml_key", lambda self, k: "false")

    # WHEN a key is set
    # THEN the charm refuses to believe the exit code
    with pytest.raises(pihole.PiholeError, match="reads back as"):
        pihole.Pihole().set_ftl_key("dns.dnssec", "true")
```

That second test is not paranoia — it encodes a real, verified defect. Every
snap interaction in `pihole.py` deserves one like it.

Also worth covering at this layer:

- `_is_snapd_safe_key` against snapd's regex, with `dns.upstreams` (reachable)
  and `dns.listeningMode` (not).
- DHCP key ordering: pool before `active`.
- systemd-resolved drop-in written on install and removed on `remove`.
- v6 command syntax — assert the code never emits `pihole -a -p` or
  `pihole restartdns`.

## Testing actions and status

**`ctx.run_action` does not exist.** It was removed and raises `AttributeError`
with the replacement in the message. Actions go through `ctx.run` like everything
else:

```python
def test_update_gravity_reports_entry_count(ctx, base_state, mock_pihole):
    # GIVEN a running unit
    mock_pihole.update_gravity.return_value = 12345

    # WHEN the action runs
    ctx.run(ctx.on.action("update-gravity", params={"force": True}), base_state)

    # THEN the entry count is returned to the operator
    assert ctx.action_results == {"entries": 12345}
    assert "Updating gravity" in ctx.action_logs
```

- `ctx.on.action(name, params=...)` — `name` uses **dashes**, as in the metadata.
- `ctx.action_results` is `None` if the charm never called `set_results`.
- `ctx.action_logs` is a list of strings.
- If the charm calls `event.fail(...)`, `ctx.run` raises `testing.ActionFailed`:

```python
with pytest.raises(testing.ActionFailed) as exc_info:
    ctx.run(ctx.on.action("update-gravity"), base_state)
assert exc_info.value.message == "gravity update failed"
```

**Testing `collect_unit_status`: prefer the indirect route.** The framework emits
it after every hook, so the resolved status is already in the output state. That
exercises the reconciler and the status handler together, with their real
interaction:

```python
state_out = ctx.run(ctx.on.config_changed(), state_in)
assert state_out.unit_status == testing.BlockedStatus("port 53 is in use by systemd-resolved")
```

`ctx.run(ctx.on.collect_unit_status(), state)` exists for isolated parametric
tables, but do not make it the primary way you test status.

`ctx.unit_status_history` gives the full sequence of intermediate statuses — useful
for asserting the unit passes through `MaintenanceStatus` during install rather than
jumping straight to Active.

Ports assert as `testing.TCPPort` / `testing.UDPPort`, which exist **only** in
`ops.testing` (production code uses `ops.Port`):

```python
assert state_out.opened_ports == {
    testing.TCPPort(53), testing.UDPPort(53),
    testing.TCPPort(80), testing.TCPPort(443),
    testing.UDPPort(123),
}
```

That asymmetry between `ops.Port` and `testing.UDPPort` is real, not a typo.

## Treat warnings as errors

Run pytest with `-W error`. `ops` itself does this in its own unit tests, and the
official how-to recommends it. It is how you find an ops deprecation while it is
still a warning instead of after it becomes a breakage.

## Coverage

`fail_under = 90` in `[tool.coverage.report]`. Coverage measures `src/` only.
Do not chase the number by testing getters; chase it by covering failure paths,
which is where this charm's risk lives.

If coverage is hard to reach, that is usually a design signal rather than a
testing problem: logic that is expensive to cover is logic sitting on the wrong
side of the decide/act boundary. Move the decision into a pure function and the
coverage follows for free.

## Determinism

Tests must not depend on iteration order. If an assertion compares a serialised
collection, sort at construction (`tuple(sorted(...))`), not in the assertion —
otherwise the test passes while the production code still flaps. This is the same
defect `flaplint` looks for; see `python-style`.

## Integration tests

`jubilant` supports machine models as a first-class case — the official machine
charm tutorial uses `jubilant` + `pytest-jubilant` on LXD.

Environment setup with Concierge:

```
sudo concierge prepare -p machine
```

```python
# tests/integration/conftest.py
import os
import pathlib

import jubilant
import pytest


@pytest.fixture(scope="module")
def charm_path() -> pathlib.Path:
    """Pack once, reuse across every test file in the run."""
    if path := os.environ.get("CHARM_PATH"):
        return pathlib.Path(path)
    pytest.skip("CHARM_PATH not set; pack the charm first")
```

```python
# tests/integration/test_deploy.py
def test_deploy_reaches_active(juju: jubilant.Juju, charm_path):
    # GIVEN a fresh machine model
    # WHEN the charm is deployed
    juju.deploy(charm_path, "pihole", base="ubuntu@24.04")

    # THEN it converges without any relations
    juju.wait(jubilant.all_active, timeout=900)


def test_dns_answers(juju: jubilant.Juju):
    # GIVEN an active unit
    # WHEN we query it over DNS from the machine itself
    result = juju.exec("dig +short @127.0.0.1 example.com", unit="pihole/0")

    # THEN we get an answer
    assert result.stdout.strip()
```

### Machine-only jubilant APIs

- `Juju.add_machine(...)` — *"Unavailable in Kubernetes clouds."* Accepts `base`,
  `constraints`, `disks`, `num_machines`, and placement directives (`lxd:25`).
- `Juju.deploy(..., attach_storage=...)` — *"Not available for Kubernetes models."*
- `Juju.deploy(..., to="lxd:25")`, `constraints=`, `num_units=`, `base=`.
- `Juju.exec(..., machine=0)` — the `machine` parameter is machine-only; `container=`
  is Kubernetes-only.
- `Status.machines` → `Mapping[str, MachineStatus]` with `base`, `hardware`,
  `hostname`, `instance_id`, `ip_addresses`, `network_interfaces`.

### Behaviour that differs from Kubernetes

`Juju.remove_unit()` takes **unit names** on machines
(`juju.remove_unit("pihole/1")`); on Kubernetes it takes the app name plus
`num_units`, because *"individual units are not named"*.

### Juju CLI names changed in 3.x — do not copy 2.9 examples

The rename is a trap because the same string means different things:

| Intent | Juju 2.9 | **Juju 3.6** |
|---|---|---|
| run an arbitrary command | `juju run` | **`juju exec`** |
| run a charm action | `juju run-action` | **`juju run`** |
| pick a series/base | `--series` | **`--base`** |

So `juju run` in a 2.9 tutorial is `juju exec` today, and `juju run` today is what
used to be `juju run-action`. Both still exist, which is why the mistake is silent.

For verifying real snap state from a test (non-negotiable #6):

```
juju exec --unit pihole/0 -- snap get pihole-by-rajannpatel ftl.dns.upstreams
```

`--unit` runs as **root inside a hook context**; `--machine` runs as `ubuntu`,
where `snap get` on config may fail on permissions. Use `--unit`.

`juju wait-for application|unit|machine|model` is the native alternative to
hand-rolled polling loops, though `jubilant`'s own wait helpers are usually
better inside tests.

### `update-status` interval

Default is **5m**, changed with `juju model-config
update-status-hook-interval=30s`. Lowering it in the test model fixture makes any
test that depends on `update-status` converge much faster.

But note the design signal: **if the charm only reaches `ActiveStatus` via
`update-status`, some event that should trigger a reconcile is not observed.**
Lowering the interval to make a test pass hides that bug rather than fixing it.

### Practical notes for this charm

- The charm rewrites `/etc/systemd/resolved.conf.d/` and binds port 53. Inside an
  LXD container that conflicts with the container's own resolver — expect to need
  a dedicated machine or careful teardown.
- Port 67 is likely occupied by LXD's `lxdbr0` dnsmasq. Gate DHCP tests behind a
  marker rather than letting them crash-loop the daemon.
- Gravity bootstrap is asynchronous and downloads a blocklist. Budget generous
  timeouts (900s) and assert on `pihole api dns/blocking`, not on unit status
  alone.
- Never pack inside the test. Pack once by hand (`charmcraft pack`), export
  `CHARM_PATH`, and reuse it. Packing is slow and every test file would repeat it.
- **Do not test `juju expose`.** The LXD provider does not implement Juju's
  `Firewaller` interface — there is no `OpenPorts`/`ClosePorts`/`IngressRules` in
  `internal/provider/lxd/`. On LXD, `expose` records a flag and `juju status`
  displays it, but nothing changes at the network level: port 53 is reachable with
  or without it. A test asserting that exposing opens the port, or that not
  exposing closes it, verifies nothing and will pass for the wrong reason. On
  MAAS/EC2/OpenStack the behaviour is different, so anything we validate on LXD
  says nothing about a real cloud. Note that limitation in the README.
- **Name relation endpoints explicitly.** `juju integrate pihole
  opentelemetry-collector` may resolve via the implicit `juju-info` endpoint rather
  than `cos-agent`. Always write `pihole:cos-agent otelcol:cos-agent`.
- Debugging tools worth knowing when a reconcile appears to hang:
  `juju show-status-log <unit>` (the full status transition history — invaluable for
  a flapping reconciler), `juju debug-log`, `juju debug-hooks`, and
  `juju_machine_lock` via `juju-introspect` on the machine. Installing a snap holds
  the machine lock, which is a common reason hooks look stuck.
