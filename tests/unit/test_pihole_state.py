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
    AdminPasswordState,
    ApiFacts,
    AwaitApi,
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
    ServiceStatus,
    SetAdminPassword,
    SetFtlConfig,
    SetNtpServer,
    SnapAbsent,
    SnapPresent,
    StartFtl,
    compute,
    fetch,
    open_ports,
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
        ntp_server_active=False,
        upstream_dns=None,
        listening_mode=None,
        blocking_enabled=True,
        dnssec_enabled=False,
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
    ntp: bool | None = False
    upstreams: tuple[str, ...] | None = None
    mode: str | None = None
    blocking: bool | None = True
    dnssec: bool | None = False
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

    # AND the webserver port is corrected before the first start, or
    # the webserver never binds and there is no HTTP API to gate on

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
        "dnssec_enabled",
        "ftl_status",
        "installed_revision",
        "listening_mode",
        "ntp_server_active",
        "pinned_revision",
        "port53_released",
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
