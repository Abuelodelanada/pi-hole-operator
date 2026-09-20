"""Stage 5 integration tests: COS agent relation and host metrics.

The subordinate ``opentelemetry-collector`` publishes ``ubuntu@26.04``
builds in channel ``0.130/stable``, but the channel's recommended
pointer for amd64 serves revision 449 (``ubuntu@22.04``), so a bare
``juju deploy`` fails subordinate base compatibility. The working
invocation pins the per-architecture revision — the same shape as
``SNAP_REVISIONS`` (ADR-0010). See ADR-0008 §3.
"""

import json
import platform
from typing import Any, cast

import jubilant
import pytest
from jubilant.unittypes import RelationData

from tests.integration.conftest import APP_NAME, DEPLOY_TIMEOUT

OTELCOL = "otelcol"
OTELCOL_CHARM = "opentelemetry-collector"
COS_AGENT = "cos-agent"

# Per-architecture revision pins for opentelemetry-collector, channel
# 0.130/stable. The channel's default revision for amd64 is 449
# (ubuntu@22.04), which fails subordinate base compatibility against
# our ubuntu@26.04 principal. 491 and 486 are the 26.04 builds.
# See ADR-0008 §3.
OTELCOL_REVISIONS: dict[str, int] = {"amd64": 491, "arm64": 486}

LOG_DIR = "/var/snap/pihole-by-rajannpatel/common/var/log/pihole"
LOG_FILES = (
    f"{LOG_DIR}/FTL.log",
    f"{LOG_DIR}/pihole.log",
    f"{LOG_DIR}/webserver.log",
    f"{LOG_DIR}/gravity-init.log",
    f"{LOG_DIR}/gravity-first-run.log",
)

DATABAG_FIELDS = frozenset(
    {
        "log_alert_rules",
        "dashboards",
        "metrics_scrape_jobs",
        "log_slots",
        "metrics_alert_rules",
    }
)


def _otelcol_revision() -> int:
    """The pinned revision for this test runner's architecture."""
    arch = platform.machine()
    # Map platform.machine() spellings to the store's.
    _arch_map: dict[str, str] = {"x86_64": "amd64", "aarch64": "arm64"}
    store_arch = _arch_map.get(arch, arch)
    if store_arch not in OTELCOL_REVISIONS:
        pytest.skip(f"no otelcol revision pinned for {store_arch}")
    return OTELCOL_REVISIONS[store_arch]


def pihole_settled(status: jubilant.Status) -> bool:
    """Pihole alone is done: workload active, its own agents idle.

    Used after removing the cos-agent relation. Juju destroys a
    subordinate that loses its last relation — its agent churns for
    minutes ("cleaning up prior to charm deletion") and the app then
    vanishes from status entirely — so a model-wide settle can never
    match again, and the acceptance box is about *this charm*, not the
    subordinate's death throes. Verified live: the first green run of
    this test won a one-second race against the destruction.
    """
    app = status.apps.get(APP_NAME)
    if app is None or app.app_status.current != "active":
        return False
    return jubilant.all_agents_idle(status, APP_NAME)


def settled(status: jubilant.Status) -> bool:
    """Both the workload and the agent are done.

    ``all_active`` alone gates on *workload* status, which stays
    ``active`` for the whole reconcile — with ``successes=3`` at one
    second that window can close before the hook even starts, and the
    assertions then race the charm.

    The subordinate (otelcol) may be ``blocked`` when it has no backend
    to forward to (no Prometheus/Loki/Grafana). That is expected for
    this test — we only need the cos-agent databag to be published.
    """
    if not jubilant.all_agents_idle(status):
        return False
    # pihole must be active; otelcol may be blocked (no backend).
    pihole = status.apps.get(APP_NAME)
    if pihole is None or pihole.app_status.current != "active":
        return False
    otelcol = status.apps.get(OTELCOL)
    if otelcol is None:
        return False
    if otelcol.app_status.current not in ("active", "blocked"):
        return False
    return all(
        unit.workload_status.current in ("active", "blocked") for unit in otelcol.units.values()
    )


# ----------------------------------------------------------------------
# Test 1: the cos-agent relation publishes the expected databag
# ----------------------------------------------------------------------


def test_cos_agent_integrates_and_publishes(deployed: jubilant.Juju) -> None:
    """Relate, wait for both apps settled, then assert the evidence.

    The unit's raw databag on the relation must contain the ``config``
    key with ``log_alert_rules``, ``dashboards``,
    ``metrics_scrape_jobs``, ``log_slots`` and ``metrics_alert_rules``
    fields. An "integrated" status alone is not evidence — the databag
    is.

    The databag is read from **otelcol/0's** show-unit view, because
    ``juju show-unit`` does not display a unit's own databag — only a
    related unit's view shows it.
    """
    # GIVEN a deployed charm, and the subordinate deployed alongside it
    # with the per-architecture revision pin (ADR-0008 §3).
    # NOTE: do not wait for settled after deploying the subordinate —
    # it is a subordinate charm and has no units until related.
    rev = _otelcol_revision()
    deployed.deploy(OTELCOL_CHARM, OTELCOL, channel="0.130/stable", revision=rev)

    # WHEN the cos-agent relation is formed, both endpoints named
    deployed.integrate(f"{APP_NAME}:{COS_AGENT}", f"{OTELCOL}:{COS_AGENT}")
    deployed.wait(settled, timeout=DEPLOY_TIMEOUT)

    # THEN the charm stays Active
    status = deployed.status()
    assert jubilant.all_active(status, APP_NAME), (
        f"pihole not active: {status.apps[APP_NAME].app_status}"
    )

    # AND the databag carries the expected fields.  Read the raw unit
    # data from the relation — `juju show-unit` parsed via jubilant,
    # but from the **subordinate's** view — a unit's own databag
    # is only visible from the related unit's side.
    otelcol_info = deployed.show_unit(f"{OTELCOL}/0")
    cos_relation = _find_relation(otelcol_info, COS_AGENT)
    assert cos_relation is not None, (
        f"no {COS_AGENT} relation found in otelcol/0: {otelcol_info.relation_info}"
    )
    pihole_data = cos_relation.related_units.get(f"{APP_NAME}/0")
    assert pihole_data is not None, (
        f"pihole/0 not in otelcol/0 related-units: {list(cos_relation.related_units)}"
    )
    raw_config: dict[str, Any] = json.loads(pihole_data.data.get("config", "null"))
    assert isinstance(raw_config, dict), f"config is not a dict: {raw_config!r}"

    present = frozenset(raw_config)
    missing = DATABAG_FIELDS - present
    assert not missing, f"config databag missing fields: {missing}"

    # AND log_alert_rules is empty — the fresh-pack proof: the current
    # tree has no src/loki_alert_rules/ directory, so the provider must
    # publish no Pihole log rules.
    log_rules: object = raw_config.get("log_alert_rules")
    assert _is_empty_rules(log_rules), (
        f"log_alert_rules should be empty (no src/loki_alert_rules/), got: {log_rules!r}"
    )


# ----------------------------------------------------------------------
# Test 2: host metrics arrive via the subordinate's node-exporter
# ----------------------------------------------------------------------


def test_host_metrics_arrive_via_node_exporter(deployed: jubilant.Juju) -> None:
    """Assert the node-exporter snap is installed and active.

    The user-facing claim this stage makes: relating cos-agent yields
    HOST metrics with no exporter of ours. The subordinate installs and
    scrapes the ``node-exporter`` snap itself, so this is evidence that
    the subordinate's side of the relation is functioning.
    """
    # GIVEN the cos-agent relation is active (set up by the previous
    # test — module-scoped fixture means the same deployed unit is
    # shared). Check from otelcol/0's view because pihole/0's
    # show-unit does not display its own databag.
    otelcol_info = deployed.show_unit(f"{OTELCOL}/0")
    cos_relation = _find_relation(otelcol_info, COS_AGENT)
    if cos_relation is None:
        pytest.skip(
            f"{COS_AGENT} relation not present; run test_cos_agent_integrates_and_publishes first"
        )
    assert cos_relation is not None  # narrow after pytest.skip
    if f"{APP_NAME}/0" not in cos_relation.related_units:
        pytest.skip(f"{APP_NAME}/0 not in otelcol/0 related-units")

    # WHEN the machine is examined for the node-exporter snap
    result = deployed.exec(
        "snap list node-exporter --unicode=never --color=never",
        unit=f"{APP_NAME}/0",
    )

    # THEN the snap is installed. If it is not present, `snap list`
    # prints a header line and an error to stderr, so the second line
    # is the listing row.
    lines = result.stdout.strip().splitlines()
    assert len(lines) >= 2, f"node-exporter snap not installed: {result.stdout}"

    # AND the service is active — parsed from the Current column, not
    # substring-matched: "active" is a substring of "inactive", so
    # `"active" in stdout` passes for a stopped service
    svc = deployed.exec(
        "snap services node-exporter",
        unit=f"{APP_NAME}/0",
    )
    rows = svc.stdout.strip().splitlines()
    current = rows[1].split()[2]
    assert current == "active", f"node-exporter service is not active: {svc.stdout}"

    # AND the otelcol units are up — active, or blocked the way
    # settled() documents (no COS backends related). This test's
    # subject is node-exporter, not the subordinate's backend status.
    status = deployed.status()
    otelcol_app = status.apps.get(OTELCOL)
    assert otelcol_app is not None, f"{OTELCOL} not in status"
    assert all(
        unit.workload_status.current in ("active", "blocked")
        for unit in otelcol_app.units.values()
    ), f"{OTELCOL} units not up: {otelcol_app.units}"


# ----------------------------------------------------------------------
# Test 3: the log chain — files exist, and the slot that exposes them
# is declared and connected
# ----------------------------------------------------------------------


def test_log_paths_exist(deployed: jubilant.Juju) -> None:
    """Verify the log files exist and the content slot is connected.

    The slot is in the pinned snap revisions (upstream PR #18,
    ADR-0008 §1.2) and the subordinate connects its ``logs`` plug to
    it, so the chain is asserted end to end: files on disk, slot
    declared, connection live.
    """
    # GIVEN a converged unit
    # WHEN each log file's existence is checked
    for path in LOG_FILES:
        result = deployed.exec(
            f"test -f {path} && echo present || echo missing",
            unit=f"{APP_NAME}/0",
        )

        # THEN every one of them is present on disk
        assert result.stdout.strip() == "present", (
            f"{path} is missing from the snap's log directory"
        )

    # AND the content slot is declared and connected — the subordinate's
    # plug on one side, our slot on the other. A missing slot in a
    # future revision fails here instead of silently forwarding nothing.
    conns = deployed.exec(
        "snap connections pihole-by-rajannpatel",
        unit=f"{APP_NAME}/0",
    )
    assert "opentelemetry-collector:logs" in conns.stdout, (
        f"the logs slot is not connected: {conns.stdout}"
    )
    assert "pihole-by-rajannpatel:logs" in conns.stdout, (
        f"the logs slot is not connected: {conns.stdout}"
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _find_relation(unit_info: jubilant.UnitInfo, endpoint: str) -> RelationData | None:
    """Return the RelationData for *endpoint*, or None."""
    for rel in unit_info.relation_info:
        if rel.endpoint == endpoint:
            return rel
    return None


def _is_empty_rules(rules: object) -> bool:
    """True if *rules* represents no alert rules.

    Accepts ``{}`` (empty dict) or ``{"groups": []}`` (the AlertRules
    serialisation when no rules files were found).
    """
    if not isinstance(rules, dict):
        return False
    if not rules:
        return True
    d: dict[str, object] = cast(dict[str, object], rules)
    groups: object = d.get("groups")
    if not isinstance(groups, list):
        return False
    return len(cast(list[object], groups)) == 0


# ----------------------------------------------------------------------
# Test 4 (last, and destructive — it removes the relation): rule 5 —
# the charm stays active with the relation removed
# ----------------------------------------------------------------------


def test_charm_stays_active_with_relation_removed(deployed: jubilant.Juju) -> None:
    """Remove the relation, wait, and assert pihole is still Active.

    This is the acceptance box for rule 5: every relation is optional,
    and the charm must reach ``ActiveStatus`` with zero relations.
    """
    # GIVEN the cos-agent relation is active (checked from otelcol/0's
    # view — a unit's own databag is only visible from the related
    # unit's side).
    otelcol_info = deployed.show_unit(f"{OTELCOL}/0")
    if _find_relation(otelcol_info, COS_AGENT) is None:
        pytest.skip(
            f"{COS_AGENT} relation not present; run test_cos_agent_integrates_and_publishes first"
        )

    # WHEN the relation is removed, both endpoints named. The wait is
    # pihole-only: removing the relation destroys the subordinate (it
    # loses its last relation), so a model-wide settle never matches.
    deployed.remove_relation(f"{APP_NAME}:{COS_AGENT}", f"{OTELCOL}:{COS_AGENT}")
    deployed.wait(pihole_settled, timeout=DEPLOY_TIMEOUT)

    # THEN the charm is still Active — the relation was optional
    status = deployed.status()
    app = status.apps[APP_NAME]
    assert app.app_status.current == "active", (
        f"pihole not active after relation removal: "
        f"{app.app_status.current} — {app.app_status.message}"
    )
