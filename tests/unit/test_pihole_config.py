"""Tests for the pydantic config model.

No mocks: the model is pure data and validation, so testing it is
construction and `==`. See ADR-0006 section 2.1.
"""

import pydantic
import pytest

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
    config = PiholeConfig(upstream_dns=None)  # type: ignore[arg-type]

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


def test_listening_mode_accepts_ftl_vocabulary():
    """Every FTL value is accepted."""
    for mode in ("LOCAL", "SINGLE", "BIND", "ALL", "NONE"):
        config = PiholeConfig(dns_listening_mode=mode)  # type: ignore[arg-type]
        assert config.dns_listening_mode == ListeningMode(mode)


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
        PiholeConfig(dns_listening_mode="INVALID")  # type: ignore[arg-type]


def test_listening_mode_empty_string_yields_none():
    # GIVEN an empty string for listening_mode
    config = PiholeConfig(dns_listening_mode="")  # type: ignore[arg-type]

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
        config.blocking_enabled = False  # type: ignore[misc]
