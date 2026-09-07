# ADR-0010: The Snap Revision Is Charm Policy — Pinned per Release, Held Against Auto-Refresh

**Status:** Accepted
**Date:** 2026-09-05
**Amended:** 2026-09-05 — §4 said "a constant". Revisions are **per
architecture**: the store numbers every build of the same source separately, so
one number cannot serve both platforms this charm declares. Corrected to a map,
and the bump ritual now queries the store per architecture. Found by review
before any arm64 deployment existed.
**Related:** [ADR-0006: Configuration Surface](0006-configuration-surface.md),
[snap-constraints §1](../snap-constraints.md), [snap-constraints §1.4](../snap-constraints.md)

---

## 1. Context

The snap publishes **no tracks** — only `latest/stable` and `latest/edge`
([snap-constraints §1](../snap-constraints.md)) — and its publisher is unproven.
There is no version line to depend on, and snapd's auto-refresh will move the
installed revision whenever the store does.

Revisions move **silently**: on 2026-09-05 a deployed unit held revision `1389`
while the store's `latest/stable` carried `1400`, both reporting the identical
version string `v6.4.3+git.f47b8ed`. A rebuild without a version change is
indistinguishable from "no change" in every signal the charm reads — except the
revision. The `cli_pw` session becoming read-only for config
([snap-constraints §7.2.8](../snap-constraints.md)) plausibly arrived on a unit
this way, between the ADR-0004 spike and the first Stage 2 deploy.

[ADR-0006 §2.1](0006-configuration-surface.md) accepted `snap-channel` and
`snap-revision` as **operator** config options. They were declared in
`charmcraft.yaml` and parsed, but never wired to the intent or to `install()` —
dead options, which is worse than absent ones. This ADR replaces them.

## 2. Approaches

### A. Operator config options (the ADR-0006 shape)

The operator pins via `juju config pihole snap-revision=1400`; empty means
track the channel.

**Pros**
- Flexible per-deployment choice.

**Cons**
- The default — empty — is auto-refresh, which is exactly the exposure being
  removed. A pin that every deployment must remember to set is a pin that will
  not be set.
- Rule 4's third alternative applies: which revision a charm's workload runs is
  **release engineering, not deployment shape**. It belongs to the charm's
  release process, not to each operator.

### B. Charm-owned pin, held against auto-refresh

`SNAP_REVISIONS` is a constant in the charm — one revision per architecture, see
the amendment above; each charm release bumps every entry. The
charm installs that revision, holds refreshes, and reverts drift.

**Pros**
- Every deployment is reproducible by construction, not by discipline.
- The update gesture becomes `juju refresh` of the charm — one release notes
  entry covers every unit.
- Belt and suspenders: the hold stops snapd's timer, and a revision-drift check
  reverts even a manual refresh on the next reconcile.

**Cons**
- Security updates arrive only when a charm release ships. The release cadence
  becomes the patch cadence.
- Nothing in-machine can detect a *stale* pin — the store moves, the machine
  cannot know without asking. A forgotten bump is invisible until someone looks.

### C. Hold only, track the channel

Hold refreshes but let the revision float to whatever `latest/stable` carries
when the charm does refresh.

**Pros**
- No pin to maintain.

**Cons**
- Reproducibility is lost: two units installed a week apart run different
  revisions with no record of which. This is the status quo's failure with the
  timer removed.

## 3. Recommendation

**B.** The deciding factor is rule 4's ordering: the revision is neither data
another charm owns, nor network placement, nor deployment shape — it is the
charm's own release engineering. A config option was the wrong owner, and the
dead options proved it.

## 4. Design

- `SNAP_REVISIONS` lives in `pihole_state.py`, beside `SNAP_NAME`: the drift
  check in the pure core needs it, and the core cannot import `pihole.py`. It is
  a **map of store architecture to revision**, because a revision number
  identifies one build, not one source commit — verified 2026-09-05:
  `latest/stable` was **1400** on amd64 and **1398** on arm64, both reporting
  `v6.4.3+git.f47b8ed`. A comment names the release ritual: **bump every
  entry**, using

  ```bash
  curl -s -H 'Snap-Device-Series: 16' -H 'Snap-Device-Architecture: arm64' \
    'https://api.snapcraft.io/v2/snaps/info/pihole-by-rajannpatel?fields=revision'
  ```

  once per architecture in `platforms:`. The publisher builds them at different
  times — on the same day, `latest/edge` was 1413 on amd64 and 1403 on arm64 —
  so the numbers will not be in step and cannot be guessed from each other.
- `revision_for(machine)` is pure: it maps a `platform.machine()` spelling
  (`x86_64`, `aarch64`) to the store's (`amd64`, `arm64`) and then to the pin.
  The machine is read through an injected collaborator, and the resolved pin
  reaches the core as the `pinned_revision` fact — so `compute` compares two
  observed facts and stays free of anything machine-shaped.
- **An architecture with no pin refuses to install.** `install()` raises
  `PiholeError` naming the architecture rather than taking whatever the store
  offers, because an unpinned snap auto-refreshes out from under the charm —
  the exact exposure this ADR removes. `_converge` plans no re-pin in that
  state, so the refusal is the single place it is answered.
- `install()` pins via `Snap.ensure(SnapState.Present, revision=<this arch's pin>)`
  — verified against the installed `charmlibs-snap` 1.0.1, whose own
  docstring prescribes the hold: *"Installing a revision doesn't pin the snap
  to it — the next refresh will move the snap... Use `hold`."*
- `HoldSnapRefresh` outcome: `Snap.hold()` (indefinite), read back via the
  `Snap.held` property — `snap info` answering `hold:` — verified on a live
  unit. Bootstrap order: `InstallSnap` → `HoldSnapRefresh` → `ReleasePort53`
  → …; converge emits it when `held` is false.
- Revision drift: `state.revision != SNAP_REVISION` → `InstallSnap` again,
  which re-pins (and reverts a manual refresh). The outcome is field-less: the
  revision is a constant, not a decision anyone makes per reconcile.
- `snap-channel` and `snap-revision` are **removed** from `charmcraft.yaml`.
  The charm has never been published, so removal is free. ADR-0006 is amended
  accordingly.
- The machine-level global hold some environments carry (concierge bootstrap)
  is **not** relied upon: the charm establishes and verifies its own per-snap
  hold.

## 5. Consequences

### Positive

- The workload cannot change under the charm: not by timer, not by hand, not by
  store-side rebuilds with unchanged version strings.
- Updates become visible: a revision bump is a charm release, reviewable and
  reversible like any other change.
- Two dead public options are removed before ever shipping.

### Negative

- **Security updates wait for a charm release.** A CVE in FTL stays unpatched
  until we cut one. This is the explicit trade, documented here the same way
  ADR-0006 §2.3 documents the NTP divergence — silently changing a snapd
  default is defensible only in writing.
- The bump ritual can be forgotten; nothing in-machine detects a stale pin.
  The release checklist owns it.
- Divergence from snapd norms: operators comparing against a manual install
  will see a held snap. The charm's docs must say so.
