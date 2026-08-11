# ADR-0002: Tech Stack and Repository Architecture

**Status:** Accepted
**Date:** 2026-08-07
**Accepted:** 2026-08-08
**Related:** [ADR-0001: Charm Scope and Specification](0001-charm-scope-and-specification.md), [ADR-0003: Reconciler and Functional Core](0003-reconciler-and-functional-core.md)

---

## 1. Context

ADR-0001 specifies a single-unit Ubuntu machine charm for the
`pihole-by-rajannpatel` snap. This ADR fixes the toolchain, the packaging
metadata, the target base, and the module layout.

Two constraints make these choices less free than they look:

1. **The charm only ever runs on the base image's Python interpreter.** There is
   no scenario in which it runs on anything else, so the `requires-python` floor
   is a statement about the base, not about compatibility breadth.
2. **The module layout is what makes the charm testable.** `ops.testing` offers
   no mocking of apt, snap, systemd, or `subprocess` — `Container` and `Exec`
   are Kubernetes-only. So the seam has to exist in *our* code or it does not
   exist at all.

---

## 2. Decisions

### 2.1 Base: `ubuntu@26.04`, platforms `amd64` + `arm64`

```yaml
base: ubuntu@26.04
platforms:
  amd64:
  arm64:
```

`bases:` is deprecated; `base:` + `platforms:` is current. The shorthand
(`amd64:` with a null value) expands to `build-on: [amd64], build-for: [amd64]`.

**Single-base, not multi-base.** Multi-base is supported
(`platforms: {ubuntu@24.04:amd64:, ubuntu@26.04:amd64:}`, which forbids a
top-level `base`) but it doubles the revisions per release and forces the code to
stay correct on 3.12 *and* 3.14 permanently. A charm with no installed user base
does not need that. Reserve multi-base for when real users cannot move.

No `build-base` is needed: 26.04 is an LTS, not a development base.

### 2.2 Why 26.04 and not 24.04 — correcting an earlier error in this ADR

An earlier revision of this ADR chose 24.04 on the grounds that
`opentelemetry-collector` publishes no 26.04 revision, concluding that *"26.04
today is a charm with no COS integration."*

**That conclusion was wrong, and the error was conflating the charm's base with
the availability of one subordinate.** The `opentelemetry-collector` gap does not
gate the base — it gates **Stage 5 only**
([roadmap](../roadmap.md)). Stages 0 through 4 have no dependency on it
whatsoever: they install a snap, free port 53, apply FTL configuration, and set a
password. Choosing 26.04 therefore does not mean a charm without COS; it means a
charm whose COS *stage* waits on a subordinate revision that is already in
flight.

The asymmetry decides it:

| Choice | Cost |
|---|---|
| **24.04 now, migrate later** | Rewrite `charmcraft.yaml` and `pyproject.toml`, re-lock, re-run everything on a new interpreter, and **discover any 3.14 incompatibility after the code is written**. Publish a new track, freeze the old one. Churn paid *after* the code exists. |
| **26.04 now** | Stage 5's integration test waits for the otelcol revision. Integration tests must run in LXD **VMs** rather than containers (§2.2.2). |

Writing the code against the interpreter it will actually run on, from the first
commit, is worth more than either residual cost.

#### 2.2.1 Verified ready (2026-08-07, empirically, not from documentation)

| Claim | Evidence |
|---|---|
| 26.04 deploys on this LXD controller | `juju deploy ubuntu --base ubuntu@26.04` on Juju 3.6.25 → `active/idle` |
| 26.04 ships Python **3.14.4** | `python3 --version` in the unit |
| 3.14 is the **only** interpreter in the archive | `apt-cache search '^python3\.[0-9]+$'` returns `python3.14` alone — **there is no fallback interpreter** |
| snapd is the same series as 24.04 | `2.76+ubuntu26.04.3` vs `2.76+ubuntu24.04.1` |
| `ruff` accepts `target-version = "py314"` | ruff 0.16.2, PEP 695 `type X = A \| B` passes `check` and `format --check` |
| `pyright` accepts `pythonVersion = "3.14"` | strict mode, 0 errors on PEP 695 syntax |
| `pydantic-core` has cp314 Linux wheels | 20 wheels on 2.48.0 |
| `ops` and `tenacity` are pure-Python | `py3-none-any`, so version-agnostic |
| The Pi-hole snap runs on 26.04 | installed and started in a 26.04 LXD **VM**; `core26` base snap resolves natively |
| Host and workload align | the snap is `base: core26`; on 24.04 we were running a core26 snap on a 24.04 host |

#### 2.2.2 Residual risk 1 — the snap cannot be installed in a 26.04 LXD *container*

Verified, and this is the sharpest cost of the decision:

```
mount: /snap/snapd/27591: wrong fs type, bad option, bad superblock
       on /var/lib/snapd/snaps/snapd_27591.snap
```

Neither the 24.04 nor the 26.04 container has `/dev/loop*`. On **24.04** snapd
falls back to its fuse mounter and every snap is mounted `type fuse.snapfuse`. In
a **Juju-created 26.04 container** it attempts a kernel squashfs mount of the
`snapd` snap, fails, and **no snap can be installed** — the bootstrap mount is the
one that breaks.

**Narrowed 2026-08-11, after a field failure.** An earlier version of this section
claimed 26.04 containers cannot mount snaps at all. That is **too broad and
false**: a plain `lxc launch ubuntu:26.04` installs snaps fine, via
`fuse.snapfuse`. The difference is the **`snapd` snap itself**:

| Container | `snapd` snap present | `snap install` |
|---|---|---|
| `lxc launch ubuntu:26.04` | **yes** (seeded, mounted `fuse.snapfuse`) | succeeds |
| Juju-created 26.04 | **no** | fails |

Both have `/usr/bin/snapfuse`, neither has loop devices, and both report `lxc` from
`systemd-detect-virt`. So snapd's fuse fallback works *once `snapd` is installed*;
what fails is the initial mount of the `snapd` snap, which is attempted as a kernel
squashfs mount. A Juju container has to perform that bootstrap because it does not
arrive with the snap seeded.

**Bug, not a 26.04 feature — and not in the image.** It is a fallback-selection
defect in snapd's bootstrap path inside a container. Worth reporting upstream; see
[BACKLOG.md](../BACKLOG.md). **NOT VERIFIED:** why snapd picks fuse for later mounts
but not for the bootstrap one.

The operational consequence for us is unchanged: **integration tests and manual
deployments must use LXD VMs.**

In a 26.04 LXD **VM** (`virt-type=virtual-machine`, which has
`/dev/loop-control`) everything installs normally.

**Consequence: integration tests must use LXD VMs.** That is a real cost — VMs are
slower and heavier than containers — but note it is a cost we were largely going
to pay anyway: this charm binds port 53 and rewrites `/etc/systemd/resolved.conf.d`,
which conflicts with a container's own resolver. The testing guidance already
warned that a dedicated machine would likely be needed. The base decision brings
that forward rather than creating it.

This is snapd/26.04 ecosystem lag, not a defect in our charm or in the Pi-hole
snap. Worth reporting upstream; tracked in [BACKLOG.md](../BACKLOG.md).

#### 2.2.3 Residual risk 2 — `opentelemetry-collector` has no 26.04 revision yet

State on 2026-08-07:

- [`canonical/opentelemetry-collector-operator#369`](https://github.com/canonical/opentelemetry-collector-operator/pull/369)
  *"feat: add ubuntu@26.04 bases"* — **open, unmerged**, `mergeable_state:
  unstable`, no reviews, 4 additions in 1 file.
- Charmhub publishes 22.04 and 24.04 only, across **every** channel including
  `dev/edge`.

So it is in flight but not landed. Since Juju enforces base compatibility between
a principal and its subordinates, until a 26.04 revision is published the
`cos-agent` integration cannot be exercised without `--force-base`.

**This is a Stage 5 precondition, not a base blocker.** Re-check with:

```bash
curl -s "https://api.charmhub.io/v2/charms/info/opentelemetry-collector?fields=channel-map" \
  | python3 -c "import json,sys;print(sorted({b['channel'] for e in json.load(sys.stdin)['channel-map'] for b in (e['revision']['bases'] or [])}))"
```

Stage 5 can still be *implemented and unit-tested* without it; only the
integration test blocks.

#### 2.2.4 Rules that keep the choice sound

- Never put `/` in a part name — forbidden on 26.04 and later bases.
- Never use the legacy `charm` plugin — it **does not exist** on the 26.04 base.
  `uv` is the only valid choice.
- CI runs on **3.14 only**. Keeping 3.12 in the matrix would test an interpreter
  the charm never runs on, and would silently forbid 3.13+ syntax for no user's
  benefit.

### 2.3 Python 3.14

`requires-python = ">=3.14"`, `ruff target-version = "py314"`, `pyright
pythonVersion = "3.14"`.

The reasoning is the constraint stated in §1: the charm only ever runs on the
base image's interpreter, and on `ubuntu@26.04` that is 3.14 — **the only Python
in the archive**, with no fallback. Declaring a lower floor would describe a
configuration that never exists in production while constraining the syntax we
may use.

For the same reason, do not write code that merely *tolerates* older
interpreters. Earlier drafts of this ADR targeted 3.12 for 24.04, where the
binding constraint was that `>=3.10` breaks the functional style
[ADR-0003](0003-reconciler-and-functional-core.md) mandates:

```
pyright pythonVersion=3.10 → error: Type alias statement requires Python 3.12 or newer
ruff    target-version=py310 → Cannot use `type` alias statement on Python 3.10
```

### 2.4 Toolchain: `uv` + `tox` + `ruff` + `pyright`

No `pip`, no `poetry`, no `black`/`isort`/`flake8`.

```yaml
parts:
  charm:
    plugin: uv
    source: .
    build-snaps:
      - astral-uv
```

**`parts:` is not optional in practice.** Omit it and charmcraft applies the
legacy `charm` plugin, which builds from `requirements.txt`. `UV_FROZEN` defaults
to `true` in the `uv` plugin, so `uv.lock` must exist and is the only source of
truth. `UV_PYTHON_DOWNLOADS=never` and `UV_PYTHON_PREFERENCE=only-system` mean
the build uses the base's interpreter — which is exactly why §2.3 pins 3.14.

`tox` environments: `fmt`, `lint`, `static`, `unit`, `integration`, `lock`, and
`flaplint` **deliberately outside `env_list`** (it catches a real defect class but
is a young single-maintainer project with no PyPI release; both its git tag and
its `--python` pin are load-bearing).

### 2.5 Dependencies, and where they are allowed to live

Runtime, in `pyproject.toml`:

| Package | Why |
|---|---|
| `ops~=3.8` | Only 3.8 is Active inside 3.x; the policy requires the latest minor for fixes. `~=3.7` resolves to 3.8 today but documents the wrong floor. |
| `charmlibs-snap`, `charmlibs-systemd` | `charms.operator_libs_linux.*` is **deprecated**. |
| `pydantic>=2,<3` | Config, databags, and the TOML subset. v2 accumulates validation errors, which covers multi-error config validation with no `Result` type. |
| `tenacity` | Bounded in-hook retry for the snap store. |

**No `tomli`** — `tomllib` has been stdlib since 3.11 and we only ever *read* TOML.

Dev group: `ops[testing]`, `pytest`, `pytest-cov`, `coverage[toml]`, `jubilant`,
`pytest-jubilant`, `ruff`, `pyright`. Note `ops[harness]` exists only to ease
migration; `Harness` is legacy and must not appear in new code.

**`charm-libs:` in `charmcraft.yaml` is only for Charmhub-hosted libraries.**
Exactly one qualifies (`grafana_agent.cos_agent`, ADR-0008) because no PyPI
replacement exists. Everything else goes in `pyproject.toml`. Note the `uv`
plugin does **not** install transitive `PYDEPS` of Charmhub libraries — add them
by hand.

### 2.6 Lint and type configuration that encodes the non-negotiables

Two settings are not stylistic; they are the machine-checked part of `AGENTS.md`:

- **`PLC0415` must be in `select`.** `E402` only catches late *module-level*
  imports and lets `def f(): import x` through. `PLC0415` is what actually
  enforces non-negotiable #3. That rule exists because a Juju hook runs once and
  exits: an import inside a rarely-taken branch fails in production, in that
  branch, against a venv the tests never exercised.
- **`line-length = 99` **and** `max-doc-length = 72`.** PEP 8 grants 99 *on the
  condition* that comments and docstrings stay at 72. Both limits enforced, or
  neither is.

`pyright` runs `typeCheckingMode = "strict"` with `pythonVersion = "3.14"`.
Coverage: `branch = true`, `source = ["src"]`, `fail_under = 90`.

### 2.7 Module layout

```
charmcraft.yaml
pyproject.toml
uv.lock
tox.ini
.jujuignore              # includes /.opencode
icon.svg
README.md
CONTRIBUTING.md
docs/                    # ADRs, roadmap, snap-constraints, backlog
lib/charms/grafana_agent/ # vendored third-party. Never edited, never linted.
src/
  charm.py               # ops only: observe -> _reconcile -> collect_unit_status
  pihole.py              # snap/systemd/filesystem. Never imports ops.
  pihole_config.py       # pydantic models, charm config -> FTL key mapping. Pure.
  pihole_state.py        # frozen snapshot + outcome ADT + pure compute(). No IO.
  resolved.py            # systemd-resolved drop-in. Never imports ops.
  grafana_dashboards/
  prometheus_alert_rules/
  loki_alert_rules/
tests/
  unit/{conftest.py,test_charm.py,test_pihole.py,test_pihole_config.py,test_pihole_state.py}
  integration/{conftest.py,test_deploy.py}
```

The `charm.py` / `pihole.py` split is mandatory and comes from the official
machine-charm guidance. Rules:

- **`pihole.py`** never imports `ops`; owns every call to `snap`, `systemd`,
  `subprocess`, and every filesystem write; knows nothing about relations,
  config options, or statuses.
- **`charm.py`** never imports `charmlibs.*` or `subprocess` and never writes
  files; translates config into arguments and return values into statuses.
- **`pihole_state.py`** is separate from `pihole.py` on purpose: it is the one
  module with *zero* IO imports. See ADR-0003.

**Detecting a broken boundary is cheap and objective:** a test of `charm.py` that
patches `subprocess` or `charmlibs`, or a test of `compute()` that needs
`monkeypatch`. Either one is a design defect, not a test problem.

`src/{grafana_dashboards,prometheus_alert_rules,loki_alert_rules}/` are the
`COSAgentProvider` **defaults** — see ADR-0008 for why we pass no path arguments.

---

## 3. Consequences

### Positive

- `uv` + 3.14 targets the interpreter the charm actually runs on from the first
  commit, so no 3.12→3.14 migration is ever paid, and no 3.14 incompatibility can
  be discovered after the code exists.
- Host and workload finally agree: the snap is `base: core26` and the base is
  26.04.
- The five-module split makes unit tests possible without patching `subprocess`,
  which is the failure mode that rots charm test suites.
- `PLC0415` and `max-doc-length` turn two `AGENTS.md` rules from review burden
  into build failures.
- Every readiness claim in §2.2.1 was verified by running it, not by reading
  documentation — including three that contradicted an earlier draft.

### Negative

- Five modules for one workload is more structure than a small charm usually
  carries; it only pays off because of ADR-0003 and ADR-0004.
- `typeCheckingMode = "strict"` will reject third-party stubs and force
  `# pyright: ignore` comments at the `charmlibs` boundary.
- **Integration tests must run in LXD VMs, not containers** (§2.2.2), which makes
  them slower and heavier. Every test fixture needs
  `constraints="virt-type=virtual-machine"`, and a contributor who forgets it gets
  an opaque snapd mount failure rather than a clear message.
- **Stage 5 cannot be integration-tested until `opentelemetry-collector` publishes
  a 26.04 revision** (§2.2.3). The PR is open but unmerged with failing CI, so the
  timing is outside our control. Stages 0–4 are unaffected.
- Being on the newest LTS means we hit ecosystem lag first, and the snapd
  container bug is proof that we will keep finding it. That is the cost of not
  being one release behind.
- **A green `tox -e lint,static,unit` is not evidence of compliance** with
  non-negotiables 1, 2, 4, 6, 7, or 8. Those need `charm-reviewer`. The
  toolchain can enforce style; it cannot enforce architecture.
