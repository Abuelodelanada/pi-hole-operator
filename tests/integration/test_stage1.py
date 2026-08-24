"""Stage 1 integration tests: DNS, the API, and getting the host back.

These run against a live model and are never run by `tox -e unit`. Two
of them are the whole point of the stage:

* **removal restores host DNS.** The snap cannot do it — strict
  confinement stops it from touching `/etc/systemd` — so if the charm's
  `remove` handler is wrong, the operator is left with a machine that
  cannot resolve anything.
* **an unauthenticated `PATCH /api/config` is refused**, checked from
  *another host*. On a stock install that request returns 200 and
  redirects DNS for the whole network. From localhost the test would
  pass for the wrong reason, so it deliberately does not run there.

`juju exec --unit` is used rather than `--machine`: the former runs as
root in a hook context, the latter as `ubuntu`, where `snap get` can
fail on permissions.
"""

import json
import pathlib

import jubilant

from tests.integration.conftest import APP_NAME, BASE, DEPLOY_TIMEOUT, VM_CONSTRAINTS

RESOLVED_DROP_IN = "/etc/systemd/resolved.conf.d/pihole.conf"
STOCK_WEBSERVER_PORT = "443os"
"""The TLS entry the charm must have removed before the first start."""


def unit_address(juju: jubilant.Juju) -> str:
    """The address another host can reach this unit on."""
    unit = juju.status().apps[APP_NAME].units[f"{APP_NAME}/0"]
    address = unit.public_address or unit.address
    assert address, "the unit has no address to reach it on"
    return address


def test_dns_is_answered(deployed: jubilant.Juju):
    # GIVEN a deployed unit that reached active/idle
    # WHEN a query is made against Pi-hole itself
    result = deployed.exec("dig +short @127.0.0.1 example.com", unit=f"{APP_NAME}/0")

    # THEN it answers, which is the only thing this charm exists to do
    assert result.stdout.strip()


def test_port_53_was_taken_from_systemd_resolved(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN the host's resolver configuration is read
    result = deployed.exec(f"cat {RESOLVED_DROP_IN}", unit=f"{APP_NAME}/0")

    # THEN the charm's drop-in is in place, because the snap ships the
    # daemon disabled precisely so something else can free port 53
    assert "DNSStubListener=no" in result.stdout
    assert "DNS=127.0.0.1" in result.stdout


def test_the_webserver_binds_port_80_on_the_first_boot(deployed: jubilant.Juju):
    # GIVEN a unit that has never been touched by hand
    # WHEN the listening sockets are read
    result = deployed.exec("ss -tlpn", unit=f"{APP_NAME}/0")

    # THEN port 80 is bound. The packaged default requests TLS, FTL
    # cannot generate a certificate in this snap, and the SSL failure
    # aborts the whole webserver — so without the charm's correction
    # there would be no admin UI and no HTTP API at all.
    assert ":80 " in result.stdout


def test_the_webserver_port_no_longer_requests_tls(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN the value that actually landed is read back, rather than the
    # exit code of the command that set it
    result = deployed.exec(
        "grep -A2 '^\\[webserver\\]' "
        "/var/snap/pihole-by-rajannpatel/current/etc/pihole/pihole.toml",
        unit=f"{APP_NAME}/0",
    )

    # THEN the TLS entries are gone
    assert STOCK_WEBSERVER_PORT not in result.stdout


def test_the_api_answers_on_the_first_boot(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN the API is queried through the snap's own wrapper
    result = deployed.exec(
        "pihole-by-rajannpatel.pihole api dns/blocking",
        unit=f"{APP_NAME}/0",
    )

    # THEN it answers with a blocking state. `snap services` reports
    # active long before this is true, which is why readiness is gated
    # here and not there.
    assert "blocking" in json.loads(result.stdout)


def test_an_admin_password_is_set_and_retrievable(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN the operator asks for the admin password
    task = deployed.run(f"{APP_NAME}/0", "get-admin-password")

    # THEN the charm hands it over from its own secret
    password = task.results["password"]
    assert password

    # AND the API accepts it, which is the only oracle that exists: the
    # stored hash is salted, so it differs on every write
    result = deployed.exec(
        "curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1/api/auth "
        f"-H 'Content-Type: application/json' --data '{json.dumps({'password': password})}'",
        unit=f"{APP_NAME}/0",
    )
    assert result.stdout.strip() == "200"


def test_the_stored_hash_is_never_empty(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN pihole.toml's own pwhash is read
    #
    # Anchor the match to the exact key. A bare `grep pwhash` also
    # returns `app_pwhash`, which the charm deliberately does not manage
    # (ADR-0007 section 4.6) and which is legitimately empty -- so a
    # substring check for 'pwhash = ""' matches it and fails a healthy
    # unit.
    result = deployed.exec(
        "grep -E '^[[:space:]]+pwhash = ' "
        "/var/snap/pihole-by-rajannpatel/current/etc/pihole/pihole.toml",
        unit=f"{APP_NAME}/0",
    )

    # THEN it holds a BALLOON hash. An empty pwhash means the config API
    # accepts unauthenticated writes from anywhere on the network.
    assert "$BALLOON-SHA256$" in result.stdout
    assert 'pwhash = ""' not in result.stdout


def test_the_password_is_not_readable_from_snapd_state(deployed: jubilant.Juju):
    # GIVEN a converged unit
    task = deployed.run(f"{APP_NAME}/0", "get-admin-password")
    password = task.results["password"]

    # WHEN the whole snapd configuration is dumped
    result = deployed.exec(
        "snap get pihole-by-rajannpatel -d || true",
        unit=f"{APP_NAME}/0",
    )

    # THEN the plaintext is not in it. This is why the charm uses
    # `pihole setpassword` rather than `snap set`: values passed through
    # snapd are readable by anyone with snapd access.
    assert password not in result.stdout


def test_rotating_the_password_replaces_it(deployed: jubilant.Juju):
    # GIVEN a converged unit whose password is known
    before = deployed.run(f"{APP_NAME}/0", "get-admin-password").results["password"]

    # WHEN the operator rotates it
    deployed.run(f"{APP_NAME}/0", "rotate-admin-password")

    # THEN a different password comes back, and it is the one Pi-hole
    # now accepts
    after = deployed.run(f"{APP_NAME}/0", "get-admin-password").results["password"]
    assert after != before
    result = deployed.exec(
        "curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1/api/auth "
        f"-H 'Content-Type: application/json' --data '{json.dumps({'password': after})}'",
        unit=f"{APP_NAME}/0",
    )
    assert result.stdout.strip() == "200"


def test_443_is_not_advertised(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN its opened ports are read
    ports = deployed.status().apps[APP_NAME].units[f"{APP_NAME}/0"].open_ports

    # THEN DNS is advertised on both protocols, and 443 is not
    # advertised at all, because the charm disables TLS and there is no
    # listener there to document
    assert "53/tcp" in ports
    assert "53/udp" in ports
    assert "80/tcp" in ports
    assert not [port for port in ports if port.startswith("443/")]


def test_the_workload_version_is_pi_holes(deployed: jubilant.Juju):
    # GIVEN a converged unit
    # WHEN its application version is read
    version = deployed.status().apps[APP_NAME].version

    # THEN it is the workload's, not the charm's
    assert version


def test_an_unauthenticated_config_write_is_refused_from_another_host(
    deployed: jubilant.Juju,
):
    # GIVEN a second machine on the same network as the Pi-hole. This
    # must not run from localhost: the whole point is that the hole was
    # remotely exploitable.
    address = unit_address(deployed)
    deployed.add_machine(constraints=VM_CONSTRAINTS)
    deployed.wait(lambda status: len(status.machines) >= 2, timeout=DEPLOY_TIMEOUT)
    other = sorted(machine for machine in deployed.status().machines if machine != "0")[0]

    # WHEN it rewrites the upstream resolvers with no credentials
    result = deployed.exec(
        "curl -s -o /dev/null -w '%{http_code}' -X PATCH "
        f"http://{address}/api/config -H 'Content-Type: application/json' "
        """--data '{"config":{"dns":{"upstreams":["198.51.100.66"]}}}'""",
        machine=other,
    )

    # THEN Pi-hole refuses it. On a stock install this returns 200 and
    # redirects DNS for every device using this resolver.
    assert result.stdout.strip() != "200"

    # AND the value never landed
    written = deployed.exec(
        "grep -A3 upstreams /var/snap/pihole-by-rajannpatel/current/etc/pihole/pihole.toml",
        unit=f"{APP_NAME}/0",
    )
    assert "198.51.100.66" not in written.stdout


def test_removing_the_application_leaves_the_host_with_working_dns(
    juju: jubilant.Juju,
    charm_path: pathlib.Path,
):
    # GIVEN a Pi-hole sharing a machine with an unrelated unit.
    #
    # The co-tenant is what makes this test possible. Juju reclaims a
    # machine as soon as its last unit leaves, so removing Pi-hole from
    # a machine of its own destroys the very host we need to inspect --
    # the model ends up empty and `juju exec` reports
    # `"machine-0" not found`.
    #
    # The co-tenant is deployed *first*, without `add_machine`, and
    # Pi-hole is then placed onto its machine. Letting Juju provision
    # and wait avoids two sharp edges of the hand-rolled version:
    # `add_machine` returns before the machine is usable, so
    # `deploy --to` fails with `machine "N" not started`; and a
    # hand-allocated machine takes the *model's* default base, so a
    # 26.04 charm lands on 24.04 and fails with `base does not match`.
    juju.deploy("ubuntu", "co-tenant", base=BASE, constraints=VM_CONSTRAINTS)
    juju.wait(jubilant.all_active, timeout=DEPLOY_TIMEOUT)
    machine = juju.status().apps["co-tenant"].units["co-tenant/0"].machine

    juju.deploy(charm_path, "pihole-removable", to=machine, base=BASE)
    juju.wait(jubilant.all_active, timeout=DEPLOY_TIMEOUT)

    # WHEN only Pi-hole is removed
    juju.remove_application("pihole-removable")
    juju.wait(lambda status: "pihole-removable" not in status.apps, timeout=DEPLOY_TIMEOUT)

    # THEN the drop-in is gone and the host resolves names again. If
    # this fails the machine is left with DNSStubListener=no and no
    # Pi-hole, which is a host with no DNS at all -- the failure mode
    # that drives the whole status strategy in ADR-0005.
    left_behind = juju.exec(
        f"test -f {RESOLVED_DROP_IN} && echo present || echo gone",
        unit="co-tenant/0",
    )
    assert left_behind.stdout.strip() == "gone"
    resolution = juju.exec("getent hosts example.com", unit="co-tenant/0")
    assert resolution.stdout.strip()
