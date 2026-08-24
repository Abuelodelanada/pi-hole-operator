# Contributing to the Pi-hole charm

This is a Juju **machine charm** for Pi-hole v6 on Ubuntu, built on the
`pihole-by-rajannpatel` snap. It is not a Kubernetes charm: there is no Pebble,
no `lightkube`, and no OCI image.

Read [`AGENTS.md`](AGENTS.md) first. It holds eight non-negotiables, and most of
them are **not** machine-checked — a green test run is not evidence of compliance.

---

## Getting started

### Prerequisites

- **Python 3.14** — what `ubuntu@26.04` ships, and the only interpreter the charm
  ever runs on. It is also the *only* Python in that base's archive, so there is no
  fallback. See [ADR-0002](docs/adr/0002-tech-stack-and-repo-architecture.md) §2.3.
  `uv python install 3.14` if you do not have it.
- [`uv`](https://docs.astral.sh/uv/) — no `pip`, no `poetry`.
- `tox`
- `charmcraft` (snap) for packing.
- **Juju 3.6+ with an LXD machine cloud** for integration tests:
  ```sh
  sudo concierge prepare -p machine
  ```

### Setup

```sh
uv sync
```

`uv.lock` is committed and is the only source of truth. Regenerate with
`tox -e lock` (`uv lock --upgrade`), never by hand.

---

## Where things live

```
src/
  charm.py           # ops only: observe -> _reconcile -> collect_unit_status
  pihole.py          # snap/systemd/filesystem. Never imports ops.
  pihole_config.py   # pydantic models, charm config -> FTL key mapping. Pure.
  pihole_state.py    # frozen snapshot + outcome ADT + pure compute(). No IO.
  resolved.py        # systemd-resolved drop-in. Never imports ops.
docs/
  adr/               # decisions — why the charm is shaped this way
  roadmap.md         # stages, acceptance criteria, open spikes
  snap-constraints.md# verified facts about the workload
  BACKLOG.md         # deferred work, with triggers
lib/charms/grafana_agent/   # vendored third-party. Never edited, never linted.
```

### The module boundary is not stylistic

`src/charm.py` never imports `charmlibs.*` or `subprocess` and never writes
files. `src/pihole.py` never imports `ops`. This is what makes the charm testable
at all — `ops.testing` offers **no** mocking of snap, systemd, or `subprocess`
(`Container` and `Exec` are Kubernetes-only), so the seam has to exist in our
code.

Two signals that the boundary has broken, both objective:

- a test of `charm.py` that patches `subprocess` or `charmlibs`
- a test of `compute()` that needs `monkeypatch`

Either one is a design defect, not a test problem. See
[ADR-0003](docs/adr/0003-reconciler-and-functional-core.md).

---

## The implementation workflow

**Work is driven by stages in [`docs/roadmap.md`](docs/roadmap.md), not by ADRs.**

ADRs are decisions; stages are work. Five of the eight ADRs are *cross-cutting* —
they are complied with in every stage rather than completed in one:

| ADR | Lands in | Nature |
|---|---|---|
| [0001](docs/adr/0001-charm-scope-and-specification.md) scope | never produces code | Frame. Gates what counts as in-scope |
| [0002](docs/adr/0002-tech-stack-and-repo-architecture.md) tech stack | Stage 0 | Completed there |
| [0003](docs/adr/0003-reconciler-and-functional-core.md) reconciler | Stage 1 establishes it | **Cross-cutting** — audited in 2, 3, 4, 5, 7 |
| [0005](docs/adr/0005-status-semantics-and-failure-handling.md) status | Stage 1 establishes the channel | **Cross-cutting** — every stage adds statuses |
| [0006](docs/adr/0006-configuration-surface.md) config surface | Stages 2, 3, 7 | Completed in three parts |
| [0004](docs/adr/0004-ftl-configuration-mechanism.md) FTL config | Stage 2, **after a spike** | Completed there |
| [0007](docs/adr/0007-admin-password-handling.md) password | Stage 4 | Completed there |
| [0008](docs/adr/0008-cos-integration.md) COS | Stage 5 | Completed there |

### The per-stage loop

**1. Open the stage.** Read its section in `roadmap.md` and the ADRs it lists. If
the stage has a spike, run the spike loop below *before* writing production code.

**2. Specify, if something is missing.** If a decision surfaces that no ADR
covers, stop and write the ADR (the `new-adr` skill scaffolds it). Do not decide
inside an implementation PR — a decision buried in a diff cannot be found later.

**3. Implement.** Follow the deliverables and the named tests. Write tests
alongside the code, not after, with `# GIVEN / # WHEN / # THEN` comments and
fixtures in `conftest.py`.

**4. Verify mechanically.**

```sh
tox -e fmt                    # ruff format + ruff check --fix
tox -e lint,static,unit       # ruff, pyright, pytest + coverage
tox -e flaplint               # advisory: relation-databag ordering churn
```

Plus the stage's integration test, from Stage 1 onward:

```sh
charmcraft pack
export CHARM_PATH=./pihole_amd64.charm
tox -e integration
```

Pack **once** by hand and reuse via `CHARM_PATH`. Never pack inside a test.

**5. Audit.** Run `charm-reviewer` (read-only). **This is not redundant with step
4.** A green `tox` says nothing about non-negotiables 1, 2, 4, 6, 7, or 8 — those
are architectural and no linter sees them. Do not treat a passing gate as a
review.

**6. Close the stage.** Walk the stage's acceptance checklist, then **move any ADR
the stage settled from `Proposed` to `Accepted`, with the evidence recorded in
it.** A stale `Proposed` on already-implemented code poisons the value of every
other ADR: if the Status field is unreliable in one, it is unreliable in all.

One PR per stage. Stages with a spike are two PRs.

### The spike loop

Four stages depend on evidence we do not have yet. Order matters:

```
throwaway VM  →  test the candidates  →  record results in the ADR §6
              →  ADR to Accepted  →  only then implement
```

**Spike code is throwaway.** It does not become the implementation. Its only
deliverable is a written answer in the ADR.

This is stated emphatically because
[ADR-0004](docs/adr/0004-ftl-configuration-mechanism.md) is the textbook case:
three candidate mechanisms for the 66 unreachable FTL keys, and if the wrong one
is chosen on intuition, the failure mode is that `snap set` returns 0 and does
nothing. The charm reports `active/idle` with the config never applied — exactly
the defect the whole design exists to prevent, arriving silently.

Spikes need a machine where breaking DNS has no consequences, and where ports 53
and 67 are free. Inside a shared LXD container the charm fights the container's
own resolver and `lxdbr0`'s dnsmasq, and the results mean nothing.

Current open spikes are listed in
[`roadmap.md`](docs/roadmap.md#open-spikes).

---

## Architecture decisions

Significant changes must be preceded by an **accepted** ADR under `docs/adr/`.
Number sequentially, zero-padded to four digits. The `new-adr` skill holds the
house format, the Proposed/Accepted lifecycle, and — importantly — the rules for
deciding whether something belongs in an ADR, in `roadmap.md`, in
`snap-constraints.md`, or in `BACKLOG.md`.

### When code contradicts an ADR

It will happen. The rule:

- ADR is **`Proposed`** → amend it in the same PR. It was not yet a commitment.
- ADR is **`Accepted`** → **do not edit it.** Write a new ADR that supersedes it,
  and mark the old one `Superseded by ADR-NNNN`. The history is the point.
- In neither case do you deviate silently.

---

## Testing

Three layers, mirroring the source design. Do not mix them.

| Layer | Tool | What you mock |
|---|---|---|
| Pure decisions (`compute`, config mapping) | plain `pytest` | **nothing** |
| State transitions (`charm.py`) | `ops.testing` `Context` + `State` | `src.pihole` — the whole module |
| Workload (`pihole.py`) | plain `pytest` | `snap.SnapCache`, `subprocess.run`, filesystem |
| Integration | `jubilant` on LXD | nothing |

Put as much logic as possible in the first layer. Every decision that lives there
is tested without a mock, and mocks are where charm suites rot.

Three traps that will cost you an afternoon:

- **`testing.Model(type="lxd")` is mandatory.** `ops.testing` defaults to
  `"kubernetes"`; a machine charm tested with the default is in the wrong
  environment. It lives in a `conftest.py` fixture so it cannot be forgotten.
- **`ctx.run_action` does not exist.** Actions go through `ctx.run(ctx.on.action(...))`.
- **Do not test `juju expose`.** The LXD provider implements no firewaller, so
  port 53 is reachable with or without it. Such a test passes for the wrong reason.

Coverage must stay at or above **90%** (`fail_under` in `pyproject.toml`). If
coverage is hard to reach, that is usually a design signal rather than a testing
problem: logic that is expensive to cover is logic sitting on the wrong side of
the decide/act boundary.

Run pytest with `-W error`, as `ops` itself does.

### Two invariants every change must preserve

These are asserted by integration tests and are not negotiable:

1. **The charm reaches `ActiveStatus` with zero relations.**
2. **`juju remove-application` leaves the host with working DNS.** The snap
   *cannot* restore `systemd-resolved` — its `remove` hook only prints
   instructions. Our handler is the only thing that does. See
   [snap-constraints §8.2](docs/snap-constraints.md).

---

## Submitting changes

1. Branch from `main`.
2. Make the change, with tests alongside it.
3. `tox -e fmt`, then `tox -e lint,static,unit`.
4. Run `charm-reviewer` and address the findings.
5. Update the ADR statuses and `docs/BACKLOG.md` if the change settles or defers
   anything.
6. Open a PR naming the roadmap stage and the ADRs it implements.

Do not commit, amend, or push unless explicitly asked to. `uv.lock` changes go in
the same PR as the dependency change that caused them.

---

## Reporting issues

Use GitHub Issues. When reporting a workload problem rather than a charm problem,
include:

```sh
juju exec --unit pihole/0 -- pihole-by-rajannpatel.pihole snap-debug
juju show-status-log pihole/0
juju debug-log --replay --include pihole/0
```

Note `--unit` (root, hook context), not `--machine` (runs as `ubuntu`, where
`snap get` can fail on permissions).

Bear in mind the workload is an **unofficial** snap whose publisher is unproven,
and that the Pi-hole project does not support snap-based installations. A bug may
have no upstream escalation path. See
[ADR-0001](docs/adr/0001-charm-scope-and-specification.md) §1.2.
