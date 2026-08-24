# pi-hole-operator

Juju **machine charm** that deploys and operates Pi-hole v6 on Ubuntu VMs/LXD
containers via the `pihole-by-rajannpatel` snap.

This is not a Kubernetes charm. There is no Pebble, no `lightkube`, no OCI image.

## Where we are

**Stage 1 is on disk; Stage 2 has not started.** So the charm installs the snap,
frees port 53, starts FTL, restores the host resolver on removal, and owns the
admin password. `docs/roadmap.md` defines the stages and is the source of truth —
check it before treating a missing feature as a defect rather than as unstarted
work.

The public interface today is two actions, `get-admin-password` and
`rotate-admin-password`, and **nothing else**: zero config options, zero
relations. That is the state rules 4 and 5 exist to defend, so adding the first
config option or the first `requires` is a decision, not a detail.

## Non-negotiables

Only rule 3 is machine-checked. **A green `tox -e lint,static,unit` is not
evidence of compliance with 1, 2, 4, 5, 6, 7, or 8.** Rule 5 in particular looks
checkable and is not: Juju ignores `optional`, and no tool reads it. All of them
are audited by `charm-reviewer`. Do not treat a passing gate as a review.

1. **One reconciler.** Every observed event routes to a single `_reconcile`.
   Every reconcile step must be safe to run twice and safe to never run.
   The test for "deserves its own handler" is objective, from the ops docs:
   *an event that cannot be deferred needs a dedicated handler.* That set is
   exactly: actions, `stop`, `remove`, `secret_rotate`, `secret_remove`,
   `secret_expired`, and the `collect_*_status` lifecycle events. Everything
   deferrable — including `config_changed`, `upgrade_charm`, `secret_changed`,
   `leader_elected`, storage events, Pebble events, and every relation event —
   goes through `_reconcile`. The one allowed exception is
   `upgrade_charm` *if* it needs migration logic distinct from convergence.
2. **Charm logic and workload logic are separate modules.** `src/charm.py` only
   observes events, maps config to arguments, and reports status. All snap/systemd/
   file manipulation lives in the workload modules — `src/pihole.py` and
   `src/resolved.py` — neither of which ever imports `ops`. `src/pihole_state.py`
   sits between them as the pure core and imports neither `ops` nor the workload.
   This is what makes unit tests possible — tests mock the module, never
   `subprocess`. No linter checks this; if a test of `charm.py` patches
   `subprocess` or `charmlibs`, the boundary has already broken.
3. **No imports inside functions.** PEP 8 already says imports go at the top of
   the file; the reason it is restated here as absolute is charm-specific. A Juju
   hook runs once and exits, so an import inside a rarely-taken branch fails in
   production, in that branch, with a venv that differs from the one the tests
   ran against. An import at module top fails on the first hook instead.
   Enforced by `E402` **and `PLC0415`** — `E402` alone only catches late
   module-level imports, not function-level ones. `if TYPE_CHECKING:` blocks are
   at module top and therefore fine; if you need a function-level import to break
   a cycle, the `charm.py`/`pihole.py` layering has been violated — fix that
   instead.
4. **Relations over config options.** A config option is a permanent public API:
   removing or renaming one breaks every existing deployment, the same class of
   irreversibility as `limit`. So before adding one, check the three alternatives
   in order — does another charm own this data (a relation)? is it network
   placement (`extra-bindings` / a Juju space)? is it deployment shape the operator
   sets outside the charm (constraints, placement, their own deployment tooling)?
   Config options are the residue, not the default.
5. **Optional by default.** Every `requires`/`provides` entry gets
   `optional: true` unless the charm physically cannot reach `ActiveStatus`
   without it. The charm must come up clean with zero relations. Note that Juju
   does **not** enforce `optional` — it is documentation. The guarantee lives in
   `_reconcile` and `collect_unit_status`, so a correct `charmcraft.yaml` is not
   evidence. (`limit`, by contrast, *is* enforced — and adding it later breaks
   `juju refresh` for existing users. See `charm-relations`.)
6. **Never trust a success signal you did not verify.** `snap set` and several
   `pihole` subcommands return 0 without doing anything — see `pihole-snap`. The
   same shape appears in `ops`: `Secret.set_content` succeeds and the unit errors
   at the *end* of the hook if permission was missing. And `snap services` reports
   `active` long before Pi-hole is serving. In every case, read the state the
   operation was supposed to produce. An exit code is not evidence.
7. **Decide, then act — never both in one function.** A function that performs an
   effect *and* returns a flag describing what it decided cannot be tested without
   running the effect. Pure functions compute an outcome value; impure functions
   consume it. The detection signal is cheap: **if a test needs a mock to reach a
   decision, this rule was broken.** The inverse shape counts too: a boolean
   parameter that decides *whether* the function has effects — `f(generate=True)`
   from one caller, `f(generate=False)` from another — leaves the name unable to
   answer "does this mutate?", and puts the guarantee in an argument instead of in
   the type system. Split it into two named methods and let each name carry the
   answer; `_read_intent` and `_ensure_intent` in `src/charm.py` are the
   worked example. See `charm-functional-style`.
8. **Inheritance only where a framework demands it.** `ops.CharmBase` is the one
   mandatory subclass; charm libraries are instantiated, never extended.
   Everything else is composition — but note the verified constraint:
   **constructor injection into the charm is impossible.** `ops` instantiates it as
   `charm_class(framework)` and `ops.testing.Context` takes a type, not a factory.
   So inject *below* the charm, in `Pihole`, and do not invent a factory
   indirection to work around it. Pass the narrowest collaborator a function needs,
   never the charm itself.

## Toolchain

`uv` + `tox` + `ruff` + `pyright`. No `pip`, no `poetry`, no `black`/`isort`/`flake8`.

```
tox -e fmt        # ruff format + ruff check --fix
tox -e lint       # ruff check
tox -e static     # pyright
tox -e unit       # pytest tests/unit, coverage fail_under = 90
tox -e integration  # pytest tests/integration (needs a juju machine model)
tox -e lock       # regenerate uv.lock after changing pyproject.toml
tox -e flaplint   # advisory: relation-databag ordering churn. Not in envlist.
```

**A change is not done until `fmt`, `lint`, `static` and `unit` are green.** That
is the floor, not the finish line — reread the non-negotiables above, because none
of those four gates can see rules 1, 2, 4, 5, 6, 7 or 8. Run `flaplint` as well
when the change touches a databag write, a file write, or a hash.

`uv.lock` is committed. Dependencies go in `pyproject.toml`, never in
`charmcraft.yaml`'s `charm-libs` — that key is only for Charmhub-hosted libraries,
of which this charm needs exactly one (`grafana_agent.cos_agent`, because no PyPI
replacement exists).

## Layout

Present today:

```
charmcraft.yaml           # base: ubuntu@26.04, platforms: {amd64:, arm64:}
pyproject.toml            # ops, charmlibs-snap, charmlibs-systemd, pydantic, tenacity
uv.lock
tox.ini
docs/
  pattern.md              # how the charm decides what to do, taught with a small example
  adr/                    # numbered decision records. Load `new-adr` before adding one.
  implementation/         # how an existing module works. One file per module, as it lands.
  roadmap.md              # staged delivery plan
  snap-constraints.md     # what the snap cannot do, and the workarounds
  BACKLOG.md
src/
  charm.py                # PiholeCharm: observe -> _reconcile -> collect_unit_status
  pihole.py               # workload: snap install/start, config apply, readiness
  pihole_state.py         # functional core: intent, state, outcome ADT, fetch/compute
  resolved.py             # workload: systemd-resolved port 53 orchestration
tests/
  unit/                   # ops.testing, Model(type='lxd'), mocks src.pihole
  integration/            # jubilant + pytest-jubilant on LXD
```

`src/pihole_state.py` is the pure core: it holds `PiholeIntent`, the `PiholeState`
and `PiholeOutcome` unions, `fetch`, and `compute`. It imports neither `ops` nor
anything that touches the machine — it reaches the workload only through the
`PiholeFacts` protocol, which is what keeps that import out. See rule 2.

**`src/` is on `PYTHONPATH`, so imports are flat.** `tox.ini` sets
`PYTHONPATH={tox_root}/lib:{tox_root}/src`, which is what Juju's charm venv also
does. So it is `import pihole` and `import charm`, never `from src.pihole import
...` — the latter works nowhere, in the charm or in the tests.

Arriving with later stages, so do not expect them on disk yet:
`lib/charms/grafana_agent/` (vendored, never edited, never linted) and
`src/grafana_dashboards/`, `src/prometheus_alert_rules/`, `src/loki_alert_rules/`
(COSAgentProvider defaults).

## Python conventions

Authority is [PEP 8](https://peps.python.org/pep-0008/) and
[PEP 257](https://peps.python.org/pep-0257/), enforced by `ruff`. Details and the
rule-family mapping live in the `python-style` skill.

Machine-checked:

- **PEP 8 with the 99-character exception**, which PEP 8 grants explicitly —
  *provided comments and docstrings stay wrapped at 72*. That proviso is the
  condition, not a suggestion: `E501` and `W505` are both enabled.
- Type annotations everywhere; `pyright` runs `typeCheckingMode = "strict"` over
  **both `src` and `tests`**, so a test helper needs the same annotations as
  production code. They also let `flaplint` resolve cross-object calls, so they buy
  correctness twice.
- Ruff's `select` is `E W F I N UP B C4 SIM RUF ANN D PLC0415`. Two consequences
  worth knowing before you write: **`ANN` makes annotations a lint error, and `D`
  does the same for docstrings** — neither is merely a house preference. No
  `from x import *` (`F403`/`F405`). No bare `except:` (`E722`).
- Python **3.14**, which is what `ubuntu@26.04` ships — and the *only* interpreter
  in that base's archive, so there is no fallback. The charm never runs on anything
  else, so `requires-python = ">=3.14"`, `ruff target-version = "py314"` and
  `pyright pythonVersion = "3.14"`. Do not write code that merely tolerates older
  interpreters. See `docs/adr/0002-tech-stack-and-repo-architecture.md`. One
  consequence bites silently: `ruff format` rewrites `except (A, B):` into PEP
  758's unparenthesized form, which makes `flaplint` skip the module without
  saying so. Give multi-type `except` clauses an `as err:` binding — see
  `python-style`.

Not machine-checked — the reviewer's job:

- **Prefer the functional style.** Frozen dataclasses, unions as ADTs, exhaustive
  `match` with `assert_never`, `Mapping`/`Sequence`/`FrozenSet` in signatures.
  Functional core, imperative shell. See `charm-functional-style` — including what
  we deliberately do *not* adopt from `fp-edge-canonical`.
- `pydantic` for anything parsed or serialised: charm config (via
  `self.load_config`), databags (via `Relation.load`/`save`), the subset of
  `pihole.toml` the charm cares about.
- Logging via `logging.getLogger(__name__)`, never `print`. `ops.main` already
  wires this to `juju-log`, so no setup is needed.
- No `except Exception:` where a narrower exception is what actually occurs. Ruff's
  `BLE001` is deliberately not enabled because it cannot tell the difference — this
  one is judgement.
- Tests use `# GIVEN / # WHEN / # THEN` comments and live in `conftest.py`-backed
  fixtures rather than per-file setup boilerplate.
- **Docstrings say what; ADRs say why.** `D` is enforced, so the floor is one
  imperative line plus `Raises:` where the caller must handle it — but design
  rationale belongs in `docs/adr/`. Cite it (`See ADR-0005 section 2.9`) instead of
  paraphrasing it: a docstring that restates an ADR goes stale and then wins by
  proximity. Keep a rationale inline only where a reader would otherwise plausibly
  "fix" the code, and then as one sentence. A comment explains why *this line*, at
  the line, in two lines or fewer.
- **`# databag-order: ignore` suppresses one `flaplint` finding on one line.** It
  is legitimate only where the nondeterminism is the point and cannot flap: the two
  uses in `src/charm.py` are on `_store_password`, where the value is a fresh random
  token written exactly once. A suppression on a line that runs on every reconcile
  is a defect being silenced — fix the ordering instead.

## Ecosystem facts that bite (2026)

- Charmhub-hosted charm libraries (`charmcraft fetch-lib`, `LIBPATCH`/`LIBAPI`)
  are **being phased out** in favour of PyPI packages. Do not create new
  `lib/charms/...` files for code this repo owns.
- `charms.operator_libs_linux.*` is **deprecated**. Use `charmlibs-snap`,
  `charmlibs-systemd`, `charmlibs-apt` from PyPI: `from charmlibs import snap`.
- The COS machine subordinate is `opentelemetry-collector` (`grafana-agent` is
  EOL). But the interface is still `cos_agent` and its library is still published
  as `grafana_agent.cos_agent` — there is no `opentelemetry_collector` equivalent.
- `bases:` in `charmcraft.yaml` is deprecated. Use `base:` + `platforms:`.

## Agents and skills in this repo

Three agents, three jobs. Design decisions go to `charm-architect`,
implementation to `charm-engineer`, and audits to `charm-reviewer` (read-only).
Research delegates to `explore` (this repo) and `general` (the upstream
`references`), both on a cheaper model.

**Load the relevant skill instead of guessing.** Their names and trigger
conditions are already in your system prompt, so this file does not restate them.
They carry verified, sourced, dated detail — so where a skill and this file
disagree, the skill is newer and wins, and this file is the thing to fix.

Decisions live in `docs/adr/`, numbered and dated. `src/charm.py` cites them by
number in comments, so an ADR is not optional documentation — it is where the
reason for a rule lives once the rule is no longer obvious. Load `new-adr`
before adding or revising one.
