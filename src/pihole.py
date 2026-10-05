"""Every effect this charm has on the machine, and every read-back.

See ADR-0003 §2.7, ADR-0005 §2.9, and ADR-0009 §4.
"""

import contextlib
import logging
import platform
import socket
import subprocess
import time
import tomllib
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, final

import tenacity
from charmlibs import snap, systemd

import resolved
from ftl_api import ApiConfigError, ApiTimeoutError, ApiUnavailableError, FtlApi
from pihole_state import (
    GRAVITY_TIMER_DROP_IN,
    GRAVITY_TIMER_UNIT,
    PIHOLE_TOML,
    PWHASH_KEY,
    SNAP_DATA,
    SNAP_NAME,
    AdminPasswordState,
    ApiFacts,
    DhcpPool,
    ServiceStatus,
    SnapCheckConfigError,
    SnapCheckOk,
    SnapCheckResult,
    SnapCheckRuntimeError,
    config_value,
    revision_for,
)

logger = logging.getLogger(__name__)


def _bind_probe(port: int) -> bool:
    """Whether a UDP socket can bind ``0.0.0.0:port``."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True


FTL_SERVICE = "pihole-ftl"
"""The daemon the snap ships with `install-mode: disable`."""

PIHOLE_CMD = f"/snap/bin/{SNAP_NAME}.pihole"
"""The fully qualified command: the `pihole` alias does not register."""

NTP_ACTIVE_KEYS = ("ntp.ipv4.active", "ntp.ipv6.active")
"""The two keys behind FTL's NTP server on 123/udp, on by default."""

UPSTREAM_DNS_KEY = "dns.upstreams"
LISTENING_MODE_KEY = "dns.listeningMode"
BLOCKING_ACTIVE_KEY = "dns.blocking.active"
DNSSEC_KEY = "dns.dnssec"
"""FTL config keys read from pihole.toml for the Stage 2 diff."""

DHCP_ACTIVE_KEY = "dhcp.active"
DHCP_POOL_KEYS: tuple[str, ...] = ("dhcp.start", "dhcp.end", "dhcp.router", "dhcp.netmask")
"""The four DHCP pool keys, applied atomically.

See snap-constraints §4.4.
"""

INSTALL_ATTEMPTS = 3
INSTALL_WAIT = tenacity.wait_fixed(2) + tenacity.wait_random(0, 5)
"""Bounded, in-hook retry for a snap store that is genuinely flaky."""

IP_CMD = "/usr/bin/ip"
"""Absolute path: a hook's PATH is Juju's, not the system's."""

SS_CMD = "/usr/bin/ss"
"""Absolute path: a hook's PATH is Juju's, not the system's."""

DHCP_BIND_PROCESS = "pihole-FTL"
"""The process name FTL runs under: launcher-ftl.sh `exec`s the
binary."""

DHCP_BIND_POLL_INTERVAL = 2.0
"""Seconds between `ss -lunp` polls while waiting for the bind."""

SNAPD_REMEDY = "check `snap changes` and `journalctl -u snapd` on the machine"
"""Where to look when snapd failed. See ADR-0002 §2.2.2."""


# Not frozen: ops assigns `exc.__traceback__` on handler exit,
# which raises `FrozenInstanceError` on a frozen instance.
@final
@dataclass
class PiholeError(Exception):
    """A workload operation did not produce the state it claimed to."""

    operation: str
    expected: str
    actual: str
    remedy: str = ""

    def __str__(self) -> str:
        """Render the failure for an operator reading `juju status`."""
        detail = f"{self.operation}: expected {self.expected}, but {self.actual}"
        return f"{detail}; {self.remedy}" if self.remedy else detail


@contextlib.contextmanager
def _converting_snapd_failure(operation: str, remedy: str) -> Generator[None]:
    """Turn a `charmlibs.snap` failure into one this charm owns.

    The charm module cannot catch `snap.Error` without importing
    `charmlibs` (rule 2). See ADR-0005 §2.9.
    """
    try:
        yield
    except snap.Error as err:
        raise _snapd_failure(operation=operation, remedy=remedy, err=err) from err


def _snapd_failure(operation: str, remedy: str, err: snap.Error) -> PiholeError:
    """Describe a snapd refusal as an error this charm owns."""
    return PiholeError(
        operation=operation,
        expected="snapd to carry the request out",
        actual=f"it raised {type(err).__name__}: {err}",
        remedy=remedy,
    )


class Runner(Protocol):
    """The `subprocess.run` shape this module needs."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and return what it did."""
        ...


def _subprocess_run(
    args: Sequence[str],
    *,
    check: bool,
    capture_output: bool,
    text: bool,
) -> subprocess.CompletedProcess[str]:
    """Adapt `subprocess.run` to the narrow `Runner` protocol."""
    return subprocess.run(args, check=check, capture_output=capture_output, text=text)


@final
class Pihole:
    """Own every effect on the machine. Knows nothing about ops."""

    def __init__(
        self,
        cache_factory: Callable[[], Mapping[str, snap.Snap]] = snap.SnapCache,
        run: Runner = _subprocess_run,
        snap_data: Path = SNAP_DATA,
        resolved_drop_in: Path = resolved.DROP_IN,
        gravity_timer_drop_in: Path = GRAVITY_TIMER_DROP_IN,
        retry_wait: tenacity.wait.WaitBaseT = INSTALL_WAIT,
        machine: Callable[[], str] = platform.machine,
        api: FtlApi | None = None,
        probe_udp_port: Callable[[int], bool] = _bind_probe,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cache_factory = cache_factory
        self._run = run
        self._snap_data = snap_data
        self._resolved_drop_in = resolved_drop_in
        self._gravity_timer_drop_in = gravity_timer_drop_in
        self._retry_wait = retry_wait
        self._machine = machine
        self._api = api or FtlApi(snap_data=snap_data)
        self._probe_udp_port = probe_udp_port
        # Injected together: every bounded wait is a deadline plus a
        # sleep, and faking one without the other measures wall time.
        self._monotonic = monotonic
        self._sleep = sleep

    def installed_revision(self) -> str | None:
        """Return the installed revision, or None if not installed."""
        pihole = self._snap()
        if pihole is None or not pihole.present:
            return None
        return pihole.revision

    def pinned_revision(self) -> str | None:
        """Report the revision this charm pins for this machine.

        None on an architecture `SNAP_REVISIONS` does not cover. See
        ADR-0010.
        """
        return revision_for(self._machine())

    def refresh_held(self) -> bool:
        """Report whether snapd will not auto-refresh this snap."""
        pihole = self._snap()
        if pihole is None or not pihole.present:
            return False
        return pihole.held

    def workload_version(self) -> str | None:
        """Return the Pi-hole version the snap reports, if any."""
        pihole = self._snap()
        return None if pihole is None else pihole.version

    def ftl_status(self) -> ServiceStatus:
        """Report what snapd knows about the FTL daemon."""
        pihole = self._snap()
        if pihole is None:
            return ServiceStatus(enabled=False, active=False)
        service = pihole.services.get(FTL_SERVICE)
        if service is None:
            return ServiceStatus(enabled=False, active=False)
        return ServiceStatus(enabled=service["enabled"], active=service["active"])

    def ntp_server_active(self) -> bool | None:
        """Report whether FTL's NTP server is enabled on 123/udp.

        None when `pihole.toml` cannot answer.
        """
        states = [self._ftl_config_bool(key) for key in NTP_ACTIVE_KEYS]
        if any(state is None for state in states):
            return None
        return any(state for state in states)

    def upstream_dns(self) -> tuple[str, ...] | None:
        """Return `dns.upstreams` as a tuple, or None if unreadable."""
        value = config_value(self._read_toml(), UPSTREAM_DNS_KEY)
        if isinstance(value, list):
            return tuple(str(item) for item in cast("list[object]", value))
        return None

    def listening_mode(self) -> str | None:
        """Return `dns.listeningMode`, or None if unreadable."""
        value = self._ftl_config_value(LISTENING_MODE_KEY)
        return value

    def blocking_enabled(self) -> bool | None:
        """Return `dns.blocking.active`, or None if unreadable."""
        return self._ftl_config_bool(BLOCKING_ACTIVE_KEY)

    def dnssec_enabled(self) -> bool | None:
        """Return `dns.dnssec`, or None if unreadable."""
        return self._ftl_config_bool(DNSSEC_KEY)

    def dhcp_active(self) -> bool | None:
        """Return ``dhcp.active``, or None if unreadable."""
        return self._ftl_config_bool(DHCP_ACTIVE_KEY)

    def dhcp_pool(self) -> DhcpPool | None:
        """Return the four DHCP pool keys as a ``DhcpPool``.

        None when any key is absent — a partial pool is no pool.
        """
        start = self._ftl_config_value(DHCP_POOL_KEYS[0])
        end = self._ftl_config_value(DHCP_POOL_KEYS[1])
        router = self._ftl_config_value(DHCP_POOL_KEYS[2])
        netmask = self._ftl_config_value(DHCP_POOL_KEYS[3])
        if start is None or end is None or router is None or netmask is None:
            return None
        return DhcpPool(start=start, end=end, router=router, netmask=netmask)

    def machine_ipv4_addresses(self) -> frozenset[str] | None:
        """Return the IPv4 addresses on non-loopback interfaces.

        None when the command cannot be run. An empty set means the
        read succeeded and found none.
        """
        try:
            completed = self._run(
                [IP_CMD, "-4", "-o", "addr", "show"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as err:
            logger.warning("could not read machine IPv4 addresses: %s", err)
            return None

        if completed.returncode != 0:
            logger.warning("could not read machine IPv4 addresses: %s", completed.stderr.strip())
            return None

        addresses: set[str] = set()
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[2] == "inet":
                # The scope token's column varies (`brd` is optional),
                # so find it rather than assume a position. Only
                # global-scope addresses are servable: host-scope is
                # loopback-like, and link-scope (169.254/16) is not
                # routable to DHCP clients.
                try:
                    scope_idx = parts.index("scope")
                except ValueError:
                    continue
                if scope_idx + 1 < len(parts) and parts[scope_idx + 1] == "global":
                    addresses.add(parts[3].split("/", 1)[0])
        return frozenset(addresses)

    def port53_released(self) -> bool:
        """Report whether port 53 is free for Pi-hole."""
        return resolved.is_port53_released(self._resolved_drop_in)

    def port67_free(self) -> bool:
        """Probe whether 67/udp is free for FTL's DHCP server.

        The pre-flight gate: enabling into an occupied port crash-loops
        FTL (snap-constraints §4.4).
        """
        return self._probe_udp_port(67)

    def api_ready(self) -> bool:
        """Report whether `GET /api/dns/blocking` is answered."""
        return self._api.ready()

    def admin_password_state(self, password: str) -> AdminPasswordState:
        """Classify the admin password the charm holds."""
        return self._api.password_state(password)

    def api_facts(self, password: str) -> ApiFacts:
        """Establish both API facts from a single session."""
        return self._api.facts(password)

    def snap_check(self) -> SnapCheckResult:
        """Run snap-check and return its semantic outcome.

        See snap-constraints §7.3.

        Raises:
            PiholeError: The diagnostic could not be run at all.
        """
        completed = self._run_pihole(
            "snap-check",
            check=False,
            operation=f"running `{PIHOLE_CMD} snap-check`",
        )
        output = completed.stdout.strip()
        match completed.returncode:
            case 0:
                return SnapCheckOk()
            case 1:
                return SnapCheckConfigError(output=output)
            case 2:
                return SnapCheckRuntimeError(output=output)
            case _:
                raise PiholeError(
                    operation=f"running `{PIHOLE_CMD} snap-check`",
                    expected="exit code 0, 1, or 2",
                    actual=f"unexpected exit code {completed.returncode}",
                    remedy="check the snap-check source for new exit codes",
                )

    def connected_plugs(self) -> frozenset[str]:
        """Return the set of snap plugs currently connected.

        An empty set when the command cannot be run — the safe
        direction, since connecting is idempotent.
        """
        try:
            completed = self._run(
                ["snap", "connections", SNAP_NAME],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as err:
            # Total by contract: a fact must never raise. The empty
            # set is the safe direction.
            logger.warning("could not read connected plugs: %s", err)
            return frozenset()

        if completed.returncode != 0:
            return frozenset()

        connected: set[str] = set()
        for line in completed.stdout.splitlines():
            # Connected and disconnected rows have the same columns;
            # the Slot column (parts[2]) is the only discriminator.
            stripped = line.strip()
            if not stripped or stripped.startswith("Interface"):
                continue
            parts = stripped.split()
            if len(parts) >= 3 and parts[1].startswith(f"{SNAP_NAME}:") and parts[2] != "-":
                connected.add(parts[1].split(":", 1)[1])
        return frozenset(connected)

    def connect_plugs(self, plugs: Sequence[str]) -> None:
        """Connect the named snap plugs, and verify they are connected.

        Raises:
            PiholeError: A plug could not be connected or did not
                appear as connected afterwards.
        """
        connected_before = self.connected_plugs()
        for plug in plugs:
            if plug in connected_before:
                logger.debug("Plug %s already connected; skipping.", plug)
                continue
            operation = f"connecting {SNAP_NAME}:{plug}"
            try:
                self._run(
                    ["snap", "connect", f"{SNAP_NAME}:{plug}"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (subprocess.CalledProcessError, OSError) as err:
                raise PiholeError(
                    operation=operation,
                    expected="the plug to be connected",
                    actual=f"snap connect failed: {err}",
                    remedy=f"run `snap connect {SNAP_NAME}:{plug}` on the machine as root",
                ) from err
        after = self.connected_plugs()
        missing = [p for p in plugs if p not in after]
        if missing:
            raise PiholeError(
                operation=f"connecting plugs for {SNAP_NAME}",
                expected=f"all requested plugs ({', '.join(plugs)}) to be connected",
                actual=f"still disconnected: {', '.join(missing)}",
                remedy=f"run `snap connections {SNAP_NAME}` on the machine to inspect",
            )

    def _validate_gravity_schedule(self, schedule: str) -> None:
        """Validate an OnCalendar expression with systemd-analyze.

        Raises:
            PiholeError: The expression is invalid, or the command
                could not be run.
        """
        operation = "validating the gravity schedule"
        try:
            completed = self._run(
                ["systemd-analyze", "calendar", schedule],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as err:
            raise PiholeError(
                operation=operation,
                expected="systemd-analyze to validate the expression",
                actual=f"the command could not be run: {err}",
                remedy="check that systemd is running on this machine",
            ) from err
        if completed.returncode != 0:
            error_line = completed.stderr.strip() or completed.stdout.strip() or "(no output)"
            raise PiholeError(
                operation=operation,
                expected="a valid systemd OnCalendar expression",
                actual=error_line,
                remedy=(
                    "`gravity-schedule` is not a valid systemd OnCalendar expression; "
                    "correct it with `juju config`"
                ),
            )

    def _drop_in_paths(self) -> str | None:
        """Return the DropInPaths of the gravity timer unit.

        The only honest signal that our override is loaded.

        Raises:
            PiholeError: The systemctl command could not be run.
        """
        try:
            completed = self._run(
                ["systemctl", "show", GRAVITY_TIMER_UNIT, "-p", "DropInPaths", "--value"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as err:
            raise PiholeError(
                operation=f"reading the {GRAVITY_TIMER_UNIT} DropInPaths",
                expected="`systemctl show` to describe them",
                actual=f"the command could not be run: {err}",
                remedy="check that systemd is running on this machine",
            ) from err
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

    def gravity_schedule(self) -> str | None:
        """Return the schedule OUR drop-in imposes, or None if absent.

        Deliberately not the effective ``OnCalendar``: the snap ships
        its own randomized default, so the effective value is never
        None.
        """
        drop_in = self._gravity_timer_drop_in
        try:
            content = drop_in.read_text()
        except FileNotFoundError:
            return None
        except OSError as err:
            # Total by contract: None is the safe direction.
            logger.warning("could not read the gravity timer drop-in: %s", err)
            return None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("OnCalendar=") and stripped != "OnCalendar=":
                return stripped.removeprefix("OnCalendar=")
        return None

    def write_gravity_timer(self, schedule: str) -> None:
        """Write the host systemd drop-in overriding the gravity timer.

        ``OnCalendar=`` is cleared before setting because drop-ins
        append. Verified via DropInPaths afterwards.

        Raises:
            PiholeError: The expression is invalid, the drop-in could
                not be written, systemd refused to reload, or the
                drop-in path is absent from the unit's DropInPaths.
        """
        # Validate the expression before touching the filesystem.
        self._validate_gravity_schedule(schedule)

        drop_in = self._gravity_timer_drop_in
        drop_in_content = f"[Timer]\nOnCalendar=\nOnCalendar={schedule}\n"
        operation = f"writing the gravity timer drop-in at {drop_in}"
        try:
            drop_in.parent.mkdir(parents=True, exist_ok=True)
            drop_in.write_text(drop_in_content, encoding="utf-8")
        except OSError as err:
            raise PiholeError(
                operation=operation,
                expected="the drop-in to be on disk",
                actual=f"the write failed: {err}",
                remedy=f"check permissions and free space on {drop_in.parent}",
            ) from err
        if drop_in.read_text(encoding="utf-8") != drop_in_content:
            raise PiholeError(
                operation=operation,
                expected="the drop-in to contain the desired schedule",
                actual="the file does not match after the write",
                remedy=f"check permissions on {drop_in}",
            )

        # daemon_reload() so systemd sees the new drop-in.
        try:
            systemd.daemon_reload()
        except systemd.SystemdError as err:
            raise PiholeError(
                operation="reloading systemd after writing the gravity timer drop-in",
                expected="daemon-reload to succeed",
                actual=f"it failed: {err}",
                remedy="check `systemctl status` on the machine",
            ) from err

        # DropInPaths is the discriminator that our override is loaded;
        # TimersCalendar proves nothing about ours.
        drop_in_paths = self._drop_in_paths()
        if drop_in_paths is None or str(drop_in) not in drop_in_paths:
            raise PiholeError(
                operation=operation,
                expected=f"the drop-in at {drop_in} to appear in the unit's DropInPaths",
                actual=(
                    f"DropInPaths does not contain it: {drop_in_paths!r}"
                    if drop_in_paths is not None
                    else "the timer unit could not be read"
                ),
                remedy=(
                    "check `systemctl show "
                    "snap.pihole-by-rajannpatel.gravity-sync.timer "
                    "-p DropInPaths`"
                ),
            )

    def remove_gravity_timer(self) -> None:
        """Remove the host systemd drop-in, restoring the snap's timer.

        Idempotent. ``daemon_reload()`` is called afterwards.

        Raises:
            PiholeError: The drop-in could not be removed, systemd
                refused to reload, or the file survived the deletion.
        """
        drop_in = self._gravity_timer_drop_in
        operation = f"removing the gravity timer drop-in at {drop_in}"
        if not drop_in.exists():
            logger.debug("No gravity timer drop-in to remove; nothing to do.")
            return
        try:
            drop_in.unlink()
        except OSError as err:
            raise PiholeError(
                operation=operation,
                expected="the drop-in to be gone",
                actual=f"the deletion failed: {err}",
                remedy=f"run `rm -f {drop_in}` on the machine",
            ) from err
        if drop_in.exists():
            raise PiholeError(
                operation=operation,
                expected="the drop-in to be gone",
                actual="it is still on disk",
                remedy=f"run `rm -f {drop_in}` on the machine",
            )
        try:
            systemd.daemon_reload()
        except systemd.SystemdError as err:
            raise PiholeError(
                operation="reloading systemd after removing the gravity timer drop-in",
                expected="daemon-reload to succeed",
                actual=f"it failed: {err}",
                remedy="check `systemctl status` on the machine",
            ) from err
        logger.info("Removed the gravity timer drop-in at %s.", drop_in)

    def update_gravity(self, *, force: bool = False) -> None:
        """Run the full gravity update with ``pihole -g``.

        ``force`` passes ``--force`` for a full rebuild.

        Raises:
            PiholeError: The command could not be run, or ``pihole -g``
                exited non-zero.
        """
        args: list[str] = []
        if force:
            args.append("--force")
        try:
            self._run_pihole("-g", *args, check=True, operation="running `pihole -g`")
        except subprocess.CalledProcessError as err:
            # Chained: this argv carries no secret. Only the tail
            # travels — the output can be hundreds of lines.
            output = (err.stderr or err.stdout or "").strip()
            tail = "\n".join(output.splitlines()[-5:]) or "(no output)"
            raise PiholeError(
                operation="updating gravity",
                expected="`pihole -g` to complete",
                actual=f"exit {err.returncode}; last output:\n{tail}",
                remedy=(
                    "check the output above — a failed download is"
                    " usually the network or an unreachable list"
                ),
            ) from err
        logger.info("Gravity update completed (force=%s).", force)

    def _ensure_installed(self, revision: str) -> None:
        """Ask snapd for the snap at the pinned revision.

        Raises:
            snap.Error: snapd refused. `install` retries this.
        """
        self._require_snap().ensure(snap.SnapState.Present, channel=None, revision=revision)

    def install(self) -> None:
        """Install the snap at the pinned revision, with retries.

        Retries on ``snap.Error``, not ``snap.SnapError`` (verified
        against charmlibs-snap 1.0.1). See ADR-0005 §2.9 and ADR-0010.

        Raises:
            PiholeError: This charm's release pins no revision for this
                architecture, the store kept failing after the retries,
                or snapd reported success and the snap is still not
                installed at the pinned revision.
        """
        machine = self._machine()
        pinned = revision_for(machine)
        if pinned is None:
            # Refusing beats installing whatever the store offers. See
            # ADR-0010.
            raise PiholeError(
                operation=f"installing the {SNAP_NAME} snap",
                expected=f"a revision pinned for {machine}",
                actual="this charm's release pins none",
                remedy=(
                    f"{machine} is not a supported architecture; deploy on "
                    "amd64 or arm64, or add its revision to SNAP_REVISIONS"
                ),
            )

        operation = f"installing the {SNAP_NAME} snap at revision {pinned}"
        retrying = tenacity.Retrying(
            retry=tenacity.retry_if_exception_type(snap.Error),
            wait=self._retry_wait,
            stop=tenacity.stop_after_attempt(INSTALL_ATTEMPTS),
            reraise=True,
        )
        try:
            retrying(self._ensure_installed, pinned)
        except snap.Error as err:
            raise _snapd_failure(
                operation=operation,
                remedy=SNAPD_REMEDY,
                err=err,
            ) from err

        revision = self.installed_revision()
        if revision != pinned:
            raise PiholeError(
                operation=operation,
                expected=f"revision {pinned} installed",
                actual=f"snapd reports revision {revision}",
                remedy=SNAPD_REMEDY,
            )
        logger.info("Installed %s revision %s on %s.", SNAP_NAME, revision, machine)

    def hold_refresh(self) -> None:
        """Hold the snap against auto-refresh, and verify it took.

        See ADR-0010.

        Raises:
            PiholeError: snapd refused the hold, or accepted it and
                `snap info` still shows none.
        """
        operation = f"holding {SNAP_NAME} against auto-refresh"
        with _converting_snapd_failure(operation=operation, remedy=SNAPD_REMEDY):
            self._require_snap().hold()
        if not self.refresh_held():
            raise PiholeError(
                operation=operation,
                expected="a hold visible in `snap info`",
                actual="snapd reports no hold",
                remedy=f"run `snap refresh --hold=forever {SNAP_NAME}` on the machine",
            )
        logger.info("Held %s against auto-refresh.", SNAP_NAME)

    def start(self, *, enable: bool = True) -> None:
        """Start the FTL daemon, and enable it so it survives a reboot.

        Raises:
            PiholeError: snapd refused the start, or accepted it and
                the service is still not active.
        """
        with _converting_snapd_failure(
            operation=f"starting {SNAP_NAME}.{FTL_SERVICE}",
            remedy=f"check `snap logs {SNAP_NAME}.{FTL_SERVICE}` on the machine",
        ):
            self._require_snap().start([FTL_SERVICE], enable=enable)
        status = self.ftl_status()
        if not status.active:
            raise PiholeError(
                operation=f"starting {SNAP_NAME}.{FTL_SERVICE}",
                expected="an active service",
                actual="snapd reports it as inactive",
                remedy=(
                    "port 53 is the usual cause; check "
                    f"`snap logs {SNAP_NAME}.{FTL_SERVICE}` for EADDRINUSE"
                ),
            )
        logger.info("Started %s.%s (enable=%s).", SNAP_NAME, FTL_SERVICE, enable)

    def restart(self) -> None:
        """Restart the FTL daemon, and verify it is active afterwards.

        ``Snap.start`` on an active service is a no-op, so plug-drift
        recovery needs a genuine restart. See snap-constraints §3.

        Raises:
            PiholeError: snapd refused the restart, or accepted it and
                the service is still not active afterwards.
        """
        with _converting_snapd_failure(
            operation=f"restarting {SNAP_NAME}.{FTL_SERVICE}",
            remedy=f"check `snap logs {SNAP_NAME}.{FTL_SERVICE}` on the machine",
        ):
            self._require_snap().restart([FTL_SERVICE])
        status = self.ftl_status()
        if not status.active:
            raise PiholeError(
                operation=f"restarting {SNAP_NAME}.{FTL_SERVICE}",
                expected="an active service",
                actual="snapd reports it as inactive",
                remedy=(
                    "port 53 is the usual cause; check "
                    f"`snap logs {SNAP_NAME}.{FTL_SERVICE}` for EADDRINUSE"
                ),
            )
        logger.info("Restarted %s.%s.", SNAP_NAME, FTL_SERVICE)

    def set_ntp_server(self, *, active: bool) -> None:
        """Set both `ftl.ntp.*.active` keys, and verify the TOML.

        Both keys must be present and match the intended value.

        Raises:
            PiholeError: snapd refused the keys, or the TOML does not
                confirm the intended state.
        """
        direction = "enabling" if active else "disabling"
        operation = f"{direction} the FTL NTP server on 123/udp"
        with _converting_snapd_failure(operation=operation, remedy=SNAPD_REMEDY):
            self._require_snap().set(
                {f"ftl.{key}": active for key in NTP_ACTIVE_KEYS},
                typed=True,
            )
        # Both keys must be present and match. Absence is not evidence
        # the write landed — FTL's default is true (rule 6).
        after = {key: self._ftl_config_bool(key) for key in NTP_ACTIVE_KEYS}
        not_proven = [key for key, state in after.items() if state is not active]
        if not_proven:
            raise PiholeError(
                operation=operation,
                expected=f"both NTP servers {'enabled' if active else 'disabled'} in pihole.toml",
                actual=f"not proven {'on' if active else 'off'}: {', '.join(not_proven)}",
                remedy="`snap set` returns 0 on keys it drops; inspect pihole.toml on the unit",
            )
        logger.info("%s the FTL NTP server.", direction.capitalize())

    def set_password(self, password: str) -> None:
        """Apply the admin password with `pihole setpassword`.

        Verified by reading `pwhash` back: the salt is random, so a
        genuine write always changes the hash.

        Raises:
            PiholeError: The command could not be run, it failed, or
                `pwhash` did not change.
        """
        before = self._ftl_config_value(PWHASH_KEY) or ""
        try:
            self._run_pihole(
                "setpassword",
                password,
                check=True,
                operation="setting the admin password",
            )
        except subprocess.CalledProcessError as err:
            # Unchained: CalledProcessError stringifies the whole argv.
            raise PiholeError(
                operation="setting the admin password",
                expected="exit 0 from `pihole setpassword`",
                actual=f"it exited {err.returncode}",
                remedy=f"run `{PIHOLE_CMD} snap-check` on the machine",
            ) from None

        after = self._ftl_config_value(PWHASH_KEY) or ""
        if not after or after == before:
            raise PiholeError(
                operation="setting the admin password",
                expected="a fresh pwhash in pihole.toml",
                actual="the hash did not change" if after else "pwhash is still empty",
                remedy=(
                    "`pihole -a -p` is v5 syntax that prints usage and exits 0; "
                    "`pihole setpassword` is the v6 command"
                ),
            )
        logger.info("Applied a new admin password.")

    def await_api(self, timeout: float) -> None:
        """Block until the HTTP API answers, or give up and say so.

        Raises:
            PiholeError: The API never answered.
        """
        try:
            self._api.await_ready(timeout)
        except ApiTimeoutError as err:
            raise PiholeError(
                operation="waiting for the Pi-hole HTTP API on port 80",
                expected=f"an answer within {timeout:.0f}s",
                actual="it never answered",
                remedy=(
                    "check the webserver section of "
                    f"/var/snap/{SNAP_NAME}/common/var/log/pihole/FTL.log"
                ),
            ) from err

    def wait_for_dhcp_bind(self, timeout: float) -> None:
        """Block until FTL demonstrably holds 67/udp, or give up.

        The evidence is the owner: `ss -lunp` must name pihole-FTL on
        a 67/udp socket (rule 6).

        Raises:
            PiholeError: FTL never bound the port within `timeout`.
        """
        deadline = self._monotonic() + timeout
        while True:
            if self._dhcp_bind_held():
                return
            if self._monotonic() >= deadline:
                raise PiholeError(
                    operation="waiting for FTL to bind 67/udp",
                    expected=f"pihole-FTL to hold the port within {timeout:.0f}s",
                    actual="it never bound it",
                    remedy=(
                        "check `snap logs pihole-by-rajannpatel` for why the "
                        "DHCP server did not start"
                    ),
                )
            self._sleep(DHCP_BIND_POLL_INTERVAL)

    def _dhcp_bind_held(self) -> bool:
        """Whether `ss -lunp` names pihole-FTL on a 67/udp socket."""
        try:
            completed = self._run(
                [SS_CMD, "-lunp"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as err:
            logger.warning("could not read listening UDP sockets: %s", err)
            return False
        if completed.returncode != 0:
            return False
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            # The local address column ends in :67 for the DHCP
            # listener; a bare substring check would match :6700.
            if not parts[3].endswith(":67"):
                continue
            if DHCP_BIND_PROCESS in line:
                return True
        return False

    def apply_ftl_config(self, password: str, config: Mapping[str, object]) -> None:
        """Apply FTL config keys via the HTTP API, and read back.

        FTL returns 200 for unknown keys, so the read-back is the only
        defence (rule 6, ADR-0004 §5.4).

        Raises:
            PiholeError: A key was not applied, the API could not be
                reached, or it reported a 400 with a hint.
        """
        try:
            self._api.apply_config(password, config)
        except ApiUnavailableError as err:
            raise PiholeError(
                operation="applying FTL config via PATCH /api/config",
                expected="the API to accept the config",
                actual=f"it could not be applied: {err}",
                remedy=(
                    "check that the FTL daemon is running and see "
                    f"/var/snap/{SNAP_NAME}/common/var/log/pihole/FTL.log"
                ),
            ) from err
        except ApiConfigError as err:
            raise PiholeError(
                operation="applying FTL config via PATCH /api/config",
                expected="the config to be accepted",
                actual=f"FTL rejected it: {err.hint}",
                remedy="check the key name and value type against the FTL documentation",
            ) from err

        for key in config:
            expected = config[key]
            actual = config_value(self._read_toml(), key)
            if actual is None:
                # Key not found in pihole.toml at all — FTL silently
                # ignored an unknown key.
                raise PiholeError(
                    operation=f"applying {key}",
                    expected=f"{key} = {expected!r} in pihole.toml",
                    actual=f"{key} is absent from pihole.toml",
                    remedy="FTL ignores unknown keys with HTTP 200; check the key name",
                )
            # Type-appropriate comparison
            if isinstance(expected, bool):
                if not isinstance(actual, bool):
                    raise PiholeError(
                        operation=f"applying {key}",
                        expected=f"{key} = {expected}",
                        actual=f"{key} = {actual}",
                        remedy=("the API returned 200 but the value did not land in pihole.toml"),
                    )
                if actual != expected:
                    raise PiholeError(
                        operation=f"applying {key}",
                        expected=f"{key} = {expected}",
                        actual=f"{key} = {actual}",
                        remedy=("the API returned 200 but the value did not land in pihole.toml"),
                    )
            elif isinstance(expected, tuple):
                if isinstance(actual, list):
                    if expected != tuple(cast("list[object]", actual)):
                        raise PiholeError(
                            operation=f"applying {key}",
                            expected=f"{key} = {expected!r}",
                            actual=f"{key} = {actual!r}",
                            remedy=(
                                "the API returned 200 but the value did not land in pihole.toml"
                            ),
                        )
                else:
                    raise PiholeError(
                        operation=f"applying {key}",
                        expected=f"{key} = {expected!r}",
                        actual=f"{key} = {actual!r}",
                        remedy=("the API returned 200 but the value did not land in pihole.toml"),
                    )
            else:
                if str(actual) != str(expected):
                    raise PiholeError(
                        operation=f"applying {key}",
                        expected=f"{key} = {expected!r}",
                        actual=f"{key} = {actual!r}",
                        remedy=("the API returned 200 but the value did not land in pihole.toml"),
                    )
        logger.info(
            "Applied FTL config for %d keys and verified them in pihole.toml.", len(config)
        )

    def _snap(self) -> snap.Snap | None:
        """Look the snap up, tolerating snapd not knowing about it."""
        try:
            return self._cache_factory()[SNAP_NAME]
        except snap.Error as err:
            logger.debug("snapd could not describe %s: %s", SNAP_NAME, err)
            return None

    def _require_snap(self) -> snap.Snap:
        """Look the snap up, letting a snapd failure propagate.

        Raises the raw `snap.Error` for `install`'s retry. See
        ADR-0005 §2.7.
        """
        return self._cache_factory()[SNAP_NAME]

    def _run_pihole(
        self,
        *args: str,
        check: bool,
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a `pihole` subcommand, naming a failure to run it at all.

        Never includes argv in the message.

        Raises:
            PiholeError: The command could not be executed.
            subprocess.CalledProcessError: Propagates when `check` is
                set.
        """
        try:
            return self._run(
                [PIHOLE_CMD, *args],
                check=check,
                capture_output=True,
                text=True,
            )
        except OSError as err:
            raise PiholeError(
                operation=operation,
                expected=f"{PIHOLE_CMD} to be runnable",
                actual=f"it could not be run: {err}",
                remedy=f"check that the {SNAP_NAME} snap is installed on the machine",
            ) from err

    def _ftl_config_value(self, key: str) -> str | None:
        """Read one dotted key out of `pihole.toml`.

        Returns None when the file, table, or key is absent.
        """
        value = config_value(self._read_toml(), key)
        return value if isinstance(value, str) else None

    def _ftl_config_bool(self, key: str) -> bool | None:
        """Read one dotted boolean key out of `pihole.toml`.

        None when the file cannot answer.
        """
        value = config_value(self._read_toml(), key)
        return value if isinstance(value, bool) else None

    def _read_toml(self) -> Mapping[str, object]:
        """Parse `pihole.toml`, or return nothing if it is not there."""
        path = self._snap_data / PIHOLE_TOML
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as err:
            logger.debug("Could not read %s: %s", path, err)
            return {}
