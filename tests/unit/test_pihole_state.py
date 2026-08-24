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
    WEBSERVER_PORT,
    AdminPasswordState,
    ApiFacts,
    AwaitApi,
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
    SetWebserverPort,
    SnapAbsent,
    SnapPresent,
    StartFtl,
    compute,
    fetch,
)

PASSWORD = "a-generated-password"
INTENT = PiholeIntent(admin_password=PASSWORD)


def converged(**overrides: object) -> SnapPresent:
    """A machine that already matches intent, with fields overridden."""
    state = SnapPresent(
        revision="1348",
        version="6.4.3",
        ftl_enabled=True,
        ftl_active=True,
        webserver_port=WEBSERVER_PORT,
        admin_password=PasswordAccepted(),
        api_ready=True,
        stub_listener_disabled=True,
    )
    return dataclasses.replace(state, **overrides)


@dataclasses.dataclass
class FactsStub:
    """A stand-in for `pihole.Pihole` that counts its reads.

    Not a mock: `fetch` is the boundary between the pure core and the
    machine, so something has to play the machine. It returns plain
    values and records nothing but call counts.
    """

    revision: str | None = "1348"
    version: str | None = "6.4.3"
    service: ServiceStatus = dataclasses.field(
        default_factory=lambda: ServiceStatus(enabled=True, active=True)
    )
    port: str | None = WEBSERVER_PORT
    password: AdminPasswordState = dataclasses.field(default_factory=PasswordAccepted)
    ready: bool = True
    stub_disabled: bool = True
    reads: list[str] = dataclasses.field(default_factory=list[str])
    passwords_offered: list[str] = dataclasses.field(default_factory=list[str])

    def installed_revision(self) -> str | None:
        """Report the installed revision."""
        self.reads.append("installed_revision")
        return self.revision

    def workload_version(self) -> str | None:
        """Report the Pi-hole version."""
        self.reads.append("workload_version")
        return self.version

    def ftl_status(self) -> ServiceStatus:
        """Report what snapd knows about the daemon."""
        self.reads.append("ftl_status")
        return self.service

    def webserver_port(self) -> str | None:
        """Report the port `pihole.toml` holds."""
        self.reads.append("webserver_port")
        return self.port

    def api_facts(self, password: str) -> ApiFacts:
        """Classify the offered password and probe readiness at once."""
        self.reads.append("api_facts")
        self.passwords_offered.append(password)
        return ApiFacts(admin_password=self.password, api_ready=self.ready)

    def stub_listener_disabled(self) -> bool:
        """Report whether port 53 has been freed."""
        self.reads.append("stub_listener_disabled")
        return self.stub_disabled


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
        ReleasePort53(),
        SetWebserverPort("80o,[::]:80o"),
        SetAdminPassword(PASSWORD),
        StartFtl(),
        AwaitApi(),
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
    assert kinds.index(SetWebserverPort) < kinds.index(StartFtl)

    # AND the admin password is applied before the daemon serves, so
    # there is no window in which the config API is open to the network
    assert kinds.index(SetAdminPassword) < kinds.index(StartFtl)

    # AND readiness is gated after the start, not before it
    assert kinds.index(StartFtl) < kinds.index(AwaitApi)


def test_converged_machine_yields_only_noop():
    # GIVEN a machine that already matches intent
    # WHEN the plan is computed
    outcomes = compute(converged(), INTENT)

    # THEN nothing happens: the literal "safe to run twice" proof
    assert outcomes == (Noop(),)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"stub_listener_disabled": False}, ReleasePort53()),
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


@pytest.mark.parametrize(
    "port",
    ["80o,443os,[::]:80o,[::]:443os", "", "80o"],
    ids=["stock", "unreadable", "hand-edited"],
)
def test_correcting_the_port_always_brings_its_own_readiness_gate(port: str):
    # GIVEN a machine that is converged apart from its webserver port,
    # and whose API is answering right now
    state = converged(webserver_port=port, api_ready=True)

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN the plan does not end by bouncing the daemon with nothing
    # waiting for it. The configure hook restarts FTL whenever a value
    # actually changes (snap-constraints sections 4 and 2.3), so
    # `api_ready` being true at fetch time says nothing about the state
    # this plan leaves behind — and the next status handler would report
    # a restarting daemon as a fault.
    assert outcomes == (SetWebserverPort(WEBSERVER_PORT), AwaitApi())


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
        stub_listener_disabled=False,
        webserver_port="",
        admin_password=PasswordUnset(),
        ftl_enabled=False,
        ftl_active=False,
        api_ready=False,
    )

    # WHEN the plan is computed
    outcomes = compute(state, INTENT)

    # THEN it is the install sequence without the install
    assert outcomes == (
        ReleasePort53(),
        SetWebserverPort(WEBSERVER_PORT),
        SetAdminPassword(PASSWORD),
        StartFtl(),
        AwaitApi(),
    )


def test_awaiting_the_api_carries_a_bounded_timeout():
    # GIVEN the default readiness gate
    # WHEN it is constructed without arguments
    # THEN it still cannot wait forever
    assert AwaitApi().timeout == API_READY_TIMEOUT
    assert AwaitApi().timeout > 0


def test_the_password_never_appears_in_a_repr():
    # GIVEN the two places the plaintext password is carried
    intent = PiholeIntent(admin_password="hunter2")
    outcome = SetAdminPassword("hunter2")

    # WHEN either is rendered, as logging an outcome does
    # THEN the password is not in the output
    assert "hunter2" not in repr(intent)
    assert "hunter2" not in repr(outcome)


def test_fetch_reports_an_uninstalled_machine_without_reading_further():
    # GIVEN a machine with no snap
    facts = FactsStub(revision=None)

    # WHEN the world is read
    state = fetch(facts, INTENT)

    # THEN the state is the absent case, and nothing else was probed:
    # there is no daemon to ask about
    assert state == SnapAbsent()
    assert facts.reads == ["installed_revision"]


def test_fetch_reads_every_fact_exactly_once():
    # GIVEN an installed machine
    facts = FactsStub()

    # WHEN the world is read
    state = fetch(facts, INTENT)

    # THEN the snapshot holds what the machine said
    assert state == converged()

    # AND each fact was read once: fetch is the single read path, and a
    # second read of the same fact would mean two sources of truth
    assert sorted(facts.reads) == [
        "api_facts",
        "ftl_status",
        "installed_revision",
        "stub_listener_disabled",
        "webserver_port",
        "workload_version",
    ]


def test_fetch_offers_the_intended_password_to_the_oracle():
    # GIVEN an installed machine
    facts = FactsStub()

    # WHEN the world is read
    fetch(facts, INTENT)

    # THEN the password checked is the one the charm intends to have
    # set, not one read back off the machine
    assert facts.passwords_offered == [PASSWORD]


def test_fetch_reads_both_api_facts_in_one_go():
    # GIVEN a machine whose API says the password is right and whose
    # readiness endpoint says it is not serving yet
    facts = FactsStub(password=PasswordAccepted(), ready=False)

    # WHEN the world is read
    state = fetch(facts, INTENT)

    # THEN both facts landed, from a single read. FTL allows 16
    # concurrent API sessions, so asking twice per fetch — twice per
    # hook — spends slots the readiness poll needs.
    assert state == converged(admin_password=PasswordAccepted(), api_ready=False)
    assert facts.reads.count("api_facts") == 1


def test_an_unreadable_webserver_port_is_not_mistaken_for_the_right_one():
    # GIVEN a machine whose pihole.toml cannot be read yet
    facts = FactsStub(port=None)

    # WHEN the world is read and the plan computed
    outcomes = compute(fetch(facts, INTENT), INTENT)

    # THEN the port is set rather than assumed correct, and the restart
    # that setting it causes is waited out
    assert outcomes == (SetWebserverPort(WEBSERVER_PORT), AwaitApi())
