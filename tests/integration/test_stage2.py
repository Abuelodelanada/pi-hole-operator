"""Stage 2 integration tests: config verified to have landed.

Every acceptance criterion for this stage hinges on one property: the
charm applies configuration through `PATCH /api/config`, which FTL
applies **live**. So each test that changes a value also proves the
daemon was not restarted — the PID is the evidence, because a restart
on every config change would mean a DNS outage on every config change.

Two more tests carry ADR-0010's decision, which until now had only unit
tests behind it: the installed revision is the one this charm pins *for
this machine's architecture*, and the snap is held against snapd's
auto-refresh timer.
"""

import json
import time
from typing import cast

import jubilant

from pihole_state import SNAP_REVISIONS
from tests.integration.conftest import APP_NAME, DEPLOY_TIMEOUT, settled

FTL_SERVICE = "snap.pihole-by-rajannpatel.pihole-ftl.service"
PIHOLE_TOML = "/var/snap/pihole-by-rajannpatel/current/etc/pihole/pihole.toml"

UPSTREAMS = ("1.1.1.1", "9.9.9.9")
"""Two resolvers that really answer: a `dig` after the switch is the
end-to-end evidence that the applied value is the one FTL uses."""


def ftl_pid(juju: jubilant.Juju) -> str:
    """The FTL daemon's PID, straight from systemd.

    `MainPID` rather than `pgrep`: it is exact, and it is what changes
    if and only if the service was restarted.
    """
    result = juju.exec(
        f"systemctl show --property=MainPID --value {FTL_SERVICE}",
        unit=f"{APP_NAME}/0",
    )
    pid = result.stdout.strip()
    assert pid not in ("", "0"), "FTL is not running, so a PID comparison proves nothing"
    return pid


def logged(juju: jubilant.Juju, needle: str) -> int:
    """How many times `needle` appears in this unit's whole log.

    The PID cannot answer "did the charm re-apply?": a `PATCH
    /api/config` never restarts FTL (ADR-0004 §6), so the PID is
    stable whether or not a redundant PATCH was issued. The charm's own
    log lines are the only observable that distinguishes them.
    """
    log = juju.cli("debug-log", "--replay", "--no-tail", "--include", f"{APP_NAME}/0")
    return log.count(needle)


# `pihole.apply_ftl_config` emits this only after the `pihole.toml`
# read-back, so it counts applies that actually landed.
APPLIED = "Applied FTL config"

# `charm._apply` emits this once per outcome, so `Noop()` counts
# reconciles that decided there was nothing to do.
NOOP = "applying Noop()."


def toml_upstreams(juju: jubilant.Juju) -> list[str]:
    """The `dns.upstreams` array, parsed the way the charm parses it.

    A `grep` cannot read this one: FTL writes arrays across lines, so
    an anchored match returns `upstreams = [` and nothing else — which
    is exactly what the first version of this test asserted against,
    and passed. `tomllib` on the unit is the same parse `pihole.py`
    does, so the comparison is against the value the charm would read
    back.
    """
    script = (
        "import json, tomllib; "
        f"print(json.dumps(tomllib.load(open('{PIHOLE_TOML}', 'rb'))['dns']['upstreams']))"
    )
    result = juju.exec(f'python3 -c "{script}"', unit=f"{APP_NAME}/0")
    parsed: object = json.loads(result.stdout)
    assert isinstance(parsed, list), f"upstreams is not an array: {parsed!r}"
    # A TOML array of strings really is one; the cast tells pyright
    # what isinstance cannot.
    return cast("list[str]", parsed)


def toml_value(juju: jubilant.Juju, *path: str) -> object:
    """Read one key out of `pihole.toml`, parsed as the charm does.

    `grep` is the wrong tool twice over: FTL writes arrays across
    lines, and it annotates a changed key inline
    (`### CHANGED, default value was "LOCAL" ###`), so a substring
    match tests a line carrying two values.
    """
    keys = "".join(f"[{key!r}]" for key in path)
    script = (
        f"import json, tomllib; print(json.dumps(tomllib.load(open('{PIHOLE_TOML}', 'rb')){keys}))"
    )
    result = juju.exec(f'python3 -c "{script}"', unit=f"{APP_NAME}/0")
    return json.loads(result.stdout)


def test_the_installed_revision_is_the_one_pinned_for_this_architecture(
    deployed: jubilant.Juju,
):
    # GIVEN a converged unit, and the architecture it runs on. `dpkg`
    # spells it the way the store does, so no mapping is needed.
    arch = deployed.exec("dpkg --print-architecture", unit=f"{APP_NAME}/0").stdout.strip()
    assert arch in SNAP_REVISIONS, f"this charm pins no revision for {arch}"

    # WHEN snapd is asked which revision it installed
    listing = deployed.exec(
        "snap list pihole-by-rajannpatel --unicode=never --color=never",
        unit=f"{APP_NAME}/0",
    )
    installed = listing.stdout.splitlines()[1].split()[2]

    # THEN it equals the pin for *this* architecture, by equality and
    # not by membership: the store numbers every build separately, so
    # accepting either number would pass an amd64 unit running the
    # arm64 revision — the exact bug the per-architecture map fixes
    # (snap-constraints §1.4).
    assert installed == SNAP_REVISIONS[arch], (
        f"{arch} unit runs revision {installed}, pinned {SNAP_REVISIONS[arch]}"
    )


def test_the_snap_is_held_against_auto_refresh(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN snapd is asked about the snap's refresh hold
    result = deployed.exec(
        "snap info pihole-by-rajannpatel",
        unit=f"{APP_NAME}/0",
    )

    # THEN it is held indefinitely — asserted on one line, because
    # `hold: <a date>` plus the word "forever" elsewhere in the output
    # would satisfy two independent substring checks. Without the hold,
    # snapd's timer moves the pinned revision on its own schedule and
    # every defence that depends on the revision (ADR-0010) is
    # decoration.
    holds = [line for line in result.stdout.splitlines() if line.strip().startswith("hold:")]
    assert holds == ["hold:         forever"] or "forever" in holds[0], (
        f"no indefinite hold in: {holds}"
    )


def test_a_camelcase_key_lands_without_restarting_ftl(deployed: jubilant.Juju):
    # GIVEN a converged unit and the PID it is serving DNS with
    before = ftl_pid(deployed)

    # WHEN the listening mode is changed — the key `snap set` cannot
    # even express, because snapd rejects camelCase option names
    deployed.config(APP_NAME, {"dns-listening-mode": "ALL"})
    deployed.wait(settled, timeout=DEPLOY_TIMEOUT)

    # THEN the value is in `pihole.toml`, which is the only place that
    # counts: FTL answers 200 for keys it silently ignores. Parsed and
    # compared equal, because FTL annotates a changed key inline
    # (`### CHANGED, default value was "LOCAL" ###`) and a substring
    # match would be testing a line that carries two values.
    assert toml_value(deployed, "dns", "listeningMode") == "ALL"

    # AND the daemon was never restarted. This is the whole reason
    # ADR-0004 chose the API over `snap set`: config changes must not
    # cost a DNS outage.
    assert ftl_pid(deployed) == before


def test_upstream_dns_reaches_both_the_toml_and_resolution(deployed: jubilant.Juju):
    # GIVEN a converged unit
    before = ftl_pid(deployed)

    # WHEN the upstream resolvers are changed
    deployed.config(APP_NAME, {"upstream-dns": ",".join(UPSTREAMS)})
    deployed.wait(settled, timeout=DEPLOY_TIMEOUT)

    # THEN both land in the TOML as a real array, in the operator's
    # order — the charm converts CSV to JSON and preserves the order,
    # because upstream order is a preference and not noise
    assert toml_upstreams(deployed) == list(UPSTREAMS)

    # AND resolution still works through them, which is the difference
    # between "the value was written" and "the value is in use"
    answer = deployed.exec("dig +short @127.0.0.1 example.com", unit=f"{APP_NAME}/0")
    assert answer.stdout.strip()

    # AND still no restart
    assert ftl_pid(deployed) == before


def test_a_converged_machine_applies_nothing_and_restarts_nothing(
    deployed: jubilant.Juju,
):
    """Setting the same config twice costs nothing — box three.

    Asserting on the PID alone would be vacuous: a PATCH never
    restarts FTL, so the PID is stable whether the charm re-applied or
    not. What has to be observed is that `compute` emitted no outcome
    at all, and for that the witness is the charm's own log line.
    """
    # GIVEN a real change, applied once and settled
    deployed.config(APP_NAME, {"blocking-enabled": False})
    deployed.wait(settled, timeout=DEPLOY_TIMEOUT)
    assert toml_value(deployed, "dns", "blocking", "active") is False
    applies_before = logged(deployed, APPLIED)
    noops_before = logged(deployed, NOOP)
    pid_before = ftl_pid(deployed)

    # WHEN the same value is set again, and further reconciles are
    # driven by shortening the update-status interval — a config value
    # Juju already holds fires no hook at all, so the interval is what
    # makes "run it again" observable
    deployed.config(APP_NAME, {"blocking-enabled": False})
    deployed.model_config({"update-status-hook-interval": "10s"})
    try:
        time.sleep(35)
        deployed.wait(settled, timeout=DEPLOY_TIMEOUT)
    finally:
        deployed.model_config({"update-status-hook-interval": "5m"})

    # THEN reconciles really did run — without this the test would pass
    # for the wrong reason if the interval never took effect, or if 35
    # seconds were too short.
    assert logged(deployed, NOOP) > noops_before

    # AND the charm applied nothing more: a converged machine yields
    # `(Noop(),)`, so no second PATCH was issued...
    assert logged(deployed, APPLIED) == applies_before

    # ...and the daemon was never bounced. A charm that reapplied
    # unconditionally would restart FTL on every update-status, which
    # is a DNS outage every five minutes.
    assert ftl_pid(deployed) == pid_before
