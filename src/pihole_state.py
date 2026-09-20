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
from typing import Literal, Protocol, assert_never, cast, final

API_READY_TIMEOUT = 120.0
"""Seconds to wait for the HTTP API after starting the daemon."""

# Shared by `pihole.py` and `ftl_api.py`. They live here because this
# module imports neither of them, so this is the one place both can
# reach without a cycle. See ADR-0009 section 4.
SNAP_NAME = "pihole-by-rajannpatel"

SNAP_REVISIONS: Mapping[str, str] = {"amd64": "1417", "arm64": "1415"}
"""The revisions this charm's release is built against — ADR-0010.

Per architecture, because the store numbers every build of the same
source separately. The bump ritual and its evidence live in the ADR;
this docstring deliberately records nothing bump-specific, because it
would be wrong at the next bump with nothing prompting its update.
Bump **every** entry in the change that cuts a charm release — the snap
is held against auto-refresh, so this map is the only path a security
update can take, and a stale pin is invisible in-machine.
"""

_SNAP_ARCH: Mapping[str, str] = {"x86_64": "amd64", "aarch64": "arm64"}
"""`platform.machine()` spellings, mapped to the store's."""


def revision_for(machine: str) -> str | None:
    """The pinned revision for a `platform.machine()` value.

    None on an architecture this charm's release does not pin, which
    the workload answers by refusing to install rather than by taking
    whatever the store offers.
    """
    return SNAP_REVISIONS.get(_SNAP_ARCH.get(machine, ""))


SNAP_DATA = Path(f"/var/snap/{SNAP_NAME}/current")
"""Resolved through `current`; the real path is revision-versioned."""

PIHOLE_TOML = Path("etc/pihole/pihole.toml")
CLI_PW = Path("etc/pihole/cli_pw")
PWHASH_KEY = "webserver.api.pwhash"

type PortProtocol = Literal["tcp", "udp"]
"""What `ops.Port` accepts. Typed here so a typo fails `tox -e static`
rather than at `set_ports` time — the core still imports no `ops`."""

DNS_PORTS: tuple[tuple[PortProtocol, int], ...] = (("tcp", 53), ("udp", 53))
"""DNS on both protocols — a bare int would mean TCP only."""

WEB_PORTS: tuple[tuple[PortProtocol, int], ...] = (("tcp", 80), ("tcp", 443))
"""The admin UI and HTTP API on the snap's stock `webserver.port`. 443
serves the self-signed certificate the snap's launcher generates on
first boot."""

NTP_PORTS: tuple[tuple[PortProtocol, int], ...] = (("udp", 123),)
"""The NTP server, only advertised when the operator enabled it."""


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
    pinned_revision: str | None
    refresh_held: bool
    version: str | None
    ftl_enabled: bool
    ftl_active: bool
    admin_password: AdminPasswordState
    api_ready: bool
    port53_released: bool
    ntp_server_active: bool | None
    upstream_dns: tuple[str, ...] | None
    listening_mode: str | None
    blocking_enabled: bool | None
    dnssec_enabled: bool | None


type PiholeState = SnapAbsent | SnapPresent


# -- The intent. The declared desired state. ---------------------------


@final
@dataclass(frozen=True)
class PiholeIntent:
    """What the deployment is supposed to look like.

    The password is `repr=False` so that logging an outcome cannot
    leak it. See ADR-0006 section 2.1 for the config surface.
    """

    admin_password: str = field(repr=False)
    upstream_dns: tuple[str, ...] | None = None
    listening_mode: str | None = None
    blocking_enabled: bool = True
    dnssec_enabled: bool = False
    ntp_server_enabled: bool = False


@final
@dataclass(frozen=True)
class NoIntentYet:
    """Nothing can be declared yet, so there is nothing to converge to.

    The only reason today is a follower waiting for the leader to mint
    the admin password: the charm cannot converge toward a password it
    does not hold. See ADR-0007 section 1.1.

    A named case rather than `None` because it says *why* the absence
    exists at the point of use — `case NoIntentYet()` reads as the
    condition it is, and both `match` sites keep their `assert_never`.
    (An earlier draft predicted a second reason in Stage 2, invalid
    config; that arrived as `load_config(errors="blocked")` instead,
    which blocks before the intent is read.)
    """


type DeclaredIntent = NoIntentYet | PiholeIntent


# -- The outcomes. Every effect the charm can decide to perform. ------


@final
@dataclass(frozen=True)
class ReleasePort53:
    """Take port 53 away from systemd-resolved."""


@final
@dataclass(frozen=True)
class HoldSnapRefresh:
    """Hold the snap against snapd's auto-refresh timer.

    The hold is per-snap and indefinite; a manual refresh is still
    possible, which is what the revision drift check is for. See
    ADR-0010.
    """


@final
@dataclass(frozen=True)
class InstallSnap:
    """Install the snap at the pinned revision.

    Field-less on purpose: the revision is charm policy, resolved from
    `SNAP_REVISIONS` for this machine's architecture (ADR-0010) — not a
    decision anyone makes per reconcile. Emitted by `_converge` too,
    where it means "re-pin": it reverts a manual refresh on the next
    reconcile.
    """


@final
@dataclass(frozen=True)
class SetNtpServer:
    """Enable or disable the FTL NTP server on 123/udp.

    One outcome whose value carries the decision, rather than two
    separate outcomes — see ADR-0006 section 2.3.
    """

    active: bool


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
class SetFtlConfig:
    """Apply the drifted FTL keys over the HTTP API, in one PATCH.

    Config is sorted by key at construction so the request body is
    deterministic — see ADR-0004 section 5.5. Carries the admin
    password because a `cli_pw` session cannot modify config — see
    snap-constraints section 7.2.8.
    """

    config: tuple[tuple[str, str | bool | tuple[str, ...]], ...]
    password: str = field(repr=False)

    def __init__(
        self,
        config: tuple[tuple[str, str | bool | tuple[str, ...]], ...],
        password: str,
    ) -> None:
        """Store the config sorted by key for deterministic ordering."""
        object.__setattr__(self, "config", tuple(sorted(config, key=lambda item: item[0])))
        object.__setattr__(self, "password", password)


@final
@dataclass(frozen=True)
class Noop:
    """Nothing to do: the machine already matches intent."""


type PiholeOutcome = (
    ReleasePort53
    | InstallSnap
    | HoldSnapRefresh
    | SetNtpServer
    | SetAdminPassword
    | StartFtl
    | AwaitApi
    | SetFtlConfig
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

    def pinned_revision(self) -> str | None:
        """The revision this charm pins for this architecture."""
        ...

    def refresh_held(self) -> bool:
        """Whether snapd will not auto-refresh this snap."""
        ...

    def workload_version(self) -> str | None:
        """The Pi-hole version the snap declares, if any."""
        ...

    def ftl_status(self) -> ServiceStatus:
        """What snapd reports about the FTL daemon."""
        ...

    def api_facts(self, password: str) -> ApiFacts:
        """Classify the password and probe readiness in one session.

        One method rather than two, because both answers come out of
        the same `/api/auth` session and the slots are finite.
        """
        ...

    def port53_released(self) -> bool:
        """Whether port 53 is free for Pi-hole."""
        ...

    def ntp_server_active(self) -> bool | None:
        """Whether FTL's NTP server is enabled on 123/udp.

        None when it cannot be read. The decision treats unknown as
        open: the correction is idempotent and its own read-back has
        the final word, while treating unknown as closed would leave
        123/udp bound on a machine this charm could have fixed.
        """
        ...

    def upstream_dns(self) -> tuple[str, ...] | None:
        """`dns.upstreams` as `pihole.toml` holds it.

        None when the file cannot answer — missing, unparseable, or
        the key absent.
        """
        ...

    def listening_mode(self) -> str | None:
        """`dns.listeningMode` as `pihole.toml` holds it.

        None when the file cannot answer.
        """
        ...

    def blocking_enabled(self) -> bool | None:
        """`dns.blocking.active` as `pihole.toml` holds it.

        None when the file cannot answer.
        """
        ...

    def dnssec_enabled(self) -> bool | None:
        """`dns.dnssec` as `pihole.toml` holds it.

        None when the file cannot answer.
        """
        ...


def fetch(pihole: PiholeFacts, admin_password: str) -> PiholeState:
    """Read every fact the decision depends on, exactly once.

    The only impure read path in the charm — a second one would need
    mocks again to test. Password state and readiness arrive together
    as `ApiFacts`, since one session answers both.

    `admin_password` is a **measurement input, not intent**: `pwhash`
    is salted, so the stored hash matches nothing that can be
    compared, and the only oracle for "is this the right password" is
    to offer a candidate to `/api/auth` (ADR-0007 section 4.3). So
    `admin_password` on the returned state is an answer *about that
    candidate*. Taking the candidate rather than the whole intent is
    deliberate: nothing desired can reach a function whose job is to
    observe.
    """
    revision = pihole.installed_revision()
    if revision is None:
        return SnapAbsent()

    service = pihole.ftl_status()
    api = pihole.api_facts(admin_password)
    return SnapPresent(
        revision=revision,
        pinned_revision=pihole.pinned_revision(),
        refresh_held=pihole.refresh_held(),
        version=pihole.workload_version(),
        ftl_enabled=service.enabled,
        ftl_active=service.active,
        admin_password=api.admin_password,
        api_ready=api.api_ready,
        port53_released=pihole.port53_released(),
        ntp_server_active=pihole.ntp_server_active(),
        upstream_dns=pihole.upstream_dns(),
        listening_mode=pihole.listening_mode(),
        blocking_enabled=pihole.blocking_enabled(),
        dnssec_enabled=pihole.dnssec_enabled(),
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
    section 2.9); the hold follows it immediately so the timer can
    never move what was just installed (ADR-0010). Port 53 is freed
    second, before the daemon starts, because `restart-condition:
    on-failure` turns `EADDRINUSE` into an indefinite crash loop
    (snap-constraints sections 2.1 and 11). The NTP server is closed
    before the first start — attack surface nothing asked for. The
    password is
    applied before the daemon serves, because an empty `pwhash` opens
    the config API to the network. The API is the readiness gate, and
    config lands last because the API only exists after the gate. See
    ADR-0004 section 4.
    """
    outcomes: list[PiholeOutcome] = [
        InstallSnap(),
        HoldSnapRefresh(),
        ReleasePort53(),
        SetNtpServer(active=False),
        SetAdminPassword(intent.admin_password),
        StartFtl(),
        AwaitApi(),
    ]
    # Always-managed keys applied in one idempotent PATCH after the API
    # is up. Unmanaged keys (None) are excluded from bootstrap.
    ftl_config: list[tuple[str, str | bool | tuple[str, ...]]] = [
        ("dns.blocking.active", intent.blocking_enabled),
        ("dns.dnssec", intent.dnssec_enabled),
    ]
    outcomes.append(SetFtlConfig(config=tuple(ftl_config), password=intent.admin_password))
    return tuple(outcomes)


def _converge(state: SnapPresent, intent: PiholeIntent) -> Sequence[PiholeOutcome]:
    """Plan the steps an already-installed machine still needs.

    The same order as `_bootstrap`, minus whatever is already true. A
    fully converged machine yields `(Noop(),)`, which is the literal
    "safe to run twice" proof.
    """
    outcomes: list[PiholeOutcome] = []
    if state.pinned_revision is not None and state.revision != state.pinned_revision:
        # A manual refresh, or a store-side move: re-pin (ADR-0010).
        # An unpinned architecture yields None and is answered by
        # `install`, which refuses rather than guessing.
        outcomes.append(InstallSnap())
    if not state.refresh_held:
        outcomes.append(HoldSnapRefresh())
    if not state.port53_released:
        outcomes.append(ReleasePort53())

    # An NTP correction restarts FTL (the configure hook restarts
    # only when a value actually changed), so the step brings its own
    # gate rather than leaving an unguarded bounce for the next status
    # check.
    ntp_step = _ntp_step(state, intent)
    if ntp_step is not None:
        outcomes.append(ntp_step)
    if _needs_password(state.admin_password):
        outcomes.append(SetAdminPassword(intent.admin_password))
    if not (state.ftl_enabled and state.ftl_active):
        outcomes.append(StartFtl())
    if ntp_step is not None or not state.api_ready:
        outcomes.append(AwaitApi())

    drifted = _drifted_config(state, intent)
    if drifted:
        outcomes.append(SetFtlConfig(config=drifted, password=intent.admin_password))

    return tuple(outcomes) if outcomes else (Noop(),)


def _ntp_step(state: SnapPresent, intent: PiholeIntent) -> SetNtpServer | None:
    """The NTP correction, or None when the server already matches.

    A tri-state comparison: `None` (unreadable) differs from both
    `True` and `False`, so unknown deliberately drifts toward a
    correction — the step is idempotent and its own read-back has the
    final word. See `PiholeFacts.ntp_server_active` for the reasoning.
    """
    if state.ntp_server_active != intent.ntp_server_enabled:
        return SetNtpServer(active=intent.ntp_server_enabled)
    return None


def _drifted_config(
    state: SnapPresent,
    intent: PiholeIntent,
) -> tuple[tuple[str, str | bool | tuple[str, ...]], ...]:
    """The FTL keys whose current value differs from intent.

    Keys the operator never set (`None` in the intent) are not
    managed and never drift. Emitted only when non-empty — see
    ADR-0004 section 5.4.
    """
    drifted: list[tuple[str, str | bool | tuple[str, ...]]] = []
    if intent.upstream_dns is not None and state.upstream_dns != intent.upstream_dns:
        drifted.append(("dns.upstreams", intent.upstream_dns))
    if intent.listening_mode is not None and state.listening_mode != intent.listening_mode:
        drifted.append(("dns.listeningMode", intent.listening_mode))
    if state.blocking_enabled != intent.blocking_enabled:
        drifted.append(("dns.blocking.active", intent.blocking_enabled))
    if state.dnssec_enabled != intent.dnssec_enabled:
        drifted.append(("dns.dnssec", intent.dnssec_enabled))
    return tuple(drifted)


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


def open_ports(intent: PiholeIntent) -> tuple[tuple[PortProtocol, int], ...]:
    """The ports this intent serves: DNS and web always, NTP when on.

    Takes the whole intent rather than one extracted flag so later
    options (DHCP's 67/546) grow the body, not the signature. Pure so
    the mapping is tested without touching ops. See ADR-0006 section
    2.8.
    """
    ports = DNS_PORTS + WEB_PORTS
    if intent.ntp_server_enabled:
        ports += NTP_PORTS
    return ports
