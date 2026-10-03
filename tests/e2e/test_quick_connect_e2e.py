"""Quick Connect in a real browser: does the buyer SEE what the route answers?

The route answers a lapsed membership with HTTP 403 and a fragment telling the
buyer to renew. The unit tests in ``tests/test_quick_connect.py`` assert that
fragment server-side, which is not the same as it reaching the screen: htmx 2
does not swap 4xx responses by default, so the notice was silently dropped.

Real markup on both ends — the widget as rendered, the route's actual answer,
the vendored htmx — served to Chromium through request interception, so no live
server is needed.
"""

from pathlib import Path

from playwright.sync_api import Page, expect  # type: ignore

from app.services.expiry import RENEWAL_URL
from app.services.wizard_identity import WIZARD_USER_IDS_KEY
from tests.test_quick_connect import (  # noqa: F401 — fixtures
    _client_stub,
    _expire,
    _render_widget,
    jellyfin_server,
    provisioned_user,
)

ORIGIN = "http://sauron.test"


def _widget(app, monkeypatch, server) -> str:
    """Rendered AFTER the route call on purpose: Flask-Babel caches the catalog
    on the app context the fixture keeps open, so rendering first (outside any
    wizard endpoint) would pin the route's answer to English."""
    with app.test_request_context():
        return _render_widget(monkeypatch, server, enabled=True)


def _serve(page: Page, app, widget_html: str, answer) -> None:
    htmx = Path(app.static_folder, "js/vendor/htmx.min.js").read_text()
    page.route(
        f"{ORIGIN}/",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=(
                '<!doctype html><html><head><script src="/htmx.min.js"></script>'
                f"</head><body>{widget_html}</body></html>"
            ),
        ),
    )
    page.route(
        f"{ORIGIN}/htmx.min.js",
        lambda route: route.fulfill(
            status=200, content_type="text/javascript", body=htmx
        ),
    )
    page.route(
        f"{ORIGIN}/wizard/quick-connect",
        lambda route: route.fulfill(
            status=answer.status_code,
            content_type="text/html; charset=utf-8",
            body=answer.get_data(as_text=True),
        ),
    )


def test_a_successful_code_shows_connected(
    page: Page,
    app,
    client,
    provisioned_user,  # noqa: F811 — fixture imported above
    jellyfin_server,  # noqa: F811
    monkeypatch,
):
    """Control for the harness: a 200 swaps with no help from the widget."""
    from tests.test_quick_connect import _Response

    _client_stub(monkeypatch, [], response=_Response(200, "true"))
    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}
    answer = client.post("/wizard/quick-connect", data={"code": "734108"})
    assert answer.status_code == 200
    widget_html = _widget(app, monkeypatch, jellyfin_server)

    _serve(page, app, widget_html, answer)
    page.goto(f"{ORIGIN}/")
    page.fill("input[name='code']", "734108")
    page.click("form[hx-post] button[type='submit']")

    expect(page.locator("#qc-result")).to_contain_text("¡Conectado!")


def test_a_lapsed_membership_sees_the_renewal_notice(
    page: Page,
    app,
    client,
    provisioned_user,  # noqa: F811 — fixture imported above
    jellyfin_server,  # noqa: F811
    monkeypatch,
):
    _client_stub(monkeypatch, [])
    _expire(provisioned_user, disabled=True)
    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}
    answer = client.post("/wizard/quick-connect", data={"code": "734108"})
    assert answer.status_code == 403
    widget_html = _widget(app, monkeypatch, jellyfin_server)

    _serve(page, app, widget_html, answer)
    page.goto(f"{ORIGIN}/")
    page.fill("input[name='code']", "734108")
    page.click("form[hx-post] button[type='submit']")

    result = page.locator("#qc-result")
    expect(result).to_contain_text("Tu membresía no está activa")
    expect(result.locator(f"a[href='{RENEWAL_URL}']")).to_be_visible()
