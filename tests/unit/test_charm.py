"""Tests for the charm's event wiring, reconciliation, and status.

What is under test here is the *shape*: every deferrable event reaches
the single reconciler, the outcomes `compute` decided actually reach the
workload module, and the statuses reported describe the machine rather
than the charm's hopes for it.

The workload modules are mocked whole. Nothing in this file patches
`subprocess`, `urllib`, or `charmlibs` — if it needed to, the boundary
between charm logic and workload logic would already have broken.
"""

import dataclasses
from unittest.mock import MagicMock

import ops
import pytest
from ops import testing

import charm
import pihole
import pihole_state
import resolved
from tests.unit.conftest import ADMIN_PASSWORD, VERSION, api_facts

# Every deferrable event the charm observes, all of which must route to
# `_reconcile`. `secret_changed` is tested separately because it needs a
# secret in the input state.
DEFERRABLE_EVENTS = (
    "install",
    "start",
    "config_changed",
    "upgrade_charm",
    "update_status",
    "leader_elected",
)

"""The packaged default, whose TLS entries kill the whole webserver."""

EFFECTS = frozenset(
    {
        "install",
        "hold_refresh",
        "set_ntp_server",
        "set_password",
        "start",
        "await_api",
        "apply_ftl_config",
    }
)
"""The mutating calls, so a call log can exclude the fact reads."""

ORDERED_EFFECTS = frozenset(
    {f"pihole.{name}" for name in EFFECTS} | {"resolved.disable_stub_listener"}
)
"""The same, spanning both workload modules under one recorder."""


def _set_content_that_does_nothing(_secret: ops.Secret, _content: dict[str, str]) -> None:
    """Stand in for a secret write that succeeds and takes no effect.

    `Secret.set_content` returns normally when the charm lacks
    permission, and the unit errors at the *end* of the hook instead.
    """


@pytest.mark.parametrize("event_name", DEFERRABLE_EVENTS)
def test_deferrable_event_reaches_active(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
    event_name: str,
):
    # GIVEN a converged machine
    # WHEN one of the observed deferrable events fires
    state_out = ctx.run(getattr(ctx.on, event_name)(), base_state)

    # THEN the unit is active, having converged through the one
    # reconciler rather than a handler per event
    assert state_out.unit_status == testing.ActiveStatus()


def test_secret_changed_reaches_active(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a converged machine that can also see a secret it does not
    # own. The charm never receives secret-changed for the admin
    # password, because it is the owner and that event is for observers.
    observed = testing.Secret({"token": "irrelevant"})
    state_in = dataclasses.replace(base_state, secrets={*base_state.secrets, observed})

    # WHEN that secret changes
    state_out = ctx.run(ctx.on.secret_changed(observed), state_in)

    # THEN the event is observed and the unit still settles
    assert state_out.unit_status == testing.ActiveStatus()


def test_reconcile_is_idempotent(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a unit that has already converged once
    state = ctx.run(ctx.on.config_changed(), base_state)

    # WHEN the same event fires again
    state = ctx.run(ctx.on.config_changed(), state)

    # THEN nothing regresses, and nothing was touched either time: a
    # converged machine yields Noop, so no effect is reachable
    assert state.unit_status == testing.ActiveStatus()
    mock_pihole.install.assert_not_called()
    mock_pihole.hold_refresh.assert_not_called()
    mock_pihole.start.assert_not_called()
    mock_pihole.set_ntp_server.assert_not_called()
    mock_pihole.set_password.assert_not_called()
    mock_resolved.disable_stub_listener.assert_not_called()


def test_ports_advertise_the_self_signed_443(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a converged machine
    # WHEN it reconciles
    state_out = ctx.run(ctx.on.start(), base_state)

    # THEN DNS is advertised on both protocols — a bare int would mean
    # tcp only — the admin UI on 80, and 443 too: the snap's launcher
    # self-signs a certificate there on first boot, so it is a real
    # listener, self-signed and all.
    assert state_out.opened_ports == {
        testing.TCPPort(53),
        testing.UDPPort(53),
        testing.TCPPort(80),
        testing.TCPPort(443),
    }


def test_ports_never_include_the_ntp_server_by_default(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a converged machine whose NTP server the charm has closed
    # WHEN it reconciles
    state_out = ctx.run(ctx.on.start(), base_state)

    # THEN 123/udp is not advertised: the server is disabled, not
    # exposed, so there is no listener to document
    assert testing.UDPPort(123) not in state_out.opened_ports


def test_ports_include_ntp_when_enabled(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine with ntp-server-enabled=true
    state_in = dataclasses.replace(base_state, config={"ntp-server-enabled": True})

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # THEN 123/udp is opened
    assert testing.UDPPort(123) in state_out.opened_ports


def test_a_fresh_machine_is_installed_started_and_gated(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    absent_snap: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine with no snap installed
    # WHEN the install hook fires
    ctx.run(ctx.on.install(), base_state)

    # THEN the whole bootstrap sequence reached the workload, in order,
    # and port 53 was freed as part of it
    mock_resolved.disable_stub_listener.assert_called_once_with()
    effects = [name for name, _, _ in absent_snap.mock_calls if name in EFFECTS]
    assert effects == [
        "install",
        "hold_refresh",
        "set_ntp_server",
        "set_password",
        "start",
        "await_api",
        "apply_ftl_config",
    ]

    # AND the daemon was explicitly enabled, because the snap ships
    # install-mode: disable and would otherwise never run
    absent_snap.start.assert_called_once_with(enable=True)
    absent_snap.set_ntp_server.assert_called_once_with(active=False)
    absent_snap.set_password.assert_called_once_with(ADMIN_PASSWORD)
    absent_snap.apply_ftl_config.assert_called_once_with(
        password=ADMIN_PASSWORD,
        config={"dns.blocking.active": True, "dns.dnssec": False},
    )


def test_the_snap_is_fetched_before_the_host_loses_its_resolver(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    absent_snap: MagicMock,
    mock_resolved: MagicMock,
):
    """Ordering across the two workload modules, not just within one.

    The pure test in `test_pihole_state` asserts the plan; this
    asserts that `_apply` performs it in that order, which is the
    only place the two modules interleave. If the store fails after
    its retries the drop-in has not been written, so the machine
    keeps its DNS and error state costs nothing — see ADR-0005
    section 2.9.
    """
    # GIVEN one call log spanning both workload modules
    recorder = MagicMock()
    recorder.attach_mock(absent_snap, "pihole")
    recorder.attach_mock(mock_resolved, "resolved")

    # WHEN a fresh machine is bootstrapped
    ctx.run(ctx.on.install(), base_state)

    # THEN the snap arrives first, and only then is systemd-resolved
    # displaced — still before anything starts, which is the workload's
    # actual constraint
    ordered = [name for name, _, _ in recorder.mock_calls if name in ORDERED_EFFECTS]
    assert ordered == [
        "pihole.install",
        "pihole.hold_refresh",
        "resolved.disable_stub_listener",
        "pihole.set_ntp_server",
        "pihole.set_password",
        "pihole.start",
        "pihole.await_api",
        "pihole.apply_ftl_config",
    ]


def test_the_password_is_never_offered_to_snap_set(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    absent_snap: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine with no snap installed
    # WHEN it is bootstrapped
    ctx.run(ctx.on.install(), base_state)

    # THEN no value ever handed to the snapd configuration path carries
    # the password. A password in snapd state is readable by anyone with
    # snapd access, which is why setpassword exists.
    for call in absent_snap.set_ntp_server.call_args_list:
        assert ADMIN_PASSWORD not in call.args


def test_an_uninstalled_machine_is_maintenance_not_active(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    absent_snap: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine where the install steps have not taken effect —
    # the "what if this never runs" direction
    # WHEN it reconciles
    state_out = ctx.run(ctx.on.install(), base_state)

    # THEN the unit says so, rather than claiming to be serving DNS
    assert state_out.unit_status == testing.MaintenanceStatus(
        f"installing the {pihole.SNAP_NAME} snap"
    )


def test_the_unit_passes_through_maintenance_before_active(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine that is not installed when the first hook runs
    mock_pihole.installed_revision.return_value = None
    state = ctx.run(ctx.on.install(), base_state)

    # WHEN the install takes effect and the next hook runs
    mock_pihole.installed_revision.return_value = "1348"
    state = ctx.run(ctx.on.start(), state)

    # THEN the unit reached Active by way of Maintenance rather than
    # jumping straight to it
    assert state.unit_status == testing.ActiveStatus()
    assert ctx.unit_status_history == [
        testing.UnknownStatus(),
        testing.MaintenanceStatus(f"installing the {pihole.SNAP_NAME} snap"),
    ]


def test_a_stopped_daemon_is_maintenance(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN an installed machine whose daemon will not come up
    mock_pihole.ftl_status.return_value = pihole_state.ServiceStatus(enabled=False, active=False)
    mock_pihole.api_facts.return_value = api_facts(api_ready=False)

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.update_status(), base_state)

    # THEN the unit is not Active
    assert state_out.unit_status == testing.MaintenanceStatus("starting the Pi-hole FTL daemon")


def test_a_running_daemon_without_an_api_is_blocked(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a daemon that is active while its HTTP API is not answering,
    # which after the port correction is a real fault and not a delay
    mock_pihole.api_facts.return_value = api_facts(api_ready=False)

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.update_status(), base_state)

    # THEN the unit is blocked with something a human can act on
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "HTTP API on port 80" in state_out.unit_status.message


def test_an_open_config_api_is_blocked_and_named(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a serving daemon with an empty pwhash, which lets anyone on
    # the network rewrite its configuration. This should be unreachable
    # by construction; the charm asserts it anyway.
    mock_pihole.api_facts.return_value = api_facts(pihole_state.PasswordUnset())

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.update_status(), base_state)

    # THEN the status names both the exposure and the remedy
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "unauthenticated writes" in state_out.unit_status.message
    assert "rotate-admin-password" in state_out.unit_status.message


def test_a_rejected_password_is_reapplied_then_blocks(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine that refuses the password the charm holds
    mock_pihole.api_facts.return_value = api_facts(pihole_state.PasswordRejected())

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.update_status(), base_state)

    # THEN the charm reapplies it, and says so if it still does not take
    mock_pihole.set_password.assert_called_once_with(ADMIN_PASSWORD)
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "rejects the password" in state_out.unit_status.message


def test_a_workload_error_is_pushed_to_the_status_handler(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a workload that reports success and changes nothing, which
    # collect_unit_status cannot re-derive: the daemon is healthy, and
    # the hold is missing so the failing effect actually runs
    mock_pihole.refresh_held.return_value = False
    mock_pihole.hold_refresh.side_effect = pihole.PiholeError(
        operation="holding pihole-by-rajannpatel against auto-refresh",
        expected="a hold visible in `snap info`",
        actual="snapd reports no hold",
    )

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.config_changed(), base_state)

    # THEN the failure the reconciler alone knew about wins over the
    # Active status the machine's own state would have produced
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "snapd reports no hold" in state_out.unit_status.message


def test_a_resolved_failure_is_pushed_to_the_status_handler(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine where port 53 cannot be freed
    mock_pihole.port53_released.return_value = False
    mock_resolved.disable_stub_listener.side_effect = resolved.ResolvedError(
        operation="restarting systemd-resolved",
        expected="a successful restart",
        actual="systemctl reported a failure",
        remedy="run `systemctl status systemd-resolved` on the machine",
    )

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.config_changed(), base_state)

    # THEN the unit is blocked with the remedy, and removable
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "systemctl status systemd-resolved" in state_out.unit_status.message


def test_the_workload_version_is_reported(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN an installed machine
    # WHEN it reconciles
    state_out = ctx.run(ctx.on.start(), base_state)

    # THEN juju status shows Pi-hole's version, not the charm's
    assert state_out.workload_version == VERSION


def test_remove_restores_host_dns(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a unit about to go away
    # WHEN remove fires, which Juju does not allow a charm to defer
    ctx.run(ctx.on.remove(), base_state)

    # THEN the drop-in is removed, because the snap cannot do it and
    # this is the only thing between removal and a machine with no DNS
    assert ctx.emitted_events[0].handle.kind == "remove"
    mock_resolved.restore.assert_called_once_with()


def test_remove_logs_the_remedy_before_letting_the_hook_fail(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a unit whose drop-in cannot be removed, which is the one
    # failure that leaves the machine with no resolver at all
    mock_resolved.restore.side_effect = resolved.ResolvedError(
        operation="removing /etc/systemd/resolved.conf.d/pihole.conf",
        expected="the drop-in to be gone",
        actual="it is still on disk",
        remedy="run: sudo sh -c 'rm -f /etc/systemd/resolved.conf.d/pihole.conf'",
    )

    # WHEN remove fires
    # THEN the hook fails rather than reporting a cleanup it did not do.
    # Raising is right here: Juju retries the hook, and there is nothing
    # left to converge afterwards.
    with pytest.raises(testing.errors.UncaughtCharmError):
        ctx.run(ctx.on.remove(), base_state)

    # AND the remedy is in the log at ERROR, because a status set during
    # `remove` is not something anyone will read
    assert any(
        line.level == "ERROR" and "rm -f /etc/systemd/resolved.conf.d/pihole.conf" in line.message
        for line in ctx.juju_log
    )


def test_a_follower_without_a_password_converges_nothing(
    ctx: testing.Context[charm.PiholeCharm],
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a non-leader unit that cannot generate the password, and no
    # secret to read
    state_in = testing.State(model=testing.Model(type="lxd"), leader=False)

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.install(), state_in)

    # THEN it starts nothing: a daemon serving with an empty pwhash
    # would accept configuration writes from the whole network
    mock_pihole.install.assert_not_called()
    mock_pihole.start.assert_not_called()
    assert state_out.unit_status == testing.MaintenanceStatus("generating the admin password")


def test_the_leader_generates_and_stores_a_password(
    ctx: testing.Context[charm.PiholeCharm],
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a leader with no secret yet
    state_in = testing.State(model=testing.Model(type="lxd"), leader=True)

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.install(), state_in)

    # THEN an app-owned secret holds a generated password, retrievable
    # by label so nothing has to be remembered across hooks
    secret = state_out.get_secret(label=charm.ADMIN_PASSWORD_LABEL)
    assert secret.owner == "app"
    assert secret.latest_content is not None
    assert len(secret.latest_content[charm.ADMIN_PASSWORD_FIELD]) >= 24


def test_get_admin_password_returns_the_stored_secret(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a unit whose password has been generated
    # WHEN the operator asks for it
    ctx.run(ctx.on.action("get-admin-password"), base_state)

    # THEN it comes from the charm-owned secret, not from snapd state
    # and not from pihole.toml, which holds only a hash
    assert ctx.action_results == {"password": ADMIN_PASSWORD}


def test_get_admin_password_fails_before_one_exists(
    ctx: testing.Context[charm.PiholeCharm],
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a unit with no secret yet
    state_in = testing.State(model=testing.Model(type="lxd"), leader=True)

    # WHEN the operator asks for the password
    # THEN the action fails rather than returning nothing
    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(ctx.on.action("get-admin-password"), state_in)
    assert "no admin password has been generated yet" in exc_info.value.message


def test_rotate_admin_password_generates_and_applies_a_new_one(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a converged unit
    # WHEN the rotate action runs
    state_out = ctx.run(ctx.on.action("rotate-admin-password"), base_state)

    # THEN a new password was stored and applied with setpassword
    secret = state_out.get_secret(label=charm.ADMIN_PASSWORD_LABEL)
    assert secret.latest_content is not None
    rotated = secret.latest_content[charm.ADMIN_PASSWORD_FIELD]
    assert rotated != ADMIN_PASSWORD
    mock_pihole.set_password.assert_called_once_with(rotated)
    assert ctx.action_results == {"result": "the admin UI password has been rotated"}


def test_rotate_admin_password_confirms_with_the_api_oracle(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a Pi-hole that will not accept the new password, which is
    # what `setpassword` exiting 0 on v5 syntax looks like from here.
    # A rejection reaching the charm has already survived the workload
    # module's settle window, so it is a verdict rather than a race —
    # see pihole.PASSWORD_SETTLE_WINDOW.
    mock_pihole.admin_password_state.return_value = pihole_state.PasswordRejected()

    # WHEN the rotate action runs
    # THEN it fails rather than reporting a rotation that did not happen
    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(ctx.on.action("rotate-admin-password"), base_state)
    assert "not confirmed" in exc_info.value.message


def test_rotate_admin_password_does_not_fail_on_an_unverifiable_oracle(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    """The reported defect, from the operator's side.

    An exhausted session pool answers ``429``, which classifies as
    ``PasswordUnverified``. The write itself was already proven by
    ``set_password`` reading ``pwhash`` back, so the action reports
    the rotation it performed instead of telling the operator to fix
    a credential that is in fact correct.
    """
    # GIVEN a Pi-hole whose API could not be consulted afterwards
    mock_pihole.admin_password_state.return_value = pihole_state.PasswordUnverified()

    # WHEN the rotate action runs
    ctx.run(ctx.on.action("rotate-admin-password"), base_state)

    # THEN it reports the rotation rather than a security problem
    assert ctx.action_results == {"result": "the admin UI password has been rotated"}


def test_rotate_admin_password_reports_a_workload_failure(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a workload that cannot apply the password
    mock_pihole.set_password.side_effect = pihole.PiholeError(
        operation="setting the admin password",
        expected="a fresh pwhash in pihole.toml",
        actual="the hash did not change",
    )

    # WHEN the rotate action runs
    # THEN the operator is told, rather than the unit going to error
    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(ctx.on.action("rotate-admin-password"), base_state)
    assert "the password was not rotated" in exc_info.value.message


def test_rotate_admin_password_verifies_the_secret_write(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a secret write that succeeds and takes no effect, which is
    # exactly what a missing permission looks like until the hook ends
    monkeypatch.setattr(ops.Secret, "set_content", _set_content_that_does_nothing)

    # WHEN the rotate action runs
    # THEN the read-back catches it
    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(ctx.on.action("rotate-admin-password"), base_state)
    assert "the secret still holds something else" in exc_info.value.message
    mock_pihole.set_password.assert_not_called()


def test_rotate_admin_password_is_leader_only(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a follower unit
    state_in = dataclasses.replace(base_state, leader=False)

    # WHEN the rotate action runs there
    # THEN it fails, because only the leader may write an app secret
    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(ctx.on.action("rotate-admin-password"), state_in)
    assert "only the leader" in exc_info.value.message
    mock_pihole.set_password.assert_not_called()


def _add_secret_that_does_nothing(
    _app: ops.Application,
    _content: dict[str, str],
    **_kwargs: object,
) -> None:
    """Stand in for a secret creation that takes no effect."""


def test_a_secret_write_that_takes_no_effect_blocks_rather_than_errors(
    ctx: testing.Context[charm.PiholeCharm],
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
):
    # GIVEN a leader whose secret creation silently does nothing
    monkeypatch.setattr(ops.Application, "add_secret", _add_secret_that_does_nothing)
    state_in = testing.State(model=testing.Model(type="lxd"), leader=True)

    # WHEN it reconciles
    state_out = ctx.run(ctx.on.install(), state_in)

    # THEN the unit is Blocked, not in error. A unit in error needs
    # `--force` to remove, and `--force` skips the remove handler that
    # gives the host its resolver back.
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "the secret still holds something else" in state_out.unit_status.message
    mock_pihole.install.assert_not_called()


def test_config_changed_with_blocking_disabled_flows_to_intent(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine with blocking-enabled=false
    state_in = dataclasses.replace(base_state, config={"blocking-enabled": False})

    # WHEN the config-changed event fires
    ctx.run(ctx.on.config_changed(), state_in)

    # THEN the intent flows to apply_ftl_config with the disabled value
    mock_pihole.apply_ftl_config.assert_called_once_with(
        password=ADMIN_PASSWORD, config={"dns.blocking.active": False}
    )


def test_config_changed_with_upstream_dns_flows_to_intent(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine with upstream-dns set
    state_in = dataclasses.replace(base_state, config={"upstream-dns": "1.1.1.1, 9.9.9.9"})

    # WHEN the config-changed event fires
    ctx.run(ctx.on.config_changed(), state_in)

    # THEN the intent flows to apply_ftl_config
    mock_pihole.apply_ftl_config.assert_called_once_with(
        password=ADMIN_PASSWORD, config={"dns.upstreams": ("1.1.1.1", "9.9.9.9")}
    )


def test_config_changed_with_listening_mode_flows_to_intent(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine with dns-listening-mode set to ALL
    state_in = dataclasses.replace(base_state, config={"dns-listening-mode": "ALL"})

    # WHEN the config-changed event fires
    ctx.run(ctx.on.config_changed(), state_in)

    # THEN the intent flows to apply_ftl_config
    mock_pihole.apply_ftl_config.assert_called_once_with(
        password=ADMIN_PASSWORD, config={"dns.listeningMode": "ALL"}
    )


def test_config_changed_with_ntp_enabled_applies_set_ntp_server(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    mock_pihole: MagicMock,
    mock_resolved: MagicMock,
):
    # GIVEN a machine with ntp-server-enabled=true
    state_in = dataclasses.replace(base_state, config={"ntp-server-enabled": True})

    # WHEN the config-changed event fires
    ctx.run(ctx.on.config_changed(), state_in)

    # THEN set_ntp_server is called with active=True
    mock_pihole.set_ntp_server.assert_called_once_with(active=True)


def test_an_invalid_config_value_blocks_without_converging(
    ctx: testing.Context[charm.PiholeCharm],
    base_state: testing.State,
    absent_snap: MagicMock,
    mock_resolved: MagicMock,
):
    """`load_config(errors="blocked")` must not be wrapped in a `try`.

    It answers an invalid *value* by setting BlockedStatus and raising
    ops' `_Abort(0)`, which exits the hook cleanly before
    `_evaluate_status` — so nothing can override the status. That abort
    is an `Exception` subclass: catching broadly around the call would
    swallow it and the reconcile would carry on over unvalidated
    config. This test is the only thing that notices if someone does.
    """
    # GIVEN a machine with nothing installed, so a reconcile that
    # carried on would have to install the snap
    state_in = dataclasses.replace(base_state, config={"dns-listening-mode": "NOT_A_MODE"})

    # WHEN the config changes to a value the model rejects
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # THEN the unit is Blocked by ops itself, naming the config
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "Invalid config" in state_out.unit_status.message

    # AND the reconcile never ran: no effect reached the workload
    absent_snap.install.assert_not_called()
    absent_snap.hold_refresh.assert_not_called()
