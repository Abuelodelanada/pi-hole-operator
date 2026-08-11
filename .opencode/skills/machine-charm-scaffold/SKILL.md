---
name: machine-charm-scaffold
description: >-
  Use when creating or editing charmcraft.yaml, pyproject.toml, tox.ini,
  .jujuignore, or deciding the repo layout for this machine charm. Covers
  base/platforms syntax, the uv part plugin, config options, actions, and where
  dependencies belong. Load before running charmcraft init or hand-writing any
  of those files.
metadata:
  verified: "2026-08-06"
---

# Machine charm scaffolding

Reference implementation: `canonical/operator` →
`examples/machine-tinyproxy/`, available as the `ops` reference. Read it before
inventing structure.

## `charmcraft.yaml`

`bases:` is **deprecated**. Use `base:` plus `platforms:`.
([charmcraft.yaml reference](https://canonical.com/juju/docs/charmcraft/stable/reference/files/charmcraft-yaml-file/))

```yaml
name: pihole
type: charm
title: Pi-hole
summary: Network-wide DNS sinkhole and ad blocker.
description: |
  Pi-hole is a DNS sinkhole that blocks advertisements and trackers for every
  device on a network without per-device configuration.

  This charm deploys and operates Pi-hole v6 on Ubuntu machines (LXD, MAAS,
  clouds) using the pihole-by-rajannpatel snap.

  Key features:
  - Declarative upstream DNS, blocking mode, and web UI configuration
  - Automatic systemd-resolved port 53 orchestration
  - Optional DHCP server mode
  - Metrics, logs, dashboards, and alerts via the cos-agent interface

base: ubuntu@24.04
platforms:
  amd64:
  arm64:

assumes:
  - juju >= 3.6

parts:
  charm:
    plugin: uv
    source: .
    build-snaps:
      - astral-uv

config:
  options: {}

provides:
  cos-agent:
    interface: cos_agent
    limit: 1
    optional: true

requires: {}

actions: {}
```

Notes:

- The `platforms:` shorthand (`amd64:` with a null value) expands to
  `build-on: [amd64], build-for: [amd64]`.
  ([platforms reference](https://canonical.com/juju/docs/charmcraft/stable/reference/platforms/))
  To support two Ubuntu series at once, drop the top-level `base:` and use
  multi-base notation instead: `ubuntu-24.04-amd64: {build-on: [ubuntu@24.04:amd64],
  build-for: [ubuntu@24.04:amd64]}`.
- Supported bases today: `ubuntu@22.04`, `ubuntu@24.04`, `ubuntu@24.10`,
  `ubuntu@25.04`, `ubuntu@25.10`, `ubuntu@26.04`, `almalinux@9`.

## Why 24.04 and not 26.04 (verified 2026-08-07)

Ubuntu 26.04 LTS (*Resolute Raccoon*) shipped 2026-04-23, and **everything in our
own stack is ready for it**:

| Component | 26.04 status |
|---|---|
| Juju | supported since **3.6.17** and **4.0.6**; current stable is well past both |
| charmcraft | `base: ubuntu@26.04` valid; **`build-base` not needed** (it is LTS, not interim) |
| Python | **3.14** — and `charmlibs` CI literally lists `'3.14',  # Ubuntu 26.04` |
| `pydantic-core` | `cp314` manylinux wheels for every arch we target |
| snapd | same 2.76 series as 24.04 |
| the Pi-hole snap | already `base: core26`, so 26.04 would align host and workload |

**The blocker is a third-party charm.** `opentelemetry-collector` publishes
revisions for `22.04` and `24.04` only — **nothing for 26.04 in any channel,
including `edge`**. Juju enforces base compatibility for a principal's
*subordinates* (`state/application.go`, inside the loop over
`unit.SubordinateNames()`, gated by `if !force`). So a Pi-hole unit on 26.04 cannot
be related to `opentelemetry-collector` without `--force-base`, which means
**26.04 today is a charm with no COS integration** — contradicting this repo's own
design, where `cos_agent` is the one charm library we justify vendoring.

Not a Store limitation: the `ubuntu` charm publishes 26.04 fine. It is ecosystem
lag — no machine charm of operational significance (`postgresql`, `nrpe`,
`telegraf`, `opentelemetry-collector`) had 26.04 at 3.5 months post-release.

### The migration trigger

Check with this, not with intuition:

```bash
curl -s "https://api.charmhub.io/v2/charms/info/opentelemetry-collector?fields=channel-map" \
  | python3 -c "import json,sys;print(sorted({b['channel'] for e in json.load(sys.stdin)['channel-map'] for b in (e['revision']['bases'] or [])}))"
```

When `26.04` appears, migrate.

### Staying 26.04-ready now, at zero cost

- **Never put `/` in a part name.** Forbidden on 26.04 and later bases. Use a
  hyphen.
- **Never switch to the `charm` plugin.** It **does not exist** on the 26.04 base.
  `uv` is the only forward-compatible choice, which we already use.
- **Target Python 3.12, not 3.10.** The charm only ever runs on the base's
  interpreter — 3.12 on 24.04, 3.14 on 26.04. There is no scenario where it runs on
  3.10, and claiming `>=3.10` breaks the functional style this repo mandates:
  ```
  pyright pythonVersion=3.10 → error: Type alias statement requires Python 3.12 or newer
  ruff    target-version=py310 → Cannot use `type` alias statement on Python 3.10
  ```
  3.12 syntax is valid on 3.14, so `>=3.12` is correct for both bases.
- **Do not use anything that exists only in 3.12 or only in 3.14.** 26.04 jumps
  straight from 3.12 to 3.14 (skipping 3.13) and ships **no other Python in the
  archive** — there is no fallback interpreter to fall back to.
- When CI is added, run the matrix on `[3.12, 3.14]` — the same shape `charmlibs`
  uses. Cheap now, and it turns the migration into a non-event.

### When you do migrate: single-base, not multi-base

Multi-base is supported (`platforms: {ubuntu@24.04:amd64:, ubuntu@26.04:amd64:}`,
and it forbids top-level `base`/`build-base`), but it doubles the revisions per
release and forces the code to stay correct on 3.12 *and* 3.14 permanently. A charm
with no installed user base does not need that. Publish 26.04 on a new track and
freeze the 24.04 track. Reserve multi-base for when real users cannot move.

- **`parts:` is not optional in practice.** Omit it and charmcraft applies the
  legacy `charm` plugin, which builds from `requirements.txt` — not what we want
  with `uv`. The available plugins are `charm` (legacy default), `python`,
  `poetry`, and `uv`.
- `UV_FROZEN` defaults to `true` in the `uv` plugin, so `uv.lock` **must** exist
  and is the only source of truth. `UV_PYTHON_DOWNLOADS=never` and
  `UV_PYTHON_PREFERENCE=only-system` mean the build uses the base's Python (3.12 on
  `ubuntu@24.04`) — which is why we do not pin 3.13.
- No `k8s-api` in `assumes` — that is Kubernetes-only.
- The `description` is rendered on Charmhub. Treat it as a product page: lead
  with what the workload does, then bullet the charm's features. `summary` must be
  78 characters or fewer.
- `charm-libs:` is **only** for Charmhub-hosted libraries — the reference says so
  verbatim. Regular PyPI packages go in `pyproject.toml`. The only Charmhub-hosted
  library this charm needs is `grafana_agent.cos_agent`; see the
  `charm-cos-integration` skill. `version` must be a **string** (`"0"`, not `0`).
  Note that unlike the `charm` plugin, **the `uv` plugin does not install
  transitive `PYDEPS` of Charmhub libraries** — add them to `pyproject.toml`
  manually.


`charmcraft init --profile machine` generates exactly this shape, including the
`uv` plugin and `astral-uv` build-snap.

## `pyproject.toml`

```toml
[project]
name = "pihole-operator"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "ops~=3.8",
    "charmlibs-snap>=1,<2",
    "charmlibs-systemd>=1,<2",
    "pydantic>=2,<3",
    "tenacity>=9,<10",
]

[dependency-groups]
dev = [
    "ops[testing]",
    "pytest",
    "pytest-cov",
    "coverage[toml]",
    "jubilant>=1.12,<2",      # NOT >=2: jubilant 2.x does not exist, latest is 1.12.0
    "pytest-jubilant>=2.2,<3",
    "ruff",
    "pyright",
]

[tool.ruff]
line-length = 99
target-version = "py312"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "RUF", "ANN", "D", "PLC0415"]
# E501 is deliberately NOT ignored: PEP 8 only permits 99 chars on the condition
# that prose stays at 72, so both limits have to be enforced or neither is.
# PLC0415 is what actually enforces "no imports inside functions" — E402 only
# catches late module-level imports and lets `def f(): import x` through.
ignore = ["D105", "D107"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 72   # enables W505, the PEP 8 proviso for comments/docstrings

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["ANN"]

[tool.pyright]
include = ["src", "tests"]
pythonVersion = "3.12"
pythonPlatform = "Linux"
typeCheckingMode = "strict"

[tool.coverage.run]
branch = true
source = ["src"]

[tool.coverage.report]
fail_under = 90
show_missing = true
```

`uv.lock` is committed. Regenerate with `uv lock`, never edit by hand.

**Why `ops~=3.8` and not `~=3.7`.** ops ships a minor version roughly monthly, and
the support policy is explicit: *"To receive bug and security fixes within a major
version, charms must update to the latest minor release within that major
version."* Only **3.8** is listed as Active inside 3.x. `~=3.7` resolves to 3.8
today so it works, but it documents the wrong floor. `ops>=3.8,<4` is equivalent
and more explicit.

The only breaking change from 2.x to 3.0 was raising the Python floor to 3.10.

Extras: `ops[testing]` pulls `ops-scenario` (dev group), `ops[tracing]` pulls
`ops-tracing` (runtime, only if you want charm traces). `ops[harness]` exists only
to ease migration — `Harness` is legacy, do not use it in new code.

`charmlibs-*` packages require Python >= 3.10. `charmlibs-apt` pulls in
`opentelemetry-api`.

## `tox.ini`

```ini
[tox]
no_package = True
skip_missing_interpreters = True
env_list = fmt, lint, static, unit

[vars]
src_path = {tox_root}/src
tests_path = {tox_root}/tests

[testenv]
runner = uv-venv-lock-runner
set_env =
    PYTHONPATH = {tox_root}/src
    PYTHONBREAKPOINT = pdb.set_trace
pass_env = PYTHONPATH, CHARM_PATH, JUJU_*

[testenv:fmt]
description = Apply coding style standards
dependency_groups = dev
commands =
    ruff format {[vars]src_path} {[vars]tests_path}
    ruff check --fix {[vars]src_path} {[vars]tests_path}

[testenv:lint]
description = Check code against coding style standards
dependency_groups = dev
commands =
    ruff check {[vars]src_path} {[vars]tests_path}
    ruff format --check --diff {[vars]src_path} {[vars]tests_path}

[testenv:static]
description = Run static type checks
dependency_groups = dev
commands = pyright {posargs}

[testenv:unit]
description = Run unit tests
dependency_groups = dev
commands =
    coverage run --module pytest {[vars]tests_path}/unit {posargs}
    coverage report

[testenv:integration]
description = Run integration tests against a juju machine model
dependency_groups = dev
commands = pytest --exitfirst {[vars]tests_path}/integration {posargs}

[testenv:flaplint]
description = Detect relation-databag ordering churn (advisory, not in env_list)
skip_install = true
allowlist_externals = uvx
commands =
    uvx --python 3.12 --from git+https://github.com/michaeldmitry/flaplint@v1.1.0 \
        flaplint {tox_root}/src --own-only --min-confidence high

[testenv:lock]
description = Update uv.lock
commands = uv lock --upgrade
```

`flaplint` is deliberately **outside** `env_list`. It catches a defect class ruff
and pyright cannot see, but it is a six-week-old single-maintainer project with no
PyPI release and no LICENSE file — see `python-style` for the full risk assessment
and for the f-string blind spot. Both the git tag and `--python 3.12` are load-bearing pins.

## Layout

```
charmcraft.yaml
pyproject.toml
uv.lock
tox.ini
.jujuignore
icon.svg
README.md
CONTRIBUTING.md
src/
  charm.py            # events -> _reconcile -> collect_unit_status. No snap calls.
  pihole.py           # all snap/systemd/filesystem interaction. No ops imports.
  pihole_config.py    # pydantic models, charm config -> FTL key mapping
  resolved.py         # systemd-resolved drop-in management
  grafana_dashboards/      # COSAgentProvider default
  prometheus_alert_rules/  # COSAgentProvider default
  loki_alert_rules/        # COSAgentProvider default
tests/
  unit/
    conftest.py
    test_charm.py
    test_pihole.py
    test_pihole_config.py
  integration/
    conftest.py
    test_deploy.py
```

The `src/charm.py` / `src/pihole.py` split is mandatory. See
`machine-charm-workload`.

## `.jujuignore`

```
/venv
/.venv
*.py[cod]
/.tox
/.git
/tests
/.opencode
__pycache__
.coverage
.ruff_cache
```

## Config options

Every option needs a `description` and, where meaningful, a `default`. Types:
`string`, `int`, `float`, `boolean`, `secret`.

```yaml
config:
  options:
    snap-revision:
      type: string
      description: >-
        Pin a specific snap revision. Empty means track latest/stable. The snap
        publishes no versioned tracks, so revision pinning is the only way to
        get reproducible deployments — at the cost of not receiving updates.
    upstream-dns:
      type: string
      default: "1.1.1.1,1.0.0.1"
      description: Comma-separated list of upstream DNS resolvers.
    listen-all-interfaces:
      type: boolean
      default: true
      description: >-
        Serve DNS to the whole network rather than localhost only. Maps to FTL
        dns.listeningMode, which snapd cannot set (camelCase), so the charm
        applies it via pihole-FTL --config directly.
    web-password:
      type: secret
      description: >-
        Juju user secret holding the admin UI password. Unset leaves the
        password unchanged.
```

Before adding an option, apply the `AGENTS.md` test: does another charm own this
data (→ relation), or is it deployment shape the operator sets outside the
charm (constraints, placement, their own tooling)?

For `type: secret`, the value is a secret URI; the charm calls
`self.model.get_secret(id=...)` and must observe `secret_changed`.

## Actions

Actions are the escape hatch for imperative operations that do not belong in
`_reconcile`. **Always set `additionalProperties` explicitly** — the default
differs between Juju 3 (`true`) and Juju 4 (`false`), so omitting it means the
behaviour changes under you.

```yaml
actions:
  update-gravity:
    description: Refresh blocklists immediately instead of waiting for the weekly timer.
    params:
      force:
        type: boolean
        description: Rebuild gravity.db from scratch rather than incrementally.
        default: false
    additionalProperties: false
  get-admin-password:
    description: Retrieve the admin UI password.
    additionalProperties: false
  snap-check:
    description: >-
      Run the snap's own diagnostic (plug connections, port conflicts, AppArmor
      denials) and return its output and exit code.
    additionalProperties: false
```

Other valid action keys the reference lists: `parallel` (boolean) and
`execution-group` (string). `required` is a list of **parameter names** — the
example in the `charmcraft.yaml` reference page is buggy (it lists a filename);
the `actions.yaml` page has it right.

Action handlers are the one place per-event handlers are correct, and actions are
non-deferrable, which is the official test for "deserves its own handler".

## `lxd-profile.yaml`

A machine-charm-only file with no Kubernetes equivalent, in the project root. It
applies an LXD profile to the container the charm is deployed into. Structure is
close to an upstream LXD profile, but **only four device types are supported**:
`unix-char`, `unix-block`, `gpu`, `usb`.

Applied on `juju deploy`, updated on `juju refresh`, inspected with
`juju show-machine`.

Relevant here because Pi-hole in an LXD container has to bind port 53 and coexist
with `systemd-resolved`. If `linux.kernel_modules` or `security.privileged` ever
becomes necessary, this file is the correct vehicle — **not** imperative code in
`pihole.py`.

One consequence for publishing: charms with an LXD profile are subject to an
allow-list, and `juju deploy --force` exists partly to *"bypass checks such as
supported base or LXD profile allow list"*. Adding this file may affect Charmhub
distribution, so do not add it speculatively.

## Endpoints, `assumes`, and keys that do not apply

`assumes` takes a list of strings and supports only two features: `juju <op>
<version>` and `k8s-api`, composable with `any-of` / `all-of` (top level is an
implicit `all-of`). **There is no `machine` feature** — a machine charm cannot
declare "do not deploy me on Kubernetes". Block style is conventional:

```yaml
assumes:
  - juju >= 3.6
```

The reference calls `assumes` *"Recommended for Kubernetes charms"*; for a machine
charm its only real use is a Juju version floor. `juju >= 3.6` is an aggressive
floor — justify it with a feature we actually need, or lower it.

**Keys that do not apply to a machine charm**, and must not appear:

| Key | Why |
|---|---|
| `containers:` | Kubernetes sidecars |
| `devices:` | Kubernetes GPU requests |
| `charm-user:` | *"has no effect on machine charms"* per the reference |
| `resources: {type: oci-image}` | only `type: file` makes sense without containers |
| `build-base:` | only valid when `base` is a development base (`ubuntu@devel`) |

**Keys that do apply and are easy to forget**: `storage:` (`type: filesystem` or
`block`), `peers:`, `extra-bindings:` (network spaces beyond relation endpoints),
`subordinate:` (needs at least one `requires` with `scope: container`), `title`,
`links` (`contact`, `documentation`, `issues`, `source`, `website`).

For `links.documentation`, the reference is explicit: link the *charm's* docs, not
the application's. Do not point it at `docs.pi-hole.net`.

## `charmcraft` linters run during pack

`charmcraft pack` runs analyzers implicitly and a linter in error state **blocks
the pack** unless `--force`. Two attributes matter here:

- `language` must resolve to `python` — dispatch must execute a `.py` entrypoint.
- `framework` must resolve to `operator` — requires `venv/ops` present in the
  packed charm and `import ops` in the entrypoint.

**Both of these currently FAIL for a `plugin: uv` charm, verified 2026-08-08 with
charmcraft 4.3.1.** An earlier version of this skill claimed they are "satisfied by
default" — that is wrong. Two independent charmcraft defects:

1. **`entrypoint` / `language`.** `charmcraft/dispatch.py` emits
   `exec "${python_path}" "${dispatch_path}/src/charm.py"`, but
   `linters.py::get_entrypoint_from_dispatch` takes `shlex.split(last_line)[-1]` and
   joins it to the base directory **without expanding the shell variable**. It then
   looks for a literal `${dispatch_path}/src/charm.py` and fails. This affects every
   charm packed with charmcraft 4.x's generated dispatch.
2. **`framework`.** `_check_operator` requires `basedir/venv/ops` to be a
   *directory*. The `uv` plugin builds a real virtualenv, so `ops` lands at
   `venv/lib/pythonX.Y/site-packages/ops`. There is no `venv/ops`, so `framework`
   can never resolve to `operator`.

Consequences: `manifest.yaml` records `language: unknown` and `framework: unknown`,
so this affects what Charmhub sees, not just CLI output. **`charmcraft pack` is not
blocked** — the analysers are advisory unless a linter is in error state.

Do **not** vendor a hand-written `dispatch` to work around #1 without reading the
`uv` plugin first: it deletes `venv/bin/python*` during build, so the template's
`ln -s $(which python3)` and `LD_LIBRARY_PATH` setup are load-bearing at runtime.
And #2 cannot be fixed from inside a charm repo at all.

One thing that *is* ours: the entrypoint must be executable (`chmod +x src/charm.py`),
because `check_dispatch_with_python_entrypoint` calls `os.access(entrypoint, os.X_OK)`.

Linters can be silenced via `analysis: {ignore: {attributes: [...], linters: [...]}}`.

