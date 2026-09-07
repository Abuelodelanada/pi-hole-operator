"""Pydantic model for the charm's config options.

Parsed by `ops` via `self.load_config(PiholeConfig, errors="blocked")`.
Imports pydantic and stdlib only — no `ops` import, so it is testable
without a harness. See ADR-0006 section 2.1.
"""

from collections.abc import Mapping
from enum import StrEnum

import pydantic


class ListeningMode(StrEnum):
    """FTL's `dns.listeningMode` vocabulary.

    See ADR-0006 section 2.1 and snap-constraints section 4.1.
    """

    LOCAL = "LOCAL"
    SINGLE = "SINGLE"
    BIND = "BIND"
    ALL = "ALL"
    NONE = "NONE"


def _parse_upstream_csv(value: str) -> tuple[str, ...]:
    """Split a CSV string into a tuple of stripped, non-empty entries.

    An empty string yields an empty tuple, which the charm maps to
    None/unmanaged. See ADR-0006 section 2.4.
    """
    if not value.strip():
        return ()
    return tuple(entry for raw in value.split(",") if (entry := raw.strip()))


class PiholeConfig(pydantic.BaseModel):
    """Schema for the charm's config options.

    Kebab-case Juju names map to snake_case automatically via ops.
    Defaults match FTL's own — except `ntp-server-enabled`, which the
    charm diverges to false (ADR-0006 section 2.3).
    """

    model_config = pydantic.ConfigDict(frozen=True)

    upstream_dns: str = pydantic.Field(
        default="",
        description=(
            "Comma-separated upstream DNS resolvers (e.g. 1.1.1.1,9.9.9.9). Empty means unmanaged."
        ),
    )
    dns_listening_mode: ListeningMode | None = pydantic.Field(
        default=None,
        description=(
            "FTL listening mode: LOCAL, SINGLE, BIND, ALL, or NONE. Unset means unmanaged."
        ),
    )
    blocking_enabled: bool = pydantic.Field(
        default=True,
        description="Whether DNS-based ad blocking is active.",
    )
    dnssec_enabled: bool = pydantic.Field(
        default=False,
        description="Whether DNSSEC validation is enabled.",
    )
    ntp_server_enabled: bool = pydantic.Field(
        default=False,
        description="Whether the FTL NTP server on 123/udp is enabled. Charm default is false.",
    )

    @pydantic.field_validator("upstream_dns", mode="before")
    @classmethod
    def _normalise_upstream(cls, value: str | None) -> str:
        """Accept None as empty, normalise whitespace."""
        if value is None:
            return ""
        return value.strip()

    @pydantic.field_validator("dns_listening_mode", mode="before")
    @classmethod
    def _normalise_listening_mode(cls, value: str | None) -> str | None:
        """Accept empty string as None."""
        if value is None or value == "":
            return None
        return value

    def intent_fields(self) -> Mapping[str, object]:
        """Return the kwargs for PiholeIntent, minus admin_password.

        Empty upstream_dns and None listening_mode are mapped to None
        so the charm treats them as unmanaged.
        """
        upstreams = _parse_upstream_csv(self.upstream_dns)
        return {
            "upstream_dns": upstreams if upstreams else None,
            "listening_mode": self.dns_listening_mode.value if self.dns_listening_mode else None,
            "blocking_enabled": self.blocking_enabled,
            "dnssec_enabled": self.dnssec_enabled,
            "ntp_server_enabled": self.ntp_server_enabled,
        }
