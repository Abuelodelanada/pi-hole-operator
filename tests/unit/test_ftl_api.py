"""Tests for the FTL API client.

The theme of this file is non-negotiable 6: **an exit code is never
evidence.** `FtlApi` is constructed directly, with no snap involved,
because it owns the session budget, the settle window and the retry
semantics that most need a regression test. See ADR-0009 section 4.
"""

import http.client
import pathlib
import urllib.error
import urllib.request

import pytest

import ftl_api
from pihole_state import (
    AdminPasswordState,
    ApiFacts,
    PasswordAccepted,
    PasswordRejected,
    PasswordUnset,
    PasswordUnverified,
)
from tests.unit.conftest import (
    AUTH_OK,
    BLOCKING_OK,
    CLI_PW,
    LOGOUT_OK,
    NEVER,
    NEW_HASH,
    OLD_HASH,
    PASSWORD,
    SID,
    ApiOutcome,
    FakeClock,
    FakeResponse,
    HttpReply,
    api,
    http_error,
    write_cli_pw,
    write_pihole_toml,
)


@pytest.fixture
def ftl(snap_data: pathlib.Path, clock: FakeClock) -> ftl_api.FtlApi:
    """The API client wired to fakes, with nothing that waits."""
    return ftl_api.FtlApi(snap_data=snap_data, sleep=clock.sleep, monotonic=clock.monotonic)


# -- Reading an HTTP status. ------------------------------------------
#
# These need no collaborator at all: the classification is a pure
# function of a status code, which is exactly why it is one.


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (PasswordRejected(), True),
        (PasswordAccepted(), False),
        (PasswordUnset(), False),
        (PasswordUnverified(), False),
    ],
)
def test_only_a_rejection_is_worth_waiting_out(state: AdminPasswordState, expected: bool):
    # GIVEN one of the four things the oracle can conclude
    # WHEN the settle window asks whether the answer could still change
    # THEN only a refusal can: FTL validates against a hash it has not
    # reloaded for about a second after a write (snap-constraints
    # 7.2.5), while a 429 is capacity and an unreachable API is
    # silence — neither improves for being asked again, so neither may
    # spend the window
    assert ftl_api.is_transient(state) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, PasswordAccepted()),
        (401, PasswordRejected()),
        (429, PasswordUnverified()),
        (500, PasswordUnverified()),
        (503, PasswordUnverified()),
    ],
)
def test_only_401_means_the_password_is_wrong(status: int, expected: object):
    # GIVEN one of the statuses `POST /api/auth` can answer with
    # WHEN it is read
    # THEN only 401 accuses the credential. `PasswordRejected` drives a
    # BlockedStatus telling the operator to rotate, so a 429 landing
    # there is a false security alarm.
    assert ftl_api.classify_auth_status(status) == expected


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"blocking": "enabled"}, ftl_api.BlockingAnswered()),
        (200, {"error": "still starting"}, ftl_api.BlockingSilent()),
        (401, {}, ftl_api.SessionRefused()),
        (429, {}, ftl_api.BlockingSilent()),
        (500, {}, ftl_api.BlockingSilent()),
    ],
)
def test_readiness_is_only_a_200_that_carries_a_blocking_state(
    status: int,
    payload: dict[str, object],
    expected: object,
):
    # GIVEN one of the answers `GET /api/dns/blocking` can give
    # WHEN it is read
    # THEN a 200 alone is not readiness, and a 401 is a statement about
    # the session rather than about the daemon
    assert ftl_api.classify_blocking(status, payload) == expected


# -- Readiness. -------------------------------------------------------


def test_readiness_is_false_when_nothing_is_listening(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where the webserver never started, which is what a
    # stock install looks like
    write_cli_pw(snap_data, CLI_PW)
    api(monkeypatch, {})

    # WHEN readiness is checked
    # THEN it is false rather than an exception: not being ready yet is
    # a normal state, not a failure
    assert ftl.ready() is False


def test_readiness_is_false_when_the_answer_is_not_a_blocking_state(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that answers 200 with something else entirely
    write_cli_pw(snap_data, CLI_PW)
    api(
        monkeypatch,
        {**AUTH_OK, **LOGOUT_OK, "GET dns/blocking": FakeResponse(200, {"error": "nope"})},
    )

    # WHEN readiness is checked
    # THEN a 200 is not taken as evidence on its own
    assert ftl.ready() is False


def test_the_cli_password_is_re_read_on_every_call(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole that has rotated its CLI password, as it does on
    # every single FTL restart
    write_cli_pw(snap_data, "before-the-restart")
    fake = api(monkeypatch, {**AUTH_OK, **BLOCKING_OK, **LOGOUT_OK})
    ftl.ready()
    write_cli_pw(snap_data, "after-the-restart")

    # WHEN the API is used again
    ftl.ready()

    # THEN the second call used the new value: a cached cli_pw is wrong
    # the moment the daemon bounces
    offered = [request.body for request in fake.requests if request.route == "POST auth"]
    assert offered == [{"password": "before-the-restart"}, {"password": "after-the-restart"}]


def test_the_api_is_asked_unauthenticated_when_there_is_no_cli_password(
    ftl: ftl_api.FtlApi,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a machine where cli_pw does not exist yet
    fake = api(monkeypatch, {**BLOCKING_OK})

    # WHEN readiness is checked
    assert ftl.ready() is True

    # THEN no session was invented, and none was presented
    assert [request.route for request in fake.requests] == ["GET dns/blocking"]
    assert fake.requests[0].sid is None


def test_readiness_spends_no_request_when_there_is_no_session(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole with no session slots left
    write_cli_pw(snap_data, CLI_PW)
    fake = api(
        monkeypatch,
        {"POST auth": http_error(429, {"error": {"key": "api_seats_exceeded"}}), **BLOCKING_OK},
    )

    # WHEN readiness is checked
    assert ftl.ready() is False

    # THEN it stopped at the refusal. Without a slot the next request
    # can only produce another 429, and readiness is unproven either
    # way — so it reports unproven rather than spending the request.
    assert [request.route for request in fake.requests] == ["POST auth"]


def test_a_refused_cli_password_does_not_stop_the_readiness_check(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a cli_pw the API rejects, which happens for one request
    # after every FTL restart if anything cached it
    write_cli_pw(snap_data, CLI_PW)
    api(
        monkeypatch,
        {"POST auth": http_error(401, {"error": {"key": "unauthorized"}}), **BLOCKING_OK},
    )

    # WHEN readiness is checked
    # THEN the answer still comes from the endpoint that matters
    assert ftl.ready() is True


def test_a_session_that_cannot_be_deleted_is_not_fatal(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that answers but refuses the logout
    write_cli_pw(snap_data, CLI_PW)
    api(monkeypatch, {**AUTH_OK, **BLOCKING_OK})

    # WHEN readiness is checked
    # THEN failing to give a session back does not make a ready Pi-hole
    # look unready
    assert ftl.ready() is True


def test_a_reply_that_is_not_json_is_not_mistaken_for_a_state(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an endpoint answering 200 with an HTML error page
    write_cli_pw(snap_data, CLI_PW)
    api(
        monkeypatch,
        {**AUTH_OK, **LOGOUT_OK, "GET dns/blocking": _RawResponse()},
    )

    # WHEN readiness is checked
    # THEN the unparseable body is not read as a blocking state
    assert ftl.ready() is False


@pytest.mark.parametrize(
    "fault",
    [
        http.client.BadStatusLine("<html>500 Internal Server Error</html>"),
        http.client.IncompleteRead(b'{"bloc'),
        http.client.RemoteDisconnected("Remote end closed connection"),
    ],
    ids=["BadStatusLine", "IncompleteRead", "RemoteDisconnected"],
)
def test_a_webserver_answering_garbage_is_unreachable_not_a_crash(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: Exception,
):
    """The one exception in `FtlApi` that could reach the caller.

    ``http.client.HTTPException`` is **not** an ``OSError``, and
    FTL's webserver answers a malformed reply rather than nothing
    when it is unhappy. Uncaught, it escapes a *fact read* — which
    `collect_unit_status` performs on every hook — and puts the unit
    in error state, where ``--force`` is needed to remove it and the
    host never gets its resolver back.
    """
    # GIVEN a webserver that answers something the HTTP client cannot
    # even parse as a response, on a Pi-hole that does have a password
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    write_cli_pw(snap_data, CLI_PW)
    api(monkeypatch, {"POST auth": fault, "GET dns/blocking": fault})

    # WHEN the facts are read
    # THEN it reads as "not answering yet", which is what it is, and the
    # password is unverified rather than rejected
    assert ftl.ready() is False
    assert ftl.password_state(PASSWORD) == PasswordUnverified()


# RemoteDisconnected inherits from both `HTTPException` and
# `ConnectionResetError`, so it would have been caught either way. It is
# in the list above to document that, not because it is the risk.


# -- Password oracle. -------------------------------------------------


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (FakeResponse(200, {"session": {"sid": SID}}), PasswordAccepted()),
        (http_error(401, {"error": {"key": "unauthorized"}}), PasswordRejected()),
        (http_error(429, {"error": {"key": "api_seats_exceeded"}}), PasswordUnverified()),
        (http_error(500, {"error": {"key": "internal_error"}}), PasswordUnverified()),
        (urllib.error.URLError("Connection refused"), PasswordUnverified()),
    ],
    ids=["200", "401", "429", "500", "unreachable"],
)
def test_the_password_oracle_reads_the_api_answer(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    route: ApiOutcome,
    expected: object,
):
    # GIVEN a Pi-hole with a password set, answering in one of the ways
    # it can
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    api(monkeypatch, {"POST auth": route, **LOGOUT_OK})

    # WHEN the password the charm holds is checked
    # THEN only 401 is read as a rejection: an unreachable API and an
    # exhausted session pool are both "not verified", which is a
    # different state and a different status
    assert ftl.password_state(PASSWORD) == expected


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (None, "the file does not exist at all"),
        ("[webserver.api\npwhash = ", "the file exists but is not valid TOML"),
        ('webserver = "not-a-table"\n', "a key on the path is not a table"),
    ],
    ids=["missing", "unparseable", "wrong-shape"],
)
def test_an_unreadable_pihole_toml_reads_as_no_password_set(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
    why: str,
):
    # GIVEN a `pihole.toml` the charm cannot get a `pwhash` out of,
    # for each of the three reasons that can happen
    if raw is not None:
        write_pihole_toml(snap_data, raw=raw)
    called = api(monkeypatch, {"POST auth": FakeResponse(200, {"session": {"sid": SID}})})

    # WHEN the password is classified
    state = ftl.password_state(PASSWORD)

    # THEN it is `PasswordUnset`, not `PasswordUnverified`: an
    # unreadable hash is treated the same as an empty one, because
    # both mean the charm cannot prove a password is in force
    assert state == PasswordUnset(), why

    # AND the API is never consulted, because while `pwhash` is empty
    # FTL accepts *any* password and would answer 200 for a credential
    # nobody set
    assert called.requests == []


def test_the_timeout_error_says_how_long_it_waited():
    # GIVEN a timeout that ran out
    err = ftl_api.ApiTimeoutError(timeout=120.0)

    # WHEN it is rendered for an operator
    # THEN the message names the wait, because "the API never answered"
    # without a duration does not tell anyone whether to wait longer
    assert str(err) == "the API never answered within 120s"


def test_a_session_limit_is_not_a_wrong_password(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A 429 must never be read as a credential failure.

    Verified on a live unit: ``webserver.api.max_sessions`` is 16
    and ``POST /api/auth`` answers 429 once they are gone. Reading
    that as a rejection made ``rotate-admin-password`` fail on a
    rotation that had demonstrably succeeded, and told the operator
    to fix a security problem that did not exist.
    """
    # GIVEN a Pi-hole with a password set, whose session pool is full
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    api(
        monkeypatch,
        {"POST auth": http_error(429, {"error": {"key": "api_seats_exceeded"}}), **LOGOUT_OK},
    )

    # WHEN the password the charm holds is checked
    state = ftl.password_state(PASSWORD)

    # THEN the charm says it could not tell, rather than accusing the
    # operator of holding a password Pi-hole rejects
    assert state == PasswordUnverified()
    assert state != PasswordRejected()


def test_a_password_refused_only_while_ftl_reloads_is_accepted(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    """The regression test for the settle window.

    Measured on a live unit (snap-constraints 7.2.5): ``pihole
    setpassword`` exits 0 and writes ``pihole.toml`` synchronously,
    but FTL keeps validating against the *old* hash for about a
    second -- ``401`` at +0.91s, +1.20s and +1.49s, then ``200`` at
    +1.78s. Consulting the oracle once and believing the first
    refusal made ``rotate-admin-password`` fail with *"the new
    password was written but not confirmed"* on a rotation that had
    demonstrably succeeded: the secret, ``pwhash`` and ``/api/auth``
    were all consistent immediately afterwards.
    """
    # GIVEN an FTL that refuses twice before it reloads its hash
    write_pihole_toml(snap_data, pwhash=NEW_HASH)
    fake = SettlingAuth(refusals=2)
    monkeypatch.setattr(ftl_api.urllib.request, "urlopen", fake)

    # WHEN the freshly applied password is confirmed
    state = ftl.password_state(PASSWORD)

    # THEN the answer is the one FTL settles on, not the one it gave
    # while it was still catching up
    assert state == PasswordAccepted()

    # AND it cost exactly one attempt per answer, spaced by the settle
    # interval — asserted on recorded calls, never on elapsed time
    assert fake.attempts == 3
    assert clock.sleeps == [ftl_api.PASSWORD_SETTLE_INTERVAL] * 2


def test_a_password_refused_for_the_whole_window_is_still_rejected(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    # GIVEN an FTL that refuses the password however long it is given,
    # which is what a genuinely wrong credential looks like
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    fake = SettlingAuth(refusals=NEVER)
    monkeypatch.setattr(ftl_api.urllib.request, "urlopen", fake)

    # WHEN the password is confirmed
    state = ftl.password_state(PASSWORD)

    # THEN patience does not become permission: the window must not
    # mask a password Pi-hole really does reject
    assert state == PasswordRejected()

    # AND the window is bounded, and every wait inside it was one
    # interval, so the loop cannot spin or run on
    assert clock.sleeps == [ftl_api.PASSWORD_SETTLE_INTERVAL] * len(clock.sleeps)
    assert (
        ftl_api.PASSWORD_SETTLE_WINDOW
        <= clock.now
        < (ftl_api.PASSWORD_SETTLE_WINDOW + ftl_api.PASSWORD_SETTLE_INTERVAL)
    )
    assert fake.attempts == len(clock.sleeps) + 1


def test_an_accepted_password_costs_one_request_and_no_waiting(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    # GIVEN an FTL that accepts the password straight away, which is
    # every reconcile on a converged machine
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    fake = api(monkeypatch, {**AUTH_OK, **LOGOUT_OK})

    # WHEN the password is confirmed
    state = ftl.password_state(PASSWORD)

    # THEN the healthy path pays nothing for the window: one question,
    # one answer, and the session handed straight back
    assert state == PasswordAccepted()
    assert [request.route for request in fake.requests] == ["POST auth", "DELETE auth"]
    assert clock.sleeps == []


def test_a_session_limit_does_not_burn_the_settle_window(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    # GIVEN a Pi-hole whose session pool is full
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    fake = api(
        monkeypatch,
        {"POST auth": http_error(429, {"error": {"key": "api_seats_exceeded"}}), **LOGOUT_OK},
    )

    # WHEN the password is confirmed
    state = ftl.password_state(PASSWORD)

    # THEN it is unverified immediately. Waiting could not turn a
    # capacity answer into a credential answer, and eleven more
    # attempts against an exhausted pool is the load that exhausted it.
    assert state == PasswordUnverified()
    assert [request.route for request in fake.requests] == ["POST auth"]
    assert clock.sleeps == []


def test_an_unreachable_api_does_not_burn_the_settle_window(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    # GIVEN a daemon that is not answering at all, which is the normal
    # state between `setpassword` and the first successful start
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    api(monkeypatch, {})

    # WHEN the password is confirmed
    # THEN silence is not a refusal, so there is nothing to wait out
    assert ftl.password_state(PASSWORD) == PasswordUnverified()
    assert clock.sleeps == []


# -- Both facts. ------------------------------------------------------


def test_the_steady_state_check_also_waits_out_a_reloading_ftl(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    """`fetch` settles too, deliberately.

    A hook that applies a password and then reports status reads the
    oracle again within the same second, so a transient 401 here
    would flap a ``BlockedStatus`` telling the operator that Pi-hole
    rejects the charm's password -- and ADR-0005 section 2.8 is that
    one spurious ``Blocked`` masks every other status the handler
    adds.
    """
    # GIVEN an FTL that has just reloaded its hash, and a serving
    # readiness endpoint
    write_pihole_toml(snap_data, pwhash=NEW_HASH)
    write_cli_pw(snap_data, CLI_PW)
    fake = SettlingAuth(refusals=1)
    monkeypatch.setattr(ftl_api.urllib.request, "urlopen", fake)

    # WHEN both API facts are read
    facts = ftl.facts(PASSWORD)

    # THEN the status the operator sees is the settled one, and the
    # session the oracle finally got still answers readiness — the
    # window costs an authentication, not the shared session
    assert facts == ApiFacts(admin_password=PasswordAccepted(), api_ready=True)
    assert fake.attempts == 2
    assert clock.sleeps == [ftl_api.PASSWORD_SETTLE_INTERVAL]
    assert fake.routes.count("GET dns/blocking") == 1


def test_a_correct_password_on_a_daemon_that_is_not_serving_yet(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole that accepts the charm's password while its
    # readiness endpoint still answers 200 with something else — the
    # normal state between `setpassword` and serving
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    write_cli_pw(snap_data, CLI_PW)
    fake = api(
        monkeypatch,
        {**AUTH_OK, **LOGOUT_OK, "GET dns/blocking": FakeResponse(200, {"error": "starting"})},
    )

    # WHEN both API facts are read
    facts = ftl.facts(PASSWORD)

    # THEN the two facts are independent: a right password does not
    # make a daemon ready, and one session established both
    assert facts == ApiFacts(admin_password=PasswordAccepted(), api_ready=False)
    assert [request.route for request in fake.requests].count("POST auth") == 1


def test_an_exhausted_pool_is_neither_a_rejection_nor_a_readiness_claim(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole that has no session slots left for anyone
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    write_cli_pw(snap_data, CLI_PW)
    api(
        monkeypatch,
        {"POST auth": http_error(429, {"error": {"key": "api_seats_exceeded"}}), **BLOCKING_OK},
    )

    # WHEN both API facts are read
    facts = ftl.facts(PASSWORD)

    # THEN neither fact is invented: the password is unverified rather
    # than rejected, and readiness is unproven rather than assumed
    assert facts == ApiFacts(admin_password=PasswordUnverified(), api_ready=False)


def test_a_rejected_password_still_gets_a_readiness_answer(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a Pi-hole that refuses the password the charm holds
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    write_cli_pw(snap_data, CLI_PW)
    fake = api(
        monkeypatch,
        {
            "POST auth": http_error(401, {"error": {"key": "unauthorized"}}),
            **BLOCKING_OK,
            **LOGOUT_OK,
        },
    )

    # WHEN both API facts are read
    facts = ftl.facts(PASSWORD)

    # THEN the oracle's verdict stands once the settle window is spent,
    # and readiness falls back to a session of its own — there was none
    # to share
    assert facts == ApiFacts(admin_password=PasswordRejected(), api_ready=True)
    offered = [request.body for request in fake.requests if request.route == "POST auth"]
    assert offered[:-1] == [{"password": PASSWORD}] * (len(offered) - 1)
    assert offered[-1] == {"password": CLI_PW}


def test_a_shared_session_the_readiness_endpoint_refuses_is_not_a_verdict(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Sharing a session must not be able to invent a fault.

    Reusing the oracle's session for readiness rests on FTL treating
    an admin session the way the admin UI's own session is treated,
    which is what the UI does but is **not verified here**. If it
    ever refuses one, readiness has to fall back rather than report a
    serving daemon as silent -- that would be the same class of false
    alarm this whole change removes.
    """
    # GIVEN a Pi-hole that issues the oracle a session the readiness
    # endpoint will not accept, and accepts the next one
    write_pihole_toml(snap_data, pwhash=OLD_HASH)
    write_cli_pw(snap_data, CLI_PW)
    fake = RestartingApi()
    monkeypatch.setattr(ftl_api.urllib.request, "urlopen", fake)

    # WHEN both API facts are read
    facts = ftl.facts(PASSWORD)

    # THEN the refusal costs one more session rather than a wrong answer
    assert facts == ApiFacts(admin_password=PasswordAccepted(), api_ready=True)
    assert fake.auths == 2
    assert fake.routes.count("GET dns/blocking") == 2
    assert fake.routes.count("DELETE auth") == 2


# -- Awaiting the API. ------------------------------------------------


def test_awaiting_the_api_polls_until_it_answers(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    # GIVEN an API that is not up on the first look. The daemon reports
    # active long before it serves, so a single check would be a race.
    write_cli_pw(snap_data, CLI_PW)
    fake = api(monkeypatch, {})

    def answer_after_the_first_look(*_: object, **__: object) -> HttpReply:
        fake.routes = {**AUTH_OK, **BLOCKING_OK, **LOGOUT_OK}
        monkeypatch.setattr(ftl_api.urllib.request, "urlopen", fake)
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(ftl_api.urllib.request, "urlopen", answer_after_the_first_look)

    # WHEN the gate is waited on with room for a second attempt
    ftl.await_ready(timeout=30.0)

    # THEN it waited between attempts rather than spinning
    assert clock.sleeps == [ftl_api.API_POLL_INTERVAL]


def test_awaiting_the_api_authenticates_once_across_every_poll(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    """One session for the whole wait, not one per poll.

    FTL allows 16 concurrent API sessions and answers 429 for the
    seventeenth. ``DELETE /api/auth`` is best effort and does not
    free slots fast enough to keep up with a 3-second poll, so a
    loop that authenticated per attempt drained the pool inside a
    single hook -- and the password oracle that runs afterwards then
    saw a 429.
    """
    # GIVEN an API that authenticates immediately but does not serve
    # the readiness endpoint for another two polls
    write_cli_pw(snap_data, CLI_PW)
    blocking = SlowBlocking(silent_looks=2)
    fake = api(monkeypatch, {**AUTH_OK, **LOGOUT_OK, "GET dns/blocking": blocking})

    # WHEN the gate is waited on
    ftl.await_ready(timeout=30.0)

    # THEN readiness was polled three times on **one** session, which
    # was opened once and given back once
    routes = [request.route for request in fake.requests]
    assert routes.count("GET dns/blocking") == 3
    assert routes.count("POST auth") == 1
    assert routes.count("DELETE auth") == 1
    assert clock.sleeps == [ftl_api.API_POLL_INTERVAL, ftl_api.API_POLL_INTERVAL]

    # AND every poll presented the same session
    offered = {request.sid for request in fake.requests if request.route == "GET dns/blocking"}
    assert offered == {SID}


def test_awaiting_the_api_gives_its_session_back_when_it_gives_up(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN an API that authenticates but never serves
    write_cli_pw(snap_data, CLI_PW)
    fake = api(
        monkeypatch,
        {**AUTH_OK, **LOGOUT_OK, "GET dns/blocking": SlowBlocking(silent_looks=99)},
    )

    # WHEN the gate is waited on and runs out of time
    with pytest.raises(ftl_api.ApiTimeoutError):
        ftl.await_ready(timeout=0.0)

    # THEN the one session it held was released rather than leaked: a
    # slot abandoned here is a slot the next hook does not have
    routes = [request.route for request in fake.requests]
    assert routes.count("POST auth") == 1
    assert routes.count("DELETE auth") == 1


def test_awaiting_the_api_re_authenticates_when_its_session_is_refused(
    ftl: ftl_api.FtlApi,
    snap_data: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
):
    # GIVEN an FTL that forgets its sessions once, as a restart does:
    # `cli_pw` is regenerated every time the daemon bounces, so a
    # session opened before one stops being accepted after it
    write_cli_pw(snap_data, CLI_PW)
    fake = RestartingApi()
    monkeypatch.setattr(ftl_api.urllib.request, "urlopen", fake)

    # WHEN the gate is waited on
    ftl.await_ready(timeout=30.0)

    # THEN reusing a session is not the same as trusting it forever: a
    # 401 costs exactly one more authentication, and the stale session
    # is handed back rather than held
    assert fake.auths == 2
    assert fake.routes.count("DELETE auth") == 2
    assert clock.sleeps == [ftl_api.API_POLL_INTERVAL]


# -- Helper classes for the API tests. ---------------------------------
#
# These live here rather than in `conftest.py` because `test_ftl_api.py`
# is the only file that needs them — the delegation tests in
# `test_pihole.py` use the session-level helpers from conftest instead.


class _RawResponse:
    """A response whose body is not JSON at all."""

    status = 200

    def __enter__(self) -> _RawResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        """Return an HTML error page, as a broken webserver would."""
        return b"<html><body>Internal Server Error</body></html>"


class SlowBlocking:
    """A readiness endpoint that only answers after so many looks.

    FTL reports its daemon active long before it serves, and answers
    200 with a body that is not a blocking state in the meantime.
    """

    status = 200

    def __init__(self, silent_looks: int) -> None:
        self.silent_looks = silent_looks
        self.looks = 0

    def __enter__(self) -> SlowBlocking:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        """Answer with a blocking state once the looks have gone by."""
        self.looks += 1
        if self.looks <= self.silent_looks:
            return b'{"error": "still starting"}'
        return b'{"blocking": "enabled", "timer": null}'


class SettlingAuth:
    """A `urlopen` whose `/api/auth` refuses until FTL catches up.

    Verified on a live unit (snap-constraints 7.2.5): `pihole
    setpassword` exits 0 and `pihole.toml` holds the new `pwhash`
    about a second before FTL stops validating against the old one, so
    the API answers 401 two or three times and then 200. `refusals`
    is how many of those 401s to give before accepting.
    """

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.attempts = 0
        self.routes: list[str] = []

    def __call__(
        self,
        request: urllib.request.Request,
        timeout: float | None = None,
    ) -> HttpReply:
        """Answer one request, recording the route it asked for."""
        route = f"{request.method} {request.full_url.split('/api/', 1)[1]}"
        self.routes.append(route)
        if route == "POST auth":
            self.attempts += 1
            if self.attempts <= self.refusals:
                raise http_error(401, {"error": {"key": "unauthorized"}}).build()
            return FakeResponse(200, {"session": {"sid": SID}})
        if route == "DELETE auth":
            return FakeResponse(204, None)
        return FakeResponse(200, {"blocking": "enabled", "timer": None})


class RestartingApi:
    """A `urlopen` that forgets its first session, as a restart does.

    Issues a fresh sid on every authentication, and refuses the first
    one on the readiness endpoint — which is what a session opened
    before an FTL restart looks like afterwards, because `cli_pw` is
    regenerated every time the daemon bounces.
    """

    def __init__(self) -> None:
        self.auths = 0
        self.routes: list[str] = []

    def __call__(
        self,
        request: urllib.request.Request,
        timeout: float | None = None,
    ) -> HttpReply:
        """Answer one request, recording the route it asked for."""
        route = f"{request.method} {request.full_url.split('/api/', 1)[1]}"
        self.routes.append(route)
        if route == "POST auth":
            self.auths += 1
            return FakeResponse(200, {"session": {"sid": f"sid-{self.auths}"}})
        if route == "DELETE auth":
            return FakeResponse(204, None)
        if request.get_header("Sid") == "sid-1":
            raise http_error(401, {"error": {"key": "unauthorized"}}).build()
        return FakeResponse(200, {"blocking": "enabled", "timer": None})
