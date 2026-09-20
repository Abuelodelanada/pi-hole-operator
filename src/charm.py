#!/usr/bin/env python3

"""Charmed operator for Pi-hole v6 on Ubuntu machines.

Every deferrable event routes to a single `_reconcile`, which converges
the machine toward the operator's declared intent. Only events that
cannot be deferred get a handler of their own: `collect_unit_status`,
`remove`, and the two actions.

This module owns `ops` and nothing else — no `charmlibs.*`, no
`subprocess`, no file writes — which is what keeps it unit-testable.
The reconciler is three stages: `fetch` reads the machine once,
`compute` decides purely, `_apply` acts dumbly. See rule 2 and ADR-0003.

Stage 2 adds config-driven intent, the FTL config API path, and
conditional ports. See ADR-0004 section 5 and ADR-0006 section 2.1.
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
        framework.observe(self.on.collect_unit_status, self._on_collect_status)
        framework.observe(self.on.remove, self._on_remove)
        framework.observe(
            self.on["get-admin-password"].action,
            self._on_get_admin_password,
        )
        framework.observe(
            self.on["rotate-admin-password"].action,
            self._on_rotate_admin_password,
        )

    # -- The reconciler. -----------------------------------------------

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the machine toward the declared intent.

        Every step must be safe to run twice or never. See ADR-0003
        section 2.5 on why ordering lives in `compute`'s sequence.
        """
        # No wrapper here: errors="blocked" answers invalid *values*
        # with BlockedStatus and a clean exit-0 abort. That abort is an
        # Exception subclass, so catching broadly here would swallow it
        # and the hook would keep converging on unvalidated config.
        config = self.load_config(pihole_config.PiholeConfig, errors="blocked")

        # Everything that can fail is inside the try: error state
        # needs `--force`, which skips the `remove` handler (ADR-0005
        # section 2.9).
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
            # operation silently failed. See ADR-0005 section 2.4.
            logger.error("reconcile failed: %s", err)
            self._reconcile_failure = ops.BlockedStatus(str(err))

    def _apply(self, outcome: pihole_state.PiholeOutcome) -> None:
        """Perform one decided outcome. Deliberately stupid.

        Exhaustive by construction: `tox -e static` fails if a new
        `PiholeOutcome` member has no branch here. See ADR-0003
        section 2.5.
        """
        logger.info("applying %s.", outcome)
        match outcome:
            case pihole_state.ReleasePort53():
                resolved.disable_stub_listener()
            case pihole_state.InstallSnap():
                self._pihole.install()
            case pihole_state.HoldSnapRefresh():
                self._pihole.hold_refresh()
            case pihole_state.SetNtpServer(active=active):
                self._pihole.set_ntp_server(active=active)
            case pihole_state.SetAdminPassword(password=password):
                self._pihole.set_password(password)
            case pihole_state.StartFtl():
                self._pihole.start(enable=True)
            case pihole_state.AwaitApi(timeout=timeout):
                self._pihole.await_api(timeout)
            case pihole_state.SetFtlConfig(config=config, password=password):
                self._pihole.apply_ftl_config(password=password, config=dict(config))
            case pihole_state.Noop():
                logger.debug("converged: nothing to do.")
            case _ as unreachable:
                assert_never(unreachable)

    def _advertise_ports(self, intent: pihole_state.PiholeIntent) -> None:
        """Tell Juju which ports this intent serves, NTP's when enabled.

        Declares, it does not open: FTL binds these itself, and
        `set_ports` only records them on the unit. Whether anything
        acts on the record is the provider's business — the LXD
        provider has no firewaller at all, and `open-port` does
        nothing until the application is exposed (ADR-0006 §2.8).

        The pure core answers protocol-and-number pairs because it
        cannot import `ops`; the conversion to `ops.Port` lives here,
        with the rest of the model-facing code.
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

    # -- Status. -------------------------------------------------------

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        """Report the unit's status.

        Must not mutate anything, so it reads the password rather
        than generating one. The pushed failure is read first — see
        ADR-0005 section 2.4.
        """
        if self._reconcile_failure is not None:
            event.add_status(self._reconcile_failure)
            return

        match _intent_from(self._read_password()):
            case pihole_state.NoIntentYet():
                event.add_status(ops.MaintenanceStatus("generating the admin password"))
            case pihole_state.PiholeIntent() as intent:
                event.add_status(_machine_status(self._pihole, intent))
            case _ as unreachable:
                assert_never(unreachable)

    # -- Non-deferrable handlers. --------------------------------------

    def _on_remove(self, _: ops.RemoveEvent) -> None:
        """Return the machine to a usable state before the unit goes.

        The snap cannot do this itself: strict confinement stops it from
        touching `/etc/systemd`, so this handler is the only thing
        between `juju remove-application` and a machine with no DNS.

        A failure is logged with its remedy and then re-raised. Raising
        is right here and nowhere else in the charm, because there is
        nothing left to converge afterwards. See ADR-0005 section 2.9.
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

        Takes no parameters: see ADR-0007 section 4.4 on why a
        password argument would leak via `juju show-task`.
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

    # -- Intent, which for Stage 2 includes config. ---------------

    def _ensure_password(self) -> str | None:
        """Return the admin password, minting one if none exists yet.

        Minted only once, so later reconciles never flap it. A
        follower returns whatever the leader has stored, or None.
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
            # `peek_content` always returns the latest revision;
            # `get_content` can be served from this hook's own
            # pre-write cache.
            return secret.peek_content().get(ADMIN_PASSWORD_FIELD)
        except ops.SecretNotFoundError:
            return None

    def _store_password(self, password: str) -> None:
        """Write the password to an app-owned secret, and read it back.

        `Secret.set_content` succeeds even when it will not take effect
        — the unit errors at the *end* of the hook instead — so the
        write is verified rather than trusted (rule 6).

        Raises:
            pihole.PiholeError: The password is not readable back
                afterwards. Deliberately not `ops.SecretNotFoundError`,
                which is caught here and answered by creating the
                secret.
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
    config: pihole_config.PiholeConfig | None = None,
) -> pihole_state.DeclaredIntent:
    """Name what the charm can declare, given the password it holds.

    `NoIntentYet` without a password — a follower waiting on the
    leader to mint one. Which password reaches this function is the
    caller's choice, and it is the whole difference between the two
    call sites: `_reconcile` passes `_ensure_password()`, which mints
    on a leader; `_on_collect_status` passes `_read_password()`,
    because that handler must not mutate anything. See rule 7 and
    ADR-0005 section 2.4.

    The status handler passes no config: it needs the intent only to
    offer the password to the API oracle, and building one from
    unvalidated config there would invent values the reconciler never
    applied.
    """
    if password is None:
        return pihole_state.NoIntentYet()
    if config is None:
        return pihole_state.PiholeIntent(admin_password=password)
    return pihole_state.PiholeIntent(admin_password=password, **config.intent_fields())


def _machine_status(
    facts: pihole_state.PiholeFacts,
    intent: pihole_state.PiholeIntent,
) -> ops.StatusBase:
    """Read the machine once and map what it finds onto one status."""
    match pihole_state.fetch(facts, intent.admin_password):
        case pihole_state.SnapAbsent():
            return ops.MaintenanceStatus(f"installing the {pihole.SNAP_NAME} snap")
        case pihole_state.SnapPresent() as state:
            return _installed_status(state)
        case _ as unreachable:
            assert_never(unreachable)


def _installed_status(state: pihole_state.SnapPresent) -> ops.StatusBase:
    """Map an installed machine's facts onto one status.

    `Blocked` is reserved for what a human can act on and the charm
    can name — a spurious Blocked masks everything else. See ADR-0005
    section 2.8.
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
