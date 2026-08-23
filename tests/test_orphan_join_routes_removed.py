"""Phase 6a: the orphaned per-server signup endpoints are gone.

`create_media_blueprint` registered a second public signup route for six server
types -- /jf/join, /emby/join, /abs/join, /kavita/join, /komga/join,
/romm/join. No template or JS ever referenced them, they carried no rate limit
and no `@login_required`, and they bypassed `try_claim_invitation` entirely, so
the single-use replay race stayed open there even after it was closed on
/invitation/process.

Keeping them meant every future fix to the invitation flow had to be applied in
two places -- which is how the claim-collision bug was born.
"""

import pytest

ORPHANED = [
    "/jf/join",
    "/emby/join",
    "/abs/join",
    "/kavita/join",
    "/komga/join",
    "/romm/join",
]


@pytest.mark.parametrize("path", ORPHANED)
def test_orphaned_join_route_is_gone(client, path):
    response = client.post(path, data={"code": "WHATEVER"})
    assert response.status_code == 404, (
        f"{path} still accepts signups outside the claimed flow"
    )


@pytest.mark.parametrize("path", ORPHANED)
def test_orphaned_join_route_is_unregistered(app, path):
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert path not in rules


def test_admin_scan_routes_survive(app):
    """Only the public signup duplicate is removed; admin scanning stays."""
    rules = {r.rule for r in app.url_map.iter_rules()}
    for path in ("/jf/scan", "/jf/scan-specific", "/emby/scan"):
        assert path in rules, f"{path} was removed along with the orphan"


def test_the_real_signup_route_still_exists(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/invitation/process" in rules
