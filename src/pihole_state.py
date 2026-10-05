"""The functional core: facts in, an ordered plan out.

See ADR-0003 §2.5, ADR-0006 §2.9, and snap-constraints §4.4.
"""

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, assert_never, cast, final

API_READY_TIMEOUT = 120.0
"""Seconds to wait for the HTTP API after starting the daemon."""

DHCP_BIND_TIMEOUT = 30.0
"""Seconds to wait for FTL to bind 67/udp after the DHCP enable."""

# Shared by pihole.py and ftl_api.py. See ADR-0009 §4.
SNAP_NAME = "pihole-by-rajannpatel"

SNAP_REVISIONS: Mapping[str, str] = {"amd64": "1417", "arm64": "1415"}
"""The revisions this charm's release is built against. See ADR-0010."""

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
"""What `ops.Port` accepts."""

DNS_PORTS: tuple[tuple[PortProtocol, int], ...] = (("tcp", 53), ("udp", 53))
"""DNS on both protocols — a bare int would mean TCP only."""

WEB_PORTS: tuple[tuple[PortProtocol, int], ...] = (("tcp", 80), ("tcp", 443))
"""The admin UI and HTTP API on the snap's stock `webserver.port`. 443
serves the self-signed certificate the snap's launcher generates on
first boot."""

NTP_PORTS: tuple[tuple[PortProtocol, int], ...] = (("udp", 123),)
"""The NTP server, only advertised when the operator enabled it."""

GRAVITY_TIMER_UNIT = "snap.pihole-by-rajannpatel.gravity-sync.timer"
"""The systemd timer unit that drives the weekly gravity update."""

GRAVITY_TIMER_DROP_IN_DIR = Path(f"/etc/systemd/system/{GRAVITY_TIMER_UNIT}.d")
"""The host directory for the charm's override drop-in."""

GRAVITY_TIMER_DROP_IN = GRAVITY_TIMER_DROP_IN_DIR / "override.conf"
"""The host file the charm writes to set the gravity schedule."""

UNCONDITIONAL_PLUGS: tuple[str, ...] = (
    "system-observe",
    "hardware-observe",
    "mount-observe",
    "time-control",
    "process-control",
)
"""Plugs this charm always connects. See snap-constraints §3."""

DHCP_PLUGS: tuple[str, ...] = ("network-control", "firewall-control")
"""Plugs the DHCP server needs. See ADR-0006 §2.9."""

DHCP_PORTS: tuple[tuple[PortProtocol, int], ...] = (("udp", 67),)
"""The DHCP server port, advertised when DHCP is enabled.

See ADR-0006 §2.8.
"""


def config_value(toml: Mapping[str, object], key: str) -> object | None:
    """Walk one dotted key through a parsed TOML document.

    Returns None when the document's shape does not reach the key.
    """
    node: object = toml
    for segment in key.split("."):
        if not isinstance(node, dict):
            return None
        # A parsed TOML table really is a str-keyed mapping of
        # anything; the cast tells pyright what isinstance cannot.
        node = cast("Mapping[str, object]", node).get(segment)
    return node


@final
@dataclass(frozen=True)
class DhcpPool:
    """The complete DHCP lease pool, applied atomically.

    See snap-constraints §4.4.
    """

    start: str
    end: str
    router: str
    netmask: str


@final
@dataclass(frozen=True)
class ServiceStatus:
    """What snapd reports about a snap service."""

    enabled: bool
    active: bool


@final
@dataclass(frozen=True)
class PasswordUnset:
    """`pwhash` is empty: the config API accepts writes from anyone."""


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
    """A hash is set, but the API could not be consulted."""


type AdminPasswordState = PasswordUnset | PasswordAccepted | PasswordRejected | PasswordUnverified


@final
@dataclass(frozen=True)
class SnapCheckOk:
    """snap-check exit 0: the snap is healthy."""


@final
@dataclass(frozen=True)
class SnapCheckConfigError:
    """snap-check exit 1: a config error (plug disconnected, etc.)."""

    output: str


@final
@dataclass(frozen=True)
class SnapCheckRuntimeError:
    """snap-check exit 2: a runtime error (port conflict, etc.)."""

    output: str


type SnapCheckResult = SnapCheckOk | SnapCheckConfigError | SnapCheckRuntimeError
"""The three semantic exit codes of ``pihole snap-check``.

See snap-constraints §7.3.
"""


@final
@dataclass(frozen=True)
class ApiFacts:
    """The two facts one authenticated API session can establish.

    See snap-constraints §7.2.4.
    """

    admin_password: AdminPasswordState
    api_ready: bool


@final
@dataclass(frozen=True)
class SnapAbsent:
    """The snap is not installed on this machine."""


@final
@dataclass(frozen=True)
class SnapPresent:
    """Facts read off the machine. Every field is observed."""

    revision: str
    pinned_revision: str | None
    refresh_held: bool
    version: str | None
    ftl_enabled: bool
    ftl_active: bool
    admin_password: AdminPasswordState
    api_ready: bool
    port53_released: bool
    port67_free: bool
    ntp_server_active: bool | None
    upstream_dns: tuple[str, ...] | None
    listening_mode: str | None
    blocking_enabled: bool | None
    dnssec_enabled: bool | None
    connected_plugs: frozenset[str]
    gravity_schedule: str | None
    dhcp_active: bool | None
    dhcp_pool: DhcpPool | None
    machine_ipv4_addresses: frozenset[str] | None


type PiholeState = SnapAbsent | SnapPresent


@final
@dataclass(frozen=True)
class PiholeIntent:
    """What the deployment is supposed to look like.

    The password is `repr=False`. See ADR-0006 §2.1.
    """

    admin_password: str = field(repr=False)
    upstream_dns: tuple[str, ...] | None = None
    listening_mode: str | None = None
    blocking_enabled: bool = True
    dnssec_enabled: bool = False
    ntp_server_enabled: bool = False
    gravity_schedule: str | None = None
    dhcp_enabled: bool = False
    dhcp_pool: DhcpPool | None = None


@final
@dataclass(frozen=True)
class NoIntentYet:
    """Nothing can be declared yet, so there is nothing to converge to.

    See ADR-0007 §4.1.
    """


type DeclaredIntent = NoIntentYet | PiholeIntent


@final
@dataclass(frozen=True)
class ReleasePort53:
    """Take port 53 away from systemd-resolved."""


@final
@dataclass(frozen=True)
class HoldSnapRefresh:
    """Hold the snap against snapd's auto-refresh timer.

    See ADR-0010.
    """


@final
@dataclass(frozen=True)
class InstallSnap:
    """Install the snap at the pinned revision. See ADR-0010."""


@final
@dataclass(frozen=True)
class SetNtpServer:
    """Enable or disable the FTL NTP server on 123/udp.

    See ADR-0003 §2.4.
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
class RestartFtl:
    """Restart the FTL daemon so capability warnings clear.

    See snap-constraints §3 and ADR-0003 §2.4.
    """

    reason: str


@final
@dataclass(frozen=True)
class AwaitApi:
    """Wait for the HTTP API, the only honest readiness signal."""

    timeout: float = API_READY_TIMEOUT


@final
@dataclass(frozen=True)
class SetFtlConfig:
    """Apply the drifted FTL keys over the HTTP API, in one PATCH.

    Sorted by key at construction. See ADR-0004 §5.5 and
    snap-constraints §7.2.8.
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
class WaitForDhcpBind:
    """Wait until FTL demonstrably holds 67/udp, or fail the reconcile.

    See ADR-0006 §2.9.
    """

    timeout: float = DHCP_BIND_TIMEOUT


@final
@dataclass(frozen=True)
class Noop:
    """Nothing to do: the machine already matches intent."""


@final
@dataclass(frozen=True)
class ConnectPlugs:
    """Connect the snap plugs this intent needs.

    See snap-constraints §3.
    """

    plugs: tuple[str, ...]


@final
@dataclass(frozen=True)
class WriteGravityTimer:
    """Write the host systemd drop-in for the gravity timer schedule.

    See snap-constraints §2.2.
    """

    schedule: str


@final
@dataclass(frozen=True)
class RemoveGravityTimer:
    """Remove the host drop-in, restoring the snap's own timer."""


type PiholeOutcome = (
    ReleasePort53
    | InstallSnap
    | HoldSnapRefresh
    | SetNtpServer
    | SetAdminPassword
    | StartFtl
    | RestartFtl
    | AwaitApi
    | SetFtlConfig
    | WaitForDhcpBind
    | ConnectPlugs
    | WriteGravityTimer
    | RemoveGravityTimer
    | Noop
)


class PiholeFacts(Protocol):
    """The reads `fetch` needs, implemented by `pihole.Pihole`."""

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
        """Classify the password and probe readiness in one session."""
        ...

    def port53_released(self) -> bool:
        """Whether port 53 is free for Pi-hole."""
        ...

    def port67_free(self) -> bool:
        """Whether 67/udp is free for FTL's DHCP server."""
        ...

    def ntp_server_active(self) -> bool | None:
        """Whether FTL's NTP server is enabled on 123/udp.

        None when it cannot be read.
        """
        ...

    def upstream_dns(self) -> tuple[str, ...] | None:
        """`dns.upstreams` as `pihole.toml` holds it.

        None when the file cannot answer.
        """
        ...

    def listening_mode(self) -> str | None:
        """`dns.listeningMode` as `pihole.toml` holds it."""
        ...

    def blocking_enabled(self) -> bool | None:
        """`dns.blocking.active` as `pihole.toml` holds it."""
        ...

    def dnssec_enabled(self) -> bool | None:
        """`dns.dnssec` as `pihole.toml` holds it."""
        ...

    def connected_plugs(self) -> frozenset[str]:
        """The set of snap plugs currently connected."""
        ...

    def gravity_schedule(self) -> str | None:
        """The schedule OUR drop-in imposes, or None if absent.

        Deliberately not the effective ``OnCalendar``: the snap ships
        its own randomized default.
        """
        ...

    def dhcp_active(self) -> bool | None:
        """Whether ``dhcp.active`` is true in ``pihole.toml``.

        None when the file cannot answer.
        """
        ...

    def dhcp_pool(self) -> DhcpPool | None:
        """The four DHCP pool keys as ``pihole.toml`` holds them.

        None when any of the four is absent.
        """
        ...

    def machine_ipv4_addresses(self) -> frozenset[str] | None:
        """The IPv4 addresses on this machine's non-loopback interfaces.

        None when the command cannot be run. An empty set means the
        read succeeded and found none.
        """
        ...


def fetch(pihole: PiholeFacts, admin_password: str) -> PiholeState:
    """Read every fact the decision depends on, exactly once.

    `admin_password` is a measurement input, not intent. See ADR-0007
    §4.3.
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
        port67_free=pihole.port67_free(),
        ntp_server_active=pihole.ntp_server_active(),
        upstream_dns=pihole.upstream_dns(),
        listening_mode=pihole.listening_mode(),
        blocking_enabled=pihole.blocking_enabled(),
        dnssec_enabled=pihole.dnssec_enabled(),
        connected_plugs=pihole.connected_plugs(),
        gravity_schedule=pihole.gravity_schedule(),
        dhcp_active=pihole.dhcp_active(),
        dhcp_pool=pihole.dhcp_pool(),
        machine_ipv4_addresses=pihole.machine_ipv4_addresses(),
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


def plugs_for(intent: PiholeIntent) -> tuple[str, ...]:
    """Return the snap plugs this intent needs.

    Unconditional plugs always; ``network-control`` and
    ``firewall-control`` join when DHCP is enabled.
    """
    if intent.dhcp_enabled:
        return UNCONDITIONAL_PLUGS + DHCP_PLUGS
    return UNCONDITIONAL_PLUGS


def _bootstrap(intent: PiholeIntent) -> Sequence[PiholeOutcome]:
    """Plan a first install, in the one order that is correct.

    See ADR-0005 §2.9, ADR-0010, snap-constraints §2.1, §3, §11, and
    ADR-0004 §4.
    """
    outcomes: list[PiholeOutcome] = [
        InstallSnap(),
        HoldSnapRefresh(),
        ReleasePort53(),
        ConnectPlugs(plugs=plugs_for(intent)),
        SetNtpServer(active=False),
        SetAdminPassword(intent.admin_password),
        StartFtl(),
        AwaitApi(),
    ]
    # Always-managed keys applied after the API is up. DHCP lands on
    # the first converge, gated by dhcp_unservable. See snap-constraints
    # §4.4.
    ftl_config: list[tuple[str, str | bool | tuple[str, ...]]] = [
        ("dns.blocking.active", intent.blocking_enabled),
        ("dns.dnssec", intent.dnssec_enabled),
    ]
    outcomes.append(SetFtlConfig(config=tuple(ftl_config), password=intent.admin_password))
    if intent.gravity_schedule is not None:
        outcomes.append(WriteGravityTimer(schedule=intent.gravity_schedule))
    return tuple(outcomes)


def _converge(state: SnapPresent, intent: PiholeIntent) -> Sequence[PiholeOutcome]:
    """Plan the steps an already-installed machine still needs.

    A fully converged machine yields `(Noop(),)`.
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

    # Plug drift: ConnectPlugs before RestartFtl — capability
    # warnings clear only after a restart (snap-constraints §3).
    needed = frozenset(plugs_for(intent))
    restarted = False
    if not needed.issubset(state.connected_plugs):
        missing = sorted(needed - state.connected_plugs)
        outcomes.append(ConnectPlugs(plugs=tuple(missing)))
        # Plugs were just connected, so the capability warnings
        # cannot have cleared yet. Force an FTL restart so the
        # warnings disappear on the next status check. The restart
        # itself is idempotent — if the daemon is already down,
        # StartFtl brings it up instead.
        if state.ftl_enabled and state.ftl_active:
            outcomes.append(
                RestartFtl(
                    reason="connecting plugs clears capability warnings only after a restart"
                )
            )
            restarted = True

    # An NTP correction restarts FTL; the step brings its own gate.
    ntp_step = _ntp_step(state, intent)
    if ntp_step is not None:
        outcomes.append(ntp_step)
    if _needs_password(state.admin_password):
        outcomes.append(SetAdminPassword(intent.admin_password))
    if not (state.ftl_enabled and state.ftl_active):
        outcomes.append(StartFtl())
    if ntp_step is not None or not state.api_ready or restarted:
        outcomes.append(AwaitApi())

    drifted = _drifted_config(state, intent)
    if drifted:
        outcomes.append(SetFtlConfig(config=drifted, password=intent.admin_password))

    # DHCP: pool PATCH before active PATCH. See _dhcp_steps and
    # snap-constraints §4.4.
    outcomes.extend(_dhcp_steps(state, intent))

    # Gravity timer drift in both directions: set writes, unset removes.
    if intent.gravity_schedule is None:
        if state.gravity_schedule is not None:
            outcomes.append(RemoveGravityTimer())
    elif state.gravity_schedule != intent.gravity_schedule:
        outcomes.append(WriteGravityTimer(schedule=intent.gravity_schedule))

    return tuple(outcomes) if outcomes else (Noop(),)


def _ntp_step(state: SnapPresent, intent: PiholeIntent) -> SetNtpServer | None:
    """The NTP correction, or None when the server already matches."""
    if state.ntp_server_active != intent.ntp_server_enabled:
        return SetNtpServer(active=intent.ntp_server_enabled)
    return None


def _drifted_config(
    state: SnapPresent,
    intent: PiholeIntent,
) -> tuple[tuple[str, str | bool | tuple[str, ...]], ...]:
    """The FTL keys whose current value differs from intent.

    Unmanaged keys (None in intent) never drift. See ADR-0004 §5.4.
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

    See ADR-0007 §4.3.
    """
    match password:
        case PasswordUnset() | PasswordRejected():
            return True
        case PasswordAccepted() | PasswordUnverified():
            return False
        case _ as unreachable:
            assert_never(unreachable)


def _pool_config(pool: DhcpPool) -> tuple[tuple[str, str], ...]:
    """Return the four ``(key, value)`` pairs for the pool PATCH.

    See snap-constraints §4.4.
    """
    return (
        ("dhcp.start", pool.start),
        ("dhcp.end", pool.end),
        ("dhcp.router", pool.router),
        ("dhcp.netmask", pool.netmask),
    )


def _dhcp_steps(
    state: SnapPresent, intent: PiholeIntent
) -> tuple[SetFtlConfig | WaitForDhcpBind, ...]:
    """The ordered DHCP corrections, or empty when a gate fires.

    Pool PATCH before active PATCH. See snap-constraints §4.4 and
    ADR-0006 §2.9.
    """
    if dhcp_unservable(state, intent):
        return ()
    if dhcp_port_blocked(state, intent):
        return ()

    if not intent.dhcp_enabled:
        if state.dhcp_active is not False:
            return (
                SetFtlConfig(
                    config=(("dhcp.active", False),),
                    password=intent.admin_password,
                ),
            )
        return ()

    # Enabled: pool must be present (config validation guarantees it).
    assert intent.dhcp_pool is not None
    steps: list[SetFtlConfig | WaitForDhcpBind] = []
    if state.dhcp_pool != intent.dhcp_pool:
        steps.append(
            SetFtlConfig(
                config=_pool_config(intent.dhcp_pool),
                password=intent.admin_password,
            )
        )
    if state.dhcp_active is not True:
        steps.append(
            SetFtlConfig(
                config=(("dhcp.active", True),),
                password=intent.admin_password,
            )
        )
        # The active PATCH makes FTL bind 67; the wait closes the
        # window before a status collection can sample 67 free. See
        # ADR-0006 §2.9.
        steps.append(WaitForDhcpBind())
    return tuple(steps)


def dhcp_unservable(state: SnapPresent, intent: PiholeIntent) -> bool:
    """Whether the DHCP pool cannot be served on this machine.

    See snap-constraints §4.4.
    """
    if not intent.dhcp_enabled or intent.dhcp_pool is None:
        return False
    if state.machine_ipv4_addresses is None:
        return True
    return not any(in_pool_subnet(addr, intent.dhcp_pool) for addr in state.machine_ipv4_addresses)


def dhcp_unservable_reason(state: SnapPresent, intent: PiholeIntent) -> str:
    """Name why the DHCP pool cannot be served."""
    assert intent.dhcp_pool is not None
    pool = intent.dhcp_pool
    if state.machine_ipv4_addresses is None:
        return (
            f"DHCP pool {pool.start}-{pool.end}/{pool.netmask} cannot be served: "
            "the machine's IPv4 addresses could not be read; "
            "check the unit's network tooling"
        )
    return (
        f"DHCP pool {pool.start}-{pool.end}/{pool.netmask} cannot be served: "
        "no machine IPv4 address falls inside the pool's subnet; "
        "the serving interface needs an address in the pool's subnet "
        "(see snap-constraints §4.4)"
    )


def dhcp_port_blocked(state: SnapPresent, intent: PiholeIntent) -> bool:
    """Whether DHCP is enabled but FTL is not demonstrably serving 67.

    Evidence-based, not inferred from the config key alone (rule 6).
    See snap-constraints §4.4.
    """
    if not intent.dhcp_enabled or intent.dhcp_pool is None:
        return False
    if state.port67_free:
        # Port free: only a problem when FTL is up with DHCP
        # active but never bound it.
        return state.ftl_active and state.dhcp_active is True
    # Port taken: a problem unless FTL itself holds it.
    return not (state.ftl_active and state.dhcp_active is True)


def dhcp_port_blocked_reason(state: SnapPresent) -> str:
    """Name why DHCP is not being served, for a Blocked message."""
    if state.port67_free:
        return (
            "DHCP is enabled but FTL is not holding 67/udp; check "
            "`snap logs pihole` for why the DHCP server did not start"
        )
    return (
        "DHCP cannot be served: 67/udp is already in use, and FTL "
        "crash-loops when it cannot bind the port (snap-constraints "
        "§4.4); stop the other service so FTL can bind the port"
    )


def in_pool_subnet(address: str, pool: DhcpPool) -> bool:
    """Whether an IPv4 address falls inside the pool's subnet."""
    try:
        network = ipaddress.IPv4Network(f"{pool.start}/{pool.netmask}", strict=False)
        return ipaddress.IPv4Address(address) in network
    except ValueError:
        return False


def open_ports(intent: PiholeIntent) -> tuple[tuple[PortProtocol, int], ...]:
    """The ports this intent serves.

    DNS and web always; NTP and DHCP when enabled. See ADR-0006 §2.8.
    """
    ports = DNS_PORTS + WEB_PORTS
    if intent.ntp_server_enabled:
        ports += NTP_PORTS
    if intent.dhcp_enabled:
        ports += DHCP_PORTS
    return ports
