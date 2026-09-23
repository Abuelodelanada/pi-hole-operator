"""Tests for the workload module.

The theme of this file is non-negotiable 6: **an exit code is never
evidence.** `snap set` returns 0 on keys it silently drops, and
`pihole -a -p` prints usage and exits 0. So most of these tests hand
`Pihole` a collaborator that reports success while changing nothing,
and assert that the charm refuses to believe it.

Nothing here goes near `ops`. The collaborators are injected rather than
patched, which is what makes "a workload that lies" expressible at all.
"""

import inspect
import pathlib
import socket
import subprocess
import urllib.error
from collections.abc import Callable, Sequence

import pytest
import tenacity
from charmlibs import snap, systemd

import pihole
import pihole_state
import resolved
from pihole_state import (
    SNAP_REVISIONS,
    ApiFacts,
    PasswordAccepted,
    PasswordUnset,
    ServiceStatus,
    SnapCheckConfigError,
    SnapCheckOk,
    SnapCheckRuntimeError,
)
from tests.unit.conftest import (
    AUTH_OK,
    BLOCKING_OK,
    CLI_PW,
    LOGOUT_OK,
    NEW_HASH,
    OLD_HASH,
    PASSWORD,
    REVISION,
    SID,
    VERSION,
    FakeCache,
    FakeClock,
    FakeResponse,
    FakeRunner,
    FakeSnap,
    api,
    http_error,
    write_cli_pw,
    write_pihole_toml,
)

MOUNT_FAILURE = 'Mount snap "snapd" (27591): wrong fs type, bad option, bad superblock'
"""What snapd really says in a 26.04 LXD container.

The container has no `/dev/loop*` and 26.04 snapd no longer falls back
to its fuse mounter. See ADR-0002 section 2.2.2.
"""


# -- Facts. -----------------------------------------------------------


def test_an_installed_snap_reports_its_revision_and_version(
    workload: pihole.Pihole,
):
    # GIVEN an installed snap
    # WHEN the machine is read
    # THEN the facts come from snapd rather than from anything cached
    assert workload.installed_revision() == REVISION
    assert workload.workload_version() == VERSION


def test_an_uninstalled_snap_reports_no_revision(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a snap snapd knows about but has not installed
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(present=False)),
        run=fake_runner,
        snap_data=snap_data,
    )

    # WHEN the machine is read
    # THEN it is absent, which is the state `compute` bootstraps from
    assert workload.installed_revision() is None


def test_a_snapd_failure_is_reported_as_absent_not_raised(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a machine where snapd cannot describe the snap at all
    workload = pihole.Pihole(
        cache_factory=FakeCache(None),
        run=fake_runner,
        snap_data=snap_data,
    )

    # WHEN the facts are read
    # THEN reading facts never raises: a reconcile that cannot see the
    # machine still has to be able to report a status
    assert workload.installed_revision() is None
    assert workload.workload_version() is None
    assert workload.ftl_status() == ServiceStatus(enabled=False, active=False)


def test_the_ftl_service_state_comes_from_snapd(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
):
    # GIVEN a snap whose daemon is enabled but not running
    fake_snap.enabled = True
    fake_snap.active = False

    # WHEN the service is read
    # THEN both facts are reported separately, because the snap ships
    # the daemon disabled and "enabled" is not "running"
    assert workload.ftl_status() == ServiceStatus(enabled=True, active=False)


def test_a_missing_ftl_service_is_not_treated_as_running(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
):
    # GIVEN a snap that reports no services at all
    fake_snap.has_ftl_service = False

    # WHEN the service is read
    assert workload.ftl_status() == ServiceStatus(enabled=False, active=False)


def test_the_stub_listener_fact_comes_from_the_drop_in(
    workload: pihole.Pihole,
    drop_in: pathlib.Path,
):
    # GIVEN a machine where port 53 has not been freed
    assert workload.port53_released() is False

    # WHEN the drop-in is written
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text(resolved.DROP_IN_CONTENT, encoding="utf-8")

    # THEN the workload module reports it
    assert workload.port53_released() is True


def test_snap_check_returns_its_exit_code_verbatim(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a diagnostic that reports a runtime error with output
    runner = FakeRunner(returncode=2)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN it is run
    result = workload.snap_check()

    # THEN the semantic exit code and output survive, and the
    # diagnostic is asked for by its fully qualified name — the
    # `pihole` alias never registers
    assert isinstance(result, SnapCheckRuntimeError)
    assert result.output == ""
    assert runner.calls == [[pihole.PIHOLE_CMD, "snap-check"]]


# -- Stage 7.b: DHCP facts. --------------------------------------------


def test_dhcp_active_reads_true(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    """Whether dhcp.active is true in pihole.toml."""
    # GIVEN a pihole.toml with dhcp.active = true
    write_pihole_toml(snap_data, dhcp_active=True)

    # WHEN the fact is read
    # THEN it is True
    assert workload.dhcp_active() is True


def test_dhcp_active_reads_false(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    """dhcp_active reads false when dhcp.active is false."""
    write_pihole_toml(snap_data, dhcp_active=False)
    assert workload.dhcp_active() is False


def test_dhcp_active_absent_is_none(
    workload: pihole.Pihole,
):
    """dhcp_active returns None when the key is absent."""
    assert workload.dhcp_active() is None


def test_dhcp_pool_returns_dhcppool_when_all_four_present(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    """dhcp_pool returns a DhcpPool when all four keys are present."""
    pool = pihole_state.DhcpPool(
        start="192.168.1.10", end="192.168.1.50", router="192.168.1.1", netmask="255.255.255.0"
    )
    write_pihole_toml(snap_data, dhcp_pool=pool)

    result = workload.dhcp_pool()
    assert result == pool


def test_dhcp_pool_returns_none_when_any_key_missing(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    """dhcp_pool returns None when any of the four keys is absent."""
    # GIVEN a pihole.toml with only dhcp.active, no pool keys
    write_pihole_toml(snap_data, dhcp_active=True)

    # WHEN the fact is read
    # THEN it is None — a partial pool is no pool
    assert workload.dhcp_pool() is None


def test_machine_ipv4_addresses_parses_ip_output(
    snap_data: pathlib.Path,
    drop_in: pathlib.Path,
):
    """machine_ipv4_addresses parses `ip -4 -o addr show` output."""
    # GIVEN a scripted ip output with a loopback line and two real
    # interfaces
    ip_output = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       "
        "valid_lft forever preferred_lft forever\n"
        "2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0\\       "
        "valid_lft forever preferred_lft forever\n"
        "3: eth1    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth1\\       "
        "valid_lft forever preferred_lft forever\n"
    )

    def script(args: Sequence[str]) -> str:
        if args[0] == pihole.IP_CMD:
            return ip_output
        return ""

    runner = FakeRunner(stdout_script=script)
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap()),
        run=runner,
        snap_data=snap_data,
        resolved_drop_in=drop_in,
    )

    # WHEN the fact is read
    addrs = workload.machine_ipv4_addresses()

    # THEN lo is skipped, and the two real addresses are returned
    assert addrs == frozenset({"10.0.0.5", "192.168.1.100"})


def test_machine_ipv4_addresses_excludes_link_and_host_scope(
    snap_data: pathlib.Path,
    drop_in: pathlib.Path,
):
    """Only global-scope addresses are servable.

    A link-local (169.254/16) or host-scope address on a real
    interface must not count as servable: DHCP clients cannot reach
    either, so the unservable gate would be fooled into letting a
    pool through that FTL cannot serve.
    """
    # GIVEN an ip output with a link-local and a host-scope address
    # on real interfaces, plus one global address
    ip_output = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       "
        "valid_lft forever preferred_lft forever\n"
        "2: eth0    inet 169.254.9.9/16 brd 169.254.255.255 scope link eth0\\       "
        "valid_lft forever preferred_lft forever\n"
        "3: eth1    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth1\\       "
        "valid_lft forever preferred_lft forever\n"
        "4: eth2    inet6 fe80::1/64 scope link eth2\\       "
        "valid_lft forever preferred_lft forever\n"
        "5: eth3    inet 10.1.1.1/24 brd 10.1.1.255 eth3\\       "
        "valid_lft forever preferred_lft forever\n"
    )

    def script(args: Sequence[str]) -> str:
        if args[0] == pihole.IP_CMD:
            return ip_output
        return ""

    runner = FakeRunner(stdout_script=script)
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap()),
        run=runner,
        snap_data=snap_data,
        resolved_drop_in=drop_in,
    )

    # WHEN the fact is read
    addrs = workload.machine_ipv4_addresses()

    # THEN only the global-scope address is returned
    assert addrs == frozenset({"10.0.0.5"})


def test_machine_ipv4_addresses_returns_none_on_command_failure(
    snap_data: pathlib.Path,
    drop_in: pathlib.Path,
):
    """machine_ipv4_addresses returns None on nonzero exit."""
    # GIVEN an `ip` command that fails
    runner = FakeRunner(returncode=1)
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap()),
        run=runner,
        snap_data=snap_data,
        resolved_drop_in=drop_in,
    )

    # WHEN the fact is read
    addrs = workload.machine_ipv4_addresses()

    # THEN it is None — the read failed, which is not "no addresses"
    assert addrs is None


def test_machine_ipv4_addresses_returns_none_on_oserror(
    snap_data: pathlib.Path,
    drop_in: pathlib.Path,
):
    """machine_ipv4_addresses returns None on OSError."""

    # GIVEN an `ip` command that cannot be run at all
    def raise_oserror(_args: Sequence[str]) -> None:
        raise OSError("no such command")

    runner = FakeRunner(effect=raise_oserror)
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap()),
        run=runner,
        snap_data=snap_data,
        resolved_drop_in=drop_in,
    )

    # WHEN the fact is read
    addrs = workload.machine_ipv4_addresses()

    # THEN it is None — the read failed, which is not "no addresses"
    assert addrs is None


def test_port67_free_probes_the_port(
    snap_data: pathlib.Path,
    drop_in: pathlib.Path,
):
    """port67_free probes 67/udp; a successful bind means free."""
    # GIVEN a probe that binds successfully
    probed: list[int] = []

    def probe(port: int) -> bool:
        probed.append(port)
        return True

    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap()),
        run=FakeRunner(),
        snap_data=snap_data,
        resolved_drop_in=drop_in,
        probe_udp_port=probe,
    )

    # WHEN the fact is read
    free = workload.port67_free()

    # THEN the probe targeted 67/udp and reported the port free
    assert probed == [67]
    assert free is True


def test_port67_free_reports_taken_when_bind_fails(
    snap_data: pathlib.Path,
    drop_in: pathlib.Path,
):
    """port67_free reports False when the bind raises EADDRINUSE."""

    # GIVEN a probe whose bind fails (the port is held)
    def probe(port: int) -> bool:
        return False

    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap()),
        run=FakeRunner(),
        snap_data=snap_data,
        resolved_drop_in=drop_in,
        probe_udp_port=probe,
    )

    # WHEN the fact is read
    free = workload.port67_free()

    # THEN it reports the port taken — the probe is the pre-flight gate
    assert free is False


def test_bind_probe_succeeds_on_a_free_port():
    """The real probe reports True when nothing holds the port."""
    # GIVEN a port nothing holds (0 = any free port)
    # WHEN the probe runs
    # THEN it reports free
    assert pihole._bind_probe(0) is True  # pyright: ignore[reportPrivateUsage]


def test_bind_probe_reports_taken_when_port_is_held():
    """The real probe reports False when the port is held."""
    # GIVEN a port held by a bound socket (the wildcard 0.0.0.0 bind
    # conflicts with any specific binding)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        # WHEN the probe runs against that port
        # THEN it reports taken
        assert pihole._bind_probe(port) is False  # pyright: ignore[reportPrivateUsage]


# -- Install and start. -----------------------------------------------


def test_install_ensures_the_snap_at_the_pinned_revision(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
):
    # GIVEN a machine with nothing installed
    fake_snap.present = False

    # WHEN the snap is installed
    workload.install()

    # THEN snapd was asked for the charm's pinned revision (ADR-0010),
    # with no channel: the revision is the identity, not the channel
    assert fake_snap.ensure_calls == [(snap.SnapState.Present, None, SNAP_REVISIONS["amd64"])]


def test_install_does_not_believe_snapd_without_a_revision(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a snapd that accepts the install and installs nothing
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(present=False, honest=False)),
        run=fake_runner,
        snap_data=snap_data,
    )

    # WHEN the snap is installed
    # THEN the read-back catches it: the pin is proven, not assumed
    with pytest.raises(pihole.PiholeError, match="snapd reports revision"):
        workload.install()


def test_install_retries_a_flaky_store_a_bounded_number_of_times(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a store that fails twice and then works
    cache = FakeCache(FakeSnap(present=False), errors=2)
    workload = pihole.Pihole(
        cache_factory=cache,
        run=fake_runner,
        snap_data=snap_data,
        retry_wait=tenacity.wait_none(),
    )

    # WHEN the snap is installed
    workload.install()

    # THEN it was retried in-hook rather than left to Juju, whose
    # automatic retry is model configuration we cannot rely on
    assert cache.calls == 4


def test_install_gives_up_rather_than_retrying_forever(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a store that is simply down
    cache = FakeCache(FakeSnap(present=False), errors=99)
    workload = pihole.Pihole(
        cache_factory=cache,
        run=fake_runner,
        snap_data=snap_data,
        retry_wait=tenacity.wait_none(),
    )

    # WHEN the snap is installed
    # THEN it gives up after a bounded number of tries, and what
    # escapes is ours rather than charmlibs'. `charm.py` cannot catch
    # `snap.Error` without importing `charmlibs`, so an unconverted one
    # would reach error state — and a unit in error needs `--force` to
    # remove, which skips the handler that gives the host its resolver
    # back (ADR-0005 section 2.9).
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.install()
    assert cache.calls == pihole.INSTALL_ATTEMPTS

    # AND the store's own message survives, with somewhere to look
    assert "the snap store is having a moment" in str(exc_info.value)
    assert "journalctl -u snapd" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, snap.Error)


@pytest.mark.parametrize(
    "error",
    [
        snap.SnapError("the store is having a moment"),
        snap.SnapAPIError({}, 500, "error", "snapd returned an error response"),
        snap.SnapNotFoundError("not in the store right now"),
    ],
    ids=["SnapError", "SnapAPIError", "SnapNotFoundError"],
)
def test_install_retries_every_snap_error_not_just_snap_error(
    fake_snap: FakeSnap,
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
    error: Exception,
):
    """Retrying ``snap.SnapError`` alone silently skips its siblings.

    Verified against charmlibs-snap 1.0.1: ``SnapError``,
    ``SnapAPIError`` and ``SnapNotFoundError`` all inherit
    directly from ``Error``, and none is a subclass of another.
    Retrying only the first leaves a store or lookup failure
    un-retried, so the unit goes to error state -- and a unit in
    error needs ``--force`` to remove, which skips the cleanup
    that restores host DNS.
    """
    # GIVEN a store that fails twice with one of the sibling errors
    cache = FakeCache(fake_snap, errors=2, error=error)
    workload = pihole.Pihole(
        cache_factory=cache,
        run=fake_runner,
        snap_data=snap_data,
        retry_wait=tenacity.wait_none(),
    )

    # WHEN the snap is installed
    workload.install()

    # THEN both injected failures were retried rather than
    # escaping, and the install completed. Asserting on consumed
    # errors rather than a call count keeps this independent of
    # how often install() reaches for the cache.
    assert cache.remaining_errors == 0
    assert fake_snap.ensure_calls == [(snap.SnapState.Present, None, SNAP_REVISIONS["amd64"])]


# -- Which remedy an install failure names. ----------------------------
#
# A container is the one install failure the charm can fully explain,
# and ADR-0005 section 2.2 says Blocked exists for exactly that: a
# situation where the charm can tell the human what to do. Pointing that
# operator at `snap changes` sends them to read a squashfs mount failure
# whose real answer is "you are in a container". Only the **remedy**
# changes — snapd's own words still travel, because a container is not
# the only reason an install fails.


@pytest.mark.parametrize(
    ("in_container", "expected"),
    [(True, pihole.CONTAINER_REMEDY), (False, pihole.SNAPD_REMEDY)],
    ids=["container", "vm-or-bare-metal"],
)
def test_the_install_remedy_is_a_pure_choice(in_container: bool, expected: str):
    # GIVEN nothing but a fact about the machine
    # WHEN the remedy is chosen
    # THEN it is decided without executing anything, which is why the
    # choice is a function and the detection is not
    assert pihole.install_remedy(in_container=in_container) == expected


def test_an_install_failure_in_a_container_names_the_constraint_to_redeploy_with(
    snap_data: pathlib.Path,
):
    # GIVEN a 26.04 LXD container, where snapd can mount no snap at all
    # — not even `snapd` itself — and the mount failure it really
    # reports there (ADR-0002 section 2.2.2)
    runner = FakeRunner(container="lxc")
    workload = pihole.Pihole(
        cache_factory=FakeCache(
            FakeSnap(present=False), errors=99, error=snap.SnapError(MOUNT_FAILURE)
        ),
        run=runner,
        snap_data=snap_data,
        retry_wait=tenacity.wait_none(),
    )

    # WHEN the snap is installed
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.install()

    # THEN the operator is given the one thing that fixes it, verbatim
    assert pihole.CONTAINER_REMEDY in str(exc_info.value)
    assert "--constraints virt-type=virtual-machine" in str(exc_info.value)

    # AND the diagnosis is untouched: snapd's own words still travel,
    # because they are the part that distinguishes this from the next
    # install failure
    assert MOUNT_FAILURE in str(exc_info.value)

    # AND the question asked was about containers only, since a VM is
    # virtualisation this charm is perfectly happy with
    assert [pihole.DETECT_VIRT_CMD, "--container"] in runner.calls


def test_an_install_failure_outside_a_container_keeps_the_snapd_remedy(
    snap_data: pathlib.Path,
):
    # GIVEN a VM or bare metal, where `systemd-detect-virt --container`
    # exits non-zero, and a store that is simply down
    runner = FakeRunner()
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(present=False), errors=99),
        run=runner,
        snap_data=snap_data,
        retry_wait=tenacity.wait_none(),
    )

    # WHEN the snap is installed
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.install()

    # THEN the remedy is the one that was always there, and the operator
    # is not told to redeploy a machine that is already a VM
    assert pihole.SNAPD_REMEDY in str(exc_info.value)
    assert "virt-type" not in str(exc_info.value)


def test_an_install_that_lands_nothing_in_a_container_names_the_constraint(
    snap_data: pathlib.Path,
):
    # GIVEN a container, and a snapd that accepts the install and
    # installs nothing — the read-back path rather than the raise
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(present=False, honest=False)),
        run=FakeRunner(container="lxc"),
        snap_data=snap_data,
    )

    # WHEN the snap is installed
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.install()

    # THEN both ways an install can fail name the same remedy
    assert "snapd reports revision" in str(exc_info.value)
    assert pihole.CONTAINER_REMEDY in str(exc_info.value)


def test_a_missing_systemd_detect_virt_is_never_the_reason_a_hook_fails(
    snap_data: pathlib.Path,
):
    # GIVEN a machine without the detection binary at all, and a store
    # that is down. A diagnostic that raises would turn a Blocked unit
    # into a unit in error state — which needs `--force` to remove,
    # which skips the handler that gives the host its resolver back.
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(present=False), errors=99),
        run=FakeRunner(detect_virt_error=FileNotFoundError(2, "No such file or directory")),
        snap_data=snap_data,
        retry_wait=tenacity.wait_none(),
    )

    # WHEN the snap is installed
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.install()

    # THEN the failure reported is the install's, with the remedy that
    # applied before any of this existed — not the OSError from the
    # helper
    assert pihole.SNAPD_REMEDY in str(exc_info.value)
    assert "the snap store is having a moment" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, snap.Error)


def test_a_successful_install_does_not_exec_the_diagnostic(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
    fake_runner: FakeRunner,
):
    # GIVEN a machine with nothing installed
    fake_snap.present = False

    # WHEN the install succeeds
    workload.install()

    # THEN nothing was run: the remedy is chosen after a failure, so the
    # healthy path pays nothing for it
    assert fake_runner.calls == []


def test_start_enables_the_service_the_snap_ships_disabled(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
):
    # GIVEN an installed snap whose daemon has never run
    fake_snap.active = False
    fake_snap.enabled = False

    # WHEN it is started
    workload.start()

    # THEN it was started *and enabled*: the snap ships
    # `install-mode: disable`, so a charm that only installs has a
    # Pi-hole that never runs, and one that starts without enabling has
    # a Pi-hole that does not survive a reboot
    assert fake_snap.start_calls == [(["pihole-ftl"], True)]


def test_enabling_the_daemon_cannot_be_switched_off_by_accident():
    # GIVEN the signature of the start effect
    parameters = inspect.signature(pihole.Pihole.start).parameters

    # WHEN the way `enable` may be passed is inspected
    # THEN it is keyword-only. A positional `start(False)` reads like
    # "start it" and produces a Pi-hole that does not come back after a
    # reboot, which is a silence the type checker should not allow.
    assert parameters["enable"].kind is inspect.Parameter.KEYWORD_ONLY


def test_start_does_not_believe_snapd_without_an_active_service(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a snapd that accepts the start and starts nothing, which is
    # what an EADDRINUSE crash loop looks like from here
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(active=False, honest=False)),
        run=fake_runner,
        snap_data=snap_data,
    )

    # WHEN it is started
    # THEN the failure names the usual cause
    with pytest.raises(pihole.PiholeError, match="EADDRINUSE"):
        workload.start()


# -- Nothing charmlibs raises may leave this module. ------------------
#
# `charm.py` catches `pihole.PiholeError` and `resolved.ResolvedError`
# and nothing else, because widening that tuple would mean importing
# `charmlibs` into the charm module. So anything that escapes from here
# as a `snap.Error` or an `OSError` reaches Juju as error state — and a
# unit in error needs `--force` to remove, which skips the handler that
# gives the host its resolver back. See ADR-0005 section 2.9.


SNAPD_EFFECTS: list[tuple[Callable[[pihole.Pihole], None], str]] = [
    (lambda workload: workload.install(), "installing"),
    (lambda workload: workload.start(), "starting"),
    (
        lambda workload: workload.set_ntp_server(active=False),
        "disabling the FTL NTP server",
    ),
]
"""Every effect that reaches snapd, and the operation it should name."""


@pytest.mark.parametrize(
    ("effect", "operation"),
    SNAPD_EFFECTS,
    ids=["install", "start", "set_ntp_server"],
)
def test_no_effect_lets_a_snapd_lookup_failure_escape(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
    effect: Callable[[pihole.Pihole], None],
    operation: str,
):
    # GIVEN a snapd that cannot describe the snap at all, which is what
    # `_require_snap` propagates raw so that `install` can retry it
    workload = pihole.Pihole(
        cache_factory=FakeCache(None),
        run=fake_runner,
        snap_data=snap_data,
        retry_wait=tenacity.wait_none(),
    )

    # WHEN each effect is attempted
    # THEN what escapes is ours, names what was being attempted, and
    # keeps the original as its cause for the log
    with pytest.raises(pihole.PiholeError) as exc_info:
        effect(workload)
    assert operation in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, snap.Error)


def test_a_snapd_refusal_to_start_the_daemon_is_named_rather_than_re_raised(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a snapd that refuses the start outright. `Snap.start`
    # reaches `subprocess.run(check=True)` by way of `_snap_daemons`, so
    # a systemctl failure arrives here as a `snap.Error` — not as the
    # inactive-service case the read-back already covers.
    workload = pihole.Pihole(
        cache_factory=FakeCache(
            FakeSnap(active=False, refusal=snap.SnapError("cannot start service"))
        ),
        run=fake_runner,
        snap_data=snap_data,
    )

    # WHEN the daemon is started
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.start()

    # THEN the operator is pointed at the logs rather than at a
    # traceback, and the unit stays removable
    assert "snap logs" in str(exc_info.value)
    assert "cannot start service" in str(exc_info.value)


def test_a_missing_pihole_wrapper_is_named_and_never_quotes_the_password(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a machine where the `pihole` wrapper is not on disk, which
    # is what a half-installed snap looks like from here. `OSError` is
    # not a `snap.Error` and not a `CalledProcessError`, so nothing else
    # would have caught it.
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(effect=_missing_wrapper),
        snap_data=snap_data,
    )

    # WHEN the password is applied
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.set_password(PASSWORD)

    # THEN the failure names the snap to check, and the password is not
    # in the message that reaches juju-log
    assert "could not be run" in str(exc_info.value)
    assert pihole.SNAP_NAME in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


def test_a_diagnostic_that_cannot_run_is_not_reported_as_an_exit_code(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a machine where the `pihole` wrapper is not on disk
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(effect=_missing_wrapper),
        snap_data=snap_data,
    )

    # WHEN the diagnostic is run
    # THEN it says it could not run, rather than inventing one of the
    # semantic exit codes it did not receive
    with pytest.raises(pihole.PiholeError, match="could not be run"):
        workload.snap_check()


def _missing_wrapper(_args: Sequence[str]) -> None:
    """Stand in for a `pihole` wrapper that is not on disk at all."""
    raise FileNotFoundError(2, "No such file or directory", pihole.PIHOLE_CMD)


# -- The one snap set, and the password. ------------------------------


def test_closing_the_ntp_server_verifies_pihole_toml(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap whose configure hook actually applies the values
    write_pihole_toml(snap_data, ntp_active=False)

    # WHEN the NTP server is closed
    workload.set_ntp_server(active=False)

    # THEN both keys went through the `ftl.` namespace, as real
    # booleans rather than strings — a string "False" would not parse
    # as TOML false on FTL's side
    assert fake_snap.set_calls == [{"ftl.ntp.ipv4.active": False, "ftl.ntp.ipv6.active": False}]


def test_an_ntp_server_still_enabled_after_the_set_is_caught(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap that accepts the keys and keeps the old value — the
    # verified behaviour of `snap set` on keys it drops
    write_pihole_toml(snap_data, ntp_active=True)

    # WHEN the NTP server is closed
    # THEN the charm refuses to believe the exit code
    with pytest.raises(pihole.PiholeError, match="not proven off"):
        workload.set_ntp_server(active=False)


def test_an_ntp_key_absent_from_the_toml_is_not_evidence_it_is_off(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    # GIVEN a pihole.toml where only one of the two keys landed. FTL's
    # default for both is true, so a missing key can mean the write
    # never happened — absence is not evidence of a closed port.
    write_pihole_toml(snap_data, raw="[ntp.ipv4]\nactive = false\n")

    # WHEN the NTP server is closed
    # THEN the read-back refuses to declare victory on half the answer
    with pytest.raises(pihole.PiholeError, match="not proven off"):
        workload.set_ntp_server(active=False)


def test_enabling_the_ntp_server_verifies_pihole_toml(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap whose configure hook actually applies the values
    write_pihole_toml(snap_data, ntp_active=True)

    # WHEN the NTP server is enabled
    workload.set_ntp_server(active=True)

    # THEN both keys are set to True
    assert fake_snap.set_calls == [{"ftl.ntp.ipv4.active": True, "ftl.ntp.ipv6.active": True}]


def test_an_ntp_server_still_disabled_after_set_is_caught(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap that accepts the keys and keeps the old value
    write_pihole_toml(snap_data, ntp_active=False)

    # WHEN the NTP server is enabled
    # THEN the charm refuses to believe the exit code
    with pytest.raises(pihole.PiholeError, match="not proven on"):
        workload.set_ntp_server(active=True)


def test_an_unreadable_toml_reports_an_unknown_ntp_state(
    workload: pihole.Pihole,
):
    # GIVEN a machine whose pihole.toml does not exist yet
    # WHEN the fact is read
    # THEN it is None, not False: "cannot read" and "confirmed closed"
    # are different answers, and only the caller that knows the
    # context may decide what unknown means
    assert workload.ntp_server_active() is None


@pytest.mark.parametrize(
    ("ntp_active", "expected"),
    [(False, False), (True, True)],
    ids=["closed", "open"],
)
def test_a_readable_toml_reports_what_it_says(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    ntp_active: bool,
    expected: bool,
):
    # GIVEN a pihole.toml whose NTP keys can be read
    write_pihole_toml(snap_data, ntp_active=ntp_active)

    # WHEN the fact is read
    # THEN it answers exactly what the file says, on either key
    assert workload.ntp_server_active() is expected


def test_setting_the_password_uses_the_v6_command_and_never_snap_set(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a Pi-hole that hashes the password into pihole.toml
    write_pihole_toml(snap_data, pwhash=OLD_HASH)

    def rehash(_args: object) -> None:
        write_pihole_toml(snap_data, pwhash=NEW_HASH)

    runner = FakeRunner(effect=rehash)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN the password is applied
    workload.set_password(PASSWORD)

    # THEN it went through `pihole setpassword`, so the plaintext never
    # reaches snapd state, where anyone with snapd access could read it
    assert runner.calls == [[pihole.PIHOLE_CMD, "setpassword", PASSWORD]]
    assert fake_snap.set_calls == []


def test_the_password_never_reaches_a_v5_flag(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a Pi-hole that hashes the password into pihole.toml
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    runner = FakeRunner(effect=lambda _args: write_pihole_toml(snap_data, pwhash=NEW_HASH))
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN the password is applied
    workload.set_password(PASSWORD)

    # THEN `pihole -a -p` never appears. It is v5 syntax that prints
    # usage and exits 0, so a charm using it reports success having
    # done nothing at all.
    argv = [argument for call in runner.calls for argument in call]
    assert "-a" not in argv
    assert "-p" not in argv
    assert "restartdns" not in argv


def test_a_password_that_does_not_change_the_hash_is_a_failure(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    # GIVEN a command that exits 0 and leaves pihole.toml alone, which
    # is what v5 syntax does
    write_pihole_toml(snap_data, pwhash=OLD_HASH)

    # WHEN the password is applied
    # THEN the read-back catches it: the salt is random, so a genuine
    # write always produces a different hash
    with pytest.raises(pihole.PiholeError, match="the hash did not change"):
        workload.set_password(PASSWORD)


def test_an_empty_pwhash_after_setting_a_password_is_a_failure(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    # GIVEN a Pi-hole with no password, and a command that changes
    # nothing
    write_pihole_toml(snap_data, pwhash="")

    # WHEN the password is applied
    # THEN the charm says so, because a daemon serving with an empty
    # pwhash accepts configuration writes from the whole network
    with pytest.raises(pihole.PiholeError, match="pwhash is still empty"):
        workload.set_password(PASSWORD)


def test_a_failing_setpassword_does_not_leak_the_password(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a command that fails
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(returncode=1),
        snap_data=snap_data,
    )

    # WHEN the password is applied
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.set_password(PASSWORD)

    # THEN the error names the exit code but not the password.
    # `CalledProcessError` stringifies the whole argv, so it must not be
    # chained into anything that reaches juju-log.
    assert "exited 1" in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


# -- The HTTP API (delegation coverage). ------------------------------
#
# The session-level cases moved to `test_ftl_api.py`. These five
# exercise the four delegating methods on `Pihole` — the forwarders
# plus the `ApiTimeoutError` → `PiholeError` conversion, which is the
# only interesting logic in those four. See ADR-0009 section 4.


def test_readiness_is_gated_on_the_api_answering(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole whose API answers
    write_cli_pw(snap_data, CLI_PW)
    fake = api(monkeypatch, {**AUTH_OK, **BLOCKING_OK, **LOGOUT_OK})

    # WHEN readiness is checked
    assert workload.api_ready() is True

    # THEN it authenticated with the CLI password, presented the
    # session on the request that matters, and gave the session back
    assert [request.route for request in fake.requests] == [
        "POST auth",
        "GET dns/blocking",
        "DELETE auth",
    ]
    assert fake.requests[0].body == {"password": CLI_PW}
    assert fake.requests[1].sid == SID


def test_an_empty_pwhash_is_classified_without_asking_the_api(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole with no password set
    write_pihole_toml(snap_data, pwhash="")
    fake = api(monkeypatch, {**AUTH_OK, **LOGOUT_OK})

    # WHEN the password is classified
    state = workload.admin_password_state(PASSWORD)

    # THEN the hash is read first and the API is not consulted at all:
    # while pwhash is empty FTL accepts *any* password, so the oracle
    # would answer 200 for a credential nobody ever set
    assert state == PasswordUnset()
    assert fake.requests == []


def test_both_api_facts_come_out_of_one_session(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a serving Pi-hole with the charm's password set
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    write_cli_pw(snap_data, CLI_PW)
    fake = api(monkeypatch, {**AUTH_OK, **BLOCKING_OK, **LOGOUT_OK})

    # WHEN both API facts are read
    facts = workload.api_facts(PASSWORD)

    # THEN they agree with the machine
    assert facts == ApiFacts(admin_password=PasswordAccepted(), api_ready=True)

    # AND exactly one session was opened and given back: the oracle's
    # own session answers the readiness endpoint too, which halves what
    # a hook spends out of FTL's 16 slots
    assert [request.route for request in fake.requests] == [
        "POST auth",
        "GET dns/blocking",
        "DELETE auth",
    ]
    assert fake.requests[0].body == {"password": PASSWORD}
    assert fake.requests[1].sid == SID


def test_awaiting_the_api_returns_as_soon_as_it_answers(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that answers
    write_cli_pw(snap_data, CLI_PW)
    api(monkeypatch, {**AUTH_OK, **BLOCKING_OK, **LOGOUT_OK})

    # WHEN the gate is waited on
    # THEN it returns without raising
    workload.await_api(timeout=0.0)


def test_awaiting_the_api_gives_up_and_points_at_the_log(
    workload: pihole.Pihole,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that never comes up
    api(monkeypatch, {})

    # WHEN the gate is waited on
    # THEN it gives up rather than hanging the hook forever, and names
    # where to look
    with pytest.raises(pihole.PiholeError, match=r"FTL\.log"):
        workload.await_api(timeout=0.0)


# -- wait_for_dhcp_bind. -----------------------------------------------


def _ss_output(*rows: str) -> str:
    """Render `ss -lunp` output with the header and the given rows."""
    header = "State   Recv-Q  Send-Q  Local Address:Port  Peer Address:Port  Process\n"
    return header + "".join(rows)


def _bound_row() -> str:
    """A listening 67/udp row owned by pihole-FTL."""
    return (
        "UNCONN  0       0       0.0.0.0:67          0.0.0.0:*          "
        'users:(("pihole-FTL",pid=1234,fd=5))\n'
    )


def test_wait_for_dhcp_bind_returns_when_ftl_holds_the_port(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """Ss naming pihole-FTL on 67/udp ends the wait."""
    # GIVEN ss output naming pihole-FTL on 0.0.0.0:67
    runner = FakeRunner(stdout_script=lambda args: _ss_output(_bound_row()))
    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    workload.wait_for_dhcp_bind(timeout=5.0)

    # THEN it returns without raising, and never slept
    assert clock.sleeps == []


def test_wait_for_dhcp_bind_retries_until_the_bind_appears(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """A later poll naming pihole-FTL ends the wait."""
    # GIVEN ss output that names pihole-FTL only on the second poll
    polls = {"count": 0}

    def script(args: Sequence[str]) -> str:
        polls["count"] += 1
        return _ss_output(_bound_row()) if polls["count"] >= 2 else _ss_output()

    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(stdout_script=script),
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    workload.wait_for_dhcp_bind(timeout=5.0)

    # THEN it polled twice and slept once between the polls
    assert polls["count"] == 2
    assert clock.sleeps == [pihole.DHCP_BIND_POLL_INTERVAL]


def test_wait_for_dhcp_bind_times_out_when_ftl_never_binds(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """Ss never naming pihole-FTL → PiholeError after the deadline."""
    # GIVEN ss output with no DHCP listener (only the DNS one)
    ss_output = _ss_output(
        "UNCONN  0       0       0.0.0.0:53          0.0.0.0:*          "
        'users:(("pihole-FTL",pid=1234,fd=5))\n'
    )
    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(stdout_script=lambda args: ss_output),
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs past its deadline
    with pytest.raises(pihole.PiholeError) as excinfo:
        workload.wait_for_dhcp_bind(timeout=5.0)

    # THEN it names the port and where to look
    assert "67/udp" in str(excinfo.value)
    assert "snap logs" in str(excinfo.value)
    # AND it polled until the deadline: 5.0s / 2.0s interval = 3 sleeps
    assert clock.sleeps == [pihole.DHCP_BIND_POLL_INTERVAL] * 3


def test_wait_for_dhcp_bind_ignores_a_socket_on_port_6700(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """A :6700 listener must not satisfy the :67 check."""
    # GIVEN ss output with pihole-FTL on 6700/udp, not 67/udp
    ss_output = _ss_output(
        "UNCONN  0       0       0.0.0.0:6700         0.0.0.0:*          "
        'users:(("pihole-FTL",pid=1234,fd=5))\n'
    )
    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(stdout_script=lambda args: ss_output),
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    # THEN it times out — a substring match would have returned early
    with pytest.raises(pihole.PiholeError):
        workload.wait_for_dhcp_bind(timeout=1.0)


def test_wait_for_dhcp_bind_accepts_the_ipv6_listener_form(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """The `[::]:67` form ss can print also satisfies the check."""
    # GIVEN ss output naming pihole-FTL on [::]:67
    ss_output = _ss_output(
        "UNCONN  0       0       [::]:67             [::]:*              "
        'users:(("pihole-FTL",pid=1234,fd=5))\n'
    )
    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(stdout_script=lambda args: ss_output),
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    # THEN it returns without raising
    workload.wait_for_dhcp_bind(timeout=5.0)


def test_wait_for_dhcp_bind_tolerates_a_failed_ss_read(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """A failed ss read is 'not held', not a crash — it polls on."""

    # GIVEN ss that cannot be run at all
    def failing_run(args: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise OSError("no ss on this machine")

    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=failing_run,
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    # THEN it keeps polling and the timeout names the failure
    with pytest.raises(pihole.PiholeError, match=r"67/udp"):
        workload.wait_for_dhcp_bind(timeout=1.0)


def test_wait_for_dhcp_bind_tolerates_a_nonzero_ss_exit(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """A non-zero ss exit is 'not held', not a crash — it polls on."""
    # GIVEN ss that exits non-zero
    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(returncode=1),
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    # THEN it keeps polling and the timeout names the failure
    with pytest.raises(pihole.PiholeError, match=r"67/udp"):
        workload.wait_for_dhcp_bind(timeout=1.0)


def test_wait_for_dhcp_bind_skips_malformed_ss_lines(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """A line with too few columns is skipped, not a crash."""
    # GIVEN ss output with a truncated row before the real one
    ss_output = _ss_output(
        "UNCONN  0       0\n",
        _bound_row(),
    )
    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(stdout_script=lambda args: ss_output),
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    # THEN the malformed line is skipped and the real row ends the wait
    workload.wait_for_dhcp_bind(timeout=5.0)
    assert clock.sleeps == []


def test_wait_for_dhcp_bind_does_not_accept_a_foreign_owner(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    """67/udp held by another process is not FTL serving DHCP.

    The whole point of naming the owner: a port taken by dnsmasq is
    the conflict the wait must surface, not a bind to accept.
    """
    # GIVEN ss output with 67/udp held by dnsmasq, not pihole-FTL
    ss_output = _ss_output(
        "UNCONN  0       0       0.0.0.0:67          0.0.0.0:*          "
        'users:(("dnsmasq",pid=4321,fd=7))\n'
    )
    clock = FakeClock()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=FakeRunner(stdout_script=lambda args: ss_output),
        snap_data=snap_data,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # WHEN the wait runs
    # THEN it times out — the port is taken, but not by FTL
    with pytest.raises(pihole.PiholeError, match=r"67/udp"):
        workload.wait_for_dhcp_bind(timeout=1.0)


# -- apply_ftl_config. ------------------------------------------------


def test_apply_ftl_config_applies_and_verifies(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole whose API accepts the config and whose TOML
    # reflects it
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, blocking_active=False, dnssec=False)
    fake = api(
        monkeypatch,
        {
            **AUTH_OK,
            **LOGOUT_OK,
            "PATCH config": FakeResponse(
                200, {"config": {"dns": {"blocking": {"active": False}}}}
            ),
        },
    )

    # WHEN config is applied
    workload.apply_ftl_config(PASSWORD, {"dns.blocking.active": False})

    # THEN the API was called with the right body
    assert [request.route for request in fake.requests] == [
        "POST auth",
        "PATCH config",
        "DELETE auth",
    ]
    assert fake.requests[1].body == {"config": {"dns": {"blocking": {"active": False}}}}


def test_apply_ftl_config_catches_a_lying_api(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The lying-API test: 200 returned, TOML keeps the old value.

    FTL returns 200 for unknown keys and silently ignores them, so
    the read-back is the only defence (rule 6, ADR-0004 section 5.4).
    """
    # GIVEN a Pi-hole whose API returns 200 but whose TOML keeps the
    # old value — the verified unknown-key behaviour
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, blocking_active=True)
    api(
        monkeypatch,
        {
            **AUTH_OK,
            **LOGOUT_OK,
            "PATCH config": FakeResponse(200, {}),
        },
    )

    # WHEN config is applied
    # THEN the read-back catches the lie
    with pytest.raises(pihole.PiholeError, match=r"dns\.blocking\.active"):
        workload.apply_ftl_config(PASSWORD, {"dns.blocking.active": False})


def test_apply_ftl_config_surfaces_a_400_hint_verbatim(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole that rejects the config with a 400 hint
    write_cli_pw(snap_data, CLI_PW)
    api(
        monkeypatch,
        {
            **AUTH_OK,
            **LOGOUT_OK,
            "PATCH config": http_error(400, {"hint": "dns.listeningMode: invalid option"}),
        },
    )

    # WHEN config is applied
    # THEN the hint reaches the error message verbatim
    with pytest.raises(pihole.PiholeError, match=r"dns\.listeningMode: invalid option"):
        workload.apply_ftl_config(PASSWORD, {"dns.listeningMode": "INVALID"})


def test_apply_ftl_config_unknown_key_absent_from_toml(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """FTL ignores unknown keys with 200 — the key is absent from TOML.

    This simulates what happens when a key name is misspelled: the API
    returns 200, the key never appears in pihole.toml, and the
    read-back catches it.
    """
    # GIVEN a Pi-hole whose API returns 200 for an unknown key
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data)
    api(
        monkeypatch,
        {
            **AUTH_OK,
            **LOGOUT_OK,
            "PATCH config": FakeResponse(200, {}),
        },
    )

    # WHEN config is applied with a key FTL does not know
    # THEN the read-back catches the absence
    with pytest.raises(pihole.PiholeError, match=r"absent from pihole\.toml"):
        workload.apply_ftl_config(PASSWORD, {"dns.typoKey": "value"})


def test_apply_ftl_config_upstreams_round_trip(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole whose API accepts upstream config
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, upstreams=["1.1.1.1", "9.9.9.9"])
    api(
        monkeypatch,
        {
            **AUTH_OK,
            **LOGOUT_OK,
            "PATCH config": FakeResponse(200, {}),
        },
    )

    # WHEN upstreams are applied
    workload.apply_ftl_config(PASSWORD, {"dns.upstreams": ("1.1.1.1", "9.9.9.9")})

    # THEN the read-back passes — the TOML matches


def test_apply_ftl_config_upstreams_mismatch(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole whose TOML has different upstreams than applied
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, upstreams=["8.8.8.8"])
    api(
        monkeypatch,
        {
            **AUTH_OK,
            **LOGOUT_OK,
            "PATCH config": FakeResponse(200, {}),
        },
    )

    # WHEN upstreams are applied
    # THEN the mismatch is caught
    with pytest.raises(pihole.PiholeError, match=r"dns\.upstreams"):
        workload.apply_ftl_config(PASSWORD, {"dns.upstreams": ("1.1.1.1",)})


# -- The default runner. ----------------------------------------------


def test_the_default_runner_actually_runs_a_command():
    # GIVEN the adapter the module uses when nothing is injected. Every
    # other test replaces it, so this is the only place a wrong keyword
    # would be caught before production.
    completed = pihole._subprocess_run(  # pyright: ignore[reportPrivateUsage]
        ["/bin/echo", "pihole"],
        check=True,
        capture_output=True,
        text=True,
    )

    # WHEN its result is read
    # THEN output was captured as text, and the exit code is real
    assert completed.returncode == 0
    assert completed.stdout == "pihole\n"


# -- Stage 2 facts, read from pihole.toml. -----------------------------


def test_the_stage_two_facts_are_read_from_pihole_toml(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
):
    # GIVEN a pihole.toml holding all four Stage 2 keys
    write_pihole_toml(
        snap_data,
        upstreams=["1.1.1.1", "9.9.9.9"],
        listening_mode="ALL",
        blocking_active=True,
        dnssec=False,
    )

    # WHEN each fact is read
    # THEN it answers exactly what the file says
    assert workload.upstream_dns() == ("1.1.1.1", "9.9.9.9")
    assert workload.listening_mode() == "ALL"
    assert workload.blocking_enabled() is True
    assert workload.dnssec_enabled() is False


def test_absent_stage_two_facts_read_as_none(workload: pihole.Pihole):
    # GIVEN a machine whose pihole.toml does not exist yet
    # WHEN the four facts are read
    # THEN every one is None — unreadable and unset are the same
    # "cannot answer" for a fact, and the pure core decides what that
    # means per key
    assert workload.upstream_dns() is None
    assert workload.listening_mode() is None
    assert workload.blocking_enabled() is None
    assert workload.dnssec_enabled() is None


def test_a_bool_expected_over_a_string_landed_is_caught(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that claims success while pihole.toml holds a string
    # where a boolean belongs — the value did not land as configured
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, raw='[dns.blocking]\nactive = "yes"\n')
    api(monkeypatch, {**AUTH_OK, **LOGOUT_OK, "PATCH config": FakeResponse(200, {})})

    # WHEN the config is applied
    # THEN the type mismatch is refused rather than compared equal
    with pytest.raises(pihole.PiholeError, match="did not land"):
        workload.apply_ftl_config(PASSWORD, {"dns.blocking.active": True})


def test_an_upstreams_value_that_is_not_a_list_is_caught(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that claims success while pihole.toml holds a bare
    # string where the list of upstreams belongs
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, raw='[dns]\nupstreams = "1.1.1.1"\n')
    api(monkeypatch, {**AUTH_OK, **LOGOUT_OK, "PATCH config": FakeResponse(200, {})})

    # WHEN the config is applied
    # THEN the shape mismatch is refused
    with pytest.raises(pihole.PiholeError, match="did not land"):
        workload.apply_ftl_config(PASSWORD, {"dns.upstreams": ("1.1.1.1",)})


def test_a_listening_mode_that_did_not_land_is_caught(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that claims success while the TOML keeps the old mode
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, listening_mode="LOCAL")
    api(monkeypatch, {**AUTH_OK, **LOGOUT_OK, "PATCH config": FakeResponse(200, {})})

    # WHEN the config is applied
    # THEN the drift between answer and file is the failure
    with pytest.raises(pihole.PiholeError, match="did not land"):
        workload.apply_ftl_config(PASSWORD, {"dns.listeningMode": "ALL"})


def test_upstreams_that_landed_are_verified_quietly(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API whose answer and whose pihole.toml agree on the list
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, upstreams=["1.1.1.1", "9.9.9.9"])
    api(monkeypatch, {**AUTH_OK, **LOGOUT_OK, "PATCH config": FakeResponse(200, {})})

    # WHEN the config is applied
    # THEN the matching list passes the read-back without a sound
    workload.apply_ftl_config(PASSWORD, {"dns.upstreams": ("1.1.1.1", "9.9.9.9")})


def test_a_listening_mode_that_landed_is_verified_quietly(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API whose answer and whose pihole.toml agree on the mode
    write_cli_pw(snap_data, CLI_PW)
    write_pihole_toml(snap_data, listening_mode="ALL")
    api(monkeypatch, {**AUTH_OK, **LOGOUT_OK, "PATCH config": FakeResponse(200, {})})

    # WHEN the config is applied
    # THEN the matching value passes the read-back without a sound
    workload.apply_ftl_config(PASSWORD, {"dns.listeningMode": "ALL"})


def test_workload_exceptions_survive_the_ops_event_boundary():
    # GIVEN every exception this module can raise across a handler
    errors = [
        pihole.PiholeError(operation="op", expected="expected", actual="actual", remedy=""),
        resolved.ResolvedError(operation="op", expected="expected", actual="actual"),
    ]

    # WHEN ops' `_event_context` assigns `__traceback__` on the way out
    # — which is what it does to any exception leaving a handler
    for err in errors:
        err.__traceback__ = err.__traceback__

    # THEN none of them is a frozen dataclass: the assignment would
    # raise `FrozenInstanceError` and replace the real error with a
    # crash. Verified against a deployed unit.


def test_an_unreachable_api_becomes_a_pihole_error(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that cannot be reached at all
    write_pihole_toml(snap_data, blocking_active=False)
    api(monkeypatch, {"POST auth": urllib.error.URLError("connection refused")})

    # WHEN the config is applied
    # THEN the failure is converted, not allowed to escape the
    # workload module — an escaping exception is an error state, and
    # this is a condition a human can act on, which is Blocked
    with pytest.raises(pihole.PiholeError, match="could not be applied"):
        workload.apply_ftl_config(PASSWORD, {"dns.blocking.active": False})


# -- The hold against auto-refresh (ADR-0010). -------------------------


def test_holding_the_snap_verifies_the_hold(workload: pihole.Pihole, fake_snap: FakeSnap):
    # GIVEN an installed snap that is not held
    assert fake_snap.held is False

    # WHEN the hold is applied
    workload.hold_refresh()

    # THEN snapd was told once, and the read-back confirms it
    assert fake_snap.hold_calls == 1
    assert fake_snap.held is True


def test_a_hold_snapd_claims_but_does_not_show_is_caught(
    snap_data: pathlib.Path,
):
    # GIVEN a snapd that accepts the hold and reports none — the same
    # lying shape as every other snapd claim this charm defends against
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(honest=False)),
        run=FakeRunner(),
        snap_data=snap_data,
    )

    # WHEN the hold is applied
    # THEN the charm refuses to believe the exit code
    with pytest.raises(pihole.PiholeError, match="no hold"):
        workload.hold_refresh()


def test_an_absent_snap_reports_no_hold(workload: pihole.Pihole, fake_snap: FakeSnap):
    # GIVEN a machine with nothing installed
    fake_snap.present = False

    # WHEN the fact is read
    # THEN there is nothing to hold, which reads as not held
    assert workload.refresh_held() is False


# -- The per-architecture pin, at the workload boundary. ---------------


def test_the_pinned_revision_fact_follows_the_machine(
    fake_snap: FakeSnap,
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN the same charm on an amd64 and on an arm64 host
    amd = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=fake_runner,
        snap_data=snap_data,
        machine=lambda: "x86_64",
    )
    arm = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=fake_runner,
        snap_data=snap_data,
        machine=lambda: "aarch64",
    )

    # WHEN each reads the revision it pins
    # THEN each gets its own architecture's build, because the store
    # numbers them separately
    assert amd.pinned_revision() == SNAP_REVISIONS["amd64"]
    assert arm.pinned_revision() == SNAP_REVISIONS["arm64"]


def test_an_unpinned_architecture_refuses_to_install(
    fake_snap: FakeSnap,
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a machine this charm's release pins no revision for
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=fake_runner,
        snap_data=snap_data,
        machine=lambda: "riscv64",
    )

    # WHEN the install is attempted
    # THEN it refuses, naming the architecture — installing whatever
    # the store offers would leave an unpinned snap that auto-refreshes
    # out from under the charm, which ADR-0010 exists to prevent
    with pytest.raises(pihole.PiholeError, match="riscv64"):
        workload.install()

    # AND snapd was never asked for anything
    assert fake_snap.ensure_calls == []


# -- Stage 3: connected_plugs and connect_plugs. -----------------------


def test_connected_plugs_parses_snap_connections_output(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN snap connections output showing three connected plugs AND
    # one disconnected row (Slot column is "-") — the disconnected
    # row must be excluded, because the old parser that counted
    # columns instead of reading the Slot column counted every
    # disconnected plug as connected
    output = (
        "Interface        Plug                            Slot              Notes\n"
        "network-control  pihole-by-rajannpatel:network-control  :network-control  manual\n"
        "system-observe   pihole-by-rajannpatel:system-observe   :system-observe   manual\n"
        "time-control     pihole-by-rajannpatel:time-control     -                 -\n"
        "hardware-observe pihole-by-rajannpatel:hardware-observe :hardware-observe manual\n"
    )

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN the connected plugs are read
    plugs = workload.connected_plugs()

    # THEN the three connected plugs are returned; the disconnected
    # time-control row is excluded
    assert plugs == frozenset({"network-control", "system-observe", "hardware-observe"})


def test_connect_plugs_connects_and_reads_back(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where time-control is not connected
    connected_output = (
        "Interface        Plug                            Slot              Notes\n"
        "system-observe   pihole-by-rajannpatel:system-observe   :system-observe   manual\n"
    )
    after_output = (
        "Interface        Plug                            Slot              Notes\n"
        "system-observe   pihole-by-rajannpatel:system-observe   :system-observe   manual\n"
        "time-control     pihole-by-rajannpatel:time-control     :time-control     manual\n"
    )
    calls: list[list[str]] = []
    call_count = 0

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        calls.append(list(args))
        if args[0] == "snap" and args[1] == "connections":
            if call_count == 1:
                return subprocess.CompletedProcess(
                    args=list(args), returncode=0, stdout=connected_output, stderr=""
                )
            return subprocess.CompletedProcess(
                args=list(args), returncode=0, stdout=after_output, stderr=""
            )
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN time-control is connected
    workload.connect_plugs(["time-control"])

    # THEN snap connect was called
    assert ["snap", "connect", "pihole-by-rajannpatel:time-control"] in calls


def test_connect_plugs_skips_already_connected(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where all plugs are already connected
    output = (
        "Interface        Plug                            Slot              Notes\n"
        "time-control     pihole-by-rajannpatel:time-control     :time-control     manual\n"
    )
    calls: list[list[str]] = []

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[0] == "snap" and args[1] == "connections":
            return subprocess.CompletedProcess(
                args=list(args), returncode=0, stdout=output, stderr=""
            )
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN time-control is connected (already connected)
    workload.connect_plugs(["time-control"])

    # THEN no snap connect was called — it was already connected
    snap_connect_calls = [c for c in calls if c[0] == "snap" and c[1] == "connect"]
    assert snap_connect_calls == []


# -- Stage 3: gravity timer. -------------------------------------------


def test_gravity_schedule_reads_our_drop_in(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
):
    # GIVEN our drop-in on disk with a known OnCalendar — the fact is
    # what WE wrote, never the snap's randomized default, or an
    # unmanaged intent would plan a removal on every reconcile
    drop_in = tmp_path / "override.conf"
    drop_in.parent.mkdir(parents=True, exist_ok=True)
    drop_in.write_text("[Timer]\nOnCalendar=\nOnCalendar=Sun *-*-* 04:00\n")
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the schedule is read
    schedule = workload.gravity_schedule()

    # THEN the drop-in's OnCalendar is returned
    assert schedule == "Sun *-*-* 04:00"

    # AND with no drop-in at all, the fact is None — unmanaged
    workload_absent = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=tmp_path / "absent.conf",
    )
    assert workload_absent.gravity_schedule() is None


def test_gravity_schedule_malformed_drop_in_yields_none(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
):
    # GIVEN a drop-in that exists but carries no OnCalendar line
    drop_in = tmp_path / "override.conf"
    drop_in.parent.mkdir(parents=True, exist_ok=True)
    drop_in.write_text("[Timer]\nSomeOtherKey=1\n")
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the schedule is read
    # THEN the fact is None — nothing we recognise as a schedule
    assert workload.gravity_schedule() is None


def test_gravity_schedule_validation_and_drop_in_paths_error_paths(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The validation and DropInPaths read-back failure modes.

    ``write_gravity_timer`` validates the expression with
    ``systemd-analyze calendar`` before writing, then reads back
    ``systemctl show -p DropInPaths --value`` after daemon-reload.
    Both must surface as PiholeError, not as silent success.
    """
    drop_in = tmp_path / "override.conf"

    def make_workload(fake_run: object) -> pihole.Pihole:
        monkeypatch.setattr(pihole.subprocess, "run", fake_run)
        monkeypatch.setattr(pihole.systemd, "daemon_reload", lambda: None)
        return pihole.Pihole(
            cache_factory=FakeCache(fake_snap),
            snap_data=snap_data,
            gravity_timer_drop_in=drop_in,
        )

    def run_ok(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    # GIVEN systemd-analyze rejects the expression as invalid
    def run_invalid_calendar(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "systemd-analyze":
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=1,
                stdout="",
                stderr="Failed to parse calendar specification: not a valid\n",
            )
        return run_ok(args)

    workload = make_workload(run_invalid_calendar)
    with pytest.raises(pihole.PiholeError, match="not a valid systemd OnCalendar"):
        workload.write_gravity_timer("not-a-valid-schedule")

    # AND nothing was written — validation precedes any filesystem
    # change, so an invalid expression cannot leave a broken override
    assert not drop_in.exists()

    # GIVEN systemd-analyze cannot run at all
    def run_analyze_oserror(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "systemd-analyze":
            raise OSError(2, "No such file or directory", "systemd-analyze")
        return run_ok(args)

    workload = make_workload(run_analyze_oserror)
    with pytest.raises(pihole.PiholeError, match="could not be run"):
        workload.write_gravity_timer("Sun *-*-* 04:00")

    # GIVEN systemctl show DropInPaths cannot run at all
    def run_show_oserror(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "systemctl" and args[1] == "show":
            raise OSError(2, "No such file or directory", "systemctl")
        return run_ok(args)

    workload = make_workload(run_show_oserror)
    with pytest.raises(pihole.PiholeError, match="could not be run"):
        workload.write_gravity_timer("Sun *-*-* 04:00")

    # GIVEN systemctl show answers non-zero
    def run_show_nonzero(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "systemctl" and args[1] == "show":
            return subprocess.CompletedProcess(
                args=list(args), returncode=1, stdout="", stderr="unit not found"
            )
        return run_ok(args)

    workload = make_workload(run_show_nonzero)
    with pytest.raises(pihole.PiholeError, match="could not be read"):
        workload.write_gravity_timer("Sun *-*-* 04:00")

    # GIVEN systemctl show returns empty — the timer has no DropInPaths
    def run_show_empty(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "systemctl" and args[1] == "show":
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return run_ok(args)

    workload = make_workload(run_show_empty)
    with pytest.raises(pihole.PiholeError, match="could not be read"):
        workload.write_gravity_timer("Sun *-*-* 04:00")


def test_write_gravity_timer_writes_drop_in_and_reads_back(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a writable drop-in directory and a systemctl that reports
    # our drop-in is loaded in DropInPaths
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "systemctl" and args[1] == "show":
            # Real output shape: the path(s) systemd has loaded.
            # Our drop-in must appear in this output.
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=0,
                stdout=f"{drop_in}\n",
                stderr="",
            )
        if args[0] == "systemd-analyze":
            # Validation passes: systemd-analyze calendar exits 0.
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    monkeypatch.setattr(pihole.systemd, "daemon_reload", lambda: None)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is written
    workload.write_gravity_timer("Sun *-*-* 03:00")

    # THEN the drop-in exists with OnCalendar= cleared before being set
    content = drop_in.read_text(encoding="utf-8")
    assert "OnCalendar=\nOnCalendar=Sun *-*-* 03:00" in content


def test_remove_gravity_timer_removes_drop_in(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an existing drop-in
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"
    drop_in_dir.mkdir(parents=True)
    drop_in.write_text("[Timer]\nOnCalendar=\nOnCalendar=Sun *-*-* 03:00\n", encoding="utf-8")

    monkeypatch.setattr(pihole.systemd, "daemon_reload", lambda: None)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is removed
    workload.remove_gravity_timer()

    # THEN the drop-in is gone
    assert not drop_in.exists()


def test_update_gravity_runs_pihole_g(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a workload
    runner = FakeRunner()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN gravity is updated without force
    workload.update_gravity(force=False)

    # THEN pihole -g is called without --force
    assert runner.calls == [[pihole.PIHOLE_CMD, "-g"]]


def test_update_gravity_with_force_passes_flag(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a workload
    runner = FakeRunner()
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN gravity is updated with force
    workload.update_gravity(force=True)

    # THEN pihole -g --force is called
    assert runner.calls == [[pihole.PIHOLE_CMD, "-g", "--force"]]


# -- Stage 3: connected_plugs error paths. -----------------------------


def test_connected_plugs_oserror_returns_the_safe_default(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where snap connections cannot be run at all
    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "snap" and args[1] == "connections":
            raise OSError(2, "No such file or directory", "snap")
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN connected_plugs is called
    # THEN the empty set comes back — a fact is total by contract and
    # never raises: the status handler calls it outside any try, and
    # an action hook that raised would error the unit, the failure
    # mode that costs the machine its DNS. The apply path surfaces
    # the real error if snapd is truly broken.
    assert workload.connected_plugs() == frozenset()


def test_connected_plugs_nonzero_return_returns_empty(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN snap connections that exits non-zero
    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "snap" and args[1] == "connections":
            return subprocess.CompletedProcess(
                args=list(args), returncode=1, stdout="", stderr="snapd is not running"
            )
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN connected_plugs is called
    plugs = workload.connected_plugs()

    # THEN an empty frozenset is returned — safe direction, every plug
    # looks disconnected and the idempotent connect will fix it
    assert plugs == frozenset()


def test_connected_plugs_skips_non_matching_lines(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN output with lines that have 4+ parts but a different snap
    output = (
        "Interface        Plug                            Slot              Notes\n"
        "network-control  pihole-by-rajannpatel:network-control  :network-control  manual\n"
        "system-observe   other-snap:system-observe             :system-observe   manual\n"
        "time-control     pihole-by-rajannpatel:time-control     :time-control     manual\n"
    )

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN the connected plugs are read
    plugs = workload.connected_plugs()

    # THEN only the plugs belonging to our snap are returned; the
    # other-snap line is skipped
    assert plugs == frozenset({"network-control", "time-control"})


# -- Stage 3: connect_plugs error paths. -------------------------------


def test_connect_plugs_called_process_error(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where snap connect fails
    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "snap" and args[1] == "connect":
            raise subprocess.CalledProcessError(1, list(args), output="", stderr="denied")
        if args[0] == "snap" and args[1] == "connections":
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN connect_plugs is called
    # THEN the failure is converted
    with pytest.raises(pihole.PiholeError, match="snap connect failed"):
        workload.connect_plugs(["time-control"])


def test_connect_plugs_oserror(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where snap connect raises OSError
    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "snap" and args[1] == "connect":
            raise OSError(2, "No such file or directory", "snap")
        if args[0] == "snap" and args[1] == "connections":
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN connect_plugs is called
    # THEN the OSError is converted
    with pytest.raises(pihole.PiholeError, match="snap connect failed"):
        workload.connect_plugs(["time-control"])


def test_connect_plugs_missing_after_connect(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where snap connect succeeds but the plug still
    # does not appear as connected afterwards
    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "snap" and args[1] == "connections":
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
    )

    # WHEN connect_plugs is called
    # THEN the read-back catches it
    with pytest.raises(pihole.PiholeError, match="still disconnected"):
        workload.connect_plugs(["time-control"])


# -- Stage 3: gravity_schedule error paths. ----------------------------


def test_gravity_schedule_oserror(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
):
    # GIVEN a drop-in path that cannot be read as a file — a
    # directory where the file should be raises IsADirectoryError,
    # which is an OSError
    drop_in = tmp_path / "override.conf"
    drop_in.mkdir(parents=True)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN gravity_schedule is called
    # THEN None comes back — total by contract, same reasoning as
    # connected_plugs; a set intent rewrites the unreadable drop-in
    assert workload.gravity_schedule() is None


def test_write_gravity_timer_oserror_write(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a drop-in path whose parent directory cannot be created
    # (parent is a file, not a directory)
    parent_file = tmp_path / "not-a-dir"
    parent_file.write_text("", encoding="utf-8")
    drop_in = parent_file / "override.conf"

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        # systemd-analyze passes
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is written
    # THEN the OSError is converted
    with pytest.raises(pihole.PiholeError, match="the write failed"):
        workload.write_gravity_timer("Sun *-*-* 03:00")


def test_write_gravity_timer_readback_mismatch(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a drop-in that, after being written, does not match —
    # simulated by making read_text return something else
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"

    original_read_text = drop_in.read_text

    def lying_read_text(*args: object, **kwargs: object) -> str:
        return "[Timer]\nOnCalendar=something-else\n"

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        # systemd-analyze passes
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    monkeypatch.setattr(pihole.systemd, "daemon_reload", lambda: None)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # Write it first so it exists, then monkey-patch read_text to lie
    drop_in_dir.mkdir(parents=True)
    drop_in.write_text("", encoding="utf-8")
    monkeypatch.setattr(drop_in.__class__, "read_text", lying_read_text)

    # WHEN the timer is written
    # THEN the read-back mismatch is caught
    with pytest.raises(pihole.PiholeError, match="does not match"):
        workload.write_gravity_timer("Sun *-*-* 03:00")

    # Restore so the tmp_path cleanup works
    monkeypatch.setattr(drop_in.__class__, "read_text", original_read_text)


def test_write_gravity_timer_daemon_reload_error(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a daemon-reload that fails
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        # systemd-analyze passes
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pihole.systemd,
        "daemon_reload",
        lambda: (_ for _ in ()).throw(systemd.SystemdError("failed")),
    )
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is written
    # THEN the daemon-reload failure is converted
    with pytest.raises(pihole.PiholeError, match="daemon-reload"):
        workload.write_gravity_timer("Sun *-*-* 03:00")


def test_write_gravity_timer_drop_in_paths_missing(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where systemctl show reports DropInPaths that do
    # not include our drop-in after daemon-reload
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"

    def fake_run(
        args: Sequence[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "systemctl" and args[1] == "show":
            # DropInPaths has OTHER paths but not ours
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=0,
                stdout="/etc/systemd/system/something-else.conf\n",
                stderr="",
            )
        # systemd-analyze passes
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pihole.subprocess, "run", fake_run)
    monkeypatch.setattr(pihole.systemd, "daemon_reload", lambda: None)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is written
    # THEN the DropInPaths mismatch is caught
    with pytest.raises(pihole.PiholeError, match="DropInPaths"):
        workload.write_gravity_timer("Sun *-*-* 03:00")


# -- Stage 3: remove_gravity_timer error paths. ------------------------


def test_remove_gravity_timer_no_drop_in_is_noop(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
):
    # GIVEN a machine with no drop-in to remove
    drop_in = tmp_path / "nonexistent" / "override.conf"
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is removed
    # THEN it returns without error — idempotent
    workload.remove_gravity_timer()


def test_remove_gravity_timer_oserror_unlink(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a drop-in that exists but cannot be removed
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"
    drop_in_dir.mkdir(parents=True)
    drop_in.write_text("[Timer]\nOnCalendar=Sun *-*-* 03:00\n", encoding="utf-8")

    original_unlink = drop_in.unlink

    def failing_unlink(*args: object, **kwargs: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(drop_in.__class__, "unlink", failing_unlink)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is removed
    # THEN the OSError is converted
    with pytest.raises(pihole.PiholeError, match="deletion failed"):
        workload.remove_gravity_timer()

    monkeypatch.setattr(drop_in.__class__, "unlink", original_unlink)
    # Clean up so tmp_path cleanup works
    drop_in.unlink(missing_ok=True)


def test_remove_gravity_timer_still_exists_after_unlink(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a drop-in that claims to unlink but stays on disk
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"
    drop_in_dir.mkdir(parents=True)
    drop_in.write_text("[Timer]\nOnCalendar=Sun *-*-* 03:00\n", encoding="utf-8")

    original_unlink = drop_in.unlink

    def lying_unlink(*args: object, **kwargs: object) -> None:
        # Don't actually remove — simulate snapd lying
        pass

    monkeypatch.setattr(drop_in.__class__, "unlink", lying_unlink)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is removed
    # THEN the read-back catches it — the file is still there
    with pytest.raises(pihole.PiholeError, match="still on disk"):
        workload.remove_gravity_timer()

    monkeypatch.setattr(drop_in.__class__, "unlink", original_unlink)
    drop_in.unlink(missing_ok=True)


def test_remove_gravity_timer_daemon_reload_error(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a daemon-reload that fails after the drop-in is removed
    drop_in_dir = tmp_path / "snap.pihole-by-rajannpatel.gravity-sync.timer.d"
    drop_in = drop_in_dir / "override.conf"
    drop_in_dir.mkdir(parents=True)
    drop_in.write_text("[Timer]\nOnCalendar=Sun *-*-* 03:00\n", encoding="utf-8")

    monkeypatch.setattr(
        pihole.systemd,
        "daemon_reload",
        lambda: (_ for _ in ()).throw(systemd.SystemdError("failed")),
    )
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        snap_data=snap_data,
        gravity_timer_drop_in=drop_in,
    )

    # WHEN the timer is removed
    # THEN the daemon-reload failure is converted
    with pytest.raises(pihole.PiholeError, match="daemon-reload"):
        workload.remove_gravity_timer()


# -- Stage 3: update_gravity error paths. ------------------------------


def test_update_gravity_pihole_error_on_oserror(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a machine where the pihole wrapper is not on disk
    def missing_wrapper(
        args: Sequence[str],
        **_: object,
    ) -> None:
        raise FileNotFoundError(2, "No such file or directory", pihole.PIHOLE_CMD)

    runner = FakeRunner(effect=missing_wrapper)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN gravity is updated
    # THEN the OSError inside _run_pihole is converted
    with pytest.raises(pihole.PiholeError, match="could not be run"):
        workload.update_gravity()


def test_update_gravity_called_process_error_converts(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a pihole -g that exits non-zero with check=True
    runner = FakeRunner(returncode=1)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN gravity is updated
    # THEN the failure is the workload's own type, carrying the exit
    # code and the output tail — a raw CalledProcessError stringifies
    # without its output, which is the part that says why it failed
    with pytest.raises(pihole.PiholeError, match="exit 1") as exc_info:
        workload.update_gravity()

    # AND the output tail travels — the FakeRunner's stderr is what a
    # real failure would quote
    assert "Usage: pihole [options]" in str(exc_info.value)


# -- Stage 3: snap-check exit codes. -------------------------------


def test_snap_check_exit_0_returns_ok(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap-check that exits 0 (healthy)
    runner = FakeRunner(returncode=0)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )
    # WHEN the diagnostic is run
    result = workload.snap_check()
    # THEN the semantic outcome is SnapCheckOk
    assert isinstance(result, SnapCheckOk)


def test_snap_check_exit_1_returns_config_error(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap-check that exits 1 (config error)
    runner = FakeRunner(returncode=1)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )
    # WHEN the diagnostic is run
    result = workload.snap_check()
    # THEN the semantic outcome is SnapCheckConfigError with output
    assert isinstance(result, SnapCheckConfigError)
    assert result.output == ""


# -- Stage 3: restart. ----------------------------------------------


def test_restart_verifies_active_service(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
):
    # GIVEN an installed, running snap
    # WHEN the daemon is restarted
    workload.restart()
    # THEN snapd was asked to restart the FTL service
    assert fake_snap.restart_calls == [(["pihole-ftl"], False)]


def test_restart_catches_inactive_service(
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a snapd that accepts the restart and leaves the service
    # inactive — the same lying shape as every other snapd claim
    workload = pihole.Pihole(
        cache_factory=FakeCache(FakeSnap(active=False, honest=False)),
        run=fake_runner,
        snap_data=snap_data,
    )
    # WHEN the daemon is restarted
    # THEN the read-back catches it
    with pytest.raises(pihole.PiholeError, match="EADDRINUSE"):
        workload.restart()


def test_snap_check_unexpected_exit_code(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap-check that exits with a code the charm does not
    # recognise (3 — not 0, 1, or 2)
    runner = FakeRunner(returncode=3)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )
    # WHEN the diagnostic is run
    # THEN it raises PiholeError rather than silently inventing a
    # semantic code
    with pytest.raises(pihole.PiholeError, match="unexpected exit code"):
        workload.snap_check()
