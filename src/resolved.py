"""Take port 53 away from systemd-resolved, and give it back.

See snap-constraints §8.1.
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


# Not frozen: ops assigns `exc.__traceback__` on handler exit.
@final
@dataclass
class ResolvedError(Exception):
    """A change to systemd-resolved did not take effect."""

    operation: str
    expected: str
    actual: str
    remedy: str = ""

    def __str__(self) -> str:
        """Render the failure for an operator reading `juju status`."""
        detail = f"{self.operation}: expected {self.expected}, but {self.actual}"
        return f"{detail}; {self.remedy}" if self.remedy else detail


def is_port53_released(drop_in: Path = DROP_IN) -> bool:
    """Report whether port 53 is free for Pi-hole."""
    return _read_drop_in(drop_in) == DROP_IN_CONTENT


def disable_stub_listener(
    drop_in: Path = DROP_IN,
    restart: ServiceRestarter = systemd.service_restart,
) -> None:
    """Free port 53 for Pi-hole, restarting resolved only when needed.

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

    Raises:
        ResolvedError: The drop-in could not be deleted, survived the
            deletion, or resolved refused to restart.
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
    """Spell out how to get this machine's DNS back by hand."""
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
