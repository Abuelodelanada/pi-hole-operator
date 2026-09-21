"""Pydantic model for the charm's config options and action params.

Parsed by `ops` via `self.load_config(PiholeConfig, errors="blocked")`
and `event.load_params(UpdateGravityParams, errors="fail")`.
Imports pydantic and stdlib only — no `ops` import, so it is testable
without a harness. See ADR-0006 section 2.1.
"""

from enum import StrEnum
from typing import TypedDict

import pydantic


class ListeningMode(StrEnum):
    """The `dns.listeningMode` values this charm can honour.

    Narrower than FTL's five: `SINGLE` and `BIND` need `dns.interface`,
    which the charm does not manage. See ADR-0006 section 2.11 for why
    that is a silent failure, and why widening later is safe.
    """

    LOCAL = "LOCAL"
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


class IntentFields(TypedDict):
    """The `PiholeIntent` fields a config value can set.

    Mirrors `PiholeIntent` minus `admin_password`, which the charm owns
    rather than the operator. Keeping it a `TypedDict` rather than a
    plain dict is what lets pyright check the seam.
    """

    upstream_dns: tuple[str, ...] | None
    listening_mode: str | None
    blocking_enabled: bool
    dnssec_enabled: bool
    ntp_server_enabled: bool
    gravity_schedule: str | None


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
            "FTL listening mode: LOCAL, ALL, or NONE. Unset means unmanaged. "
            "FTL's SINGLE and BIND need an interface the charm does not manage."
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
    gravity_schedule: str | None = pydantic.Field(
        default=None,
        description=(
            "A systemd OnCalendar expression for the weekly gravity update timer. "
            "Unset means the charm does not manage the schedule (the snap's randomised "
            "default applies). Example: 'Sun *-*-* 03:00'."
        ),
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

    @pydantic.field_validator("gravity_schedule", mode="before")
    @classmethod
    def _normalise_gravity_schedule(cls, value: str | None) -> str | None:
        """Accept empty string as None."""
        if value is None or value == "":
            return None
        return value

    def intent_fields(self) -> IntentFields:
        """Return the kwargs for PiholeIntent, minus admin_password.

        Empty upstream_dns and None listening_mode are mapped to None
        so the charm treats them as unmanaged.

        The `TypedDict` return is what makes the config-to-intent seam
        checkable: `PiholeIntent(admin_password=..., **fields)` is
        verified key by key, so renaming a field on either side fails
        `tox -e static` instead of every hook at runtime.
        """
        upstreams = _parse_upstream_csv(self.upstream_dns)
        return {
            "upstream_dns": upstreams if upstreams else None,
            "listening_mode": self.dns_listening_mode.value if self.dns_listening_mode else None,
            "blocking_enabled": self.blocking_enabled,
            "dnssec_enabled": self.dnssec_enabled,
            "ntp_server_enabled": self.ntp_server_enabled,
            "gravity_schedule": self.gravity_schedule,
        }


class UpdateGravityParams(pydantic.BaseModel):
    """Parameters for the update-gravity action."""

    model_config = pydantic.ConfigDict(frozen=True)

    force: bool = pydantic.Field(
        default=False,
        description="Rebuild gravity.db from scratch rather than incrementally.",
    )
