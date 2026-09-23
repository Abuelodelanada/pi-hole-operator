"""Tests for the functional core.

There is not a single mock in this file, and that is the point: every
decision the charm makes is a pure function of frozen data, so testing
it is construction and `==`. The ordering assertions in particular are
assertions on a returned tuple rather than on mock call order — which
is precisely why the ordering lives in data.
"""

import dataclasses

import pytest

from pihole_state import (
    API_READY_TIMEOUT,
    SNAP_REVISIONS,
    UNCONDITIONAL_PLUGS,
    AdminPasswordState,
    ApiFacts,
    AwaitApi,
    ConnectPlugs,
    DhcpPool,
    HoldSnapRefresh,
    InstallSnap,
    Noop,
    PasswordAccepted,
    PasswordRejected,
    PasswordUnset,
    PasswordUnverified,
    PiholeIntent,
    PiholeOutcome,
    ReleasePort53,
    RemoveGravityTimer,
    RestartFtl,
    ServiceStatus,
    SetAdminPassword,
    SetFtlConfig,
    SetNtpServer,
    SnapAbsent,
    SnapPresent,
    StartFtl,
    WaitForDhcpBind,
    WriteGravityTimer,
    compute,
    dhcp_port_blocked,
    dhcp_port_blocked_reason,
    dhcp_unservable,
    dhcp_unservable_reason,
    fetch,
    in_pool_subnet,
    open_ports,
    plugs_for,
    revision_for,
)

PASSWORD = "a-generated-password"
INTENT = PiholeIntent(admin_password=PASSWORD)


def converged(**overrides: object) -> SnapPresent:
    """A machine that already matches intent, with fields overridden."""
    state = SnapPresent(
        revision=SNAP_REVISIONS["amd64"],
        pinned_revision=SNAP_REVISIONS["amd64"],
        refresh_held=True,
        version="6.4.3",
        ftl_enabled=True,
        ftl_active=True,
        admin_password=PasswordAccepted(),
        api_ready=True,
        port53_released=True,
        port67_free=True,
        ntp_server_active=False,
        upstream_dns=None,
        listening_mode=None,
        blocking_enabled=True,
        dnssec_enabled=False,
        connected_plugs=frozenset(UNCONDITIONAL_PLUGS),
        gravity_schedule=None,
        dhcp_active=False,
        dhcp_pool=None,
        machine_ipv4_addresses=frozenset(),
    )
    return dataclasses.replace(state, **overrides)


@dataclasses.dataclass
class FactsStub:
    """A stand-in for `pihole.Pihole` that counts its reads.

    Not a mock: `fetch` is the boundary between the pure core and the
    machine, so something has to play the machine. It returns plain
    values and records nothing but call counts.
    """

    revision: str | None = SNAP_REVISIONS["amd64"]
    pinned: str | None = SNAP_REVISIONS["amd64"]
    held: bool = True
    version: str | None = "6.4.3"
    service: ServiceStatus = dataclasses.field(
        default_factory=lambda: ServiceStatus(enabled=True, active=True)
    )
    password: AdminPasswordState = dataclasses.field(default_factory=PasswordAccepted)
    ready: bool = True
    port53_free: bool = True
    port67_free_value: bool = True
    ntp: bool | None = False
    upstreams: tuple[str, ...] | None = None
    mode: str | None = None
    blocking: bool | None = True
    dnssec: bool | None = False
    plugs: frozenset[str] = dataclasses.field(
        default_factory=lambda: frozenset(UNCONDITIONAL_PLUGS)
    )
    schedule: str | None = None
    dhcp_active_value: bool | None = False
    pool: DhcpPool | None = None
    machine_addrs: frozenset[str] | None = frozenset()
    reads: list[str] = dataclasses.field(default_factory=list[str])
    passwords_offered: list[str] = dataclasses.field(default_factory=list[str])

    def installed_revision(self) -> str | None:
        """Report the installed revision."""
        self.reads.append("installed_revision")
        return self.revision

    def pinned_revision(self) -> str | None:
        """Report the revision pinned for this machine."""
        self.reads.append("pinned_revision")
        return self.pinned

    def refresh_held(self) -> bool:
        """Report whether the snap is held."""
        self.reads.append("refresh_held")
        return self.held

    def workload_version(self) -> str | None:
        """Report the Pi-hole version."""
        self.reads.append("workload_version")
        return self.version

    def ftl_status(self) -> ServiceStatus:
        """Report what snapd knows about the daemon."""
        self.reads.append("ftl_status")
        return self.service

    def api_facts(self, password: str) -> ApiFacts:
        """Classify the offered password and probe readiness at once."""
        self.reads.append("api_facts")
        self.passwords_offered.append(password)
        return ApiFacts(admin_password=self.password, api_ready=self.ready)

    def port53_released(self) -> bool:
        """Report whether port 53 has been freed."""
        self.reads.append("port53_released")
        return self.port53_free

    def port67_free(self) -> bool:
        """Report whether 67/udp is free."""
        self.reads.append("port67_free")
        return self.port67_free_value

    def ntp_server_active(self) -> bool | None:
        """Report whether FTL's NTP server is enabled."""
        self.reads.append("ntp_server_active")
        return self.ntp

    def upstream_dns(self) -> tuple[str, ...] | None:
        """Report the upstream DNS servers."""
        self.reads.append("upstream_dns")
        return self.upstreams

    def listening_mode(self) -> str | None:
        """Report the listening mode."""
        self.reads.append("listening_mode")
        return self.mode

    def blocking_enabled(self) -> bool | None:
        """Report whether blocking is enabled."""
        self.reads.append("blocking_enabled")
        return self.blocking

    def dnssec_enabled(self) -> bool | None:
        """Report whether DNSSEC is enabled."""
        self.reads.append("dnssec_enabled")
        return self.dnssec

    def connected_plugs(self) -> frozenset[str]:
        """Report the set of connected snap plugs."""
        self.reads.append("connected_plugs")
        return self.plugs

    def gravity_schedule(self) -> str | None:
        """Report our drop-in schedule, or None if absent."""
        self.reads.append("gravity_schedule")
        return self.schedule

    def dhcp_active(self) -> bool | None:
        """Report whether dhcp.active is true."""
        self.reads.append("dhcp_active")
        return self.dhcp_active_value

    def dhcp_pool(self) -> DhcpPool | None:
        """Report the DHCP pool."""
        self.reads.append("dhcp_pool")
        return self.pool

    def machine_ipv4_addresses(self) -> frozenset[str] | None:
        """Report machine's IPv4 addresses, or None if unreadable."""
        self.reads.append("machine_ipv4_addresses")
        return self.machine_addrs


def test_absent_snap_yields_the_whole_ordered_install_sequence():
    # GIVEN a machine with nothing installed
    # WHEN the plan is computed
    outcomes = compute(SnapAbsent(), INTENT)

    # THEN it is exactly the sequence the workload demands, in order.
    # The literal port value is spelled out here on purpose: it is the
    # workaround for a workload defect, and a test that read the
    # constant would not notice it changing.
    assert outcomes == (
        InstallSnap(),
        HoldSnapRefresh(),
        ReleasePort53(),
        ConnectPlugs(plugs=UNCONDITIONAL_PLUGS),
        SetNtpServer(active=False),
        SetAdminPassword(PASSWORD),
        StartFtl(),
        AwaitApi(),
        SetFtlConfig(
            password=PASSWORD, config=(("dns.blocking.active", True), ("dns.dnssec", False))
        ),
    )


def test_the_bootstrap_order_is_the_correctness_condition():
    # GIVEN a machine with nothing installed
    kinds = [type(outcome) for outcome in compute(SnapAbsent(), INTENT)]

    # WHEN the relative order of the steps is inspected
    # THEN the snap is fetched before the host's resolver is displaced.
    # If the store fails after its retries the drop-in was never
    # written, so error state leaves the machine's DNS intact — and a
    # unit in error needs `--force` to remove, which skips the handler
    # that would have put the resolver back. See ADR-0005 section 2.9.
    assert kinds.index(InstallSnap) < kinds.index(ReleasePort53)

    # AND port 53 is still freed before anything starts, which is the
    # workload's actual constraint: the launcher no longer pre-checks
    # the port and crash-loops on EADDRINUSE. Installing does not start
    # it, because the snap ships install-mode: disable.
    assert kinds.index(ReleasePort53) < kinds.index(StartFtl)

    # AND the NTP server is closed before the first start, so 123/udp
    # is never served, not even briefly
    assert kinds.index(SetNtpServer) < kinds.index(StartFtl)

    # AND the admin password is applied before the daemon serves, so
    # there is no window in which the config API is open to the network
    assert kinds.index(SetAdminPassword) < kinds.index(StartFtl)

    # AND readiness is gated after the start, not before it
    assert kinds.index(StartFtl) < kinds.index(AwaitApi)

    # AND config lands last, because the API only exists after the gate
    assert kinds.index(AwaitApi) < kinds.index(SetFtlConfig)


def test_converged_machine_yields_only_noop():
    # GIVEN a machine that already matches intent
    # WHEN the plan is computed
    outcomes = compute(converged(), INTENT)

    # THEN nothing happens: the literal "safe to run twice" proof
    assert outcomes == (Noop(),)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"port53_released": False}, ReleasePort53()),
        ({"revision": "1348"}, InstallSnap()),
        ({"refresh_held": False}, HoldSnapRefresh()),
        ({"admin_password": PasswordUnset()}, SetAdminPassword(PASSWORD)),
        ({"admin_password": PasswordRejected()}, SetAdminPassword(PASSWORD)),
        ({"ftl_active": False}, StartFtl()),
        ({"ftl_enabled": False}, StartFtl()),
        ({"api_ready": False}, AwaitApi()),
    ],
)
def test_one_drifted_fact_yields_exactly_one_outcome(
    overrides: dict[str, object],
    expected: PiholeOutcome,
):
    # GIVEN an otherwise converged machine with one fact drifted
    state = converged(**overrides)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN only the step that fact needs is planned
    assert outcomes == (expected,)


def test_closing_the_ntp_server_brings_its_own_readiness_gate():
    # GIVEN a machine that is converged apart from the NTP server it
    # serves, and whose API is answering right now
    state = converged(ntp_server_active=True, api_ready=True)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the plan does not end by bouncing the daemon with nothing
    # waiting for it. The configure hook restarts FTL whenever a value
    # actually changes, so `api_ready` being true at fetch time says
    # nothing about the state this plan leaves behind.
    assert outcomes == (SetNtpServer(active=False), AwaitApi())


def test_an_unknown_ntp_state_is_treated_as_open():
    # GIVEN a machine whose pihole.toml could not answer — the fact is
    # None rather than True or False
    state = converged(ntp_server_active=None, api_ready=True)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the correction is planned anyway. Treating unknown as open
    # costs one idempotent `snap set` whose own read-back has the final
    # word; treating it as closed would leave 123/udp bound on a
    # machine this charm could have fixed.
    assert outcomes == (SetNtpServer(active=False), AwaitApi())


def test_an_unverifiable_password_is_left_alone():
    # GIVEN a machine whose pwhash is set but whose API cannot be asked
    # — the normal state between setting the password and starting FTL
    state = converged(admin_password=PasswordUnverified(), ftl_active=False, api_ready=False)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the password is not rewritten: nothing is exposed while a
    # hash is set, and rewriting it every reconcile would be churn
    assert outcomes == (StartFtl(), AwaitApi())


def test_an_empty_pwhash_is_always_reapplied():
    # GIVEN a running daemon whose config API is open to the network
    state = converged(admin_password=PasswordUnset())

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the password is applied, because while pwhash is empty the
    # /api/auth oracle cannot tell a correct password from no password
    assert outcomes == (SetAdminPassword(PASSWORD),)


def test_a_wholly_drifted_machine_keeps_the_bootstrap_order():
    # GIVEN an installed machine on which nothing else was ever done
    state = converged(
        port53_released=False,
        admin_password=PasswordUnset(),
        ftl_enabled=False,
        ftl_active=False,
        api_ready=False,
        ntp_server_active=True,
    )

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN it is the install sequence without the install, plus the
    # FTL config that always follows the API gate
    kinds = [type(outcome) for outcome in outcomes]
    assert ReleasePort53 in kinds
    assert SetNtpServer in kinds
    assert SetAdminPassword in kinds
    assert StartFtl in kinds
    assert AwaitApi in kinds
    # The port correction precedes the NTP correction, exactly as in
    # `_bootstrap` — the parity this test exists to enforce
    assert kinds.index(SetAdminPassword) < kinds.index(StartFtl)
    assert kinds.index(StartFtl) < kinds.index(AwaitApi)


def test_awaiting_the_api_carries_a_bounded_timeout():
    # GIVEN the default readiness gate
    # WHEN it is constructed without arguments
    # THEN it still cannot wait forever
    assert AwaitApi().timeout == API_READY_TIMEOUT
    assert AwaitApi().timeout > 0


def test_the_password_never_appears_in_a_repr():
    # GIVEN the three places the plaintext password is carried
    intent = PiholeIntent(admin_password="hunter2")
    outcome = SetAdminPassword("hunter2")
    config = SetFtlConfig(config=(("dns.dnssec", True),), password="hunter2")

    # WHEN any of them is rendered, as logging an outcome does
    # THEN the password is not in the output
    assert "hunter2" not in repr(intent)
    assert "hunter2" not in repr(outcome)
    assert "hunter2" not in repr(config)


def test_fetch_reports_an_uninstalled_machine_without_reading_further():
    # GIVEN a machine with no snap
    facts = FactsStub(revision=None)

    # WHEN the world is read
    state = fetch(facts, PASSWORD)

    # THEN the state is the absent case, and nothing else was probed:
    # there is no daemon to ask about
    assert state == SnapAbsent()
    assert facts.reads == ["installed_revision"]


def test_fetch_reads_every_fact_exactly_once():
    # GIVEN an installed machine
    facts = FactsStub()

    # WHEN the world is read
    state = fetch(facts, PASSWORD)

    # THEN the snapshot holds what the machine said
    assert state == converged()

    # AND each fact was read once: fetch is the single read path, and a
    # second read of the same fact would mean two sources of truth
    assert sorted(facts.reads) == [
        "api_facts",
        "blocking_enabled",
        "connected_plugs",
        "dhcp_active",
        "dhcp_pool",
        "dnssec_enabled",
        "ftl_status",
        "gravity_schedule",
        "installed_revision",
        "listening_mode",
        "machine_ipv4_addresses",
        "ntp_server_active",
        "pinned_revision",
        "port53_released",
        "port67_free",
        "refresh_held",
        "upstream_dns",
        "workload_version",
    ]


def test_fetch_offers_the_candidate_password_to_the_oracle():
    # GIVEN an installed machine
    facts = FactsStub()

    # WHEN the world is read with a candidate password
    fetch(facts, PASSWORD)

    # THEN that candidate is what the oracle was asked about — the
    # salted hash cannot be compared, so the answer is only ever
    # "does FTL accept *this* one", never a free-standing fact
    assert facts.passwords_offered == [PASSWORD]


def test_fetch_reads_both_api_facts_in_one_go():
    # GIVEN a machine whose API says the password is right and whose
    # readiness endpoint says it is not serving yet
    facts = FactsStub(password=PasswordAccepted(), ready=False)

    # WHEN the world is read
    state = fetch(facts, PASSWORD)

    # THEN both facts landed, from a single read. FTL allows 16
    # concurrent API sessions, so asking twice per fetch — twice per
    # hook — spends slots the readiness poll needs.
    assert state == converged(admin_password=PasswordAccepted(), api_ready=False)
    assert facts.reads.count("api_facts") == 1


def test_an_unknown_ntp_fact_passes_through_fetch_unchanged():
    # GIVEN a machine whose pihole.toml cannot answer about NTP — the
    # workload fact is None, and fetch must not guess on its way past
    facts = FactsStub(ntp=None)

    # WHEN the world is read
    state = fetch(facts, PASSWORD)

    # THEN the unknown reaches the pure core intact, where it is
    # treated as open rather than silently resolved to closed
    assert state == converged(ntp_server_active=None)


# -- Stage 2: FTL config diff. ----------------------------------------


def test_drifted_blocking_enabled_yields_set_ftl_config():
    # GIVEN a machine where blocking is off but intent says on
    state = converged(blocking_enabled=False)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the drifted key is applied via the API
    assert outcomes == (SetFtlConfig(password=PASSWORD, config=(("dns.blocking.active", True),)),)


def test_drifted_dnssec_yields_set_ftl_config():
    # GIVEN a machine where dnssec is on but intent says off
    state = converged(dnssec_enabled=True)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the drifted key is applied
    assert outcomes == (SetFtlConfig(password=PASSWORD, config=(("dns.dnssec", False),)),)


def test_drifted_upstream_dns_yields_set_ftl_config():
    # GIVEN an intent with managed upstreams that differ from state
    intent = PiholeIntent(admin_password=PASSWORD, upstream_dns=("1.1.1.1", "9.9.9.9"))
    state = converged(upstream_dns=("8.8.8.8",))

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN the drifted key is applied
    assert outcomes == (
        SetFtlConfig(password=PASSWORD, config=(("dns.upstreams", ("1.1.1.1", "9.9.9.9")),)),
    )


def test_drifted_listening_mode_yields_set_ftl_config():
    # GIVEN an intent with a managed listening mode that differs
    intent = PiholeIntent(admin_password=PASSWORD, listening_mode="ALL")
    state = converged(listening_mode="LOCAL")

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN the drifted key is applied
    assert outcomes == (SetFtlConfig(password=PASSWORD, config=(("dns.listeningMode", "ALL"),)),)


def test_multiple_drifts_are_sorted_by_key():
    # GIVEN a machine with several drifted FTL config keys
    intent = PiholeIntent(
        admin_password=PASSWORD,
        upstream_dns=("1.1.1.1",),
        listening_mode="ALL",
        blocking_enabled=False,
        dnssec_enabled=True,
    )
    state = converged(
        upstream_dns=None,
        listening_mode="LOCAL",
        blocking_enabled=True,
        dnssec_enabled=False,
    )

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN the config is sorted by key name, not by insertion order
    assert outcomes == (
        SetFtlConfig(
            password=PASSWORD,
            config=(
                ("dns.blocking.active", False),
                ("dns.dnssec", True),
                ("dns.listeningMode", "ALL"),
                ("dns.upstreams", ("1.1.1.1",)),
            ),
        ),
    )


def test_unmanaged_upstream_dns_produces_no_outcome():
    # GIVEN an intent where upstream_dns is None (unmanaged)
    intent = PiholeIntent(admin_password=PASSWORD, upstream_dns=None)
    state = converged(upstream_dns=("8.8.8.8",))

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN no SetFtlConfig is emitted for upstreams
    assert outcomes == (Noop(),)


def test_unmanaged_listening_mode_produces_no_outcome():
    # GIVEN an intent where listening_mode is None (unmanaged)
    intent = PiholeIntent(admin_password=PASSWORD, listening_mode=None)
    state = converged(listening_mode="ALL")

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN no SetFtlConfig is emitted for listening mode
    assert outcomes == (Noop(),)


def test_none_blocking_state_drifts():
    # GIVEN a machine where blocking_enabled is None (unreadable)
    state = converged(blocking_enabled=None)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN None drifts — it is not the same as True
    assert outcomes == (SetFtlConfig(password=PASSWORD, config=(("dns.blocking.active", True),)),)


def test_none_dnssec_state_drifts():
    # GIVEN a machine where dnssec_enabled is None (unreadable)
    state = converged(dnssec_enabled=None)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN None drifts — it is not the same as False
    assert outcomes == (SetFtlConfig(password=PASSWORD, config=(("dns.dnssec", False),)),)


def test_ntp_server_enabled_when_state_is_not_clearly_enabled():
    # GIVEN an intent that wants NTP on, and a state that is not
    # clearly enabled (None or False)
    intent = PiholeIntent(admin_password=PASSWORD, ntp_server_enabled=True)
    state = converged(ntp_server_active=None)

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN the NTP server is enabled, and the restart is waited out
    assert outcomes == (SetNtpServer(active=True), AwaitApi())


def test_ntp_server_not_enabled_when_already_on():
    # GIVEN an intent that wants NTP on, and a state that already has it
    intent = PiholeIntent(admin_password=PASSWORD, ntp_server_enabled=True)
    state = converged(ntp_server_active=True)

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN nothing happens — already converged
    assert outcomes == (Noop(),)


def test_ntp_server_disabled_when_intent_says_off_and_state_is_open():
    # GIVEN an intent that wants NTP off (default), and a state that is
    # open (True or None)
    state = converged(ntp_server_active=True)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the NTP server is closed
    assert outcomes == (SetNtpServer(active=False), AwaitApi())


# -- open_ports. -------------------------------------------------------


def test_open_ports_without_ntp():
    # GIVEN an intent with NTP disabled
    # WHEN the ports are computed
    ports = open_ports(PiholeIntent(admin_password=PASSWORD, ntp_server_enabled=False))

    # THEN 123/udp is not included
    assert ports == (("tcp", 53), ("udp", 53), ("tcp", 80), ("tcp", 443))


def test_open_ports_with_ntp():
    # GIVEN an intent with NTP enabled
    # WHEN the ports are computed
    ports = open_ports(PiholeIntent(admin_password=PASSWORD, ntp_server_enabled=True))

    # THEN 123/udp is included
    assert ports == (("tcp", 53), ("udp", 53), ("tcp", 80), ("tcp", 443), ("udp", 123))


# -- The NTP tri-state comparison. -------------------------------------


@pytest.mark.parametrize(
    ("ntp_server_active", "ntp_server_enabled", "expected"),
    [
        (True, True, (Noop(),)),
        (False, True, (SetNtpServer(active=True), AwaitApi())),
        (None, True, (SetNtpServer(active=True), AwaitApi())),
        (True, False, (SetNtpServer(active=False), AwaitApi())),
        (False, False, (Noop(),)),
        (None, False, (SetNtpServer(active=False), AwaitApi())),
    ],
    ids=[
        "on-and-wanted",
        "off-but-wanted",
        "unknown-but-wanted",
        "on-but-unwanted",
        "off-and-unwanted",
        "unknown-and-unwanted",
    ],
)
def test_the_ntp_decision_is_a_tri_state_comparison(
    ntp_server_active: bool | None,
    ntp_server_enabled: bool,
    expected: tuple[PiholeOutcome, ...],
):
    # GIVEN a machine's NTP state and an operator's NTP intent
    state = converged(ntp_server_active=ntp_server_active)
    intent = PiholeIntent(admin_password=PASSWORD, ntp_server_enabled=ntp_server_enabled)

    # WHEN the plan is computed
    # THEN the NTP step is the one-line drift answer — with unknown
    # drifting to a correction in both directions — and a correction
    # always carries its own readiness gate
    assert compute(state, intent) == expected


# -- The per-architecture pin (ADR-0010). ------------------------------


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", SNAP_REVISIONS["amd64"]),
        ("aarch64", SNAP_REVISIONS["arm64"]),
        ("armv7l", None),
        ("riscv64", None),
        ("", None),
    ],
    ids=["amd64", "arm64", "armhf", "riscv64", "empty"],
)
def test_the_pin_is_resolved_per_architecture(machine: str, expected: str | None):
    # GIVEN what `platform.machine()` reports on some host
    # WHEN the pinned revision is resolved
    # THEN each supported architecture gets its own number — the store
    # numbers every build separately, so one constant cannot fit both —
    # and an unsupported one gets None rather than a wrong revision
    assert revision_for(machine) == expected


def test_the_two_pinned_revisions_are_different_numbers():
    # GIVEN the pin map
    # WHEN the two supported architectures are compared
    # THEN they differ: assuming otherwise is the bug this map fixes
    assert SNAP_REVISIONS["amd64"] != SNAP_REVISIONS["arm64"]


def test_an_unpinned_architecture_plans_no_reinstall():
    # GIVEN an installed machine whose architecture this release does
    # not pin — the fact reads None
    state = converged(pinned_revision=None, revision="9999")

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN no re-pin is planned against a revision that does not exist.
    # `install` is what refuses, with the architecture in the message.
    assert outcomes == (Noop(),)


# -- plugs_for. --------------------------------------------------------


def test_plugs_for_returns_the_unconditional_plugs_only():
    # GIVEN an intent without DHCP enabled
    # WHEN the required plugs are computed
    plugs = plugs_for(INTENT)

    # THEN only the five unconditional plugs are returned
    assert plugs == UNCONDITIONAL_PLUGS


def test_disconnected_plugs_yield_connectplugs_and_restartftl():
    # GIVEN a converged machine where one required plug is disconnected
    state = converged(
        connected_plugs=frozenset({"system-observe", "hardware-observe", "mount-observe"})
    )

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the two missing plugs are connected, and because capability
    # warnings clear only after a restart, FTL is restarted — and the
    # restart is gated: the API is down until it comes back, so AwaitApi
    # follows rather than leaving an unguarded bounce.
    assert outcomes == (
        ConnectPlugs(plugs=("process-control", "time-control")),
        RestartFtl(reason="connecting plugs clears capability warnings only after a restart"),
        AwaitApi(),
    )


def test_all_plugs_connected_yields_no_connectplugs():
    # GIVEN a converged machine with all plugs connected
    state = converged(
        connected_plugs=frozenset(UNCONDITIONAL_PLUGS),
    )

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN nothing happens
    assert outcomes == (Noop(),)


# -- WriteGravityTimer. ------------------------------------------------


def test_gravity_schedule_drift_yields_writegravitytimer():
    # GIVEN an intent with a managed schedule that differs from state
    intent = PiholeIntent(admin_password=PASSWORD, gravity_schedule="Sun *-*-* 03:00")
    state = converged(gravity_schedule="Sun *-*-* 04:25")

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN a write is planned
    assert outcomes == (WriteGravityTimer(schedule="Sun *-*-* 03:00"),)


def test_gravity_schedule_matches_yields_no_write():
    # GIVEN an intent with a schedule that already matches state
    intent = PiholeIntent(admin_password=PASSWORD, gravity_schedule="Sun *-*-* 03:00")
    state = converged(gravity_schedule="Sun *-*-* 03:00")

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN nothing happens
    assert outcomes == (Noop(),)


def test_unmanaged_gravity_schedule_with_no_drop_in_yields_no_write():
    # GIVEN an intent where gravity_schedule is None (unmanaged) and no
    # drop-in on disk either
    intent = PiholeIntent(admin_password=PASSWORD, gravity_schedule=None)
    state = converged(gravity_schedule=None)

    # WHEN the plan is computed
    outcomes = compute(state, intent)

    # THEN nothing is emitted — unmanaged and already absent
    assert outcomes == (Noop(),)


def test_gravity_schedule_in_bootstrap():
    # GIVEN an intent with a gravity schedule set
    intent = PiholeIntent(admin_password=PASSWORD, gravity_schedule="Sun *-*-* 03:00")

    # WHEN a fresh machine is bootstrapped
    outcomes = compute(SnapAbsent(), intent)

    # THEN WriteGravityTimer appears after SetFtlConfig
    kinds = [type(o) for o in outcomes]
    assert WriteGravityTimer in kinds
    assert kinds.index(SetFtlConfig) < kinds.index(WriteGravityTimer)


def test_unsetting_the_schedule_removes_the_drop_in():
    """Unmanaged intent with a drop-in on disk plans its removal.

    The one-directional version of this drift check was a real bug,
    found while covering the error paths: unset config left the old
    override behind forever, and `remove_gravity_timer` was dead code.
    """
    # GIVEN a machine with a drop-in active and an intent that no
    # longer manages the schedule
    state = converged(gravity_schedule="Sun *-*-* 04:00")
    intent = PiholeIntent(admin_password=PASSWORD, gravity_schedule=None)

    # WHEN the machine converges
    outcomes = compute(state, intent)

    # THEN the removal is planned, as its own variant — the
    # discriminator lives in the type, not in a sentinel value
    assert outcomes == (RemoveGravityTimer(),)


def test_disconnected_plugs_with_ftl_not_active_skips_restart():
    # GIVEN a machine where plugs are disconnected but FTL is not active
    # — the daemon is down, so there is nothing to restart
    state = converged(
        connected_plugs=frozenset({"system-observe", "hardware-observe", "mount-observe"}),
        ftl_enabled=False,
        ftl_active=False,
        api_ready=False,
    )

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the missing plugs are connected, but StartFtl is NOT emitted
    # as a restart — it will be emitted by the separate ftl_active check
    # later in _converge. The restart guard only fires when FTL is
    # already running.
    kinds = [type(o) for o in outcomes]
    assert ConnectPlugs in kinds
    assert RestartFtl not in kinds
    # StartFtl appears once (from the ftl_active check), not twice
    assert kinds.count(StartFtl) == 1
    # The ConnectPlugs comes before the single StartFtl
    assert kinds.index(ConnectPlugs) < kinds.index(StartFtl)


# -- Stage 7.b: DHCP server mode. --------------------------------------

DHCP_POOL = DhcpPool(
    start="192.168.1.10", end="192.168.1.50", router="192.168.1.1", netmask="255.255.255.0"
)
DHCP_INTENT = PiholeIntent(admin_password=PASSWORD, dhcp_enabled=True, dhcp_pool=DHCP_POOL)


def test_dhcp_first_enable_orders_pool_before_active():
    """The roadmap acceptance criterion: pool PATCH before active.

    The spike recorded that one PATCH /api/config applies the pool
    atomically, but the mandatory order still applies to the first
    enable (active false → true). See snap-constraints §4.4.
    """
    # GIVEN a converged machine with DHCP disabled and no pool
    state = converged(
        dhcp_active=False,
        dhcp_pool=None,
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed with DHCP enabled
    outcomes = compute(state, DHCP_INTENT)

    # THEN the pool SetFtlConfig (contains dhcp.start) strictly
    # precedes the active SetFtlConfig (contains dhcp.active)
    set_ftl_indices = [i for i, o in enumerate(outcomes) if isinstance(o, SetFtlConfig)]
    assert len(set_ftl_indices) >= 2
    pool_step = outcomes[set_ftl_indices[0]]
    active_step = outcomes[set_ftl_indices[1]]
    assert isinstance(pool_step, SetFtlConfig)
    assert isinstance(active_step, SetFtlConfig)
    assert any(k == "dhcp.start" for k, _ in pool_step.config)
    assert any(k == "dhcp.active" for k, _ in active_step.config)
    assert set_ftl_indices[0] < set_ftl_indices[1]


def test_dhcp_enable_with_pool_already_matching_emits_active_only():
    """Pool already matches, only active step needed."""
    # GIVEN a machine where the pool already matches but active is False
    state = converged(
        dhcp_active=False,
        dhcp_pool=DHCP_POOL,
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed
    outcomes = compute(state, DHCP_INTENT)

    # THEN only the active step is emitted
    set_ftls = [o for o in outcomes if isinstance(o, SetFtlConfig)]
    assert len(set_ftls) == 1
    assert set_ftls[0].config == (("dhcp.active", True),)


def test_dhcp_pool_drift_with_active_true_emits_pool_only():
    """Pool drifted but active is already True — pool step only."""
    # GIVEN a machine with a different pool but active already True
    # (FTL holds 67, so the port gate's exemption applies)
    old_pool = DhcpPool(
        start="10.0.0.10", end="10.0.0.50", router="10.0.0.1", netmask="255.255.255.0"
    )
    state = converged(
        dhcp_active=True,
        dhcp_pool=old_pool,
        port67_free=False,
        machine_ipv4_addresses=frozenset[str]({"10.0.0.5", "192.168.1.5"}),
    )

    # WHEN the plan is computed
    outcomes = compute(state, DHCP_INTENT)

    # THEN only the pool step is emitted
    set_ftls = [o for o in outcomes if isinstance(o, SetFtlConfig)]
    assert len(set_ftls) == 1
    assert any(k == "dhcp.start" for k, _ in set_ftls[0].config)


def test_dhcp_first_enable_waits_for_the_bind():
    """The enable ends with WaitForDhcpBind, after the active PATCH.

    The PATCH lands in pihole.toml before FTL binds 67, so the wait
    closes the window in which a status collection would sample the
    port free and Block on a gate that self-clears (ADR-0006 §2.9).
    """
    # GIVEN a converged machine with DHCP disabled and no pool
    state = converged(
        dhcp_active=False,
        dhcp_pool=None,
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed with DHCP enabled
    outcomes = compute(state, DHCP_INTENT)

    # THEN the last DHCP outcome is the bind wait, after the active
    # PATCH
    dhcp_outcomes = [o for o in outcomes if isinstance(o, (SetFtlConfig, WaitForDhcpBind))]
    assert isinstance(dhcp_outcomes[-1], WaitForDhcpBind)
    active_index = next(
        i
        for i, o in enumerate(dhcp_outcomes)
        if isinstance(o, SetFtlConfig) and any(k == "dhcp.active" for k, _ in o.config)
    )
    assert active_index < len(dhcp_outcomes) - 1


def test_dhcp_pool_drift_with_active_true_does_not_wait():
    """Pool drift with active already True → no bind wait.

    FTL already demonstrably holds 67 (the port gate's exemption
    requires it), so there is nothing to wait for.
    """
    # GIVEN a machine with a different pool but active already True
    old_pool = DhcpPool(
        start="10.0.0.10", end="10.0.0.50", router="10.0.0.1", netmask="255.255.255.0"
    )
    state = converged(
        dhcp_active=True,
        dhcp_pool=old_pool,
        port67_free=False,
        machine_ipv4_addresses=frozenset[str]({"10.0.0.5", "192.168.1.5"}),
    )

    # WHEN the plan is computed
    outcomes = compute(state, DHCP_INTENT)

    # THEN no WaitForDhcpBind is emitted
    assert not any(isinstance(o, WaitForDhcpBind) for o in outcomes)


def test_dhcp_disable_does_not_wait_for_a_bind():
    """Disabling DHCP never waits for a bind."""
    # GIVEN a machine with DHCP active
    state = converged(
        dhcp_active=True,
        dhcp_pool=DHCP_POOL,
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed with DHCP disabled
    outcomes = compute(state, INTENT)

    # THEN no WaitForDhcpBind is emitted
    assert not any(isinstance(o, WaitForDhcpBind) for o in outcomes)


def test_dhcp_disable_with_active_true_emits_active_false():
    """Disabling DHCP when active emits the active-false step."""
    # GIVEN a machine with DHCP active
    state = converged(
        dhcp_active=True,
        dhcp_pool=DHCP_POOL,
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed with DHCP disabled
    outcomes = compute(state, INTENT)

    # THEN the active-false step is emitted
    set_ftls = [o for o in outcomes if isinstance(o, SetFtlConfig)]
    assert len(set_ftls) == 1
    assert set_ftls[0].config == (("dhcp.active", False),)


def test_dhcp_disabled_with_active_none_emits_active_false():
    """Unknown as open: active None drifts to the active-false step."""
    # GIVEN a machine where dhcp_active is None (unreadable)
    state = converged(dhcp_active=None, dhcp_pool=None)

    # WHEN the plan is computed with DHCP disabled
    outcomes = compute(state, INTENT)

    # THEN the active-false step is emitted — unknown is not False
    set_ftls = [o for o in outcomes if isinstance(o, SetFtlConfig)]
    assert len(set_ftls) == 1
    assert set_ftls[0].config == (("dhcp.active", False),)


def test_dhcp_enabled_converged_yields_no_dhcp_steps():
    """Enabled + converged → no DHCP steps from _dhcp_steps."""
    # GIVEN a machine where DHCP is already enabled with the right pool
    state = converged(
        dhcp_active=True,
        dhcp_pool=DHCP_POOL,
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed
    outcomes = compute(state, DHCP_INTENT)

    # THEN no SetFtlConfig for DHCP is emitted
    dhcp_keys = {"dhcp.start", "dhcp.end", "dhcp.router", "dhcp.netmask", "dhcp.active"}
    for o in outcomes:
        if isinstance(o, SetFtlConfig):
            for k, _ in o.config:
                assert k not in dhcp_keys


def test_dhcp_unservable_skips_dhcp_steps():
    """Enabled + unservable → no DHCP steps even though pool drifted."""
    # GIVEN a machine with no IPv4 addresses in the pool's subnet
    state = converged(
        dhcp_active=False,
        dhcp_pool=None,
        machine_ipv4_addresses=frozenset({"10.0.0.1"}),
    )

    # WHEN the plan is computed with DHCP enabled
    outcomes = compute(state, DHCP_INTENT)

    # THEN no DHCP steps are emitted — the status handler re-derives
    # the Blocked message from the same state
    dhcp_keys = {"dhcp.start", "dhcp.end", "dhcp.router", "dhcp.netmask", "dhcp.active"}
    for o in outcomes:
        if isinstance(o, SetFtlConfig):
            for k, _ in o.config:
                assert k not in dhcp_keys


# -- plugs_for with DHCP. ----------------------------------------------


def test_plugs_for_with_dhcp_enabled_includes_dhcp_plugs():
    """plugs_for(enabled) includes network/firewall control plugs."""
    # GIVEN an intent with DHCP enabled
    # WHEN the required plugs are computed
    plugs = plugs_for(DHCP_INTENT)
    # THEN the DHCP plugs are included alongside the unconditional ones
    assert "network-control" in plugs
    assert "firewall-control" in plugs
    for plug in UNCONDITIONAL_PLUGS:
        assert plug in plugs


def test_plugs_for_with_dhcp_disabled_equals_unconditional():
    """plugs_for(disabled) equals UNCONDITIONAL_PLUGS."""
    # GIVEN an intent with DHCP disabled
    # WHEN the required plugs are computed
    plugs = plugs_for(INTENT)
    # THEN only the unconditional plugs are required
    assert plugs == UNCONDITIONAL_PLUGS


def test_first_dhcp_enable_with_missing_plugs_gates_restart_before_patch():
    """The plug-drift restart is gated before the DHCP PATCHes.

    The first DHCP enable on a machine whose DHCP plugs are missing
    emits ConnectPlugs + RestartFtl (plug drift) followed by the DHCP
    PATCHes. The restart leaves the API down, so AwaitApi must sit
    between RestartFtl and the first SetFtlConfig — without it the
    first enable goes Blocked with an unreachable-API error (the NTP
    restart already had this gate; the plug-drift restart is the same
    class).
    """
    # GIVEN a machine where DHCP plugs are not connected and DHCP is
    # not yet enabled (the first-enable shape: pool and active both
    # drift, so the DHCP PATCHes follow the plug-drift restart)
    state = converged(
        connected_plugs=frozenset(UNCONDITIONAL_PLUGS),
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed with DHCP enabled
    outcomes = compute(state, DHCP_INTENT)

    # THEN ConnectPlugs carries the DHCP plugs, and RestartFtl follows
    kinds = [type(o) for o in outcomes]
    assert ConnectPlugs in kinds
    connect_idx = kinds.index(ConnectPlugs)
    connect = outcomes[connect_idx]
    assert isinstance(connect, ConnectPlugs)
    assert "network-control" in connect.plugs
    assert "firewall-control" in connect.plugs
    assert RestartFtl in kinds
    # The restart leaves the API down, so the DHCP PATCHes that follow
    # must be gated: AwaitApi sits between RestartFtl and the first
    # SetFtlConfig, or the first enable goes Blocked with an
    # unreachable-API error (the NTP restart already had this gate; the
    # plug-drift restart is the same class).
    restart_idx = kinds.index(RestartFtl)
    await_idx = kinds.index(AwaitApi)
    first_set_ftl = next(i for i, o in enumerate(outcomes) if isinstance(o, SetFtlConfig))
    assert restart_idx < await_idx < first_set_ftl


# -- open_ports with DHCP. --------------------------------------------


def test_open_ports_with_dhcp_enabled_includes_dhcp_ports():
    """open_ports(enabled) includes 67/udp — DHCPv6 is not managed."""
    # GIVEN an intent with DHCP enabled
    # WHEN the ports to open are computed
    ports = open_ports(DHCP_INTENT)
    # THEN 67/udp is opened and 547/udp (DHCPv6) is not
    assert ("udp", 67) in ports
    assert ("udp", 547) not in ports


def test_open_ports_with_dhcp_disabled_excludes_dhcp_ports():
    """open_ports(disabled) does not include DHCP ports."""
    # GIVEN an intent with DHCP disabled
    # WHEN the ports to open are computed
    ports = open_ports(INTENT)
    # THEN no DHCP port is opened
    assert ("udp", 67) not in ports
    assert ("udp", 547) not in ports


# -- dhcp_unservable (pure). ------------------------------------------


def test_dhcp_unservable_with_address_in_subnet_is_false():
    """An address inside the pool subnet → False."""
    # GIVEN a machine with an address inside the pool subnet
    state = converged(machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}))
    # THEN the pool is servable
    assert dhcp_unservable(state, DHCP_INTENT) is False


def test_dhcp_unservable_with_only_addresses_outside_is_true():
    """Only addresses outside the pool subnet → True."""
    # GIVEN a machine with only addresses outside the pool subnet
    state = converged(machine_ipv4_addresses=frozenset[str]({"10.0.0.1", "172.16.0.1"}))
    # THEN the pool is unservable
    assert dhcp_unservable(state, DHCP_INTENT) is True


def test_dhcp_unservable_with_empty_addresses_is_true():
    """Empty address set -> True (unknown is unservable -- safe)."""
    # GIVEN a machine with no readable addresses
    state = converged(machine_ipv4_addresses=frozenset[str]())
    # THEN the pool is unservable
    assert dhcp_unservable(state, DHCP_INTENT) is True


def test_dhcp_unservable_with_unreadable_addresses_is_true():
    """Unreadable addresses (None) -> True (safe direction)."""
    # GIVEN a machine whose addresses could not be read
    state = converged(machine_ipv4_addresses=None)
    # THEN the pool is unservable
    assert dhcp_unservable(state, DHCP_INTENT) is True


def test_dhcp_unservable_disabled_is_false():
    """Disabled DHCP -> False regardless of addresses."""
    # GIVEN DHCP disabled with no readable addresses
    state = converged(machine_ipv4_addresses=frozenset[str]())
    # THEN the gate does not fire
    assert dhcp_unservable(state, INTENT) is False


def test_in_pool_subnet_inside():
    """An address inside the pool subnet returns True."""
    # GIVEN an address inside the pool subnet
    # THEN it is in the subnet
    assert in_pool_subnet("192.168.1.5", DHCP_POOL) is True


def test_in_pool_subnet_outside():
    """An address outside the pool subnet returns False."""
    # GIVEN an address outside the pool subnet
    # THEN it is not in the subnet
    assert in_pool_subnet("10.0.0.1", DHCP_POOL) is False


def test_in_pool_subnet_malformed_returns_false():
    """Malformed pool values return False (safety net)."""
    # GIVEN a malformed pool
    bad_pool = DhcpPool(start="bad", end="bad", router="bad", netmask="bad")
    # THEN no address is in it
    assert in_pool_subnet("192.168.1.5", bad_pool) is False


def test_dhcp_unservable_reason_names_the_pool():
    """The reason message names the pool range and netmask."""
    # GIVEN a machine with no address in the pool subnet
    state = converged(machine_ipv4_addresses=frozenset[str]({"10.0.0.1"}))
    # WHEN the reason is computed
    reason = dhcp_unservable_reason(state, DHCP_INTENT)
    # THEN it names the pool and points at snap-constraints
    assert "192.168.1.10-192.168.1.50/255.255.255.0" in reason
    assert "snap-constraints" in reason


def test_dhcp_unservable_reason_names_unreadable_addresses():
    """The reason names the failed read, not a missing match."""
    # GIVEN a machine whose addresses could not be read
    state = converged(machine_ipv4_addresses=None)
    # WHEN the reason is computed
    reason = dhcp_unservable_reason(state, DHCP_INTENT)
    # THEN it names the failed read
    assert "could not be read" in reason


def test_dhcp_does_not_appear_in_bootstrap():
    """DHCP not in bootstrap: deferred to first converge."""
    # GIVEN a machine with no snap and DHCP enabled in intent
    outcomes = compute(SnapAbsent(), DHCP_INTENT)
    # THEN no bootstrap step touches a DHCP key
    dhcp_keys = {"dhcp.start", "dhcp.end", "dhcp.router", "dhcp.netmask", "dhcp.active"}
    for o in outcomes:
        if isinstance(o, SetFtlConfig):
            for k, _ in o.config:
                assert k not in dhcp_keys


# -- dhcp_port_blocked (pure). -----------------------------------------


def test_dhcp_port_blocked_when_enabled_and_port_taken():
    """Enabled + not active + 67 taken → True."""
    # GIVEN DHCP enabled in intent, not yet active, and 67/udp held
    state = converged(dhcp_active=False, port67_free=False)
    # THEN the gate fires
    assert dhcp_port_blocked(state, DHCP_INTENT) is True


def test_dhcp_port_blocked_false_when_already_active():
    """Active + FTL up + 67 taken → False (FTL holds the port)."""
    # GIVEN DHCP already active with FTL up and 67/udp held
    state = converged(dhcp_active=True, ftl_active=True, port67_free=False)
    # THEN the gate does not fire — the listener is the daemon's own
    assert dhcp_port_blocked(state, DHCP_INTENT) is False


def test_dhcp_port_blocked_fires_when_active_but_ftl_down():
    """Active + FTL down + 67 taken → True (someone else holds it).

    The exemption keys on the daemon owning the listener, not on the
    config key: after a reboot where another service won 67,
    ``dhcp.active`` is still true but FTL is down, so the port holder
    is not FTL and the gate must fire with the port remedy.
    """
    # GIVEN DHCP configured active but FTL down, with 67/udp held
    state = converged(dhcp_active=True, ftl_active=False, port67_free=False)
    # THEN the gate fires — the config key alone is not evidence FTL
    # bound the port
    assert dhcp_port_blocked(state, DHCP_INTENT) is True


def test_dhcp_port_blocked_false_when_port_free():
    """Enabled + not active + 67 free → False."""
    # GIVEN DHCP enabled in intent, not yet active, and 67/udp free
    state = converged(dhcp_active=False, port67_free=True)
    # THEN the gate does not fire
    assert dhcp_port_blocked(state, DHCP_INTENT) is False


def test_dhcp_port_blocked_fires_when_ftl_up_but_port_free():
    """Active + FTL up + 67 free → True (FTL never bound the port).

    The inverse of the exemption: FTL being up is not evidence it
    serves DHCP — the port must be taken for the daemon to hold it.
    """
    # GIVEN DHCP active with FTL up but 67/udp free
    state = converged(dhcp_active=True, ftl_active=True, port67_free=True)
    # THEN the gate fires — FTL is not demonstrably serving
    assert dhcp_port_blocked(state, DHCP_INTENT) is True


def test_dhcp_port_blocked_false_when_ftl_down_and_port_free():
    """Active + FTL down + 67 free → False (no port conflict)."""
    # GIVEN DHCP active but FTL down with 67/udp free
    state = converged(dhcp_active=True, ftl_active=False, port67_free=True)
    # THEN the gate does not fire — StartFtl owns that state
    assert dhcp_port_blocked(state, DHCP_INTENT) is False


def test_dhcp_port_blocked_fires_when_ftl_down_and_not_active_and_port_taken():
    """Not active + FTL down + 67 taken → True (no daemon to hold it).

    The first-enable case with the daemon down: FTL cannot be the
    port holder, so enabling would crash-loop it, and the gate must
    fire with the port remedy rather than let StartFtl push first.
    """
    # GIVEN DHCP enabled in intent, not yet active, FTL down, 67 held
    state = converged(dhcp_active=False, ftl_active=False, port67_free=False)
    # THEN the gate fires — nothing demonstrably serves DHCP
    assert dhcp_port_blocked(state, DHCP_INTENT) is True


def test_dhcp_port_blocked_false_when_ftl_down_and_not_active_and_port_free():
    """Not active + FTL down + 67 free → False (StartFtl owns it)."""
    # GIVEN DHCP enabled in intent, not yet active, FTL down, 67 free
    state = converged(dhcp_active=False, ftl_active=False, port67_free=True)
    # THEN the gate does not fire — the daemon is down, not conflicted
    assert dhcp_port_blocked(state, DHCP_INTENT) is False


def test_dhcp_port_blocked_false_when_disabled():
    """Disabled DHCP → False regardless of the port."""
    # GIVEN DHCP disabled with 67/udp held
    state = converged(dhcp_active=False, port67_free=False)
    # THEN the gate does not fire
    assert dhcp_port_blocked(state, INTENT) is False


def test_dhcp_port_blocked_skips_dhcp_steps():
    """Enabled + port taken → no DHCP steps even though pool drifted."""
    # GIVEN DHCP enabled in intent, not yet active, and 67/udp held
    state = converged(
        dhcp_active=False,
        dhcp_pool=None,
        port67_free=False,
        machine_ipv4_addresses=frozenset[str]({"192.168.1.5"}),
    )

    # WHEN the plan is computed with DHCP enabled
    outcomes = compute(state, DHCP_INTENT)

    # THEN no DHCP step is emitted — enabling would crash-loop FTL
    dhcp_keys = {"dhcp.start", "dhcp.end", "dhcp.router", "dhcp.netmask", "dhcp.active"}
    for o in outcomes:
        if isinstance(o, SetFtlConfig):
            for k, _ in o.config:
                assert k not in dhcp_keys


def test_dhcp_port_blocked_reason_names_the_remedy():
    """The conflict reason names the port and the working remedy."""
    # GIVEN DHCP enabled in intent with 67/udp held by another service
    state = converged(dhcp_active=False, port67_free=False)
    # WHEN the reason is computed
    reason = dhcp_port_blocked_reason(state)
    # THEN it names the port and the remedy that works with FTL down
    assert "67/udp" in reason
    assert "stop the other service" in reason
    assert "dhcp-enabled=false" not in reason


def test_dhcp_port_blocked_reason_names_the_unbound_daemon():
    """The not-serving reason points at FTL's own logs."""
    # GIVEN FTL up with DHCP active but 67/udp free
    state = converged(dhcp_active=True, ftl_active=True, port67_free=True)
    # WHEN the reason is computed
    reason = dhcp_port_blocked_reason(state)
    # THEN it names the unbound daemon, not a port conflict
    assert "not holding 67/udp" in reason
    assert "snap logs pihole" in reason
