"""Tests for the pydantic config model.

No mocks: the model is pure data and validation, so testing it is
construction and `==`. See ADR-0006 section 2.1.
"""

from typing import cast

import pydantic
import pytest

import pihole_state
from pihole_config import ListeningMode, PiholeConfig


def test_defaults_match_the_adr():
    """Every default is the one the ADR specifies."""
    config = PiholeConfig()
    assert config.upstream_dns == ""
    assert config.dns_listening_mode is None
    assert config.blocking_enabled is True
    assert config.dnssec_enabled is False
    assert config.ntp_server_enabled is False


def test_upstream_dns_parses_csv():
    # GIVEN an upstream-dns with spaces and commas
    config = PiholeConfig(upstream_dns="1.1.1.1, 9.9.9.9 , 8.8.8.8")

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN the result is a tuple of stripped entries
    assert fields["upstream_dns"] == ("1.1.1.1", "9.9.9.9", "8.8.8.8")


def test_empty_upstream_dns_maps_to_none():
    # GIVEN an empty upstream-dns
    config = PiholeConfig(upstream_dns="")

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN upstream_dns is None (unmanaged)
    assert fields["upstream_dns"] is None


def test_whitespace_only_upstream_dns_maps_to_none():
    # GIVEN a whitespace-only upstream-dns
    config = PiholeConfig(upstream_dns="   ")

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN upstream_dns is None (unmanaged)
    assert fields["upstream_dns"] is None


def test_none_upstream_dns_normalises_to_empty():
    # GIVEN None as upstream_dns (Juju may send None for unset)
    config = PiholeConfig.model_validate({"upstream_dns": None})

    # WHEN the model is constructed
    # THEN it normalises to empty string
    assert config.upstream_dns == ""


def test_comma_only_upstream_dns_yields_empty_tuple():
    # GIVEN an upstream-dns with only commas and spaces
    config = PiholeConfig(upstream_dns=",  ,")

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN upstream_dns is None (empty tuple maps to None)
    assert fields["upstream_dns"] is None


def test_listening_mode_accepts_the_modes_the_charm_can_honour():
    # GIVEN each mode the charm offers
    # WHEN it is parsed
    # THEN it is accepted
    for mode in ("LOCAL", "ALL", "NONE"):
        config = PiholeConfig.model_validate({"dns_listening_mode": mode})
        assert config.dns_listening_mode == ListeningMode(mode)


def test_listening_mode_rejects_the_modes_that_need_an_interface():
    # GIVEN FTL's SINGLE and BIND, which need `dns.interface`
    # WHEN either is offered
    # THEN it is refused, rather than accepted and silently unable to
    # work. FTL in SINGLE with no interface can stop answering DNS
    # while the charm's readiness gate still reports Active.
    for mode in ("SINGLE", "BIND"):
        with pytest.raises(pydantic.ValidationError):
            PiholeConfig.model_validate({"dns_listening_mode": mode})


def test_listening_mode_accepts_none():
    # GIVEN no listening mode
    config = PiholeConfig(dns_listening_mode=None)

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN listening_mode is None (unmanaged)
    assert fields["listening_mode"] is None


def test_listening_mode_rejects_invalid():
    # GIVEN an invalid listening mode
    # WHEN the model is constructed
    # THEN pydantic rejects it
    with pytest.raises(pydantic.ValidationError):
        PiholeConfig.model_validate({"dns_listening_mode": "INVALID"})


def test_listening_mode_empty_string_yields_none():
    # GIVEN an empty string for listening_mode
    config = PiholeConfig.model_validate({"dns_listening_mode": ""})

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN listening_mode is None
    assert fields["listening_mode"] is None


def test_blocking_enabled_defaults():
    config = PiholeConfig()
    assert config.blocking_enabled is True
    assert dict(config.intent_fields())["blocking_enabled"] is True


def test_blocking_disabled():
    config = PiholeConfig(blocking_enabled=False)
    assert dict(config.intent_fields())["blocking_enabled"] is False


def test_dnssec_enabled_defaults():
    config = PiholeConfig()
    assert config.dnssec_enabled is False
    assert dict(config.intent_fields())["dnssec_enabled"] is False


def test_ntp_server_enabled_defaults():
    config = PiholeConfig()
    assert config.ntp_server_enabled is False
    assert dict(config.intent_fields())["ntp_server_enabled"] is False


def test_intent_fields_does_not_expose_admin_password():
    """intent_fields is for PiholeIntent kwargs, not admin."""
    config = PiholeConfig()
    fields = dict(config.intent_fields())
    assert "admin_password" not in fields
    assert "snap_channel" not in fields
    assert "snap_revision" not in fields


def test_model_is_frozen():
    """PiholeConfig is immutable, like the rest of the pure core."""
    config = PiholeConfig()
    with pytest.raises((ValueError, TypeError, AttributeError)):
        config.blocking_enabled = False


def test_gravity_schedule_normalised():
    # GIVEN a non-empty gravity-schedule string
    config = PiholeConfig.model_validate({"gravity_schedule": "Sun *-*-* 03:00"})

    # WHEN the model is constructed
    # THEN the value passes through the normaliser unchanged
    assert config.gravity_schedule == "Sun *-*-* 03:00"
    assert dict(config.intent_fields())["gravity_schedule"] == "Sun *-*-* 03:00"


def test_gravity_schedule_empty_maps_to_none():
    # GIVEN an empty gravity-schedule
    config = PiholeConfig.model_validate({"gravity_schedule": ""})

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN gravity_schedule is None (unmanaged)
    assert fields["gravity_schedule"] is None


def test_gravity_schedule_none_maps_to_none():
    # GIVEN None as gravity_schedule
    config = PiholeConfig.model_validate({"gravity_schedule": None})

    # WHEN intent_fields is called
    fields = dict(config.intent_fields())

    # THEN gravity_schedule is None (unmanaged)
    assert fields["gravity_schedule"] is None


# -- Stage 7.b: DHCP config validation. --------------------------------


def test_dhcp_disabled_defaults():
    """When dhcp-enabled is false, the pool fields are ignored."""
    # GIVEN the default config (dhcp-enabled false)
    config = PiholeConfig()
    # THEN the pool fields are empty and the intent carries no pool
    assert config.dhcp_enabled is False
    assert config.dhcp_range_start == ""
    assert config.dhcp_range_end == ""
    assert config.dhcp_router == ""
    assert config.dhcp_netmask == ""
    fields = dict(config.intent_fields())
    assert fields["dhcp_enabled"] is False
    assert fields["dhcp_pool"] is None


def test_dhcp_enabled_with_complete_valid_pool():
    """Enabled with a complete, valid pool passes validation."""
    # GIVEN a complete pool with dns-listening-mode=ALL
    config = PiholeConfig.model_validate(
        {
            "dhcp_enabled": True,
            "dns_listening_mode": "ALL",
            "dhcp_range_start": "192.168.1.10",
            "dhcp_range_end": "192.168.1.50",
            "dhcp_router": "192.168.1.1",
            "dhcp_netmask": "255.255.255.0",
        }
    )
    # THEN it validates and the intent carries the pool
    assert config.dhcp_enabled is True
    fields = dict(config.intent_fields())
    assert fields["dhcp_enabled"] is True
    pool = cast(pihole_state.DhcpPool, fields["dhcp_pool"])
    assert pool.start == "192.168.1.10"
    assert pool.end == "192.168.1.50"
    assert pool.router == "192.168.1.1"
    assert pool.netmask == "255.255.255.0"


def test_dhcp_enabled_without_all_listening_mode_raises():
    """Enabled without dns-listening-mode=ALL raises.

    FTL's default LOCAL binds localhost only, so every lease would
    point at a resolver that refuses the client.
    """
    # GIVEN a complete pool but no dns-listening-mode
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "255.255.255.0",
            }
        )
    # THEN validation fails naming the remedy
    assert "dns-listening-mode=ALL" in str(exc_info.value)


def test_dhcp_enabled_with_local_listening_mode_raises():
    """Enabled with dns-listening-mode=LOCAL raises."""
    # GIVEN a complete pool but dns-listening-mode=LOCAL
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "LOCAL",
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "255.255.255.0",
            }
        )
    # THEN validation fails naming the remedy
    assert "dns-listening-mode=ALL" in str(exc_info.value)


@pytest.mark.parametrize(
    "missing_field",
    ["dhcp-range-start", "dhcp-range-end", "dhcp-router", "dhcp-netmask"],
)
def test_dhcp_enabled_missing_field_raises(missing_field: str):
    """Enabled with a missing pool field raises ValueError."""
    # GIVEN a complete pool with one field emptied
    fields = {
        "dhcp_enabled": True,
        "dns_listening_mode": "ALL",
        "dhcp_range_start": "192.168.1.10",
        "dhcp_range_end": "192.168.1.50",
        "dhcp_router": "192.168.1.1",
        "dhcp_netmask": "255.255.255.0",
    }
    fields[missing_field.replace("-", "_")] = ""
    # WHEN the config is validated
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(fields)
    # THEN the message names the missing option in its Juju spelling
    assert missing_field in str(exc_info.value)


def test_dhcp_invalid_ipv4_raises():
    """An invalid IPv4 address in a pool field raises."""
    # GIVEN a pool whose start is not an IP address
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "not-an-ip",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "255.255.255.0",
            }
        )
    # THEN validation fails naming the offending field
    assert "not a valid IPv4 address" in str(exc_info.value)


def test_dhcp_non_contiguous_netmask_raises():
    """A non-contiguous netmask (e.g. 255.0.255.0) raises."""
    # GIVEN a non-contiguous netmask
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "255.0.255.0",
            }
        )
    # THEN validation fails
    assert "not a contiguous netmask" in str(exc_info.value)


def test_dhcp_prefix_length_netmask_raises():
    """A prefix-length netmask (e.g. 24) raises; use dotted-quad."""
    # GIVEN a prefix-length netmask
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "24",
            }
        )
    # THEN validation fails
    assert "not a valid netmask" in str(exc_info.value)


def test_dhcp_zero_netmask_raises():
    """A 0.0.0.0 netmask raises — a /0 pool would disable the gate."""
    # GIVEN a 0.0.0.0 netmask
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "0.0.0.0",
            }
        )
    # THEN validation fails
    assert "0.0.0.0" in str(exc_info.value)


def test_dhcp_host_mask_netmask_raises():
    """A host mask (e.g. 0.0.0.255) raises.

    ipaddress accepts 0.0.0.255 and normalizes it to /24, but FTL
    would receive the raw string — the shape must be rejected before
    it can be PATCHed.
    """
    # GIVEN a host mask that ipaddress would silently normalize
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "0.0.0.255",
            }
        )
    # THEN validation fails
    assert "not a contiguous netmask" in str(exc_info.value)


def test_dhcp_router_outside_subnet_raises():
    """A router outside the pool's subnet raises."""
    # GIVEN a router in a different subnet than the pool
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.1.50",
                "dhcp_router": "10.0.0.1",
                "dhcp_netmask": "255.255.255.0",
            }
        )
    # THEN validation fails
    assert "not inside the pool's subnet" in str(exc_info.value)


def test_dhcp_start_after_end_raises():
    """Start > end raises."""
    # GIVEN a pool whose start is after its end
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "192.168.1.50",
                "dhcp_range_end": "192.168.1.10",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "255.255.255.0",
            }
        )
    # THEN validation fails
    assert "must be <=" in str(exc_info.value)


def test_dhcp_different_subnets_raises():
    """Start and end in different subnets raises."""
    # GIVEN a pool whose start and end are in different subnets
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PiholeConfig.model_validate(
            {
                "dhcp_enabled": True,
                "dns_listening_mode": "ALL",
                "dhcp_range_start": "192.168.1.10",
                "dhcp_range_end": "192.168.2.50",
                "dhcp_router": "192.168.1.1",
                "dhcp_netmask": "255.255.255.0",
            }
        )
    # THEN validation fails
    assert "not in the same subnet" in str(exc_info.value)


def test_dhcp_disabled_with_partial_pool_passes():
    """Disabled with partial pool passes — pool is None."""
    # GIVEN dhcp-enabled false with a partial pool
    config = PiholeConfig.model_validate(
        {
            "dhcp_enabled": False,
            "dhcp_range_start": "192.168.1.10",
            "dhcp_range_end": "",
            "dhcp_router": "",
            "dhcp_netmask": "",
        }
    )
    # THEN the intent carries no pool
    fields = dict(config.intent_fields())
    assert fields["dhcp_enabled"] is False
    assert fields["dhcp_pool"] is None
