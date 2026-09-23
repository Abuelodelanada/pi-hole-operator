"""The functional core: facts in, an ordered plan out.

Imports nothing that touches the machine — no `subprocess`,
`charmlibs`, `urllib`, or `ops` — so every decision here is testable
by construction and `==`. The state is a union rather than an early
return, and `compute`'s output is an ordered sequence rather than a
set, because ordering belongs in data, not the event graph. See
ADR-0003 section 2.5.

`fetch` is the charm's only impure read path, impure only through the
`PiholeFacts` collaborator it is handed.

Stage 3 adds connected plugs, a gravity-schedule config option, and
the outcomes to converge them — ConnectPlugs when a required plug is
disconnected, WriteGravityTimer when the schedule has drifted. See
the Stage 3 deliverables in docs/roadmap.md.

Stage 7.b adds DHCP server mode: `DhcpPool`, the `_dhcp_steps`
ordered correction, the unservable gate and the port-67 gate that
block rather than enabling a broken DHCP server, the bounded bind
wait that verifies the enable, and the DHCP plug integration. See
ADR-0006 §2.9, snap-constraints §4.4, and docs/roadmap.md Stage 7.b.
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
"""Plugs this charm always connects, regardless of config.

Without time-control, process-control, and system-observe, FTL.log
emits CAP_SYS_TIME, CAP_SYS_NICE, and /proc/<pid>/comm warnings.
hardware-observe and mount-observe feed the diagnostics page.
network-control and firewall-control join when DHCP is enabled and
are retained after disable by decision — a connected plug is passive,
and re-enabling reconnects via drift (ADR-0006 §2.9). See
snap-constraints section 3.
"""

DHCP_PLUGS: tuple[str, ...] = ("network-control", "firewall-control")
"""Plugs the DHCP server needs. Connected when enabled, retained after
disable by decision (ADR-0006 §2.9)."""

DHCP_PORTS: tuple[tuple[PortProtocol, int], ...] = (("udp", 67),)
"""The DHCP server port, advertised when DHCP is enabled in config.

Advertised on intent (``dhcp_enabled``), not on ``dhcp.active``: an
unservable pool still opens the port while the Blocked status tells the
operator what to fix. DHCPv6 is not advertised: the charm manages no
``dhcp.ipv6`` key, so FTL's DHCPv6 server (547/udp) is never enabled.
See ADR-0006 §2.8.
"""


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
class DhcpPool:
    """The complete DHCP lease pool, a unit applied atomically.

    All four fields must be set before ``dhcp.active`` is enabled:
    setting ``dhcp.active=true`` with an empty pool causes FTL to
    exit 3 with ``DHCP start address is not valid``. See
    snap-constraints §4.4.
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

A new exit code fails ``tox -e static`` instead of being silently
swallowed — every ``match`` on this union ends in ``assert_never``.
See snap-constraints section 7.3.
"""


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
    gravity_schedule: str | None = None
    dhcp_enabled: bool = False
    dhcp_pool: DhcpPool | None = None


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
class RestartFtl:
    """Restart the FTL daemon so capability warnings clear.

    ``Snap.start`` on an active service is a no-op, so the plug-drift
    path needs a genuine restart — the capability warnings from
    ``CAP_SYS_TIME`` and ``CAP_SYS_NICE`` clear only after one. See
    snap-constraints section 3.

    Carries the reason: a restart drops DNS for every client, and the
    cause must reach the log (ADR-0003 section 2.4) — `_apply` logs the
    outcome, so the field is what makes the log line say why.
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
class WaitForDhcpBind:
    """Wait until FTL demonstrably holds 67/udp, or fail the reconcile.

    The enable PATCHes land in pihole.toml before FTL binds the port,
    so a status collection in that window would sample 67 free and
    Block on a gate that self-clears. The wait closes the window by
    naming the owner: `ss -lunp` must show pihole-FTL on 67/udp. See
    ADR-0006 §2.9.
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

    Emitted when a required plug is disconnected; connecting a
    connected plug is idempotent, so this is safe on every reconcile.
    Because plug-dependent capability warnings (CAP_SYS_TIME,
    CAP_SYS_NICE) clear only after an FTL restart, the apply order
    ConnectPlugs → StartFtl is load-bearing — see snap-constraints
    section 3.
    """

    plugs: tuple[str, ...]


@final
@dataclass(frozen=True)
class WriteGravityTimer:
    """Write the host systemd drop-in for the gravity timer schedule.

    The snap cannot manage its own timer schedule: the configure hook
    rejects all `timer.*` keys and snapd has no runtime mechanism to
    change them. The charm writes a host drop-in at
    `/etc/systemd/system/snap.pihole-by-rajannpatel.gravity-sync.timer.d/override.conf`,
    with `OnCalendar=` cleared before being set (drop-ins append
    otherwise). See snap-constraints section 2.2.
    """

    schedule: str


@final
@dataclass(frozen=True)
class RemoveGravityTimer:
    """Remove the host drop-in, restoring the snap's own timer.

    The discriminator lives in the type, not in a sentinel value: a
    `WriteGravityTimer(schedule=None)` that meant "remove" made the
    name lie for one of its two values and forced a dispatch `if` on
    the apply side.
    """


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

    def port67_free(self) -> bool:
        """Whether 67/udp is free for FTL's DHCP server."""
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

    def connected_plugs(self) -> frozenset[str]:
        """The set of snap plugs currently connected.

        Read from ``snap connections``, never assumed.
        """
        ...

    def gravity_schedule(self) -> str | None:
        """The schedule OUR drop-in imposes, or None if absent.

        Deliberately not the effective ``OnCalendar``: the snap ships
        its own randomized default, so the effective value is never
        None — and a fact that reported it would make an unmanaged
        intent plan a removal on every reconcile, forever. The fact
        is "what did WE write"; the effective schedule is what
        ``write_gravity_timer`` reads back after writing.
        """
        ...

    def dhcp_active(self) -> bool | None:
        """Whether ``dhcp.active`` is true in ``pihole.toml``.

        None when the file cannot answer. The decision treats unknown
        as open: the correction is idempotent and its own read-back
        has the final word.
        """
        ...

    def dhcp_pool(self) -> DhcpPool | None:
        """The four DHCP pool keys as ``pihole.toml`` holds them.

        None when any of the four is absent — a partial pool is no
        pool. Total by contract: a fact never raises.
        """
        ...

    def machine_ipv4_addresses(self) -> frozenset[str] | None:
        """The IPv4 addresses on this machine's non-loopback interfaces.

        None when the command cannot be run or exits non-zero — the
        read failed, which is not the same as "no addresses". An
        empty set means the read succeeded and found none. Both make
        ``dhcp_unservable`` return True, but the Blocked message
        distinguishes them so the remedy names the real cause.
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

    Every step is load-bearing, so reordering this tuple breaks the
    install. The snap comes first, while the host still has a working
    resolver, because the store is the flakiest step here (ADR-0005
    section 2.9); the hold follows it immediately so the timer can
    never move what was just installed (ADR-0010). Port 53 is freed
    second, before the daemon starts, because `restart-condition:
    on-failure` turns `EADDRINUSE` into an indefinite crash loop
    (snap-constraints sections 2.1 and 11). Plugs are connected before
    the daemon starts, because the capability warnings clear only after
    a restart (snap-constraints section 3). The NTP server is closed
    before the first start — attack surface nothing asked for. The
    password is applied before the daemon serves, because an empty
    `pwhash` opens the config API to the network. The API is the
    readiness gate, and config lands last because the API only exists
    after the gate. See ADR-0004 section 4.
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
    # Always-managed keys applied in one idempotent PATCH after the API
    # is up. Unmanaged keys (None) are excluded from bootstrap.
    # DHCP deliberately does not appear here: the unservable check needs
    # SnapPresent facts (machine_ipv4_addresses), which bootstrap has
    # none of. DHCP lands on the first converge — the config-changed
    # hook that follows install immediately — gated by dhcp_unservable.
    # See snap-constraints §4.4.
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

    # Plug drift: connect any required plug that is not already
    # connected. Connecting a connected plug is idempotent, but the
    # capability warnings clear only after an FTL restart, so
    # ConnectPlugs is emitted before RestartFtl in the sequence.
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

    # An NTP correction restarts FTL (the configure hook restarts
    # only when a value actually changed), so the step brings its own
    # gate rather than leaving an unguarded bounce for the next status
    # check. The plug-drift restart above is the same class: the DHCP
    # PATCHes that follow would hit a daemon that is still down.
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

    # DHCP config, conditionally emitted and strictly ordered — pool
    # PATCH before active PATCH for the first enable. See _dhcp_steps
    # and snap-constraints §4.4. When the pool is unservable the steps
    # are empty, but the plugs above were still connected and FTL
    # restarted: deliberate — granting capabilities is harmless, the
    # restart is a one-shot DNS blip, and the Blocked status tells the
    # operator what to fix.
    outcomes.extend(_dhcp_steps(state, intent))

    # Gravity timer schedule drift — in both directions: a set schedule
    # that differs writes the drop-in, and an unset schedule with a
    # drop-in still on disk removes it. One direction without the
    # other leaves a stale override behind an unmanaged timer.
    if intent.gravity_schedule is None:
        if state.gravity_schedule is not None:
            outcomes.append(RemoveGravityTimer())
    elif state.gravity_schedule != intent.gravity_schedule:
        outcomes.append(WriteGravityTimer(schedule=intent.gravity_schedule))

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


# -- DHCP server mode (Stage 7.b). -------------------------------------


def _pool_config(pool: DhcpPool) -> tuple[tuple[str, str], ...]:
    """Return the four ``(key, value)`` pairs for the pool PATCH.

    The keys are applied atomically via ``PATCH /api/config`` before
    ``dhcp.active`` is enabled. See snap-constraints §4.4.
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

    The mandatory order for the first enable is pool PATCH first,
    then active PATCH — see snap-constraints §4.4. When the pool
    already matches, only the active step is emitted. When DHCP is
    disabled, the active-false step is emitted if the state is not
    already False (unknown ``None`` counts as drift — the "unknown
    as open" pattern, same as NTP). The enable ends with
    ``WaitForDhcpBind``: the PATCH lands in pihole.toml before FTL
    binds 67, and the wait closes the window in which a status
    collection would sample the port free and Block on a gate that
    self-clears (ADR-0006 §2.9).

    Returns an empty tuple when ``dhcp_unservable`` or
    ``dhcp_port_blocked`` is true: the status handler re-derives the
    Blocked message (it is not pushed from ``_reconcile``), and
    everything else still converges.
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
        # The active PATCH is what makes FTL bind 67; the wait closes
        # the window between the PATCH landing and the bind, so a
        # status collection cannot sample 67 free and Block on a gate
        # that self-clears (ADR-0006 §2.9).
        steps.append(WaitForDhcpBind())
    return tuple(steps)


def dhcp_unservable(state: SnapPresent, intent: PiholeIntent) -> bool:
    """Whether the DHCP pool cannot be served on this machine.

    True when DHCP is enabled, a pool is set, and no machine IPv4
    address falls inside the pool's subnet — FTL would log ``no
    address range available`` and clients would fall back to
    link-local. Narrowed to ``SnapPresent``: the status handler
    guards on ``isinstance`` and ``_dhcp_steps`` takes ``SnapPresent``,
    so the gate is only ever reached with the snap installed. See
    snap-constraints §4.4.
    """
    if not intent.dhcp_enabled or intent.dhcp_pool is None:
        return False
    if state.machine_ipv4_addresses is None:
        return True
    return not any(in_pool_subnet(addr, intent.dhcp_pool) for addr in state.machine_ipv4_addresses)


def dhcp_unservable_reason(state: SnapPresent, intent: PiholeIntent) -> str:
    """Name why the DHCP pool cannot be served, for a Blocked message.

    The state distinguishes a failed address read from a successful
    read with no match, so the remedy names the real cause. The pool
    is guaranteed present when DHCP is enabled (config validation).
    Narrowed to ``SnapPresent``: the status handler guards on
    ``isinstance`` and ``_dhcp_steps`` takes ``SnapPresent``.
    """
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

    True when DHCP is enabled and FTL is not holding 67/udp: either
    the port is held by someone else (enabling would crash-loop FTL)
    or FTL is up with DHCP active but never bound it. The exemption
    is evidence-based — FTL up, DHCP active, *and* the port taken —
    not inferred from the config key alone (rule 6): ``dhcp.active``
    says "we configured DHCP", not "FTL bound 67". After a reboot
    where another service won the port, the key is still true but the
    daemon is down, and the gate must fire with the port remedy
    rather than let the StartFtl push name port 53. Narrowed to
    ``SnapPresent``: the status handler guards on ``isinstance`` and
    ``_dhcp_steps`` takes ``SnapPresent``. See snap-constraints §4.4.
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
    """Name why DHCP is not being served, for a Blocked message.

    Narrowed to ``SnapPresent``: the status handler guards on
    ``isinstance`` and ``_dhcp_steps`` takes ``SnapPresent``. The
    two firing states name different remedies.
    """
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
    """Whether an IPv4 address falls inside the pool's subnet.

    Any ``ValueError`` from malformed values answers False — the
    config validator already caught them, so this is a safety net.
    """
    try:
        network = ipaddress.IPv4Network(f"{pool.start}/{pool.netmask}", strict=False)
        return ipaddress.IPv4Address(address) in network
    except ValueError:
        return False


def open_ports(intent: PiholeIntent) -> tuple[tuple[PortProtocol, int], ...]:
    """The ports this intent serves.

    DNS and web always; NTP and DHCP when enabled. Takes the whole
    intent rather than one extracted flag so later options grow the
    body, not the signature. Pure so the mapping is tested without
    touching ops. See ADR-0006 section 2.8.
    """
    ports = DNS_PORTS + WEB_PORTS
    if intent.ntp_server_enabled:
        ports += NTP_PORTS
    if intent.dhcp_enabled:
        ports += DHCP_PORTS
    return ports
