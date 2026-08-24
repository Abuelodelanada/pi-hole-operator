"""Shared fixtures for the integration tests.

Two things here are load-bearing rather than convenience.

The charm is never packed by a test: pack it once by hand and point
`CHARM_PATH` at the result.

Every machine is a **VM**, not a container. snapd on `ubuntu@26.04`
cannot mount a snap inside an LXD container at all, so a container
deployment fails with an opaque squashfs mount error rather than a
useful message. The constraint lives here so it cannot be forgotten.
"""

import os
import pathlib
from collections.abc import Mapping

import jubilant
import pytest

APP_NAME = "pihole"

# See ADR-0002 section 2.2.2. Not optional, and not a preference about
# test performance.
VM_CONSTRAINTS: Mapping[str, str] = {"virt-type": "virtual-machine"}

# `juju add-machine` uses the *model's* default base, which is not ours.
# Deploying a 26.04 charm onto the resulting 24.04 machine fails with
# `base does not match`, so any hand-allocated machine must say so.
BASE = "ubuntu@26.04"

# Gravity bootstrap downloads a blocklist, so convergence is slow.
DEPLOY_TIMEOUT = 900


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
    """Deploy a single unit into an LXD VM and wait for it to settle."""
    juju.deploy(charm_path, APP_NAME, constraints=VM_CONSTRAINTS)
    juju.wait(jubilant.all_active, timeout=DEPLOY_TIMEOUT)
    return juju
