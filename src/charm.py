#!/usr/bin/env python3

"""Charmed operator for Pi-hole v6 on Ubuntu machines.

See ADR-0003 for the reconcile pattern and ADR-0005 for the
install order.
"""

import logging
import secrets
from typing import assert_never

import ops
from charms.grafana_agent.v0.cos_agent import (  # pyright: ignore[reportMissingTypeStubs]
    COSAgentProvider,
)

import pihole
import pihole_config
import pihole_state
import resolved

logger = logging.getLogger(__name__)

ADMIN_PASSWORD_LABEL = "pihole-admin-password"
"""Retrieved by label, so nothing has to be remembered across hooks."""

ADMIN_PASSWORD_FIELD = "password"
ADMIN_PASSWORD_BYTES = 24

WORKLOAD_ERRORS = (pihole.PiholeError, resolved.ResolvedError)
"""Failures a human can act on, rather than bugs in our own code."""


class PiholeCharm(ops.CharmBase):
    """Deploy and operate Pi-hole v6 via its unofficial snap."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self._pihole = pihole.Pihole()

        # Push-status channel: lives for one hook, not cross-hook
        # state. See ADR-0005 section 2.4.
        self._reconcile_failure: ops.StatusBase | None = None

        self._cos_agent = COSAgentProvider(
            self,
            relation_name="cos-agent",
            log_slots=[f"{pihole_state.SNAP_NAME}:logs"],
            refresh_events=[self.on.config_changed, self.on.upgrade_charm],
        )

        # Every deferrable event converges the same way, so they all
        # land in the same handler. See ADR-0003 section 2.1.
        for event in (
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,
            self.on.secret_changed,
        ):
            framework.observe(event, self._reconcile)

        # These cannot be deferred, which is the objective test for
        # deserving a handler of their own.
        non_reconcile = {
            self.on.collect_unit_status: self._on_collect_status,
            self.on.remove: self._on_remove,
            self.on["get-admin-password"].action: self._on_get_admin_password,
            self.on["rotate-admin-password"].action: self._on_rotate_admin_password,
            self.on["snap-check"].action: self._on_snap_check,
            self.on["update-gravity"].action: self._on_update_gravity,
            self.on["free-port-53"].action: self._on_free_port_53,
        }

        for event, handler in non_reconcile.items():
            framework.observe(event, handler)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the machine toward the declared intent.

        Every step must be safe to run twice or never. See
        ADR-0003 §2.5.
        """
        # errors="blocked" aborts on invalid values; catching
        # broadly would swallow that abort and converge on
        # unvalidated config.
        config = self.load_config(pihole_config.PiholeConfig, errors="blocked")

        # Error state needs `--force`, which skips the `remove`
        # handler. See ADR-0005 §2.9.
        try:
            match _intent_from(self._ensure_password(), config):
                case pihole_state.NoIntentYet():
                    logger.info("no admin password available yet; waiting for the leader")
                    return
                case pihole_state.PiholeIntent() as intent:
                    self._advertise_ports(intent)
                    state = pihole_state.fetch(self._pihole, intent.admin_password)
                    for outcome in pihole_state.compute(state, intent):
                        self._apply(outcome)

                    self._report_version(state)

                case _ as unreachable:
                    assert_never(unreachable)
        except WORKLOAD_ERRORS as err:
            # A push status: the daemon may be healthy while one
            # operation silently failed. See ADR-0005 §2.4.
            logger.error("reconcile failed: %s", err)
            self._reconcile_failure = ops.BlockedStatus(str(err))

    def _apply(self, outcome: pihole_state.PiholeOutcome) -> None:
        """Perform one decided outcome.

        Exhaustive by construction: `tox -e static` fails if a new
        member has no branch here. See ADR-0003 §2.5.
        """
        logger.info("applying %s.", outcome)
        match outcome:
            case pihole_state.ReleasePort53():
                resolved.disable_stub_listener()
            case pihole_state.InstallSnap():
                self._pihole.install()
            case pihole_state.HoldSnapRefresh():
                self._pihole.hold_refresh()
            case pihole_state.ConnectPlugs(plugs=plugs):
                self._pihole.connect_plugs(plugs)
            case pihole_state.SetNtpServer(active=active):
                self._pihole.set_ntp_server(active=active)
            case pihole_state.SetAdminPassword(password=password):
                self._pihole.set_password(password)
            case pihole_state.StartFtl():
                self._pihole.start(enable=True)
            case pihole_state.RestartFtl():
                self._pihole.restart()
            case pihole_state.AwaitApi(timeout=timeout):
                self._pihole.await_api(timeout)
            case pihole_state.SetFtlConfig(config=config, password=password):
                self._pihole.apply_ftl_config(password=password, config=dict(config))
            case pihole_state.WaitForDhcpBind(timeout=timeout):
                self._pihole.wait_for_dhcp_bind(timeout)
            case pihole_state.WriteGravityTimer(schedule=schedule):
                self._pihole.write_gravity_timer(schedule)
            case pihole_state.RemoveGravityTimer():
                self._pihole.remove_gravity_timer()
            case pihole_state.Noop():
                logger.debug("converged: nothing to do.")
            case _ as unreachable:
                assert_never(unreachable)

    def _advertise_ports(self, intent: pihole_state.PiholeIntent) -> None:
        """Tell Juju which ports this intent serves.

        NTP and DHCP only when enabled. See ADR-0006 §2.8.
        """
        self.unit.set_ports(
            *(ops.Port(proto, num) for proto, num in pihole_state.open_ports(intent))
        )

    def _report_version(self, state: pihole_state.PiholeState) -> None:
        """Show the Pi-hole version, not the charm's, in the status."""
        match state:
            case pihole_state.SnapPresent(version=str() as version):
                self.unit.set_workload_version(version)
            case pihole_state.SnapAbsent() | pihole_state.SnapPresent():
                pass
            case _ as unreachable:
                assert_never(unreachable)

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        """Report the unit's status.

        Must not mutate. Pull gates (DHCP unservable, port) are read
        before the pushed failure — first-added wins the tie. See
        ADR-0005 §2.4 and ADR-0006 §2.9.
        """
        config = self.load_config(pihole_config.PiholeConfig, errors="blocked")
        # Establish the snap's presence first — snap-check cannot run
        # without it, and an absent snap is Maintenance, not Blocked.
        match _intent_from(self._read_password(), config):
            case pihole_state.NoIntentYet():
                # No intent yet, so no pull gate can fire — the pushed
                # failure is the only truth available.
                if self._reconcile_failure is not None:
                    event.add_status(self._reconcile_failure)
                    return
                event.add_status(ops.MaintenanceStatus("generating the admin password"))
                return
            case pihole_state.PiholeIntent() as intent:
                status, state = _machine_status(self._pihole, intent.admin_password)
            case _ as unreachable:
                assert_never(unreachable)

        # Re-derived here, not pushed from _reconcile: actions run
        # this handler without a reconcile. See ADR-0005 §2.5.
        if isinstance(state, pihole_state.SnapPresent):
            if pihole_state.dhcp_unservable(state, intent):
                event.add_status(
                    ops.BlockedStatus(pihole_state.dhcp_unservable_reason(state, intent))
                )
            # Enabling DHCP into an occupied 67/udp crash-loops FTL
            # (snap-constraints §4.4). First-added wins the tie.
            if pihole_state.dhcp_port_blocked(state, intent):
                event.add_status(ops.BlockedStatus(pihole_state.dhcp_port_blocked_reason(state)))

        # Read after the pull gates: first-added wins the tie.
        # See ADR-0005 §2.4.
        if self._reconcile_failure is not None:
            event.add_status(self._reconcile_failure)
            return

        # Added BEFORE snap-check: first-added wins the tie, so a
        # charm-authored Blocked must not lose to a diagnostic banner.
        event.add_status(status)
        if isinstance(status, ops.MaintenanceStatus):
            return

        # snap-check exit codes: 0 healthy, 1 config error, 2 runtime
        # error. See snap-constraints §7.3.
        try:
            result = self._pihole.snap_check()
        except pihole.PiholeError as err:
            event.add_status(ops.BlockedStatus(str(err)))
            return
        match result:
            case pihole_state.SnapCheckOk():
                pass
            case pihole_state.SnapCheckConfigError(output=output):
                # The first line is always a banner; failures live in
                # [FAIL] lines further down.
                fails = [ln for ln in output.splitlines() if "[FAIL]" in ln]
                detail = " | ".join(fails) if fails else "snap-check exit 1"
                event.add_status(
                    ops.BlockedStatus(f"{detail}; run the snap-check action for the full output")
                )
            case pihole_state.SnapCheckRuntimeError(output=output):
                event.add_status(
                    ops.BlockedStatus(
                        f"port conflict detected by snap-check: {output}; "
                        "run the free-port-53 action"
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)

    def _on_remove(self, _: ops.RemoveEvent) -> None:
        """Return the machine to a usable state before the unit goes.

        A failure is re-raised: nothing left to converge afterwards.
        See ADR-0005 §2.9.
        """
        logger.info("Removing: restoring the systemd-resolved stub listener.")
        try:
            resolved.restore()
        except resolved.ResolvedError as err:
            logger.error("Could not restore host DNS: %s", err)
            raise

    def _on_get_admin_password(self, event: ops.ActionEvent) -> None:
        """Return the admin UI password from the charm-owned secret."""
        password = self._read_password()
        if password is None:
            event.fail(
                "no admin password has been generated yet; "
                "wait for the unit to reach active/idle and try again"
            )
            return
        event.set_results({ADMIN_PASSWORD_FIELD: password})

    def _on_rotate_admin_password(self, event: ops.ActionEvent) -> None:
        """Generate a new admin password, store it, and apply it.

        Takes no parameters. See ADR-0007 §4.4.
        """
        if not self.unit.is_leader():
            event.fail("only the leader can rotate the admin password; run it on the leader unit")
            return

        password = secrets.token_urlsafe(ADMIN_PASSWORD_BYTES)
        try:
            # A fresh random value is the entire point of rotating.
            self._store_password(password)  # databag-order: ignore
            self._pihole.set_password(password)
        except (ops.SecretNotFoundError, *WORKLOAD_ERRORS) as err:
            event.fail(f"the password was not rotated: {err}")
            return

        problem = _password_problem(self._pihole.admin_password_state(password))
        if problem is not None:
            event.fail(f"the new password was written but not confirmed: {problem}")
            return
        event.set_results({"result": "the admin UI password has been rotated"})

    def _on_snap_check(self, event: ops.ActionEvent) -> None:
        """Run snap-check and return its outcome verbatim."""
        try:
            result = self._pihole.snap_check()
        except pihole.PiholeError as err:
            event.fail(str(err))
            return
        match result:
            case pihole_state.SnapCheckOk():
                event.set_results({"exit-code": "0", "output": "all checks passed"})
            case pihole_state.SnapCheckConfigError(output=output):
                event.set_results({"exit-code": "1", "output": output})
            case pihole_state.SnapCheckRuntimeError(output=output):
                event.set_results({"exit-code": "2", "output": output})
            case _ as unreachable:
                assert_never(unreachable)

    def _on_update_gravity(self, event: ops.ActionEvent) -> None:
        """Refresh blocklists now instead of waiting for the timer.

        ``force`` (default false) passes ``--force`` to ``pihole -g``
        for a full rebuild.
        """
        params = event.load_params(pihole_config.UpdateGravityParams, errors="fail")
        try:
            self._pihole.update_gravity(force=params.force)
        except pihole.PiholeError as err:
            event.fail(f"gravity update failed: {err}")
            return
        event.set_results({"result": "gravity update completed"})

    def _on_free_port_53(self, event: ops.ActionEvent) -> None:
        """Re-run the port-53 freeing procedure and verify it worked.

        Idempotent. Runs snap-check afterwards: code 2 means the port
        is still occupied.
        """
        try:
            resolved.disable_stub_listener()
        except resolved.ResolvedError as err:
            event.fail(str(err))
            return
        try:
            result = self._pihole.snap_check()
        except pihole.PiholeError as err:
            event.fail(str(err))
            return
        match result:
            case pihole_state.SnapCheckRuntimeError(output=output):
                event.fail(
                    f"port 53 is still not free after writing the resolved drop-in; "
                    f"snap-check says: {output}"
                )
            case pihole_state.SnapCheckOk() | pihole_state.SnapCheckConfigError():
                event.set_results({"result": "port 53 has been freed for Pi-hole"})
            case _ as unreachable:
                assert_never(unreachable)

    def _ensure_password(self) -> str | None:
        """Return the admin password, minting one if none exists yet.

        Minted once; a follower returns the leader's or None.
        """
        existing = self._read_password()
        if existing is not None:
            return existing
        if not self.unit.is_leader():
            return None

        password = secrets.token_urlsafe(ADMIN_PASSWORD_BYTES)
        self._store_password(password)  # databag-order: ignore
        return password

    def _read_password(self) -> str | None:
        """Read the charm-owned secret by label, never by stored ID."""
        try:
            secret = self.model.get_secret(label=ADMIN_PASSWORD_LABEL)
            # peek_content always returns the latest revision.
            return secret.peek_content().get(ADMIN_PASSWORD_FIELD)
        except ops.SecretNotFoundError:
            return None

    def _store_password(self, password: str) -> None:
        """Write the password to an app-owned secret, and read it back.

        Raises:
            pihole.PiholeError: The password is not readable back
                afterwards.
        """
        content = {ADMIN_PASSWORD_FIELD: password}
        try:
            self.model.get_secret(label=ADMIN_PASSWORD_LABEL).set_content(content)
        except ops.SecretNotFoundError:
            # App-owned, so every unit can read it and it survives unit
            # replacement. Only the leader may create it.
            self.app.add_secret(content, label=ADMIN_PASSWORD_LABEL)

        if self._read_password() != password:
            raise pihole.PiholeError(
                operation="storing the admin password in a Juju secret",
                expected="the new password to be readable",
                actual="the secret still holds something else",
                remedy="check `juju secrets` and that this unit is the leader",
            )


def _intent_from(
    password: str | None,
    config: pihole_config.PiholeConfig,
) -> pihole_state.DeclaredIntent:
    """Name what the charm can declare, given the password it holds.

    `NoIntentYet` without a password — a follower waiting on the
    leader. See ADR-0005 §2.6.
    """
    if password is None:
        return pihole_state.NoIntentYet()
    return pihole_state.PiholeIntent(admin_password=password, **config.intent_fields())


def _machine_status(
    facts: pihole_state.PiholeFacts,
    admin_password: str,
) -> tuple[ops.StatusBase, pihole_state.PiholeState]:
    """Read the machine once and map what it finds onto one status.

    Returns the state alongside the status so the caller can re-derive
    the DHCP gates.
    """
    match pihole_state.fetch(facts, admin_password):
        case pihole_state.SnapAbsent() as absent:
            return (
                ops.MaintenanceStatus(f"installing the {pihole.SNAP_NAME} snap"),
                absent,
            )
        case pihole_state.SnapPresent() as state:
            return _installed_status(state), state
        case _ as unreachable:
            assert_never(unreachable)


def _installed_status(state: pihole_state.SnapPresent) -> ops.StatusBase:
    """Map an installed machine's facts onto one status.

    `Blocked` is reserved for what a human can act on. See ADR-0005
    §2.8.
    """
    problem = _password_problem(state.admin_password)
    if problem is not None and state.ftl_active:
        return ops.BlockedStatus(problem)
    if not (state.ftl_enabled and state.ftl_active):
        return ops.MaintenanceStatus("starting the Pi-hole FTL daemon")
    if not state.api_ready:
        return ops.BlockedStatus(
            "FTL is running but its HTTP API on port 80 is not answering; "
            "check the webserver lines in FTL.log on the machine"
        )
    return ops.ActiveStatus()


def _password_problem(password: pihole_state.AdminPasswordState) -> str | None:
    """Name what is wrong with the admin password, if anything."""
    match password:
        case pihole_state.PasswordUnset():
            return (
                "no admin password is set, so the Pi-hole config API accepts "
                "unauthenticated writes from the network; run the "
                "rotate-admin-password action"
            )
        case pihole_state.PasswordRejected():
            return (
                "Pi-hole rejects the password this charm holds; run the "
                "rotate-admin-password action"
            )
        case pihole_state.PasswordAccepted() | pihole_state.PasswordUnverified():
            return None
        case _ as unreachable:
            assert_never(unreachable)


if __name__ == "__main__":  # pragma: nocover
    ops.main(PiholeCharm)
