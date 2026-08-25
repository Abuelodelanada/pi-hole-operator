# Pi-hole charm

A Juju **machine charm** that deploys and operates [Pi-hole](https://pi-hole.net)
v6 on Ubuntu machines — LXD, MAAS, or a public cloud — using the
`pihole-by-rajannpatel` snap.

Not a Kubernetes charm: no Pebble, no OCI image.

> **Status: Stage 1.** The charm installs the snap, frees port 53, starts FTL,
> owns the admin password, closes the NTP server the snap opens by default, and
> restores the host resolver on removal. It has no config options and no
> relations yet. See [`docs/roadmap.md`](docs/roadmap.md).

## Deploy

```sh
juju deploy ./pihole_amd64.charm --constraints virt-type=virtual-machine
```

## Reading this charm as an example

This charm is written as a **reconciler**. It does not react to each event
separately. It works out what is true on the machine, compares that to what
should be true, and closes the gap. The code that decides is kept separate from
the code that touches the machine.

If you want to learn that shape first, start at
**[`docs/pattern.md`](docs/pattern.md)**. It explains it with a forty-line
example that has nothing to do with Pi-hole, and lists the three ways to get it
wrong.

If you would rather read the real thing, these five parts in this order are the
short path. Steps 1, 2 and 4 are 122 lines of code between them.

| # | Read | Lines | What it teaches |
|---|---|---|---|
| 1 | `_reconcile` in [`src/charm.py`](src/charm.py) | 29 | The whole loop: fetch the world once, decide, apply. Every observed event lands here. |
| 2 | `compute`, `_bootstrap`, `_converge` in [`src/pihole_state.py`](src/pihole_state.py) | 66 | **Everything this charm does**, as pure functions — no IO, no `ops`, no snap. That is what lets step 5 test it without mocks. |
| 3 | The three unions in [`src/pihole_state.py`](src/pihole_state.py) — `PiholeIntent`, `PiholeState`, `PiholeOutcome`, each under its own `# --` divider | — | Making illegal states unrepresentable. Note `SnapAbsent` carries no fields and `SnapPresent` carries the ones that only exist once installed. |
| 4 | `_apply` in [`src/charm.py`](src/charm.py) | 27 | The imperative shell. Deliberately stupid: one `match`, one effect per branch, and `assert_never` so a new outcome fails `tox -e static` instead of being silently ignored. |
| 5 | [`tests/unit/test_pihole_state.py`](tests/unit/test_pihole_state.py) | — | What the split buys: the decision logic is tested with **zero mocks**. Start at `test_the_bootstrap_order_is_the_correctness_condition`. |

**On the size ratio.** The pure core is 427 lines; the workload adapters
(`pihole.py`, `ftl_api.py`, `resolved.py`) are 1,321. That is the point, not a
defect. This workload is hostile — `snap set` returns 0 on keys it silently
drops, `pihole -a -p` prints usage and exits 0, FTL reports `active` long before
it serves, its session tokens rotate on restart, and its password hash is salted
so it cannot be compared. The pattern **quarantines all of that in the adapters**
so the decisions stay in 66 auditable lines. A charm that looks tidier than this
is usually one that trusts an exit code.

The reasoning behind each decision is in [`docs/adr/`](docs/adr/), numbered and
dated; `src/charm.py` cites them by number. Start with
[ADR-0003](docs/adr/0003-reconciler-and-functional-core.md). The core module has
a walkthrough of its own in
[`docs/implementation/pihole_state.md`](docs/implementation/pihole_state.md),
including the edge cases the types encode.

## Read this before you rely on it

**The snap is unofficial and its publisher is unproven.** The Pi-hole project
does not support snap-based installations, so a workload bug may have no
upstream escalation path. Several of the snap's own commands return exit status
0 without having done anything, which is why this charm verifies real state
after every change it makes rather than trusting a return code.

**Revision pinning is a trade-off, not a best practice.** The snap publishes no
versioned tracks — only `latest/stable` and `latest/edge`. Pinning an exact
revision is the only way to get a reproducible deployment, and it means you stop
receiving security updates until you move the pin. Tracking the channel means
the workload can change under you without a `juju refresh`. Choose
deliberately; the charm will not choose for you.

**`juju expose` does nothing on LXD.** The LXD provider implements no
firewaller, so the DNS port is reachable whether or not the application is
exposed. On MAAS, EC2, and OpenStack it does matter. Do not conclude from an
LXD test that your exposure posture is correct.

**Pi-hole takes over host DNS.** The charm points `systemd-resolved` at
127.0.0.1 and disables its stub listener so Pi-hole can bind port 53. Removing
the application restores that; force-removing a unit in error state does not.

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md).

```sh
tox -e fmt
tox -e lint,static,unit
```

### Integration tests need LXD **VMs**, not containers

```sh
charmcraft pack
export CHARM_PATH=./pihole_amd64.charm
tox -e integration
```

The test fixtures pass `constraints="virt-type=virtual-machine"` and that is not
optional. **snapd cannot mount snaps inside an `ubuntu@26.04` LXD container at
all** — a container has no `/dev/loop*`, and unlike 24.04, snapd on 26.04 does
not fall back to its fuse mounter. It attempts a kernel squashfs mount, fails
with `wrong fs type, bad option, bad superblock`, and *no* snap installs, not
even `snapd` itself. See
[ADR-0002 §2.2.2](docs/adr/0002-tech-stack-and-repo-architecture.md).
