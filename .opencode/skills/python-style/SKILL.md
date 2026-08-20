---
name: python-style
description: >-
  Use when writing Python in this repo or configuring ruff/pyright — PEP 8 line
  length and naming, PEP 257 docstrings, import ordering, type annotations, and
  the flaplint static analyser for relation-databag ordering churn. Load before
  editing pyproject.toml lint config or resolving a style question.
metadata:
  verified: "2026-08-07"
---

# Python style

Authority: [PEP 8](https://peps.python.org/pep-0008/) and
[PEP 257](https://peps.python.org/pep-0257/). Enforcement: `ruff`. Types:
`pyright`. Charm-specific defects: `flaplint`.

## Line length: 99 code / 72 prose — and this *is* PEP 8

PEP 8's default is 79, but it grants an explicit exception:

> Some teams strongly prefer a longer line length. For code maintained
> exclusively or primarily by a team that can reach agreement on this issue, it
> is okay to increase the line length limit up to 99 characters, **provided that
> comments and docstrings are still wrapped at 72 characters**.

This repo takes that exception (99 is also the Canonical charm convention). The
proviso is not optional — it is the condition on which 99 is compliant at all.
So:

```toml
[tool.ruff]
line-length = 99

[tool.ruff.lint]
# E501 must NOT be ignored. Ignoring it while claiming PEP 8 is incoherent.
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "RUF", "ANN", "D", "PLC0415"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 72   # enables W505, the proviso above

[tool.ruff.lint.pydocstyle]
convention = "google"
```

A common shortcut is to disable `E501` because the formatter handles wrapping.
Do not do that here: the formatter cannot break a long string literal or a URL,
and those are exactly the lines that drift past 99.

## Imports: `E402` is not enough

PEP 8 says *"Imports are always put at the top of the file, just after any module
comments and docstrings, and before module globals and constants."* But the
pycodestyle rule only enforces half of that:

```python
def late() -> str:
    import json          # PLC0415 only. E402 does NOT fire here.
    return json.dumps({})

import sys               # E402 fires here.
```

Verified: with `select = ["E","W","F","I"]`, the function-level import passes
clean. `PLC0415` (`import-outside-top-level`, from pylint, **stable, not preview**)
is what catches it, which is why it is in `select` explicitly.

`PLC0415`'s own docs name three legitimate reasons for a function-level import:
*"to avoid a circular dependency, to defer a costly module load, or to avoid
loading a dependency altogether in a certain runtime environment."* This repo
allows none of them, for a charm-specific reason: **a Juju hook runs once and
exits.** An import inside a rarely-taken branch fails in production, in that
branch, against the packed charm's venv rather than the dev venv the tests used.
At module top it fails on the very first hook.

The circular-dependency escape does not apply because the `charm.py` → `pihole.py`
dependency is one-way by design. A function-level import to break a cycle is
evidence the layering broke; fix the layering.

`if TYPE_CHECKING:` blocks live at module top, so they are already compliant and
are the correct tool for type-only imports.

One caveat: **do not lint `lib/`.** The one vendored Charmhub library
(`grafana_agent.cos_agent`) is third-party code we must not edit, and it may well
contain inline imports. The tox commands pass `src` and `tests` explicitly, so
`lib/` is already outside the lint scope — keep it that way rather than adding a
`per-file-ignores` entry.

## What each rule family enforces

| Prefix | Source | Covers |
|---|---|---|
| `E`, `W` | pycodestyle | PEP 8 itself: whitespace, blank lines, line length (`E501`), doc length (`W505`), late module-level imports (`E402`) |
| `N` | pep8-naming | PEP 8 naming: `snake_case` functions, `PascalCase` classes, `UPPER_CASE` constants, `_leading_underscore` for internal |
| `D` | pydocstyle | PEP 257 |
| `I` | isort | PEP 8 import ordering: stdlib, third-party, local — separated by blank lines |
| `F` | pyflakes | unused imports/names, undefined names |
| `ANN` | flake8-annotations | missing type annotations |
| `UP` | pyupgrade | modern syntax for the target version |
| `PLC0415` | pylint | **imports inside functions** — the half of PEP 8's import rule that `E402` misses |
| `B`, `C4`, `SIM`, `RUF` | bugbear, comprehensions, simplify, ruff | correctness and clarity beyond PEP 8 |

`ruff check` is the arbiter. If a style question is not decided by `ruff`, it is
decided by PEP 8, and if PEP 8 is silent, do what the surrounding code does.

## PEP 8 points that ruff cannot check

These need human attention because they are judgement, not pattern:

- **Naming carries intent.** PEP 8's overriding principle: names should reflect
  usage rather than implementation. `_reconcile` not `_handle_all_events`;
  `blocking_ready` not `check_api`.
- **Comments that contradict the code are worse than no comment.** Keep them
  current or delete them.
- **Comments are sentences.** Capital letter, and a period when they are prose.
- **`is not` over `not ... is`.** `if x is not None`, never `if not x is None`.
- **Do not compare types with `==`.** Use `isinstance`, or better, a `match` on
  an ADT (see `charm-functional-style`).
- **Bare `except:` is forbidden.** Catch the narrowest exception that can
  actually occur. In this charm, `subprocess.CalledProcessError` and
  `snap.SnapError`, not `Exception`.
- **A `try` block wraps the minimum.** If the body is ten lines, you no longer
  know which one raised.

## PEP 257 docstrings

```python
def set_ftl_key(self, key: str, value: str) -> None:
    """Set an FTL config key and verify it took effect.

    snapd rejects option names containing camelCase or underscores, so keys
    that fail its regex are applied through `pihole-FTL --config` instead.

    Args:
        key: Dotted FTL key, without the `ftl.` prefix.
        value: Value to write, already serialised.

    Raises:
        PiholeError: The value did not appear in pihole.toml after the write.
    """
```

- One-line summary, imperative mood ("Set", not "Sets"), ends with a period.
- Blank line before any further detail.
- Every public function, class, and module gets one. Wrapped at 72.
- Document `Raises:` whenever the caller has to handle it. For this charm that
  is most of `pihole.py`.

## flaplint

A static analyser built specifically for a defect class this repo is exposed to.
It is **not** redundant with ruff or pyright.

### What it detects

> flaplint is a static analyser for Juju charms. It reads your charm's source
> code and flags every place a value that has no stable byte-order — a set, a
> glob, a `uuid4()`, … — reaches a churn-sensitive write: a relation databag, an
> on-disk file, a pebble plan, or a content-hash change-detector.

The failure mode: Juju compares databag values as **bytes**, not meaning. Write
`json.dumps(list(some_set))` and the ordering varies between hook invocations.
Juju sees a change, fires `relation-changed` on the peer, the peer rewrites its
databag in a new order, wakes you back — **and the two charms ping-pong
forever**.

That is directly the failure mode non-negotiable #1 is written to prevent. A tool
whose entire thesis is "find out why your reconcile fires forever" is well aimed
at this repo.

The nastiest case, and the one simpler checkers miss: `json.dumps(x,
sort_keys=True)` does **not** save `list(some_set)`. Key sorting cannot touch
element order — the instability has already moved from key order to element
order.

### Why ruff and pyright cannot see this

`ruff` is intra-file and has no rule about cross-process ordering stability —
it is not a Python bug, it is a Juju *protocol* bug. `pyright` checks types, not
values: `set[str]` is a perfectly valid argument to `json.dumps`. Neither models
"sink" or "sanitiser". flaplint is an interprocedural taint analyser: sets, globs
and `uuid4()` are sources, databags and file writes are sinks, `sorted()` is the
sanitiser.

Nice synergy: one of flaplint's documented limitations is that cross-object calls
only resolve when a type hint tells it what class it is looking at. Because this
repo requires annotations everywhere, flaplint resolves more here than in a
typical charm.

### Running it

Not on PyPI. Run from git, pinned:

```
uvx --python 3.12 --from git+https://github.com/michaeldmitry/flaplint@v1.1.0 \
    flaplint src --own-only --min-confidence high
```

Both pins matter. Without `@v1.1.0` the build comes from mutable `main`. Without
`--python 3.12`, `uv` may pick an interpreter outside the project's tested
matrix (3.10–3.13) — and flaplint parses your source with the *running*
interpreter's `ast`, so its own Python version is load-bearing.

`--own-only` reports only `level=error` findings, which is exactly the set that
determines the exit code, so the output matches the gate.

Exit codes: `0` no charm-owned findings, `1` at least one. Findings in
dependencies are `level=warning` and never fail the run.

Output formats: `pretty` (default), `concise` (greppable, one line per finding),
`json`. **No SARIF.** `--explain-gaps` lists writes it saw but could not trace —
advisory, never fails.

**`ruff format` at py314 can silence flaplint on a whole module.** Because
flaplint parses with a Python 3.12 `ast`, and `ruff format` rewrites
`except (A, B):` into PEP 758's unparenthesized `except A, B:`, which that parser
cannot read — so it **skips the module without saying so**, and a clean run means
nothing. Give every multi-type `except` an `as err:` binding, which keeps the
parentheses and keeps the module visible.

### Reading a finding

There are **no rule codes** like `FL001` and no `rules/` directory. A finding is
a tuple of orthogonal axes:

**`rule`** — what went wrong:

| `rule` | Meaning | Fix | Survives key-sorting? |
|---|---|---|---|
| `unordered-collection` | a whole `set`/`dict` serialised unordered | `sorted()` / `sort_keys=True` | no |
| `unordered-iteration` | a list built *from* something unordered (`list(s)`, `",".join(s)`, a comprehension) | `sorted()` before materialising | **yes** — the central case |
| `unordered-pick` | item chosen by position (`addrs[0]`) | `sorted(addrs)[0]` | yes |
| `nondeterministic` | different every run (`uuid4()`, `time()`, `random()`) | derive deterministically or persist | yes |

**`kind`** — `caller` (a real bug, reported where the value was built) or `sink`
(a function writes its own parameter unordered, so it trusts its caller).

**`sink`** — `databag`, `file`, `hash`, `plan`, `render`, `secret`.

**`level`** — `error` (yours, fails the run) or `warning` (a dependency).

Sub-rule worth internalising: the builtin `hash()` is salted by `PYTHONHASHSEED`
for `str`/`bytes`, and **every Juju hook is a fresh interpreter**. So
`hash(json.dumps(x, sort_keys=True))` — the classic "I already sorted it, it's
stable" — still flaps. `hashlib.*` is unaffected.

### Suppression

One mechanism, one spelling:

```python
peers = sorted(relation.units)  # databag-order: ignore
```

The check is a plain substring match against the **reported** line. For
`unordered-iteration` and `unordered-pick` the reported line is the *fix site*
(the `list(...)`), not the databag write — put the comment there. There is no
`# noqa`, no per-rule suppression, no file-level or block-level form.

There is also **no configuration file**. It does not read `pyproject.toml`. All
configuration is CLI flags, which means it lives in `tox.ini`, and individual
rules cannot be disabled.

### Known blind spot: f-strings launder the taint

Verified by testing, undocumented upstream, and relevant to this charm:

```python
u = {"a", "b"}
rel.data[app]["k"] = f"up = {','.join(u)}"               # NOT detected
Path("/x").write_text(f"up = {','.join(u)}")             # NOT detected
rel.data[app]["k"] = ",".join(u)                         # detected
rel.data[app]["k"] = "up = {}".format(",".join(u))       # detected
```

**A charm that renders config with f-strings is a total blind spot.** Since
rendering `pihole.toml` is exactly that shape, do not treat a clean flaplint run
as proof of ordering safety. Sort at the source: `sorted()` when the collection
is created, not when it is written.

Other documented false negatives: an index mid-chain
(`self.items[0].targets = set(x)`), cross-object calls through an unannotated
parameter, variable dict keys (`cfg[key] = set(x)`), and Pydantic models two or
more subclasses deep.

### Project risk — be honest about this

Single author (`michaeldmitry`, an individual, **not** Canonical), created
2026-06-26, 37 commits, 2 stars, 0 external users, **not on PyPI**, **no LICENSE
file in the tree** (only `license = "Apache-2.0"` in metadata), one tag. Its
`pyproject.toml` claims `Development Status :: 5 - Production/Stable`, which at
six weeks old is aspirational.

Mitigating: zero runtime dependencies (stdlib `ast` only), a substantial test
suite including `test_false_positives.py`, a drift suite that runs against both
`ops==2.23` and `ops @ main`, and CI across Python 3.10–3.13. It is well built
for its age.

**Therefore: advisory gate, not a merge blocker.** `tox -e flaplint` stays out of
the default `envlist` until the project has PyPI releases, a LICENSE, and more
than one maintainer. Verified on a machine-charm fixture with no Pebble anywhere:
4 true positives, 0 false positives; and on a workload module in the shape of
`src/pihole.py` (`charmlibs.snap`, `subprocess`, `hashlib`, `Path.write_text`):
0 findings, exit 0. It does not assume Kubernetes.
