"""Deployment tests.

Stage 0 asserts only the invariant that every later stage must
preserve: the charm reaches `active/idle` with no relations at all.
"""

import jubilant


def test_deploys_active_with_no_relations(deployed: jubilant.Juju, app_name: str):
    # GIVEN a freshly deployed single unit in an LXD VM
    status = deployed.status()

    # WHEN its application status is read
    app = status.apps[app_name]

    # THEN it is active, and it got there without being integrated with
    # anything
    assert jubilant.all_active(status, app_name)
    assert app.app_status.current == "active"
    assert not app.relations
