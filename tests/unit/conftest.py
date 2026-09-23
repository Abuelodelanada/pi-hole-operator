"""Shared fixtures for the unit tests.

Three things here exist to make a mistake impossible to repeat.

`ops.testing.Model` defaults to `type="kubernetes"`, so the model type
lives in a fixture rather than in each test: a machine charm exercised
in a Kubernetes model is in the wrong environment.

`mock_pihole` and `mock_resolved` replace the *whole* workload modules.
No test of `charm.py` may patch `subprocess`, `urllib`, or `charmlibs` —
if one needs to, the boundary between charm logic and workload logic
has already broken.

The mocked workload is configured as a **converged** machine by
default, so a test that cares about a drifted fact says which one, and
only that one.
"""

import dataclasses
import email.message
import io
import json
import pathlib
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Protocol, cast
from unittest.mock import MagicMock

import pytest
import tenacity
from charmlibs import snap
from ops import testing

import charm
import ftl_api
import pihole
import pihole_state
import resolved

ADMIN_PASSWORD = "an-admin-password-24-bytes"
"""What the mocked secret holds. Never a real generated value."""

PINNED_REVISION = pihole_state.SNAP_REVISIONS["amd64"]
"""What the charm pins for the architecture the fakes pretend to be."""

REVISION = "1348"
"""A revision that is NOT the charm's pin — drift tests rely on it."""
VERSION = "6.4.3"


@pytest.fixture
def ctx() -> Iterator[testing.Context[charm.PiholeCharm]]:
    """A context for the charm under test."""
    with testing.Context(charm.PiholeCharm) as context:
        yield context


@pytest.fixture
def admin_secret() -> testing.Secret:
    """The app-owned secret the charm keeps the admin password in."""
    return testing.Secret(
        {charm.ADMIN_PASSWORD_FIELD: ADMIN_PASSWORD},
        label=charm.ADMIN_PASSWORD_LABEL,
        owner="app",
    )


@pytest.fixture
def base_state(admin_secret: testing.Secret) -> testing.State:
    """A single leader unit in a machine model, with no relations.

    `type="lxd"` is mandatory: this is a machine charm, and the default
    would place it in a Kubernetes model.
    """
    return testing.State(
        model=testing.Model(type="lxd"),
        leader=True,
        secrets={admin_secret},
    )


@pytest.fixture
def mock_pihole(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the workload module's `Pihole` with a converged fake.

    The facts are real values rather than `MagicMock`s, so `fetch` and
    `compute` run for real and the transition tests exercise the actual
    decisions instead of a stubbed answer.
    """
    mock = MagicMock()
    mock.installed_revision.return_value = PINNED_REVISION
    mock.pinned_revision.return_value = PINNED_REVISION
    mock.refresh_held.return_value = True
    mock.workload_version.return_value = VERSION
    mock.ftl_status.return_value = pihole_state.ServiceStatus(enabled=True, active=True)
    mock.api_facts.return_value = api_facts()
    mock.admin_password_state.return_value = pihole_state.PasswordAccepted()
    mock.port53_released.return_value = True
    mock.port67_free.return_value = True
    mock.ntp_server_active.return_value = False
    mock.upstream_dns.return_value = None
    mock.listening_mode.return_value = None
    mock.blocking_enabled.return_value = True
    mock.dnssec_enabled.return_value = False
    mock.connected_plugs.return_value = frozenset(pihole_state.UNCONDITIONAL_PLUGS)
    mock.gravity_schedule.return_value = None
    mock.dhcp_active.return_value = False
    mock.dhcp_pool.return_value = None
    mock.machine_ipv4_addresses.return_value = frozenset()
    mock.snap_check.return_value = pihole_state.SnapCheckOk()
    monkeypatch.setattr(charm.pihole, "Pihole", lambda: mock)
    return mock


def api_facts(
    admin_password: pihole_state.AdminPasswordState | None = None,
    *,
    api_ready: bool = True,
) -> pihole_state.ApiFacts:
    """Build the two API facts, defaulting to a converged machine.

    They come from one call because one `/api/auth` session answers
    both, so a test that drifts either one says so here.
    """
    return pihole_state.ApiFacts(
        admin_password=admin_password or pihole_state.PasswordAccepted(),
        api_ready=api_ready,
    )


@pytest.fixture
def absent_snap(mock_pihole: MagicMock) -> MagicMock:
    """Turn the mocked workload into a machine with nothing on it."""
    mock_pihole.installed_revision.return_value = None
    mock_pihole.workload_version.return_value = None
    return mock_pihole


@pytest.fixture
def mock_resolved(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the systemd-resolved module, which writes to /etc.

    The error type is the **real** class, not an auto-created mock
    attribute: `charm.py` catches `resolved.ResolvedError` by that
    name, and a `MagicMock` in an `except` clause is a `TypeError`
    rather than a caught error.
    """
    mock = MagicMock()
    mock.ResolvedError = resolved.ResolvedError
    mock.is_port53_released.return_value = True
    monkeypatch.setattr(charm, "resolved", mock)
    return mock


# ---------------------------------------------------------------------
# Workload-layer fakes.
#
# These exist so `pihole.py` can be tested against a workload that
# **lies the way the real one lies**: a snap that returns success on a
# key it drops, a command that exits 0 having printed usage. Verifying
# non-negotiable 6 needs a collaborator that can misbehave on purpose,
# which is why `Pihole` takes its collaborators rather than reaching for
# `subprocess` directly.
# ---------------------------------------------------------------------


class FakeSnap:
    """A snap that records what it was told, and can lie about it."""

    def __init__(
        self,
        *,
        present: bool = True,
        revision: str = REVISION,
        version: str | None = VERSION,
        enabled: bool = True,
        active: bool = True,
        honest: bool = True,
        refusal: Exception | None = None,
    ) -> None:
        self.present = present
        self.revision = revision
        self.version = version
        self.enabled = enabled
        self.active = active
        self.honest = honest
        # Every mutating call on a real `Snap` can raise `snap.Error`:
        # `start` reaches `subprocess.run(check=True)` by way of
        # `_snap_daemons`, and `ensure` talks to the store. Scripting it
        # is what lets a test prove the workload module converts it.
        self.refusal = refusal
        self.ensure_calls: list[tuple[snap.SnapState, str | None, str | None]] = []
        self.set_calls: list[dict[str, object]] = []
        self.start_calls: list[tuple[list[str] | None, bool]] = []
        self.restart_calls: list[tuple[list[str] | None, bool]] = []
        self.has_ftl_service = True

    @property
    def services(self) -> Mapping[str, snap.SnapServiceDict]:
        """What snapd would report about this snap's services."""
        if not self.has_ftl_service:
            return {}
        return {
            "pihole-ftl": snap.SnapServiceDict(
                daemon="simple",
                daemon_scope="system",
                enabled=self.enabled,
                active=self.active,
                activators=[],
            )
        }

    def ensure(
        self, state: snap.SnapState, *, channel: str | None = None, revision: str | None = None
    ) -> None:
        """Install the snap, or pretend to when dishonest."""
        self.ensure_calls.append((state, channel, revision))
        if self.refusal is not None:
            raise self.refusal
        if self.honest:
            self.present = True
            if revision is not None:
                self.revision = str(revision)

    def hold(self) -> None:
        """Hold the snap, or pretend to when dishonest."""
        self.hold_calls += 1
        if self.refusal is not None:
            raise self.refusal
        if self.honest:
            self.held = True

    held: bool = False
    hold_calls: int = 0

    def set(self, config: Mapping[str, snap.JSONAble], *, typed: bool = False) -> None:
        """Accept configuration, as `snap set` accepts anything."""
        self.set_calls.append(dict(config))
        if self.refusal is not None:
            raise self.refusal

    def start(self, services: list[str] | None = None, enable: bool = False) -> None:
        """Start services, or pretend to when dishonest."""
        self.start_calls.append((services, enable))
        if self.refusal is not None:
            raise self.refusal
        if self.honest:
            self.active = True
            self.enabled = self.enabled or enable

    def restart(self, services: list[str] | None = None, reload: bool = False) -> None:
        """Restart services, or pretend to when dishonest."""
        self.restart_calls.append((services, reload))
        if self.refusal is not None:
            raise self.refusal
        if self.honest:
            self.active = True


class FakeCache:
    """A snap cache that hands out one snap, or fails as snapd does."""

    def __init__(
        self,
        fake_snap: FakeSnap | None = None,
        *,
        errors: int = 0,
        error: Exception | None = None,
    ) -> None:
        self.fake_snap = fake_snap
        self.remaining_errors = errors
        self.error = error or snap.SnapError("the snap store is having a moment")
        self.calls = 0

    def __call__(self) -> Mapping[str, snap.Snap]:
        """Return the cache, raising while errors remain.

        The cast is the whole price of `FakeSnap` not inheriting from
        `snap.Snap`: it tells pyright to accept a duck. A fake that
        drifts from the real API is still caught — as a `TypeError` in
        whichever test calls it.
        """
        self.calls += 1
        if self.remaining_errors > 0:
            self.remaining_errors -= 1
            raise self.error
        if self.fake_snap is None:
            raise snap.SnapNotFoundError(f"Snap {pihole.SNAP_NAME!r} not found!")
        return cast("Mapping[str, snap.Snap]", {pihole.SNAP_NAME: self.fake_snap})


class FakeRunner:
    """A `subprocess.run` that records argv and can fail on demand.

    It answers `systemd-detect-virt --container` the way the real
    binary does, because the workload module runs it to sharpen an
    install failure. The default is **not** a container, so a test only
    says otherwise when that is the point of the test.

    ``stdout_script`` is consulted for non-detect-virt commands: a
    callable that receives the argv and returns stdout, defaulting to
    the empty string so existing tests are unaffected.
    """

    def __init__(
        self,
        returncode: int = 0,
        effect: Callable[[Sequence[str]], None] | None = None,
        *,
        container: str | None = None,
        detect_virt_error: OSError | None = None,
        stdout_script: Callable[[Sequence[str]], str] | None = None,
    ) -> None:
        self.returncode = returncode
        self.effect = effect
        self.container = container
        self.detect_virt_error = detect_virt_error
        self.stdout_script = stdout_script
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        """Record the call, apply any effect, report a result."""
        self.calls.append(list(args))
        if args[0] == pihole.DETECT_VIRT_CMD:
            return self._detect_virt(args)
        if self.effect is not None:
            self.effect(args)
        stdout = ""
        if self.stdout_script is not None:
            stdout = self.stdout_script(args)
        if check and self.returncode != 0:
            raise subprocess.CalledProcessError(
                self.returncode,
                list(args),
                output=stdout,
                stderr="Usage: pihole [options]",
            )
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=self.returncode,
            stdout=stdout,
            stderr="",
        )

    def _detect_virt(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Answer as `systemd-detect-virt --container` really does.

        Exit 0 and the technology's name inside a container; exit 1 and
        `none` on bare metal *and in a VM*, because `--container` asks
        only about containers. Verified on 26.04:
        `systemd-detect-virt --container` prints `none` and exits 1.
        """
        if self.detect_virt_error is not None:
            raise self.detect_virt_error
        detected = self.container or "none"
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0 if self.container else 1,
            stdout=f"{detected}\n",
            stderr="",
        )


class FakeClock:
    """A monotonic clock that only moves when something sleeps.

    Every bounded wait in the workload module is a deadline plus a
    sleep, so faking the sleep alone would leave the deadline reading
    real wall-clock time — and a test that waits for real is a test
    that will one day flake. Advancing the clock by exactly what was
    slept keeps the loop's arithmetic honest while costing nothing,
    and it makes the assertion "how long did it wait" a recorded list
    rather than a measurement.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record a sleep, and let that much fake time pass."""
        self.sleeps.append(seconds)
        self.now += seconds


class HttpReply(Protocol):
    """The response shape the module's HTTP client consumes."""

    @property
    def status(self) -> int:
        """The HTTP status code."""
        ...

    def read(self) -> bytes:
        """The body, as bytes."""
        ...

    def __enter__(self) -> HttpReply:
        """Enter the response context, as `urlopen` allows."""
        ...

    def __exit__(self, *args: object) -> None:
        """Leave the response context."""
        ...


@dataclasses.dataclass
class FakeResponse:
    """Enough of an HTTP response for the client under test."""

    status: int
    payload: object

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        """Return the body as bytes, as `urlopen` would."""
        if self.status == 204:
            # 204 No Content has no body — a real FTL logout answers
            # empty, and the client must not mistake that for JSON.
            return b""
        return json.dumps(self.payload).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class RecordedRequest:
    """One request the workload module made to the FTL API."""

    route: str
    sid: str | None
    body: Mapping[str, object] | None


@dataclasses.dataclass(frozen=True)
class FakeError:
    """An error answer the API can give more than once.

    `urllib.error.HTTPError` carries its body in a file object that is
    closed once read, so a single instance cannot serve two requests —
    while the real `urlopen` raises a fresh one every time. Keeping the
    status and body as data and building the exception per request is
    what lets a test exercise a path that asks twice.
    """

    code: int
    payload: object = None

    def build(self) -> urllib.error.HTTPError:
        """Build the exception `urlopen` would raise for this answer."""
        return urllib.error.HTTPError(
            url=f"{ftl_api.API_ORIGIN}/api/auth",
            code=self.code,
            msg="Unauthorized",
            hdrs=email.message.Message(),
            fp=io.BytesIO(json.dumps(self.payload).encode("utf-8")),
        )


type ApiOutcome = HttpReply | FakeError | Exception
"""What a scripted route answers with: a reply, an error, or a fault."""


class FakeApi:
    """A stand-in for `urlopen`, routed by method and path."""

    def __init__(self, routes: Mapping[str, ApiOutcome]) -> None:
        self.routes: dict[str, ApiOutcome] = dict(routes)
        self.requests: list[RecordedRequest] = []

    def __call__(
        self,
        request: urllib.request.Request,
        timeout: float | None = None,
    ) -> HttpReply:
        """Answer one request, recording what was asked for."""
        path = request.full_url.split("/api/", 1)[1]
        route = f"{request.method} {path}"
        body = json.loads(request.data) if isinstance(request.data, bytes) else None
        self.requests.append(
            RecordedRequest(route=route, sid=request.get_header("Sid"), body=body)
        )
        outcome = self.routes.get(route, urllib.error.URLError("Connection refused"))
        if isinstance(outcome, FakeError):
            raise outcome.build()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def http_error(code: int, payload: object = None) -> FakeError:
    """Script an HTTP error answer from the API.

    A 4xx is an answer from the API, not a failure to reach it, and the
    difference is what the callers branch on.
    """
    return FakeError(code=code, payload=payload)


Routes = dict[str, ApiOutcome]
"""A scripted API, keyed by `METHOD path`."""

PASSWORD = "an-admin-password"
CLI_PW = "a-forty-four-character-cli-password-value--"
SID = "a-session-id"
OLD_HASH = "$BALLOON-SHA256$v=1$s=1024,t=32$old"
NEW_HASH = "$BALLOON-SHA256$v=1$s=1024,t=32$new"

NEVER = 9_999
"""More refusals than the settle window can possibly ask for."""

AUTH_OK: Routes = {"POST auth": FakeResponse(200, {"session": {"valid": True, "sid": SID}})}
BLOCKING_OK: Routes = {
    "GET dns/blocking": FakeResponse(200, {"blocking": "enabled", "timer": None})
}
LOGOUT_OK: Routes = {"DELETE auth": FakeResponse(204, None)}


def api(monkeypatch: pytest.MonkeyPatch, routes: Routes) -> FakeApi:
    """Point the module's HTTP client at a scripted FTL API."""
    fake = FakeApi(routes)
    monkeypatch.setattr(ftl_api.urllib.request, "urlopen", fake)
    return fake


def write_cli_pw(snap_data: pathlib.Path, value: str) -> None:
    """Write the CLI password FTL regenerates on every restart."""
    path = snap_data / ftl_api.CLI_PW
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")


def write_pihole_toml(
    snap_data: pathlib.Path,
    *,
    pwhash: str | None = None,
    ntp_active: bool | None = None,
    upstreams: list[str] | None = None,
    listening_mode: str | None = None,
    blocking_active: bool | None = None,
    dnssec: bool | None = None,
    dhcp_active: bool | None = None,
    dhcp_pool: pihole_state.DhcpPool | None = None,
    raw: str | None = None,
) -> None:
    """Write the subset of `pihole.toml` this charm reads back."""
    path = snap_data / pihole.PIHOLE_TOML
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return
    lines = ["[webserver.api]"]
    if pwhash is not None:
        lines.append(f'pwhash = "{pwhash}"')
    if ntp_active is not None:
        lines += [
            "[ntp.ipv4]",
            f"active = {str(ntp_active).lower()}",
            "[ntp.ipv6]",
            f"active = {str(ntp_active).lower()}",
        ]
    # All three [dns] keys are collected first so the table header is
    # emitted once, whatever the combination.
    dns_lines: list[str] = []
    if upstreams is not None:
        dns_lines.append(f"upstreams = {upstreams!r}")
    if listening_mode is not None:
        dns_lines.append(f'listeningMode = "{listening_mode}"')
    if dnssec is not None:
        dns_lines.append(f"dnssec = {str(dnssec).lower()}")
    if dns_lines:
        lines.append("[dns]")
        lines += dns_lines
    if blocking_active is not None:
        lines += ["[dns.blocking]", f"active = {str(blocking_active).lower()}"]
    if dhcp_active is not None or dhcp_pool is not None:
        lines.append("[dhcp]")
        if dhcp_active is not None:
            lines.append(f"active = {str(dhcp_active).lower()}")
        if dhcp_pool is not None:
            lines.append(f'start = "{dhcp_pool.start}"')
            lines.append(f'end = "{dhcp_pool.end}"')
            lines.append(f'router = "{dhcp_pool.router}"')
            lines.append(f'netmask = "{dhcp_pool.netmask}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def snap_data(tmp_path: pathlib.Path) -> pathlib.Path:
    """A stand-in for `$SNAP_DATA`, resolved through `current`."""
    root = tmp_path / "snap-data"
    root.mkdir()
    return root


@pytest.fixture
def drop_in(tmp_path: pathlib.Path) -> pathlib.Path:
    """A stand-in for the resolved drop-in, with no parent directory."""
    return tmp_path / "resolved.conf.d" / "pihole.conf"


@pytest.fixture
def fake_snap() -> FakeSnap:
    """An installed, running snap that tells the truth."""
    return FakeSnap()


@pytest.fixture
def fake_runner() -> FakeRunner:
    """A command runner that succeeds and records its argv."""
    return FakeRunner()


@pytest.fixture
def clock() -> FakeClock:
    """A clock that never waits, and records what it was told to."""
    return FakeClock()


@pytest.fixture
def workload(
    fake_snap: FakeSnap,
    fake_runner: FakeRunner,
    snap_data: pathlib.Path,
    drop_in: pathlib.Path,
    clock: FakeClock,
) -> pihole.Pihole:
    """The workload module wired to fakes, with nothing that waits."""
    return pihole.Pihole(
        cache_factory=FakeCache(fake_snap),
        run=fake_runner,
        snap_data=snap_data,
        resolved_drop_in=drop_in,
        retry_wait=tenacity.wait_none(),
        # The clock is shared by the API client and the snap wrapper:
        # both bounded waits (API readiness, DHCP bind) are a deadline
        # plus a sleep, and a test that fakes one without the other
        # measures wall-clock time by accident.
        api=ftl_api.FtlApi(snap_data=snap_data, sleep=clock.sleep, monotonic=clock.monotonic),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
