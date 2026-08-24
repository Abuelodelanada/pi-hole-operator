"""Tests for the systemd-resolved orchestration.

Two properties matter here and neither is about the file's contents.

Restarting systemd-resolved drops name resolution for the whole
machine, so writing the same drop-in twice must not do it — this runs on
every reconcile. And removing the drop-in on `remove` is the only thing
that gives the host a resolver back, because the snap's own remove hook
can only print instructions.
"""

import pathlib

import pytest
from charmlibs import systemd

import resolved

EXPECTED_CONTENT = "[Resolve]\nDNS=127.0.0.1\nDNSStubListener=no\n"
"""Spelled out rather than imported: the bytes are the interface."""


class FakeRestarter:
    """A `service_restart` that records calls and can fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def __call__(self, service: str) -> bool:
        """Record the restart, raising if this one is meant to fail."""
        self.calls.append(service)
        if self.error is not None:
            raise self.error
        return True


def test_freeing_port_53_writes_the_drop_in_and_restarts(drop_in: pathlib.Path):
    # GIVEN a machine where systemd-resolved still holds port 53, and no
    # drop-in directory yet
    restarter = FakeRestarter()

    # WHEN the port is freed
    resolved.disable_stub_listener(drop_in, restarter)

    # THEN the drop-in says exactly what snap-check's remediation says,
    # and resolved was restarted so it takes effect
    assert drop_in.read_text(encoding="utf-8") == EXPECTED_CONTENT
    assert restarter.calls == ["systemd-resolved"]


def test_an_identical_drop_in_does_not_restart_resolved(drop_in: pathlib.Path):
    # GIVEN a machine already carrying this charm's drop-in
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text(EXPECTED_CONTENT, encoding="utf-8")
    restarter = FakeRestarter()

    # WHEN the port is freed again, as every reconcile does
    resolved.disable_stub_listener(drop_in, restarter)

    # THEN nothing is restarted: DNS for the whole machine would drop
    # for a moment, on every hook, for no reason
    assert restarter.calls == []


def test_a_foreign_drop_in_is_rewritten(drop_in: pathlib.Path):
    # GIVEN a drop-in that is not the one this charm writes
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text("[Resolve]\nDNSStubListener=yes\n", encoding="utf-8")
    restarter = FakeRestarter()

    # WHEN the port is freed
    resolved.disable_stub_listener(drop_in, restarter)

    # THEN the charm's content wins, and resolved is restarted
    assert drop_in.read_text(encoding="utf-8") == EXPECTED_CONTENT
    assert restarter.calls == ["systemd-resolved"]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, False),
        ("", False),
        ("[Resolve]\nDNSStubListener=yes\n", False),
        (EXPECTED_CONTENT, True),
    ],
)
def test_the_stub_listener_state_is_read_from_disk(
    drop_in: pathlib.Path,
    content: str | None,
    expected: bool,
):
    # GIVEN a machine in one of the states the drop-in can be in
    if content is not None:
        drop_in.parent.mkdir(parents=True)
        drop_in.write_text(content, encoding="utf-8")

    # WHEN the fact is read
    # THEN only the exact content counts as "port 53 is ours"
    assert resolved.is_stub_disabled(drop_in) is expected


def test_removal_deletes_the_drop_in_and_restarts(drop_in: pathlib.Path):
    # GIVEN a converged machine about to lose its Pi-hole
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text(EXPECTED_CONTENT, encoding="utf-8")
    restarter = FakeRestarter()

    # WHEN the unit is removed
    resolved.restore(drop_in, restarter)

    # THEN the host gets its resolver back. Without this the machine is
    # left with DNSStubListener=no and no Pi-hole: no DNS at all.
    assert not drop_in.exists()
    assert restarter.calls == ["systemd-resolved"]


def test_removal_is_safe_on_a_unit_that_never_converged(drop_in: pathlib.Path):
    # GIVEN a unit removed before it ever wrote the drop-in
    restarter = FakeRestarter()

    # WHEN the unit is removed
    resolved.restore(drop_in, restarter)

    # THEN nothing is restarted, and nothing raises
    assert restarter.calls == []


def test_a_write_that_takes_no_effect_is_not_believed(
    drop_in: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a filesystem that accepts the write and stores nothing
    monkeypatch.setattr(pathlib.Path, "write_text", _write_nothing)
    restarter = FakeRestarter()

    # WHEN the port is freed
    # THEN the read-back catches it, and resolved is not restarted for
    # a change that did not happen
    with pytest.raises(resolved.ResolvedError, match="does not contain it"):
        resolved.disable_stub_listener(drop_in, restarter)
    assert restarter.calls == []


def test_a_deletion_that_takes_no_effect_names_the_recovery_command(
    drop_in: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a drop-in that will not go away
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text(EXPECTED_CONTENT, encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_nothing)

    # WHEN the unit is removed
    # THEN the error tells the operator exactly how to get DNS back,
    # because at this point nothing else will
    with pytest.raises(resolved.ResolvedError) as exc_info:
        resolved.restore(drop_in, FakeRestarter())
    assert "systemctl restart systemd-resolved" in str(exc_info.value)


def test_a_failed_restart_names_the_command_to_investigate_with(drop_in: pathlib.Path):
    # GIVEN a systemd that refuses to restart resolved
    restarter = FakeRestarter(systemd.SystemdError("Job for systemd-resolved.service failed"))

    # WHEN the port is freed
    # THEN the charm reports something a human can act on rather than
    # letting systemd's multi-line output into the status
    with pytest.raises(resolved.ResolvedError) as exc_info:
        resolved.disable_stub_listener(drop_in, restarter)
    assert "systemctl status systemd-resolved" in str(exc_info.value)


def test_a_write_the_filesystem_refuses_outright_is_named(
    drop_in: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a read-only /etc, which is what an OSError from the write
    # actually means. It is not a `ResolvedError`, and `charm.py` cannot
    # catch it without knowing about the filesystem — so an unconverted
    # one reaches Juju as error state, and a unit in error needs
    # `--force` to remove, which skips the handler that gives the host
    # its resolver back (ADR-0005 section 2.9).
    monkeypatch.setattr(pathlib.Path, "write_text", _refuse_to_write)
    restarter = FakeRestarter()

    # WHEN the port is freed
    with pytest.raises(resolved.ResolvedError) as exc_info:
        resolved.disable_stub_listener(drop_in, restarter)

    # THEN the failure is ours, names somewhere to look, and resolved
    # was not restarted for a change that never happened
    assert "the write failed" in str(exc_info.value)
    assert str(drop_in.parent) in str(exc_info.value)
    assert restarter.calls == []


def test_a_directory_that_cannot_be_created_is_named(
    drop_in: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where the drop-in directory cannot be created
    monkeypatch.setattr(pathlib.Path, "mkdir", _refuse_to_mkdir)
    restarter = FakeRestarter()

    # WHEN the port is freed
    # THEN the same conversion applies: the charm never reaches error
    # state on the step that takes the host's resolver away
    with pytest.raises(resolved.ResolvedError, match="the write failed"):
        resolved.disable_stub_listener(drop_in, restarter)
    assert restarter.calls == []


def test_a_deletion_the_filesystem_refuses_names_the_recovery_command(
    drop_in: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a drop-in the filesystem will not let go of
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text(EXPECTED_CONTENT, encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "unlink", _refuse_to_unlink)

    # WHEN the unit is removed
    with pytest.raises(resolved.ResolvedError) as exc_info:
        resolved.restore(drop_in, FakeRestarter())

    # THEN the operator still gets the command that restores DNS, which
    # at this point is the only thing that will
    assert "the deletion failed" in str(exc_info.value)
    assert "systemctl restart systemd-resolved" in str(exc_info.value)


def test_a_systemctl_that_cannot_be_run_at_all_is_still_ours(drop_in: pathlib.Path):
    # GIVEN a machine where `systemctl` cannot even be executed.
    # `systemd.service_restart` converts a non-zero exit and nothing
    # else, so this arrives as a bare OSError — on the step that has
    # already taken the host's resolver away.
    restarter = FakeRestarter(FileNotFoundError(2, "No such file or directory", "systemctl"))

    # WHEN the port is freed
    # THEN it is still named and catchable, not error state
    with pytest.raises(resolved.ResolvedError) as exc_info:
        resolved.disable_stub_listener(drop_in, restarter)
    assert "systemctl status systemd-resolved" in str(exc_info.value)

    # AND systemd's own output never reaches the status message
    assert "No such file" not in str(exc_info.value)


def _write_nothing(_path: pathlib.Path, _data: str, *, encoding: str | None = None) -> int:
    """Stand in for a write that reports success and stores nothing."""
    return 0


def _refuse_to_write(_path: pathlib.Path, _data: str, *, encoding: str | None = None) -> int:
    """Stand in for a filesystem that refuses the write outright."""
    raise PermissionError(13, "Permission denied")


def _refuse_to_mkdir(
    _path: pathlib.Path,
    *,
    parents: bool = False,
    exist_ok: bool = False,
) -> None:
    """Stand in for a directory that cannot be created."""
    raise PermissionError(13, "Permission denied")


def _refuse_to_unlink(_path: pathlib.Path, *, missing_ok: bool = False) -> None:
    """Stand in for a deletion the filesystem refuses."""
    raise PermissionError(13, "Permission denied")


def _unlink_nothing(_path: pathlib.Path, *, missing_ok: bool = False) -> None:
    """Stand in for a deletion that succeeds and deletes nothing."""
