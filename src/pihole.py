"""Every effect this charm has on the machine, and every read-back.

Never imports `ops` (rule 2); collaborators are injected so a test
double can reproduce the workload's own lying behaviour — see
ADR-0003 section 2.6. An exit code is never evidence (rule 6): every
mutation below reads back its own result, and no foreign exception
leaves this module unconverted — see ADR-0005 section 2.9. The FTL
HTTP client lives in `ftl_api.py`, composed below — see ADR-0009
section 4.
"""

import contextlib
import logging
import subprocess
import time
import tomllib
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, final

import tenacity
from charmlibs import snap

import resolved
from ftl_api import ApiTimeoutError, FtlApi
from pihole_state import (
    PIHOLE_TOML,
    PWHASH_KEY,
    SNAP_DATA,
    SNAP_NAME,
    AdminPasswordState,
    ApiFacts,
    ServiceStatus,
)

logger = logging.getLogger(__name__)

SNAP_CHANNEL = "stable"
"""The only channels this snap publishes are stable and edge."""

FTL_SERVICE = "pihole-ftl"
"""The daemon the snap ships with `install-mode: disable`."""

PIHOLE_CMD = f"/snap/bin/{SNAP_NAME}.pihole"
"""The fully qualified command: the `pihole` alias does not register."""

WEBSERVER_PORT_KEY = "webserver.port"

INSTALL_ATTEMPTS = 3
INSTALL_WAIT = tenacity.wait_fixed(2) + tenacity.wait_random(0, 5)
"""Bounded, in-hook retry for a snap store that is genuinely flaky."""

DETECT_VIRT_CMD = "/usr/bin/systemd-detect-virt"
"""Run with `--container`: exit 0 inside one, exit 1 on a VM or bare
metal, since a VM is virtualisation this charm is happy with.

Absolute path because a hook's PATH is Juju's, not a login shell's. A
failed detection must degrade to "not a container" rather than raise.
See ADR-0002 section 2.2.2.
"""

SNAPD_REMEDY = "check `snap changes` and `journalctl -u snapd` on the machine"
"""Where to look when snapd failed for a reason we cannot name."""

CONTAINER_REMEDY = (
    "this unit is in a container, where snapd cannot mount the snap it "
    "needs to bootstrap; redeploy with "
    "--constraints virt-type=virtual-machine"
)
"""The remedy for the one install failure the charm can fully explain.

Says *a container*, not *a 26.04 LXD container*: a plain `lxc launch`
installs fine because snapd is pre-seeded there, and only Juju's
bootstrap mount breaks. See ADR-0002 section 2.2.2 and
snap-constraints section 1.
"""


@final
@dataclass(frozen=True)
class PiholeError(Exception):
    """A workload operation did not produce the state it claimed to.

    The context lives inside the error so the status handler can build
    an informative `BlockedStatus` without asking the machine again.
    """

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

    The charm module cannot catch `snap.Error` itself without importing
    `charmlibs`, which rule 2 forbids — so the conversion happens here,
    where the remedy text is. See ADR-0005 section 2.9 and ADR-0003
    section 2.6.
    """
    try:
        yield
    except snap.Error as err:
        raise _snapd_failure(operation=operation, remedy=remedy, err=err) from err


def _snapd_failure(operation: str, remedy: str, err: snap.Error) -> PiholeError:
    """Describe a snapd refusal as an error this charm owns.

    Separate from the context manager above: `install` needs the
    same diagnosis but picks its remedy only after the attempt has
    failed.
    """
    return PiholeError(
        operation=operation,
        expected="snapd to carry the request out",
        actual=f"it raised {type(err).__name__}: {err}",
        remedy=remedy,
    )


def install_remedy(*, in_container: bool) -> str:
    """Choose where an install failure should send the operator.

    Pure, so the mapping is tested without executing anything: the one
    impure part is establishing `in_container`, which
    `Pihole._in_container` does.
    """
    return CONTAINER_REMEDY if in_container else SNAPD_REMEDY


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


class SnapLike(Protocol):
    """The subset of `charmlibs.snap.Snap` this module uses."""

    @property
    def present(self) -> bool:
        """Whether the snap is installed."""
        ...

    @property
    def revision(self) -> str:
        """The installed revision, as a string."""
        ...

    @property
    def version(self) -> str | None:
        """The workload version the snap declares."""
        ...

    @property
    def services(self) -> Mapping[str, snap.SnapServiceDict]:
        """What snapd knows about each of the snap's services."""
        ...

    def ensure(self, state: snap.SnapState, *, channel: str | None = None) -> None:
        """Install or refresh the snap toward the given state."""
        ...

    def set(self, config: Mapping[str, snap.JSONAble], *, typed: bool = False) -> None:
        """Write snapd configuration keys."""
        ...

    def start(self, services: list[str] | None = None, enable: bool = False) -> None:
        """Start services, optionally enabling them at boot."""
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
        cache_factory: Callable[[], Mapping[str, SnapLike]] = snap.SnapCache,
        run: Runner = _subprocess_run,
        snap_data: Path = SNAP_DATA,
        resolved_drop_in: Path = resolved.DROP_IN,
        retry_wait: tenacity.wait.WaitBaseT = INSTALL_WAIT,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        api: FtlApi | None = None,
    ) -> None:
        self._cache_factory = cache_factory
        self._run = run
        self._snap_data = snap_data
        self._resolved_drop_in = resolved_drop_in
        self._retry_wait = retry_wait
        self._api = api or FtlApi(snap_data=snap_data, sleep=sleep, monotonic=monotonic)

    # -- Facts. Every one of these is safe to call at any time. --------

    def installed_revision(self) -> str | None:
        """Return the installed revision, or None if not installed."""
        pihole = self._snap()
        if pihole is None or not pihole.present:
            return None
        return pihole.revision

    def workload_version(self) -> str | None:
        """Return the Pi-hole version the snap reports, if any."""
        pihole = self._snap()
        return None if pihole is None else pihole.version

    def ftl_status(self) -> ServiceStatus:
        """Report what snapd knows about the FTL daemon.

        Never a readiness signal on its own: the daemon reports active
        long before Pi-hole answers a query. See `api_ready`.
        """
        pihole = self._snap()
        if pihole is None:
            return ServiceStatus(enabled=False, active=False)
        service = pihole.services.get(FTL_SERVICE)
        if service is None:
            return ServiceStatus(enabled=False, active=False)
        return ServiceStatus(enabled=service["enabled"], active=service["active"])

    def webserver_port(self) -> str | None:
        """Return `webserver.port` as `pihole.toml` holds it."""
        return self._ftl_config_value(WEBSERVER_PORT_KEY)

    def stub_listener_disabled(self) -> bool:
        """Report whether port 53 was taken from systemd-resolved."""
        return resolved.is_stub_disabled(self._resolved_drop_in)

    def api_ready(self) -> bool:
        """Report whether `GET /api/dns/blocking` is answered."""
        return self._api.ready()

    def admin_password_state(self, password: str) -> AdminPasswordState:
        """Classify the admin password the charm holds."""
        return self._api.password_state(password)

    def api_facts(self, password: str) -> ApiFacts:
        """Establish both API facts from a single session."""
        return self._api.facts(password)

    def snap_check(self) -> int:
        """Run the snap's own diagnostic and return its exit code.

        Semantic codes: 0 healthy, 1 config error, 2 runtime error.
        Does **not** detect a dead webserver. See snap-constraints
        section 7.3.

        Raises:
            PiholeError: The diagnostic could not be run at all, which
                is not one of its exit codes and must not be invented
                as one.
        """
        completed = self._run_pihole(
            "snap-check",
            check=False,
            operation=f"running `{PIHOLE_CMD} snap-check`",
        )
        return completed.returncode

    # -- Effects. Each one verifies the state it was meant to produce. -

    def _ensure_installed(self) -> None:
        """Ask snapd for the snap at the configured channel.

        Separate from `install` so the retry wraps this one call, and
        not the read-back that proves it worked.

        Raises:
            snap.Error: snapd refused. `install` retries this and
                converts what survives.
        """
        self._require_snap().ensure(snap.SnapState.Present, channel=SNAP_CHANNEL)

    def install(self) -> None:
        """Install the snap, retrying a flaky store a few times.

        Retries on ``snap.Error``, **not** ``snap.SnapError``. Verified
        against charmlibs-snap 1.0.1: ``SnapError``, ``SnapAPIError``
        and ``SnapNotFoundError`` are *siblings*, so retrying the first
        would miss the store and lookup failures that are the flaky
        ones. ``Error`` is also the only one of the four still present
        on charmlibs main.

        What survives the retries is converted, not re-raised — see
        ADR-0005 section 2.9. The remedy is chosen after the failure
        rather than before, so a healthy install never execs the
        diagnostic.

        Raises:
            PiholeError: The store kept failing after the retries, or
                snapd reported success and the snap is still not
                installed.
        """
        operation = f"installing the {SNAP_NAME} snap"
        retrying = tenacity.Retrying(
            retry=tenacity.retry_if_exception_type(snap.Error),
            wait=self._retry_wait,
            stop=tenacity.stop_after_attempt(INSTALL_ATTEMPTS),
            reraise=True,
        )
        try:
            retrying(self._ensure_installed)
        except snap.Error as err:
            raise _snapd_failure(
                operation=operation,
                remedy=self._install_remedy(),
                err=err,
            ) from err

        revision = self.installed_revision()
        if revision is None:
            raise PiholeError(
                operation=operation,
                expected="an installed revision",
                actual="snapd still reports the snap as absent",
                remedy=self._install_remedy(),
            )
        logger.info("Installed %s revision %s.", SNAP_NAME, revision)

    def start(self, *, enable: bool = True) -> None:
        """Start the FTL daemon, and enable it so it survives a reboot.

        `enable` is keyword-only: a positional `start(False)` would
        quietly leave Pi-hole disabled after a reboot.

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

    def set_webserver_port(self, value: str) -> None:
        """Set `ftl.webserver.port`, and verify `pihole.toml` agrees.

        The only `snap set` this charm performs. Everything else goes
        through the HTTP API, which does not exist until this has been
        applied.

        Raises:
            PiholeError: snapd refused the key, or the value did not
                appear in `pihole.toml`.
        """
        with _converting_snapd_failure(
            operation=f"setting ftl.{WEBSERVER_PORT_KEY} to {value!r}",
            remedy=SNAPD_REMEDY,
        ):
            self._require_snap().set({f"ftl.{WEBSERVER_PORT_KEY}": value})
        actual = self._ftl_config_value(WEBSERVER_PORT_KEY)
        if actual != value:
            raise PiholeError(
                operation=f"setting ftl.{WEBSERVER_PORT_KEY} to {value!r}",
                expected=f"{value!r} in pihole.toml",
                actual=f"it reads back as {actual!r}",
                remedy="`snap set` returns 0 on keys it drops; inspect pihole.toml on the unit",
            )
        logger.info("Set ftl.%s to %r.", WEBSERVER_PORT_KEY, value)

    def set_password(self, password: str) -> None:
        """Apply the admin password with `pihole setpassword`.

        The plaintext never reaches snapd state, which is why this is
        not a `snap set`. It is verified by reading `pwhash` back: the
        salt is random, so a genuine write always changes the hash.

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
            # Deliberately unchained: CalledProcessError stringifies the
            # whole argv, which would put the password in juju-log.
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
            PiholeError: The API never answered. With `webserver.port`
                corrected and the daemon active, that is not "still
                starting" — something a human must look at has gone
                wrong.
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

    # -- Snap plumbing. -----------------------------------------------

    def _snap(self) -> SnapLike | None:
        """Look the snap up, tolerating snapd not knowing about it."""
        try:
            return self._cache_factory()[SNAP_NAME]
        except snap.Error as err:
            logger.debug("snapd could not describe %s: %s", SNAP_NAME, err)
            return None

    def _require_snap(self) -> SnapLike:
        """Look the snap up, letting a snapd failure propagate.

        Raises the raw `snap.Error`: `install`'s retry is keyed on
        that type (ADR-0005 section 2.7). Every other caller must wrap
        this in `_converting_snapd_failure`.
        """
        return self._cache_factory()[SNAP_NAME]

    def _run_pihole(
        self,
        *args: str,
        check: bool,
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a `pihole` subcommand, naming a failure to run it at all.

        Never includes argv in the message: `set_password` passes a
        plaintext password through here, and `OSError` here means a
        missing or half-installed snap.

        Raises:
            PiholeError: The command could not be executed.
            subprocess.CalledProcessError: Passed through untouched
                when `check` is set, because the caller knows what a
                non-zero exit means and what it may quote from it.
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

    # -- Diagnosis. ----------------------------------------------------

    def _install_remedy(self) -> str:
        """Name the remedy that fits the machine this failed on."""
        return install_remedy(in_container=self._in_container())

    def _in_container(self) -> bool:
        """Report whether this unit runs inside a container.

        Only ever sharpens a message: a detection failure must answer
        "not a container" rather than fail the hook or misname the
        remedy on a VM.
        """
        try:
            completed = self._run(
                [DETECT_VIRT_CMD, "--container"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as err:
            logger.debug("Could not run %s: %s", DETECT_VIRT_CMD, err)
            return False
        detected = completed.stdout.strip()
        logger.debug(
            "%s --container exited %s: %r", DETECT_VIRT_CMD, completed.returncode, detected
        )
        return completed.returncode == 0

    # -- pihole.toml. --------------------------------------------------

    def _ftl_config_value(self, key: str) -> str | None:
        """Read one dotted key out of `pihole.toml`.

        Returns None when the file, the table, or the key is absent —
        the normal state before the daemon has ever run — and also when
        the value is not a string, because Stage 1 reads only strings.
        """
        node: object = self._read_toml()
        for segment in key.split("."):
            if not isinstance(node, dict):
                return None
            # A parsed TOML table really is a str-keyed mapping of
            # anything; the cast tells pyright what isinstance cannot.
            node = cast("Mapping[str, object]", node).get(segment)
        return node if isinstance(node, str) else None

    def _read_toml(self) -> Mapping[str, object]:
        """Parse `pihole.toml`, or return nothing if it is not there."""
        path = self._snap_data / PIHOLE_TOML
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as err:
            logger.debug("Could not read %s: %s", path, err)
            return {}
