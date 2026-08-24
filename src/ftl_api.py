"""Talk to FTL's HTTP API. Owns sessions, nothing else.

Never imports `ops` or `charmlibs` (rule 2); collaborators are
injected so a test double can reproduce the API's own behaviour.
An exit code is never evidence (rule 6): every session is verified
against the real API state. API sessions are scarce, capped at 16
by FTL — see snap-constraints section 7.2.4.

The path constants below duplicate `pihole.py`'s rather than
import them: `pihole.py` composes `FtlApi`, so the reverse import
would be a cycle. See ADR-0009 section 4.
"""

import http.client
import json
import logging
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never, cast, final

from pihole_state import (
    CLI_PW,
    PIHOLE_TOML,
    PWHASH_KEY,
    SNAP_DATA,
    AdminPasswordState,
    ApiFacts,
    PasswordAccepted,
    PasswordRejected,
    PasswordUnset,
    PasswordUnverified,
)

logger = logging.getLogger(__name__)

API_ORIGIN = "http://127.0.0.1"
"""The charm knows the port because the charm sets it."""

API_TIMEOUT = 10.0
API_POLL_INTERVAL = 3.0

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
"""The only status that means a credential is wrong."""

HTTP_TOO_MANY_REQUESTS = 429
"""FTL has no free API session slots.

Never a credential answer. See snap-constraints section 7.2.4.
"""

PASSWORD_SETTLE_WINDOW = 5.0
"""Seconds to keep asking `/api/auth` before believing a 401.

`setpassword` reports success before FTL reloads the hash it just
wrote, so an immediate 401 is not a verdict. See ADR-0007 section 4.3
and snap-constraints section 7.2.5.
"""

PASSWORD_SETTLE_INTERVAL = 0.5
"""Seconds between attempts inside the settle window.

A 401 issues no session, so up to eleven attempts here cannot exhaust
FTL's 16-session budget. See snap-constraints section 7.2.4.
"""


@final
@dataclass(frozen=True)
class ApiUnavailableError(Exception):
    """The FTL HTTP API could not be reached at all.

    Distinct from a 401, which is an *answer*. Not reaching the API is
    the normal state before the daemon has started.
    """

    reason: str

    def __str__(self) -> str:
        """Render the reason the API could not be reached."""
        return self.reason


@final
@dataclass(frozen=True)
class ApiTimeoutError(Exception):
    """The HTTP API never answered within the wait window.

    Raised by `await_ready` when its deadline passes. `Pihole`
    converts this to `PiholeError` because the remedy (where to look
    on the machine) lives with the snap, not the API. See ADR-0009
    section 4.
    """

    timeout: float

    def __str__(self) -> str:
        """Render the wait that ran out."""
        return f"the API never answered within {self.timeout:.0f}s"


@final
@dataclass(frozen=True)
class ApiSession:
    """A session to present on the requests that follow.

    `sid` is None when there is no credential to offer, or the one
    offered was refused; asking anyway still matters, since
    `/api/dns/blocking` needs no session while `pwhash` is empty.
    """

    sid: str | None = None


@final
@dataclass(frozen=True)
class NoSession:
    """No session was issued, and none may be assumed.

    Either the API is unreachable, or it answered 429 (no free
    session slots) — neither says anything about a credential. See
    snap-constraints section 7.2.4.
    """

    reason: str


type SessionOutcome = ApiSession | NoSession


@final
@dataclass(frozen=True)
class BlockingAnswered:
    """The readiness endpoint returned a blocking state."""


@final
@dataclass(frozen=True)
class BlockingSilent:
    """The readiness endpoint did not answer with a blocking state.

    Not being ready yet is the normal state before the daemon serves,
    so this is not a failure and carries no remedy.
    """


@final
@dataclass(frozen=True)
class SessionRefused:
    """The API answered 401: the session presented is not valid.

    `cli_pw` rotates on every FTL restart, so a session opened before
    one must reauthenticate rather than keep polling with it. See
    snap-constraints section 7.2.2.
    """


type BlockingProbe = BlockingAnswered | BlockingSilent | SessionRefused


def classify_auth_status(status: int) -> AdminPasswordState:
    """Read what `POST /api/auth` said about a password.

    Only 401 means the credential is wrong; 429 and anything else is
    "could not verify" rather than a verdict, to avoid a false
    `BlockedStatus`. See ADR-0007 section 4.3.
    """
    if status == HTTP_OK:
        return PasswordAccepted()
    if status == HTTP_UNAUTHORIZED:
        return PasswordRejected()
    return PasswordUnverified()


def is_transient(state: AdminPasswordState) -> bool:
    """Decide whether an answer could still change within the window.

    Only a rejection can improve; a 429, an unreachable API, and an
    empty `pwhash` are all settled already. Pure, so this is tested
    without a clock. See ADR-0007 section 4.3.
    """
    match state:
        case PasswordRejected():
            return True
        case PasswordAccepted() | PasswordUnset() | PasswordUnverified():
            return False
        case _ as unreachable:
            assert_never(unreachable)


def classify_blocking(status: int, payload: Mapping[str, object]) -> BlockingProbe:
    """Read what `GET /api/dns/blocking` said about readiness.

    A 200 alone is not evidence: FTL can answer 200 with a body that
    is not JSON or lacks `blocking`, so the state must be in the
    payload.
    """
    if status == HTTP_OK and "blocking" in payload:
        return BlockingAnswered()
    if status == HTTP_UNAUTHORIZED:
        return SessionRefused()
    return BlockingSilent()


@final
class FtlApi:
    """Talk to FTL's HTTP API. Owns sessions, nothing else."""

    def __init__(
        self,
        snap_data: Path = SNAP_DATA,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._snap_data = snap_data
        # Injected together: every bounded wait here is a deadline plus
        # a sleep, and a test that fakes one without the other measures
        # wall-clock time by accident.
        self._sleep = sleep
        self._monotonic = monotonic

    # -- Facts. -----------------------------------------------------

    def ready(self) -> bool:
        """Report whether `GET /api/dns/blocking` is answered.

        Only meaningful once `webserver.port` is corrected (ADR-0005
        section 2.6). Opens and closes its own session; `await_ready`
        and `facts` share one instead, since FTL only has 16.
        """
        match self._open_cli_session():
            case NoSession(reason=reason):
                logger.debug("The Pi-hole API is not answering yet: %s", reason)
                return False
            case ApiSession() as session:
                try:
                    probe = self._probe_blocking(session)
                finally:
                    self._logout(session.sid)
                return isinstance(probe, BlockingAnswered)
            case _ as unreachable:
                assert_never(unreachable)

    def password_state(self, password: str) -> AdminPasswordState:
        """Classify the admin password the charm holds.

        `pwhash` is read first because while it is empty FTL accepts
        *any* password, so the `/api/auth` oracle would answer 200 for
        a credential nobody set. A refusal then gets
        `PASSWORD_SETTLE_WINDOW` to change its mind, since FTL
        validates against the old hash for about a second after a
        write. See ADR-0007 section 4.3.
        """
        state, sid = self._classify_password_settled(password)
        self._logout(sid)
        return state

    def facts(self, password: str) -> ApiFacts:
        """Establish both API facts from a single session.

        The oracle and the readiness probe each need an authenticated
        request, and FTL has only 16 session slots — so the session
        the oracle opens answers `GET /api/dns/blocking` too.

        Where that session cannot serve, readiness falls back to a
        `cli_pw` session of its own, so readiness is unproven rather
        than false and a serving daemon is never reported silent on
        the strength of a shared session.

        The oracle settles here too, not only after a write: a hook
        that applies a password and then reports status reads this
        within the same second, and a 401 landing here would flap a
        `BlockedStatus` accusing the operator of a security problem.
        See ADR-0005 section 2.8.
        """
        state, sid = self._classify_password_settled(password)
        if sid is None:
            return ApiFacts(admin_password=state, api_ready=self.ready())
        try:
            probe = self._probe_blocking(ApiSession(sid=sid))
        finally:
            self._logout(sid)
        match probe:
            case BlockingAnswered():
                return ApiFacts(admin_password=state, api_ready=True)
            case BlockingSilent():
                return ApiFacts(admin_password=state, api_ready=False)
            case SessionRefused():
                logger.debug("The readiness endpoint refused the oracle's session.")
                return ApiFacts(admin_password=state, api_ready=self.ready())
            case _ as unreachable:
                assert_never(unreachable)

    def await_ready(self, timeout: float) -> None:
        """Block until the HTTP API answers, or raise `ApiTimeoutError`.

        Authenticates once and reuses the session across polls — a
        per-poll login exhausts FTL's session budget. See ADR-0007
        section 4.3.

        Raises:
            ApiTimeoutError: The API never answered.
        """
        deadline = self._monotonic() + timeout
        session: ApiSession | None = None
        try:
            while True:
                if session is None:
                    session = self._try_open_cli_session()
                if session is not None:
                    match self._probe_blocking(session):
                        case BlockingAnswered():
                            return
                        case SessionRefused():
                            # `cli_pw` rotated (FTL restarted); the
                            # next poll must re-authenticate.
                            self._logout(session.sid)
                            session = None
                        case BlockingSilent():
                            pass
                        case _ as unreachable:
                            assert_never(unreachable)
                if self._monotonic() >= deadline:
                    raise ApiTimeoutError(timeout=timeout)
                self._sleep(API_POLL_INTERVAL)
        finally:
            if session is not None:
                self._logout(session.sid)

    # -- Private. ---------------------------------------------------

    def _open_cli_session(self) -> SessionOutcome:
        """Authenticate as the CLI, re-reading `cli_pw` every time.

        `cli_pw` rotates on every FTL restart, so a cached value goes
        stale (snap-constraints section 7.2.2). A missing file still
        yields a session with no `sid`, valid while `pwhash` is empty.
        """
        password = self._read_cli_pw()
        if password is None:
            return ApiSession()
        try:
            status, sid = self._authenticate(password)
        except ApiUnavailableError as err:
            return NoSession(reason=str(err))
        if status == HTTP_TOO_MANY_REQUESTS:
            # A capacity answer. Asking the next endpoint without a slot
            # would only produce another 429, so do not spend a request
            # on it.
            return NoSession(reason="FTL has no free API session slots (HTTP 429)")
        if status != HTTP_OK:
            logger.debug("/api/auth refused the CLI password (HTTP %s).", status)
        return ApiSession(sid=sid)

    def _try_open_cli_session(self) -> ApiSession | None:
        """Open a CLI session, or None to leave the caller waiting."""
        match self._open_cli_session():
            case ApiSession() as session:
                return session
            case NoSession(reason=reason):
                logger.debug("The Pi-hole API is not answering yet: %s", reason)
                return None
            case _ as unreachable:
                assert_never(unreachable)

    def _classify_password(self, password: str) -> tuple[AdminPasswordState, str | None]:
        """Ask `/api/auth` about a password, keeping the session open.

        Returns the session too (None if none was issued), so callers
        such as `facts` can reuse it before logging out.
        """
        if not self._read_pwhash():
            return PasswordUnset(), None
        try:
            status, sid = self._authenticate(password)
        except ApiUnavailableError as err:
            logger.debug("Could not consult the /api/auth oracle: %s", err)
            return PasswordUnverified(), None
        if status != HTTP_OK:
            logger.debug("/api/auth answered HTTP %s for the charm's password.", status)
        return classify_auth_status(status), sid

    def _classify_password_settled(self, password: str) -> tuple[AdminPasswordState, str | None]:
        """Consult the oracle until it stops refusing, or time runs out.

        `pihole setpassword` reports success about a second before FTL
        validates against the hash it just wrote, so the first refusal
        after a write is the workload's old answer rather than its
        verdict. Source in snap-constraints section 7.2.5.

        A 200 on the first attempt costs one request, so the healthy
        path never waits; a 429, an unreachable API and an empty
        `pwhash` return immediately, because the window cannot improve
        them; and a 401 that lasts the whole window is still a
        rejection, so a wrong password is not hidden by patience.

        Returns the classification and the session the caller has to
        give back, exactly as `_classify_password` does.
        """
        deadline = self._monotonic() + PASSWORD_SETTLE_WINDOW
        while True:
            state, sid = self._classify_password(password)
            if not is_transient(state) or self._monotonic() >= deadline:
                return state, sid
            # A refusal issues no session, so this is normally a no-op
            # — but holding one across a sleep would spend a slot on
            # waiting, and there are 16 for the whole machine.
            self._logout(sid)
            logger.debug("The /api/auth oracle refused; giving FTL time to reload its hash.")
            self._sleep(PASSWORD_SETTLE_INTERVAL)

    def _probe_blocking(self, session: ApiSession) -> BlockingProbe:
        """Ask the readiness endpoint, presenting the session held."""
        try:
            status, payload = self._api_request("GET", "dns/blocking", sid=session.sid)
        except ApiUnavailableError as err:
            logger.debug("The Pi-hole API is not answering yet: %s", err)
            return BlockingSilent()
        return classify_blocking(status, payload)

    def _read_cli_pw(self) -> str | None:
        """Read `cli_pw` fresh. Never cache the result."""
        path = self._snap_data / CLI_PW
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as err:
            logger.debug("Could not read %s: %s", path, err)
            return None

    def _read_pwhash(self) -> str | None:
        """Read `pwhash` from pihole.toml, or None if absent."""
        path = self._snap_data / PIHOLE_TOML
        try:
            with path.open("rb") as handle:
                node: object = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as err:
            logger.debug("Could not read %s: %s", path, err)
            return None
        for segment in PWHASH_KEY.split("."):
            if not isinstance(node, dict):
                return None
            node = cast("Mapping[str, object]", node).get(segment)
        return node if isinstance(node, str) else None

    def _authenticate(self, password: str) -> tuple[int, str | None]:
        """Post a password to `/api/auth`, returning status and sid."""
        status, payload = self._api_request("POST", "auth", body={"password": password})
        session = payload.get("session")
        if not isinstance(session, dict):
            return status, None
        sid = cast("Mapping[str, object]", session).get("sid")
        return status, sid if isinstance(sid, str) else None

    def _logout(self, sid: str | None) -> None:
        """Drop a session. Best effort: sessions are finite."""
        if sid is None:
            return
        try:
            self._api_request("DELETE", "auth", sid=sid)
        except ApiUnavailableError as err:
            logger.debug("Could not delete the API session: %s", err)

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        sid: str | None = None,
    ) -> tuple[int, Mapping[str, object]]:
        """Make one request, returning the status and the payload.

        A 4xx is returned rather than raised — callers need to
        distinguish "401" from "nothing listening".

        Raises:
            ApiUnavailableError: The API could not be reached.
        """
        url = f"{API_ORIGIN}/api/{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if sid is not None:
            request.add_header("sid", sid)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, _decode(err.read())
        except (OSError, http.client.HTTPException) as err:
            # URLError (and hence every socket/DNS failure) is an
            # OSError, but `BadStatusLine` and `IncompleteRead` are
            # not — FTL's webserver answers garbage, not silence, when
            # unhappy. Keep `as err`: ruff format's unparenthesized
            # `except` here makes flaplint skip the module.
            raise ApiUnavailableError(reason=f"{method} {url}: {err}") from err


def _decode(raw: bytes) -> Mapping[str, object]:
    """Parse a JSON object, tolerating anything that is not one."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        # FTL answers with an HTML error page when its webserver is
        # unhappy, and that is not a state to act on.
        logger.debug("The API returned something that is not JSON: %s", err)
        return {}
    return cast("Mapping[str, object]", payload) if isinstance(payload, dict) else {}
