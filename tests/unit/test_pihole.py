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
from collections.abc import Callable, Sequence

import pytest
import tenacity
from charmlibs import snap

import pihole
import resolved
from pihole_state import (
    ApiFacts,
    PasswordAccepted,
    PasswordUnset,
    ServiceStatus,
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
    FakeRunner,
    FakeSnap,
    api,
    write_cli_pw,
    write_pihole_toml,
)

STOCK_PORT = "80o,443os,[::]:80o,[::]:443os"

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("this is not toml {{{", None),
        ("[webserver]\nport = 80\n", None),
        ("[webserver]\nport = 'a-string'\n", "a-string"),
    ],
)
def test_the_webserver_port_is_read_from_pihole_toml(
    workload: pihole.Pihole,
    snap_data: pathlib.Path,
    raw: str | None,
    expected: str | None,
):
    # GIVEN a pihole.toml in one of the states it can be in — missing
    # before the daemon has ever run, and TOML afterwards
    if raw is not None:
        write_pihole_toml(snap_data, raw=raw)

    # WHEN the value is read back
    # THEN anything unreadable reads as absent rather than as correct,
    # so the charm applies the value instead of assuming it landed
    assert workload.webserver_port() == expected


def test_the_stub_listener_fact_comes_from_the_drop_in(
    workload: pihole.Pihole,
    drop_in: pathlib.Path,
):
    # GIVEN a machine where port 53 has not been freed
    assert workload.stub_listener_disabled() is False

    # WHEN the drop-in is written
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text(resolved.DROP_IN_CONTENT, encoding="utf-8")

    # THEN the workload module reports it
    assert workload.stub_listener_disabled() is True


def test_snap_check_returns_its_exit_code_verbatim(
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a diagnostic that reports a runtime error
    runner = FakeRunner(returncode=2)
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=runner,
        snap_data=snap_data,
    )

    # WHEN it is run
    code = workload.snap_check()

    # THEN the semantic exit code survives, and the diagnostic is asked
    # for by its fully qualified name — the `pihole` alias never
    # registers, so a bare `pihole` would not be on PATH
    assert code == 2
    assert runner.calls == [[pihole.PIHOLE_CMD, "snap-check"]]


# -- Install and start. -----------------------------------------------


def test_install_ensures_the_snap_from_the_stable_channel(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
):
    # GIVEN a machine with nothing installed
    fake_snap.present = False

    # WHEN the snap is installed
    workload.install()

    # THEN snapd was asked for it, from the only channel that exists
    assert fake_snap.ensure_calls == [(snap.SnapState.Present, "stable")]


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
    # THEN the read-back catches it
    with pytest.raises(pihole.PiholeError, match="still reports the snap as absent"):
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
    assert fake_snap.ensure_calls == [(snap.SnapState.Present, pihole.SNAP_CHANNEL)]


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
    assert "still reports the snap as absent" in str(exc_info.value)
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
        lambda workload: workload.set_webserver_port("80o,[::]:80o"),
        "setting ftl.webserver.port",
    ),
]
"""Every effect that reaches snapd, and the operation it should name."""


@pytest.mark.parametrize(
    ("effect", "operation"),
    SNAPD_EFFECTS,
    ids=["install", "start", "set_webserver_port"],
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


def test_a_snapd_refusal_of_the_webserver_port_is_named_rather_than_re_raised(
    fake_snap: FakeSnap,
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
):
    # GIVEN a snapd that rejects the key itself, rather than accepting
    # it and dropping it
    fake_snap.refusal = snap.SnapError("invalid configuration key")
    workload = pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=fake_runner,
        snap_data=snap_data,
    )

    # WHEN the port is set
    with pytest.raises(pihole.PiholeError) as exc_info:
        workload.set_webserver_port("80o,[::]:80o")

    # THEN the refusal is reported as ours
    assert "invalid configuration key" in str(exc_info.value)
    assert "journalctl -u snapd" in str(exc_info.value)


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


def test_setting_the_webserver_port_verifies_pihole_toml(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap whose configure hook actually applies the value
    write_pihole_toml(snap_data, webserver_port="80o,[::]:80o")

    # WHEN the port is set
    workload.set_webserver_port("80o,[::]:80o")

    # THEN it went through the `ftl.` namespace. Without the prefix the
    # configure hook ignores the key and snapd stores it anyway.
    assert fake_snap.set_calls == [{"ftl.webserver.port": "80o,[::]:80o"}]


def test_a_silently_dropped_snap_set_is_caught_by_the_read_back(
    workload: pihole.Pihole,
    fake_snap: FakeSnap,
    snap_data: pathlib.Path,
):
    # GIVEN a snap that accepts the key and keeps the old value — the
    # verified behaviour of `snap set` on keys it drops
    write_pihole_toml(snap_data, webserver_port=STOCK_PORT)

    # WHEN the port is set
    # THEN the charm refuses to believe the exit code
    with pytest.raises(pihole.PiholeError, match="reads back as"):
        workload.set_webserver_port("80o,[::]:80o")

    # AND it reports success from snapd's point of view, which is
    # exactly why the read-back is the only defence
    assert fake_snap.set_calls == [{"ftl.webserver.port": "80o,[::]:80o"}]


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
