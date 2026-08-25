"""The functional core: facts in, an ordered plan out.

Imports nothing that touches the machine — no `subprocess`,
`charmlibs`, `urllib`, or `ops` — so every decision here is testable
by construction and `==`. The state is a union rather than an early
return, and `compute`'s output is an ordered sequence rather than a
set, because ordering belongs in data, not the event graph. See
ADR-0003 section 2.5.

`fetch` is the charm's only impure read path, impure only through the
`PiholeFacts` collaborator it is handed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, assert_never, cast, final

WEBSERVER_PORT = "80o,[::]:80o"
"""Plain HTTP on 80, and no TLS.

The packaged default requests TLS, which FTL cannot generate a
certificate for, and the failure aborts the whole webserver. See
ADR-0006 section 2.10 and snap-constraints section 5.1.
"""

API_READY_TIMEOUT = 120.0
"""Seconds to wait for the HTTP API after starting the daemon."""

# Shared by `pihole.py` and `ftl_api.py`. They live here because this
# module imports neither of them, so this is the one place both can
# reach without a cycle. See ADR-0009 section 4.
SNAP_NAME = "pihole-by-rajannpatel"

SNAP_DATA = Path(f"/var/snap/{SNAP_NAME}/current")
"""Resolved through `current`; the real path is revision-versioned."""

PIHOLE_TOML = Path("etc/pihole/pihole.toml")
CLI_PW = Path("etc/pihole/cli_pw")
PWHASH_KEY = "webserver.api.pwhash"


def config_value(toml: Mapping[str, object], key: str) -> object | None:
    """Walk one dotted key through a parsed TOML document.

    Shared by `pihole.py` and `ftl_api.py`, which both read this file
    and neither of which may import the other. Returns None when the
    document's shape does not reach the key.
    """
    node: object = toml
    for segment in key.split("."):
        if not isinstance(node, dict):
            return None
        # A parsed TOML table really is a str-keyed mapping of
        # anything; the cast tells pyright what isinstance cannot.
        node = cast("Mapping[str, object]", node).get(segment)
    return node


# -- Observed facts. What a read of the machine can come back with. ----


@final
@dataclass(frozen=True)
class ServiceStatus:
    """What snapd reports about a snap service."""

    enabled: bool
    active: bool


@final
@dataclass(frozen=True)
class PasswordUnset:
    """`pwhash` is empty: the config API accepts writes from anyone.

    While this holds, the `/api/auth` oracle cannot tell a correct
    password from no password at all, because FTL accepts both.
    """


@final
@dataclass(frozen=True)
class PasswordAccepted:
    """`POST /api/auth` returned 200 for the charm's password."""


@final
@dataclass(frozen=True)
class PasswordRejected:
    """`POST /api/auth` returned 401 for the charm's password."""


@final
@dataclass(frozen=True)
class PasswordUnverified:
    """A hash is set, but the API could not be consulted.

    Normal between setting the password and starting the daemon, and
    the reason this is a fourth case rather than a `bool`.
    """


type AdminPasswordState = PasswordUnset | PasswordAccepted | PasswordRejected | PasswordUnverified


@final
@dataclass(frozen=True)
class ApiFacts:
    """The two facts one authenticated API session can establish.

    Read together because FTL caps sessions at 16 and `fetch` runs
    twice per hook; asking separately would cost two slots each time.
    See snap-constraints section 7.2.4.
    """

    admin_password: AdminPasswordState
    api_ready: bool


# -- The state. Two cases, and the fields that only exist in one. -----


@final
@dataclass(frozen=True)
class SnapAbsent:
    """The snap is not installed on this machine."""


@final
@dataclass(frozen=True)
class SnapPresent:
    """Facts read off the machine. Every field is observed, not assumed.

    Later stages add fields here — connected plugs, the gravity
    database size, the rest of `pihole.toml`. Stage 1 reads only what a
    working, non-hijackable DNS server depends on.
    """

    revision: str
    version: str | None
    ftl_enabled: bool
    ftl_active: bool
    webserver_port: str
    admin_password: AdminPasswordState
    api_ready: bool
    stub_listener_disabled: bool
    ntp_server_active: bool | None


type PiholeState = SnapAbsent | SnapPresent


# -- The intent. The declared desired state, for Stage 1 one field. ---


@final
@dataclass(frozen=True)
class PiholeIntent:
    """What the deployment is supposed to look like.

    Stage 1 has no config options, so the only intent the charm carries
    is the admin password it generated for itself. The password is
    `repr=False` so that logging an outcome cannot leak it.
    """

    admin_password: str = field(repr=False)


@final
@dataclass(frozen=True)
class NoIntentYet:
    """Nothing can be declared yet, so there is nothing to converge to.

    Today the only reason is a follower waiting for the leader to mint
    the admin password. Starting FTL with an empty `pwhash` would open
    the config API to the network, so the charm waits. See ADR-0007
    section 1.1.

    A named case rather than `None` because Stage 2 adds a second
    reason — config that fails validation — and the two need different
    statuses.
    """


type DeclaredIntent = NoIntentYet | PiholeIntent


# -- The outcomes. Every effect the charm can decide to perform. ------


@final
@dataclass(frozen=True)
class ReleasePort53:
    """Take port 53 away from systemd-resolved."""


@final
@dataclass(frozen=True)
class InstallSnap:
    """Install the snap.

    Field-less on purpose: `snap-channel` and `snap-revision` are
    Stage 2 config options, and a field with exactly one possible value
    is a decision nobody makes.
    """


@final
@dataclass(frozen=True)
class SetWebserverPort:
    """Disable TLS, which is what makes the webserver start at all."""

    value: str


@final
@dataclass(frozen=True)
class DisableNtpServer:
    """Close the NTP server the snap opens on 123/udp by default."""


@final
@dataclass(frozen=True)
class SetAdminPassword:
    """Close the unauthenticated-API hole before the daemon serves."""

    password: str = field(repr=False)


@final
@dataclass(frozen=True)
class StartFtl:
    """Start and enable the daemon the snap ships disabled."""


@final
@dataclass(frozen=True)
class AwaitApi:
    """Wait for the HTTP API, the only honest readiness signal."""

    timeout: float = API_READY_TIMEOUT


@final
@dataclass(frozen=True)
class Noop:
    """Nothing to do: the machine already matches intent."""


type PiholeOutcome = (
    ReleasePort53
    | InstallSnap
    | SetWebserverPort
    | DisableNtpServer
    | SetAdminPassword
    | StartFtl
    | AwaitApi
    | Noop
)


# -- The effect boundary, and the two functions that use it. ----------


class PiholeFacts(Protocol):
    """The reads `fetch` needs, implemented by `pihole.Pihole`.

    A Protocol rather than the class itself, so this module stays free
    of anything that touches the machine.
    """

    def installed_revision(self) -> str | None:
        """The installed snap revision, or None if it is absent."""
        ...

    def workload_version(self) -> str | None:
        """The Pi-hole version the snap declares, if any."""
        ...

    def ftl_status(self) -> ServiceStatus:
        """What snapd reports about the FTL daemon."""
        ...

    def webserver_port(self) -> str | None:
        """`webserver.port` as `pihole.toml` actually holds it."""
        ...

    def api_facts(self, password: str) -> ApiFacts:
        """Classify the password and probe readiness in one session.

        One method rather than two, because both answers come out of
        the same `/api/auth` session and the slots are finite.
        """
        ...

    def stub_listener_disabled(self) -> bool:
        """Whether port 53 has been freed for Pi-hole."""
        ...

    def ntp_server_active(self) -> bool | None:
        """Whether FTL's NTP server is enabled on 123/udp.

        None when it cannot be read. The decision treats unknown as
        open: the correction is idempotent and its own read-back has
        the final word, while treating unknown as closed would leave
        123/udp bound on a machine this charm could have fixed.
        """
        ...


def fetch(pihole: PiholeFacts, intent: PiholeIntent) -> PiholeState:
    """Read every fact the decision depends on, exactly once.

    The only impure read path in the charm — a second one would need
    mocks again to test. Password state and readiness arrive together
    as `ApiFacts`, since one session answers both.

    Takes the intent because one fact cannot be observed without it.
    `pwhash` is salted, so the stored hash matches nothing that can be
    compared, and the only oracle for "is this the right password" is
    to offer a candidate to `/api/auth`. So `admin_password` on the
    returned state is an answer *about this intent*, not a
    free-standing fact. See ADR-0007 section 4.3.
    """
    revision = pihole.installed_revision()
    if revision is None:
        return SnapAbsent()

    service = pihole.ftl_status()
    api = pihole.api_facts(intent.admin_password)
    return SnapPresent(
        revision=revision,
        version=pihole.workload_version(),
        ftl_enabled=service.enabled,
        ftl_active=service.active,
        webserver_port=pihole.webserver_port() or "",
        admin_password=api.admin_password,
        api_ready=api.api_ready,
        stub_listener_disabled=pihole.stub_listener_disabled(),
        ntp_server_active=pihole.ntp_server_active(),
    )


def compute(state: PiholeState, intent: PiholeIntent) -> Sequence[PiholeOutcome]:
    """Decide what to do. No IO, and no exceptions for control flow."""
    match state:
        case SnapAbsent():
            return _bootstrap(intent)
        case SnapPresent():
            return _converge(state, intent)
        case _ as unreachable:
            assert_never(unreachable)


def _bootstrap(intent: PiholeIntent) -> Sequence[PiholeOutcome]:
    """Plan a first install, in the one order that is correct.

    Every step is load-bearing, so reordering this tuple breaks the
    install. The snap comes first, while the host still has a working
    resolver, because the store is the flakiest step here (ADR-0005
    section 2.9). Port 53 is freed second, before the daemon starts,
    because `restart-condition: on-failure` turns `EADDRINUSE` into an
    indefinite crash loop (snap-constraints sections 2.1 and 11).
    `webserver.port` is corrected and the NTP server is closed before
    the first start — the webserver never binds without the first fix,
    and the second is attack surface nothing asked for. The password is
    applied before the daemon serves, because an empty `pwhash` opens
    the config API to the network. The API is the readiness gate last,
    because `snap services` reports active long before Pi-hole answers.
    """
    return (
        InstallSnap(),
        ReleasePort53(),
        SetWebserverPort(WEBSERVER_PORT),
        DisableNtpServer(),
        SetAdminPassword(intent.admin_password),
        StartFtl(),
        AwaitApi(),
    )


def _converge(state: SnapPresent, intent: PiholeIntent) -> Sequence[PiholeOutcome]:
    """Plan the steps an already-installed machine still needs.

    The same order as `_bootstrap`, minus whatever is already true. A
    fully converged machine yields `(Noop(),)`, which is the literal
    "safe to run twice" proof.
    """
    outcomes: list[PiholeOutcome] = []
    if not state.stub_listener_disabled:
        outcomes.append(ReleasePort53())

    # A port correction or an NTP correction restarts FTL (the
    # configure hook restarts only when a value actually changed), so
    # either step brings its own gate rather than leaving an unguarded
    # bounce for the next status check.
    port_is_wrong = state.webserver_port != WEBSERVER_PORT
    ntp_is_open = state.ntp_server_active is not False
    if port_is_wrong:
        outcomes.append(SetWebserverPort(WEBSERVER_PORT))
    if ntp_is_open:
        outcomes.append(DisableNtpServer())
    if _needs_password(state.admin_password):
        outcomes.append(SetAdminPassword(intent.admin_password))
    if not (state.ftl_enabled and state.ftl_active):
        outcomes.append(StartFtl())
    if port_is_wrong or ntp_is_open or not state.api_ready:
        outcomes.append(AwaitApi())
    return tuple(outcomes) if outcomes else (Noop(),)


def _needs_password(password: AdminPasswordState) -> bool:
    """Decide whether `pihole setpassword` has to run.

    The random salt makes a hash comparison useless, so the API is
    the oracle instead (ADR-0007 section 4.3). `PasswordUnverified`
    does *not* need a rewrite: a hash is already set, and rewriting
    while the daemon is down would just be churn.
    """
    match password:
        case PasswordUnset() | PasswordRejected():
            return True
        case PasswordAccepted() | PasswordUnverified():
            return False
        case _ as unreachable:
            assert_never(unreachable)
