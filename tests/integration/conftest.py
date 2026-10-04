"""Shared fixtures for the integration tests.

Two things here are load-bearing rather than convenience.

The charm is never packed by a test: pack it once by hand and point
`CHARM_PATH` at the result.

Every machine is a **container**. The snapd bootstrap defect that once
made snaps uninstallable in a Juju-created 26.04 container is fixed
(ADR-0002 §2.2.2, resolved 2026-09-23), and the charm runs active in a
Juju-created container.
"""

import os
import pathlib

import jubilant
import pytest

APP_NAME = "pihole"

# `juju add-machine` uses the *model's* default base, which is not ours.
# Deploying a 26.04 charm onto the resulting 24.04 machine fails with
# `base does not match`, so any hand-allocated machine must say so.
BASE = "ubuntu@26.04"

# Gravity bootstrap downloads a blocklist, so convergence is slow.
DEPLOY_TIMEOUT = 900


def settled(status: jubilant.Status) -> bool:
    """Both the workload and the agent are done.

    ``all_active`` alone gates on *workload* status, which stays
    ``active`` for the whole reconcile — with ``successes=3`` at one
    second that window can close before the hook even starts, and the
    assertions then race the charm.
    """
    return jubilant.all_active(status) and jubilant.all_agents_idle(status)


@pytest.fixture(scope="module")
def app_name() -> str:
    """The name the charm is deployed under."""
    return APP_NAME


@pytest.fixture(scope="module")
def charm_path() -> pathlib.Path:
    """Locate the packed charm, packed once outside the test run."""
    path = os.environ.get("CHARM_PATH")
    if not path:
        pytest.skip("CHARM_PATH is not set; run `charmcraft pack` first")
    return pathlib.Path(path)


@pytest.fixture(scope="module")
def deployed(juju: jubilant.Juju, charm_path: pathlib.Path) -> jubilant.Juju:
    """Deploy a single unit into an LXD container, then settle."""
    juju.deploy(charm_path, APP_NAME)
    juju.wait(jubilant.all_active, timeout=DEPLOY_TIMEOUT)
    return juju
