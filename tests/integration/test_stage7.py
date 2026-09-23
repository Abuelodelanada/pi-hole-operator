"""Stage 7.b integration tests: DHCP server mode.

The servable test is gated behind ``@pytest.mark.dhcp`` because port 67
is likely occupied by LXD's ``lxdbr0`` dnsmasq, and enabling DHCP on a
conflicting port crash-loops the daemon. A plain ``tox -e integration``
excludes it; run ``tox -e integration -- -m dhcp`` to include it. The
unservable test is ungated: its pool is in a documentation range that
cannot match, so DHCP never enables and there is no crash-loop risk.
"""

import jubilant
import pytest

from tests.integration.conftest import APP_NAME, DEPLOY_TIMEOUT, settled


def test_dhcp_unservable_pool_blocks(deployed: jubilant.Juju):
    """Deploy with a pool in a documentation range that cannot match.

    The pool is in 192.0.2.0/24 (TEST-NET-1, RFC 5737), which no
    real machine has an address in. The unit goes Blocked with the
    unservable message. DHCP never enables, so no crash-loop risk.
    """
    # GIVEN a pool in a documentation range that no machine address
    # can fall inside
    deployed.config(
        APP_NAME,
        {
            "dhcp-enabled": True,
            "dns-listening-mode": "ALL",
            "dhcp-range-start": "192.0.2.10",
            "dhcp-range-end": "192.0.2.50",
            "dhcp-router": "192.0.2.1",
            "dhcp-netmask": "255.255.255.0",
        },
    )
    try:
        # WHEN the charm converges on that intent
        # THEN the unit is Blocked with the unservable reason — the
        # discriminating clause, not the shared "cannot be served"
        # prefix that both reason branches carry
        deployed.wait(
            lambda status: any(
                "no machine IPv4 address falls inside the pool's subnet"
                in u.workload_status.message
                for u in status.apps[APP_NAME].units.values()
            ),
            timeout=120,
        )
    finally:
        # Restore: `deployed` is module-scoped, so a Blocked unit left
        # behind would mask the servable test's Active wait.
        deployed.config(APP_NAME, {"dhcp-enabled": False})


@pytest.mark.dhcp
def test_dhcp_enabled_servable_reaches_active(deployed: jubilant.Juju):
    """Discover the unit's address, build a pool in its subnet.

    Requires port 67 free — excluded from the default integration run.
    """
    # GIVEN the unit's IPv4 address, discovered via exec
    result = deployed.exec("hostname -I", unit=f"{APP_NAME}/0")
    addrs = result.stdout.strip().split()
    ipv4_addrs = [a for a in addrs if "." in a and ":" not in a and not a.startswith("127.")]
    if not ipv4_addrs:
        pytest.skip("unit has no IPv4 address; cannot build a DHCP pool")
    # `hostname -I` order is arbitrary, so prefer an address that is
    # not the subnet's router (.1): a pool built around the router
    # address is still valid, but the router itself must not be leased.
    addr = next((a for a in ipv4_addrs if not a.endswith(".1")), ipv4_addrs[0])

    # Build a pool in the unit's subnet. Use a small range that avoids
    # the unit's own address and stays within valid octets: if the host
    # part is low, lease from the top of the /24; if high, from the
    # bottom. (The unit's subnet is assumed /24, as LXD provides.)
    parts = addr.rsplit(".", 1)
    subnet_prefix = parts[0]
    host = int(parts[1])
    if host < 128:
        pool_start = f"{subnet_prefix}.200"
        pool_end = f"{subnet_prefix}.210"
    else:
        pool_start = f"{subnet_prefix}.10"
        pool_end = f"{subnet_prefix}.20"
    router = f"{subnet_prefix}.1"

    # WHEN DHCP is enabled with that pool
    deployed.config(
        APP_NAME,
        {
            "dhcp-enabled": True,
            "dns-listening-mode": "ALL",
            "dhcp-range-start": pool_start,
            "dhcp-range-end": pool_end,
            "dhcp-router": router,
            "dhcp-netmask": "255.255.255.0",
        },
    )
    deployed.wait(settled, timeout=DEPLOY_TIMEOUT)

    # THEN FTL is actually serving port 67. `juju.exec` raises on a
    # non-zero exit, so a return-code assertion would be dead code; the
    # evidence is the listener itself, named on the same line as the
    # port so `:67` cannot match a `:670x` listener.
    listening = deployed.exec("ss -lunp", unit=f"{APP_NAME}/0")
    assert any(
        "pihole-FTL" in line and "0.0.0.0:67" in line for line in listening.stdout.splitlines()
    ), f"FTL is not listening on 67/udp:\n{listening.stdout}"
