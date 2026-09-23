"""Pydantic model for the charm's config options and action params.

Parsed by `ops` via `self.load_config(PiholeConfig, errors="blocked")`
and `event.load_params(UpdateGravityParams, errors="fail")`.
Imports pydantic, stdlib, and the pure core — no `ops` import, so it
is testable without a harness. See ADR-0006 section 2.1.

Stage 7.b adds DHCP server mode: five config options with cross-field
validation that the pool is complete and valid when enabled. See
ADR-0006 §2.9.
"""

import ipaddress
from enum import StrEnum
from typing import TypedDict

import pydantic

import pihole_state


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
    dhcp_enabled: bool
    dhcp_pool: pihole_state.DhcpPool | None


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
    dhcp_enabled: bool = pydantic.Field(
        default=False,
        description=(
            "Whether the FTL DHCP server on 67/udp is enabled. "
            "Requires a complete pool (dhcp-range-start, dhcp-range-end, "
            "dhcp-router, dhcp-netmask) and dns-listening-mode=ALL, so "
            "DHCP clients can reach the resolver. The serving interface "
            "must have an address in the pool's subnet."
        ),
    )
    dhcp_range_start: str = pydantic.Field(
        default="",
        description="DHCP pool start address (IPv4, e.g. 192.168.1.10). Required when enabled.",
    )
    dhcp_range_end: str = pydantic.Field(
        default="",
        description="DHCP pool end address (IPv4, e.g. 192.168.1.50). Required when enabled.",
    )
    dhcp_router: str = pydantic.Field(
        default="",
        description="DHCP router/gateway address (IPv4, e.g. 192.168.1.1). Required when enabled.",
    )
    dhcp_netmask: str = pydantic.Field(
        default="",
        description=(
            "DHCP subnet netmask, dotted-quad only (e.g. 255.255.255.0; "
            "prefix-length forms like 24 are rejected). Required when enabled."
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

    @pydantic.model_validator(mode="after")
    def _validate_dhcp_pool(self) -> PiholeConfig:
        """Validate the DHCP pool when enabled.

        When ``dhcp_enabled`` is true, all four pool fields must be
        non-empty, valid IPv4 addresses, with a dotted-quad contiguous
        netmask, ``start <= end``, both in the same subnet, and the
        router inside that subnet. DHCP also requires
        ``dns-listening-mode=ALL``: FTL's default ``LOCAL`` binds
        localhost only, so every lease would point at a resolver that
        refuses the client — the silent-success shape ADR-0006 §2.11
        calls the worst this charm has. See ADR-0006 §2.9.
        """
        if not self.dhcp_enabled:
            return self

        missing: list[str] = []
        for name in ("dhcp_range_start", "dhcp_range_end", "dhcp_router", "dhcp_netmask"):
            if not getattr(self, name):
                missing.append(name.replace("_", "-"))
        if missing:
            raise ValueError(
                f"dhcp-enabled is true but these pool fields are empty: {', '.join(missing)}"
            )

        # Validate each is a valid IPv4 address.
        for name in ("dhcp_range_start", "dhcp_range_end", "dhcp_router"):
            try:
                ipaddress.IPv4Address(getattr(self, name))
            except ValueError:
                raise ValueError(
                    f"{name.replace('_', '-')} is not a valid IPv4 address: "
                    f"{getattr(self, name)!r}"
                ) from None

        # Validate the netmask is a dotted-quad contiguous netmask.
        # IPv4Address rejects the prefix-length form ("24"), and the
        # contiguity check rejects 255.0.255.0. 0.0.0.0 is rejected
        # explicitly: a /0 pool would match every address and disable
        # the unservable gate.
        try:
            ipaddress.IPv4Address(self.dhcp_netmask)
        except ValueError:
            raise ValueError(
                f"dhcp-netmask is not a valid netmask: {self.dhcp_netmask!r}"
            ) from None
        if self.dhcp_netmask == "0.0.0.0":
            raise ValueError("dhcp-netmask must not be 0.0.0.0 (a /0 pool cannot be served)")
        # A dotted-quad netmask always starts with 255 (except /0,
        # rejected above). ipaddress accepts host masks like 0.0.0.255
        # and normalizes them to /24, but FTL would receive the raw
        # string — reject the shape before it can be PATCHed.
        if not self.dhcp_netmask.startswith("255."):
            raise ValueError(f"dhcp-netmask is not a contiguous netmask: {self.dhcp_netmask!r}")
        try:
            network = ipaddress.IPv4Network(f"0.0.0.0/{self.dhcp_netmask}", strict=False)
        except ValueError:
            raise ValueError(
                f"dhcp-netmask is not a contiguous netmask: {self.dhcp_netmask!r}"
            ) from None

        start = ipaddress.IPv4Address(self.dhcp_range_start)
        end = ipaddress.IPv4Address(self.dhcp_range_end)

        if start > end:
            raise ValueError(
                f"dhcp-range-start ({self.dhcp_range_start}) must be <= "
                f"dhcp-range-end ({self.dhcp_range_end})"
            )

        # Both must be in the same subnet. The addresses and netmask
        # were validated above, so IPv4Network cannot raise here.
        prefixlen = network.prefixlen
        start_net = ipaddress.IPv4Network(f"{self.dhcp_range_start}/{prefixlen}", strict=False)
        end_net = ipaddress.IPv4Network(f"{self.dhcp_range_end}/{prefixlen}", strict=False)
        if start_net != end_net:
            raise ValueError(
                f"dhcp-range-start ({self.dhcp_range_start}) and "
                f"dhcp-range-end ({self.dhcp_range_end}) are not in the same subnet"
            )

        # The router must be reachable from the pool's subnet — the
        # spike's misleading FTL error was specifically about the
        # router (snap-constraints §4.4).
        pool = pihole_state.DhcpPool(
            start=self.dhcp_range_start,
            end=self.dhcp_range_end,
            router=self.dhcp_router,
            netmask=self.dhcp_netmask,
        )
        if not pihole_state.in_pool_subnet(self.dhcp_router, pool):
            raise ValueError(f"dhcp-router ({self.dhcp_router}) is not inside the pool's subnet")

        # DHCP clients get Pi-hole as their DNS server; LOCAL binds
        # localhost only, so every lease would point at a resolver
        # that refuses them.
        if self.dns_listening_mode != ListeningMode.ALL:
            raise ValueError(
                "dhcp-enabled requires dns-listening-mode=ALL so DHCP clients can "
                "reach the DNS server; FTL's default LOCAL binds localhost only"
            )

        return self

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
            "dhcp_enabled": self.dhcp_enabled,
            "dhcp_pool": (
                pihole_state.DhcpPool(
                    start=self.dhcp_range_start,
                    end=self.dhcp_range_end,
                    router=self.dhcp_router,
                    netmask=self.dhcp_netmask,
                )
                if self.dhcp_enabled
                else None
            ),
        }


class UpdateGravityParams(pydantic.BaseModel):
    """Parameters for the update-gravity action."""

    model_config = pydantic.ConfigDict(frozen=True)

    force: bool = pydantic.Field(
        default=False,
        description="Rebuild gravity.db from scratch rather than incrementally.",
    )
