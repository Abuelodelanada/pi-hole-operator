"""Stage 3 integration tests: plugs, snap-check, actions, gravity timer.

Pins the snap-check exit codes — the wiki documents none — and proves
that capability warnings are absent after plugs are connected.
"""

import re
import time

import jubilant

from pihole_state import SNAP_NAME, UNCONDITIONAL_PLUGS
from tests.integration.conftest import APP_NAME, DEPLOY_TIMEOUT

FTL_LOG = f"/var/snap/{SNAP_NAME}/common/var/log/pihole/FTL.log"
PIHOLE_CMD = f"/snap/bin/{SNAP_NAME}.pihole"
GRAVITY_TIMER_UNIT = f"snap.{SNAP_NAME}.gravity-sync.timer"
GRAVITY_TIMER_DROP_IN = f"/etc/systemd/system/{GRAVITY_TIMER_UNIT}.d/override.conf"


def settled(status: jubilant.Status) -> bool:
    """Both the workload and the agent are done.

    ``all_active`` alone gates on *workload* status, which stays
    ``active`` for the whole reconcile — with ``successes=3`` at one
    second that window can close before the hook even starts, and the
    assertions then race the charm.
    """
    return jubilant.all_active(status) and jubilant.all_agents_idle(status)


# -- Plugs. ------------------------------------------------------------


def test_unconditional_plugs_are_connected(juju: jubilant.Juju, deployed: jubilant.Juju):
    """Every unconditional plug is connected on a converged unit.

    The assertion parses the Slot column, not a substring over the
    whole output: `snap connections` lists DISCONNECTED plugs too, in
    the same four columns, so `plug in stdout` is true whether or not
    the plug is connected — the exact bug the production parser was
    fixed for, which in assertion form let the whole deliverable be
    deleted with the suite green.
    """
    result = juju.exec(f"snap connections {SNAP_NAME}", unit=f"{APP_NAME}/0")
    connected: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith(f"{SNAP_NAME}:") and parts[2] != "-":
            connected.add(parts[1].split(":", 1)[1])
    for plug in UNCONDITIONAL_PLUGS:
        assert plug in connected, (
            f"plug {plug} is not connected; connected set: {sorted(connected)}"
        )


def test_ftl_log_is_free_of_capability_warnings(juju: jubilant.Juju, deployed: jubilant.Juju):
    """FTL.log has no CAP_SYS_TIME, CAP_SYS_NICE, or /proc/ warnings.

    These warnings appear when time-control, process-control, and
    system-observe are disconnected. After convergence with all five
    unconditional plugs connected and FTL started, the log must be
    clean.
    """
    result = juju.exec(
        f"grep -c 'CAP_SYS_TIME\\|CAP_SYS_NICE\\|/proc/' {FTL_LOG} || true",
        unit=f"{APP_NAME}/0",
    )
    warnings = result.stdout.strip()
    assert warnings == "0", f"capability warnings found in FTL.log: {warnings}"


def test_no_capability_denials_for_pihole_snap(juju: jubilant.Juju, deployed: jubilant.Juju):
    """Dmesg is free of the AppArmor denials the plugs cause.

    Scoped to `operation="capable"` and `/proc/*/comm` opens — the two
    denial shapes that disconnected plugs produce (snap-constraints
    section 3), and which connecting them clears. NOT scoped to every
    DENIED mentioning the snap: strict confinement emits noise no
    charm action can remove — curl probing `/etc/ldap/ldap.conf` during
    gravity downloads, and snap-check itself being denied `dmesg` by
    its own sandbox. Both observed on a converged, fully-plugged unit;
    asserting their absence asserts something undeliverable.
    """
    result = juju.exec(
        f"dmesg | grep -E 'DENIED.*{SNAP_NAME}.*operation=\"capable\"' "
        f"|| dmesg | grep -E 'DENIED.*{SNAP_NAME}.*name=\"/proc/.*/comm\"' "
        "|| true",
        unit=f"{APP_NAME}/0",
    )
    denied = result.stdout.strip()
    assert denied == "", f"capability/comm AppArmor denials found in dmesg: {denied}"


# -- snap-check. -------------------------------------------------------


def test_snap_check_exit_0_on_a_converged_unit(juju: jubilant.Juju, deployed: jubilant.Juju):
    """snap-check returns 0 when the unit is healthy."""
    result = juju.exec(f"{PIHOLE_CMD} snap-check", unit=f"{APP_NAME}/0")
    assert result.return_code == 0
    assert result.stdout.strip()


def test_snap_check_exit_2_on_a_port_conflict(juju: jubilant.Juju, deployed: jubilant.Juju):
    """snap-check returns 2 when port 53 is occupied while FTL is down.

    The real exit-2 scenario, per the snap-check source: port checks
    are SKIPPED while FTL is active ("Port conflict checks skipped"),
    so the conflict it detects is the crash-loop shape — something
    else holds 53 while the daemon is down. Exit 1 is unreachable for
    us: the charm connects every required plug and always sets a
    password, which is that code's other trigger.
    """
    # GIVEN FTL stopped — the only state in which snap-check looks at
    # ports at all
    juju.exec(f"snap stop {SNAP_NAME}.pihole-ftl", unit=f"{APP_NAME}/0")

    # AND port 53 held by something that persists — a root listener
    # that outlives the exec (privileged port, hence sudo)
    juju.exec(
        # The argv carries a regex-safe marker: pkill -f takes an
        # EXTENDED REGEX, and the first version's `time.sleep(300)`
        # pattern never matched anything — its parentheses were a
        # capture group, so the holder survived its own cleanup and
        # the fixed action correctly refused to report the port free.
        # The marker is a comment INSIDE the python -c string: it
        # travels in the cmdline (where pkill -f looks) without
        # opening a shell comment — the first placement, outside the
        # quotes, swallowed the redirections and the `&`, and the
        # holder ran in the foreground until the exec timed out.
        'sudo nohup python3 -c "'
        "import socket, time; "
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
        "s.bind(('127.0.0.1', 53)); s.listen(1); time.sleep(300)  "
        '# PORT53HOLDER" >/dev/null 2>&1 &',
        unit=f"{APP_NAME}/0",
    )
    time.sleep(2)

    # WHEN snap-check runs against a down daemon and a taken port.
    # jubilant's exec raises TaskError on any non-zero exit, so the
    # code is captured in stdout instead — the shell's last command is
    # the echo, which succeeds.
    result = juju.exec(
        f"{PIHOLE_CMD} snap-check; echo EXITCODE=$?",
        unit=f"{APP_NAME}/0",
    )
    match = re.search(r"EXITCODE=(\d+)", result.stdout)
    assert match is not None, f"no exit code in output: {result.stdout}"
    assert int(match.group(1)) == 2, f"expected exit 2, got {match.group(1)}"

    # AND the remedy works: kill the holder, run the action, bring FTL
    # back, and the unit converges again
    juju.exec("sudo pkill -f PORT53HOLDER || true", unit=f"{APP_NAME}/0")
    juju.run(f"{APP_NAME}/0", "free-port-53")
    juju.exec(f"snap start {SNAP_NAME}.pihole-ftl", unit=f"{APP_NAME}/0")
    juju.wait(settled, timeout=DEPLOY_TIMEOUT)


# -- update-gravity action. --------------------------------------------


def test_update_gravity_runs(juju: jubilant.Juju, deployed: jubilant.Juju):
    """The update-gravity action runs without force."""
    # wait=900: a blocklist download can legitimately take minutes.
    # The action itself has no declared timeout — the test's own
    # wait parameter is what prevents an early kill.
    juju.run(f"{APP_NAME}/0", "update-gravity", wait=900)

    # After the action, gravity.db should exist and have content.
    result = juju.exec(
        f"stat -c%s /var/snap/{SNAP_NAME}/current/etc/pihole/gravity.db",
        unit=f"{APP_NAME}/0",
    )
    size = int(result.stdout.strip())
    assert size > 0, "gravity.db is empty after update-gravity"


# -- free-port-53 action. ----------------------------------------------


def test_free_port_53_is_idempotent(juju: jubilant.Juju, deployed: jubilant.Juju):
    """free-port-53 runs successfully on an already-converged unit."""
    juju.run(f"{APP_NAME}/0", "free-port-53")

    # After the action, snap-check should return 0 again.
    result = juju.exec(f"{PIHOLE_CMD} snap-check", unit=f"{APP_NAME}/0")
    assert result.return_code == 0


# -- Gravity timer drop-in. --------------------------------------------


def test_gravity_schedule_drop_in_lands_and_reads_back(
    juju: jubilant.Juju, deployed: jubilant.Juju
):
    """Write the gravity-schedule drop-in and verify it is loaded."""
    schedule = "Sun *-*-* 03:00"
    juju.config(APP_NAME, {"gravity-schedule": schedule})
    juju.wait(settled, timeout=DEPLOY_TIMEOUT)

    # The drop-in must appear in the unit's DropInPaths — the only
    # honest signal that our override is loaded, since the snap's own
    # randomized default arms the timer regardless.
    result = juju.exec(
        f"systemctl show {GRAVITY_TIMER_UNIT} -p DropInPaths --value",
        unit=f"{APP_NAME}/0",
    )
    assert GRAVITY_TIMER_DROP_IN in result.stdout

    # AND the unit is active and idle after convergence.
    status = juju.status()
    assert settled(status), f"unit did not settle after writing the gravity schedule: {status}"


def test_gravity_schedule_unset_removes_drop_in(juju: jubilant.Juju, deployed: jubilant.Juju):
    """Unset gravity-schedule to restore the snap's default timer.

    The drop-in is created HERE, not inherited from the previous
    test: an undeclared order dependency made this test pass vacuously
    when run alone (unsetting an already-unset config about a file
    that was never written).
    """
    juju.config(APP_NAME, {"gravity-schedule": "Sun *-*-* 05:00"})
    juju.wait(settled, timeout=DEPLOY_TIMEOUT)
    pre = juju.exec(
        f"test -f {GRAVITY_TIMER_DROP_IN} && echo exists || echo absent",
        unit=f"{APP_NAME}/0",
    )
    assert pre.stdout.strip() == "exists", "precondition: the drop-in must exist"

    # WHEN the schedule is unset
    juju.config(APP_NAME, {"gravity-schedule": ""})
    juju.wait(settled, timeout=DEPLOY_TIMEOUT)

    # The drop-in should not exist.
    result = juju.exec(
        f"test -f {GRAVITY_TIMER_DROP_IN} && echo exists || echo absent",
        unit=f"{APP_NAME}/0",
    )
    assert "absent" in result.stdout

    # AND the unit is active and idle after convergence.
    status = juju.status()
    assert settled(status), f"unit did not settle after unsetting the gravity schedule: {status}"
