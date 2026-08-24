"""Take port 53 away from systemd-resolved, and give it back.

Strict confinement stops the snap from touching `/etc/systemd`, so
this module is the only thing between `juju remove-application` and
a machine with no DNS. See snap-constraints section 8.1.

Like `pihole.py`, this module never imports `ops`.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import final

from charmlibs import systemd

logger = logging.getLogger(__name__)

DROP_IN = Path("/etc/systemd/resolved.conf.d/pihole.conf")
"""The host file this charm owns. Nothing else may write it."""

DROP_IN_CONTENT = "[Resolve]\nDNS=127.0.0.1\nDNSStubListener=no\n"
"""Exactly the remediation `snap-check` prints for the conflict."""

SERVICE = "systemd-resolved"

type ServiceRestarter = Callable[[str], bool]
"""The shape of `systemd.service_restart`, injected for tests."""


@final
@dataclass(frozen=True)
class ResolvedError(Exception):
    """A change to systemd-resolved did not take effect.

    Carries its own context so the status handler can build a message
    that names a remedy without going back to the machine to ask.
    """

    operation: str
    expected: str
    actual: str
    remedy: str = ""

    def __str__(self) -> str:
        """Render the failure for an operator reading `juju status`."""
        detail = f"{self.operation}: expected {self.expected}, but {self.actual}"
        return f"{detail}; {self.remedy}" if self.remedy else detail


def is_stub_disabled(drop_in: Path = DROP_IN) -> bool:
    """Report whether this charm's drop-in is in place, byte for byte.

    A partial or hand-edited file counts as absent: the charm rewrites
    it rather than guessing what somebody meant.
    """
    return _read_drop_in(drop_in) == DROP_IN_CONTENT


def disable_stub_listener(
    drop_in: Path = DROP_IN,
    restart: ServiceRestarter = systemd.service_restart,
) -> None:
    """Free port 53 for Pi-hole, restarting resolved only when needed.

    Safe to run twice: identical content means no write and no restart.
    That matters more than it looks, because restarting
    systemd-resolved drops name resolution for the whole machine for a
    moment, and this runs on every reconcile.

    Raises:
        ResolvedError: The drop-in could not be written, did not land,
            or resolved refused to restart.
    """
    if _read_drop_in(drop_in) == DROP_IN_CONTENT:
        logger.debug("The resolved drop-in is already in place; not restarting %s.", SERVICE)
        return

    try:
        drop_in.parent.mkdir(parents=True, exist_ok=True)
        drop_in.write_text(DROP_IN_CONTENT, encoding="utf-8")
    except OSError as err:
        # An uncaught OSError here is a DNS-loss path once the
        # resolver is displaced. See ADR-0005 section 2.9.
        raise ResolvedError(
            operation=f"writing {drop_in}",
            expected="the charm's drop-in on disk",
            actual=f"the write failed: {err}",
            remedy=f"check the permissions and free space on {drop_in.parent}",
        ) from err
    if _read_drop_in(drop_in) != DROP_IN_CONTENT:
        raise ResolvedError(
            operation=f"writing {drop_in}",
            expected="the charm's drop-in on disk",
            actual="the file does not contain it after the write",
            remedy=f"check the permissions and free space on {drop_in.parent}",
        )
    _restart(restart)
    logger.info("Freed port 53: wrote %s and restarted %s.", drop_in, SERVICE)


def restore(
    drop_in: Path = DROP_IN,
    restart: ServiceRestarter = systemd.service_restart,
) -> None:
    """Give port 53 back to systemd-resolved.

    Safe to run when the drop-in was never written, which is what makes
    it safe to call from `remove` on a unit that never converged.

    Raises:
        ResolvedError: The drop-in could not be deleted, survived the
            deletion, or resolved refused to restart. Every one of
            those leaves the machine without a resolver, so none of
            them may pass silently.
    """
    if _read_drop_in(drop_in) is None:
        logger.debug("No resolved drop-in to remove; leaving %s alone.", SERVICE)
        return

    try:
        drop_in.unlink(missing_ok=True)
    except OSError as err:
        raise ResolvedError(
            operation=f"removing {drop_in}",
            expected="the drop-in to be gone",
            actual=f"the deletion failed: {err}",
            remedy=_recovery_command(drop_in),
        ) from err
    if drop_in.exists():
        raise ResolvedError(
            operation=f"removing {drop_in}",
            expected="the drop-in to be gone",
            actual="it is still on disk",
            remedy=_recovery_command(drop_in),
        )
    _restart(restart)
    logger.info("Restored the systemd-resolved stub listener on 127.0.0.53:53.")


def _recovery_command(drop_in: Path) -> str:
    """Spell out how to get this machine's DNS back by hand.

    The last thing an operator reads when the charm could not do it
    itself, so it is a command to paste rather than a description.
    """
    return (
        f"run: sudo sh -c 'rm -f {drop_in} && systemctl restart {SERVICE}' "
        "to restore DNS on this machine"
    )


def _restart(restart: ServiceRestarter) -> None:
    """Restart resolved, turning a systemd failure into ours."""
    try:
        restart(SERVICE)
    except (systemd.SystemdError, OSError) as err:
        # Both must be caught: `service_restart` converts only a
        # non-zero exit, so a bare exec failure raises `OSError` raw.
        # `err` is logged, not embedded, because `SystemdError`
        # stringifies systemctl's whole multi-line output.
        logger.exception("systemctl refused to restart %s: %s", SERVICE, err)
        raise ResolvedError(
            operation=f"restarting {SERVICE}",
            expected="a successful restart",
            actual="systemctl reported a failure",
            remedy=f"run `systemctl status {SERVICE}` on the machine",
        ) from None


def _read_drop_in(drop_in: Path) -> str | None:
    """Return the drop-in's content, or None if it is not readable."""
    try:
        return drop_in.read_text(encoding="utf-8")
    except OSError:
        return None
